import re
import aiohttp
import logging
import json
import time
import hashlib
from typing import Optional, Any, Dict, List
from pydantic import BaseModel

from dependencies import get_config

_global_index_empty_cache: Dict[str, Dict[str, Any]] = {}


class SearchResult(BaseModel):
    """Represents a single search result from AI Search."""
    title: str
    link: str
    content: str


class SearchClient:
    """
    Azure Cognitive Search client with hybrid search support.

    Handles:
    - Basic search operations (term, vector, hybrid)
    - Document retrieval by ID
    - Token acquisition and authentication
    - Embeddings generation for vector search
    """
    def __init__(self):
        """
        Initialize SearchClient with configuration.
        """
        # ==== Load all config parameters in one place ====
        self.cfg = get_config()
        self.endpoint = self.cfg.get("SEARCH_SERVICE_QUERY_ENDPOINT")
        self.api_version = self.cfg.get("AZURE_SEARCH_API_VERSION", "2024-07-01")
        self.credential = self.cfg.aiocredential

        # Hybrid search configuration
        self.search_top_k = int(self.cfg.get('SEARCH_RAGINDEX_TOP_K', 3))
        self.search_approach = self.cfg.get('SEARCH_APPROACH', 'hybrid')
        self.semantic_search_config = self.cfg.get('SEARCH_SEMANTIC_SEARCH_CONFIG', 'my-semantic-config')
        self.search_service = self.cfg.get('SEARCH_SERVICE_NAME')
        self.use_semantic = self.cfg.get('SEARCH_USE_SEMANTIC', 'false').lower() == 'true'
        self.index_name = self.cfg.get("SEARCH_RAG_INDEX_NAME", "ragindex")
        self.index_empty_cache_ttl_seconds = int(self.cfg.get("SEARCH_EMPTY_CACHE_TTL_SECONDS", 60, type=int))

        # Per-request context (kept in memory only)
        self._request_api_access_token: Optional[str] = None
        self._allow_anonymous: bool = True

        # Cached delegated Search token (OBO) for the current request
        self._cached_search_user_token: Optional[str] = None
        self._cached_search_user_token_expires_at: float = 0.0

        # Last OBO error summary (for clear logs / strict-mode failures)
        self._last_obo_error: Optional[str] = None

        # Shared aiohttp session — reuses TCP connections across all HTTP calls
        self._session: Optional[aiohttp.ClientSession] = None

        # Initialize GenAIModelClient for embeddings (only if needed for vector/hybrid search)
        self.aoai_client = None
        if self.search_approach in ["vector", "hybrid"]:
            try:
                from connectors.aifoundry import get_genai_client
                self.aoai_client = get_genai_client()
                logging.info("[SearchClient] ✅ GenAIModelClient initialized for embeddings")
            except Exception as e:
                logging.warning("[SearchClient] ⚠️ Could not initialize GenAIModelClient for embeddings: %s", e)
                logging.warning("[SearchClient] ⚠️ Falling back to term search only")
                self.search_approach = "term"

        # Cache is now maintained in the _global_index_empty_cache module variable
        # ==== End config block ====

        if not self.endpoint:
            raise ValueError("SEARCH_SERVICE_QUERY_ENDPOINT not set in config")

        logging.info("[SearchClient] ✅ Initialized with hybrid search support")
        logging.info("[SearchClient]    Index: %s", self.index_name)
        logging.info("[SearchClient]    Approach: %s", self.search_approach)
        logging.info("[SearchClient]    Top K: %s", self.search_top_k)

    async def _get_session(self) -> aiohttp.ClientSession:
        """Returns a shared aiohttp session, creating one lazily if needed."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def set_request_context(self, *, api_access_token: Optional[str], allow_anonymous: bool, principal_email: Optional[str] = None) -> None:
        """Sets per-request context used for permission trimming.

        api_access_token is the incoming user token sent to the orchestrator (audience: this API).
        It is used as the OBO assertion to acquire a Search-audience user token.
        principal_email is the logged-in user's email, used to enforce manager/practice_manager
        email restrictions so users can only query their own team.
        """
        api_access_token = (api_access_token or "").strip() or None
        if api_access_token != self._request_api_access_token:
            # Token changed (new request); drop cached OBO token.
            self._cached_search_user_token = None
            self._cached_search_user_token_expires_at = 0.0

        self._request_api_access_token = api_access_token
        self._allow_anonymous = bool(allow_anonymous)
        self._principal_email = (principal_email or "").strip().lower() or None

    def _token_fingerprint(self, token: Optional[str]) -> str:
        if not token:
            return "<none>"
        try:
            return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
        except Exception:
            return "<unknown>"

    async def _acquire_search_user_token_via_obo(self, api_access_token: str) -> Optional[str]:
        """Acquire a delegated Azure AI Search token using the OBO flow.

        This exchanges the incoming API token (user assertion) for a Search-audience token.
        """
        tenant_id = None
        client_id = None
        client_secret = None
        try:
            tenant_id = (self.cfg.get_value("OAUTH_AZURE_AD_TENANT_ID", default=None, allow_none=True) or "").strip() or None
        except Exception:
            tenant_id = None

        try:
            client_id = (self.cfg.get_value("OAUTH_AZURE_AD_CLIENT_ID", default=None, allow_none=True) or "").strip() or None
        except Exception:
            client_id = None

        try:
            client_secret = (self.cfg.get_value("OAUTH_AZURE_AD_CLIENT_SECRET", default=None, allow_none=True) or "").strip() or None
        except Exception:
            client_secret = None

        if not tenant_id or not client_id or not client_secret:
            logging.warning(
                "[Retrieval][OBO] Missing Entra configuration for OBO. tenant_id=%s client_id=%s client_secret=%s",
                "set" if tenant_id else "missing",
                "set" if client_id else "missing",
                "set" if client_secret else "missing",
            )
            return None

        token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

        form = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "requested_token_use": "on_behalf_of",
            "scope": "https://search.azure.com/user_impersonation",
            "assertion": api_access_token,
        }

        # Important: never log the assertion.
        fp = self._token_fingerprint(api_access_token)
        logging.debug("[Retrieval][OBO] Requesting Search delegated token via OBO (assertion_fp=%s)", fp)

        session = await self._get_session()
        async with session.post(token_url, data=form) as resp:
                raw = await resp.text()
                if resp.status >= 400:
                    # Token endpoint errors often include trace_id/correlation_id.
                    try:
                        err = json.loads(raw)
                        error = err.get("error")
                        desc = err.get("error_description")
                        trace_id = err.get("trace_id")
                        correlation_id = err.get("correlation_id")
                        error_codes = err.get("error_codes")
                        self._last_obo_error = (
                            f"status={resp.status} error={error} codes={error_codes} trace_id={trace_id} correlation_id={correlation_id}"
                        )
                        logging.error(
                            "[Retrieval][OBO] OBO failed (status=%d error=%s codes=%s trace_id=%s correlation_id=%s desc=%s)",
                            resp.status,
                            error,
                            error_codes,
                            trace_id,
                            correlation_id,
                            (str(desc)[:240] + "…") if desc and len(str(desc)) > 240 else desc,
                        )
                    except Exception:
                        self._last_obo_error = f"status={resp.status} body={raw[:200]}"
                        logging.error("[Retrieval][OBO] OBO failed (status=%d body=%s)", resp.status, raw[:400])
                    return None

                data = {}
                try:
                    data = json.loads(raw)
                except Exception:
                    logging.error("[Retrieval][OBO] Token endpoint returned non-JSON response")
                    return None

                token = data.get("access_token")
                expires_in = data.get("expires_in")
                if not token:
                    self._last_obo_error = "token endpoint response missing access_token"
                    logging.error("[Retrieval][OBO] Token endpoint response missing access_token")
                    return None

                # Cache for the remainder of the request.
                try:
                    ttl = int(expires_in) if expires_in is not None else 0
                except Exception:
                    ttl = 0
                self._cached_search_user_token = token
                self._cached_search_user_token_expires_at = time.time() + max(0, ttl - 30)

                self._last_obo_error = None

                logging.info("[Retrieval][OBO] ✅ Acquired Search delegated token via OBO")
                return token

    async def _get_search_user_token_for_trimming(self) -> Optional[str]:
        # Use cached token if still valid.
        if self._cached_search_user_token and time.time() < self._cached_search_user_token_expires_at:
            return self._cached_search_user_token

        # No incoming user token -> cannot do OBO.
        if not self._request_api_access_token:
            if self._allow_anonymous:
                logging.info(
                    "[Retrieval][Trimming] No incoming user token; running without x-ms-query-source-authorization because ALLOW_ANONYMOUS=true"
                )
                return None

            logging.error(
                "[Retrieval][Trimming] Missing incoming user token. Permission trimming is required but ALLOW_ANONYMOUS=false. "
                "Refusing to call Search without x-ms-query-source-authorization."
            )
            raise RuntimeError(
                "Permission trimming requires an incoming user access token. "
                "No Authorization header was available and ALLOW_ANONYMOUS=false."
            )

        token = await self._acquire_search_user_token_via_obo(self._request_api_access_token)
        if token:
            return token

        if self._allow_anonymous:
            logging.warning(
                "[Retrieval][Trimming] OBO failed; running without x-ms-query-source-authorization because ALLOW_ANONYMOUS=true (details=%s)",
                self._last_obo_error or "<no-details>",
            )
            return None

        logging.error(
            "[Retrieval][Trimming] OBO failed and ALLOW_ANONYMOUS=false. Refusing to call Search without x-ms-query-source-authorization. Details: %s",
            self._last_obo_error or "<no-details>",
        )
        raise RuntimeError(
            "Failed to acquire Azure AI Search delegated token via OBO. "
            "Ensure API permissions include Azure Cognitive Search delegated user_impersonation and admin consent is granted."
        )

    async def search(self, index_name: str, body: dict, *, search_user_token: Optional[str] = None) -> dict:
        """
        Executes a search POST against /indexes/{index_name}/docs/search.
        """
        url = (
            f"{self.endpoint}"
            f"/indexes/{index_name}/docs/search"
            f"?api-version={self.api_version}"
        )

        # get bearer token
        try:
            token = (await self.credential.get_token("https://search.azure.com/.default")).token
        except Exception:
            logging.exception("[search] failed to acquire token")
            raise

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }

        # Optional: user context token for permission trimming.
        if search_user_token:
            headers["x-ms-query-source-authorization"] = f"Bearer {search_user_token}"

        session = await self._get_session()
        async with session.post(url, headers=headers, json=body) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    logging.error(f"[search] {resp.status} {text}")
                    raise RuntimeError(f"Search failed: {resp.status} {text}")
                return await resp.json()

    async def get_document(self, index_name: str, document_id: str, select_fields: list = None) -> dict:
        """
        Retrieves a single document by ID from the index.
        GET /indexes/{index_name}/docs/{document_id}
        
        Args:
            index_name: Name of the search index
            document_id: Document key/ID
            select_fields: Optional list of fields to retrieve (e.g., ['filepath', 'title'])
            
        Returns:
            Document dictionary with requested fields
        """
        # Build URL with optional $select parameter
        url = (
            f"{self.endpoint}"
            f"/indexes/{index_name}/docs('{document_id}')"
            f"?api-version={self.api_version}"
        )
        
        if select_fields:
            fields_str = ",".join(select_fields)
            url += f"&$select={fields_str}"
        
        # Get bearer token
        try:
            token = (await self.credential.get_token("https://search.azure.com/.default")).token
        except Exception:
            logging.exception("[search] failed to acquire token for get_document")
            raise

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }

        session = await self._get_session()
        async with session.get(url, headers=headers) as resp:
                text = await resp.text()
                if resp.status == 404:
                    logging.warning(f"[search] Document not found: {document_id}")
                    return None
                if resp.status >= 400:
                    logging.error(f"[search] {resp.status} {text}")
                    raise RuntimeError(f"Get document failed: {resp.status} {text}")
                return await resp.json()

    async def is_index_empty(self):
        """
        Fast check to see if the search index is completely empty, caching the result.
        Returns True if empty, False if it has documents.
        """
        global _global_index_empty_cache
        cached_entry = _global_index_empty_cache.get(self.index_name)
        if cached_entry:
            # Backward compatibility: older cache shape used raw bool values.
            if isinstance(cached_entry, bool):
                logging.info(f"[Retrieval] Index '{self.index_name}' empty cache hit (legacy); bypassing index probe")
                return cached_entry

            cached_is_empty = bool(cached_entry.get("is_empty", False))
            cached_at = float(cached_entry.get("checked_at", 0.0))
            cache_age_seconds = max(0.0, time.time() - cached_at)

            if cache_age_seconds < self.index_empty_cache_ttl_seconds:
                logging.info(
                    "[Retrieval] Index '%s' empty cache hit (age=%.1fs, ttl=%ss); bypassing index probe",
                    self.index_name,
                    cache_age_seconds,
                    self.index_empty_cache_ttl_seconds,
                )
                return cached_is_empty

            logging.info(
                "[Retrieval] Index '%s' empty cache expired (age=%.1fs >= ttl=%ss); re-probing",
                self.index_name,
                cache_age_seconds,
                self.index_empty_cache_ttl_seconds,
            )

        try:
            logging.info(f"[Retrieval] Probing if index '{self.index_name}' is empty...")
            # Simple query requesting 1 document with no vector/hybrid overhead
            search_body: Dict[str, Any] = {
                "search": "*",
                "select": "id",
                "top": 1
            }
            
            search_user_token = await self._get_search_user_token_for_trimming()
            
            results = await self.search(
                index_name=self.index_name,
                body=search_body,
                search_user_token=search_user_token,
            )
            
            # If no 'value' or empty list, it's empty
            has_results = len(results.get('value', [])) > 0
            is_empty_result = not has_results
            _global_index_empty_cache[self.index_name] = {
                "is_empty": is_empty_result,
                "checked_at": time.time(),
            }
            
            if is_empty_result:
                logging.info(f"[Retrieval] Probe confirmed: Index '{self.index_name}' is EMPTY.")
            else:
                logging.info(f"[Retrieval] Probe confirmed: Index '{self.index_name}' HAS DOCUMENTS.")
                
            return is_empty_result
            
        except Exception as e:
            logging.error(f"[Retrieval] Failed to check if index is empty: {e}", exc_info=True)
            # Default to not empty if we can't tell, to avoid false bypasses
            return False

    async def search_knowledge_base(self, query: str) -> str:
        """
        Searches the knowledge base for relevant documents using hybrid search.
        
        :param query: The search query to find relevant documents.
        :return: Search results as a JSON string containing a list of documents with title, link and content.
        """
        
        logging.info(f"[Retrieval] AI Search index: {self.index_name}")
        logging.info(f"[Retrieval] Search approach: {self.search_approach}")
        logging.info(f"[Retrieval] Executing search for query: {query}")

        try:
            logging.info("[Retrieval] Using Azure AI Search for document retrieval")
            
            # Build search body according to search approach
            search_body: Dict[str, Any] = {
                "select": "title,content,url,filepath,chunk_id,manager_email,manager_name,practice_manager_email,practice_manager_name",
                "top": self.search_top_k
            }
            
            # Generate embeddings for vector/hybrid search
            if self.search_approach in ["vector", "hybrid"] and self.aoai_client:
                start_time = time.time()
                logging.info(f"[Retrieval] Generating embeddings for query")
                embeddings_query = await self.aoai_client.get_embeddings(query)
                logging.info(f"[Retrieval] Embeddings generated in {round(time.time() - start_time, 2)} seconds")
                
                if self.search_approach == "vector":
                    search_body["vectorQueries"] = [{
                        "kind": "vector",
                        "vector": embeddings_query,
                        "fields": "contentVector",
                        "k": self.search_top_k
                    }]
                elif self.search_approach == "hybrid":
                    search_body["search"] = query
                    search_body["vectorQueries"] = [{
                        "kind": "vector",
                        "vector": embeddings_query,
                        "fields": "contentVector",
                        "k": self.search_top_k
                    }]
            else:
                # Term search only
                search_body["search"] = query
            
            # Execute search
            search_user_token = await self._get_search_user_token_for_trimming()
            if search_user_token:
                logging.info("[Retrieval][Trimming] Using x-ms-query-source-authorization (OBO token acquired)")
            else:
                logging.info("[Retrieval][Trimming] Not sending x-ms-query-source-authorization")

            search_results = await self.search(
                index_name=self.index_name,
                body=search_body,
                search_user_token=search_user_token,
            )
            
            # Process search results
            results_list = []
            for result in search_results.get('value', []):
                title = result.get('title', 'reference') or 'reference'
                link = result.get('filepath') or result.get('url', '') or ''
                content = result.get('content', '')
                
                # Debug log each document with formatted output (remove line breaks)
                content_preview = content[:200] if len(content) > 200 else content
                content_preview = ' '.join(content_preview.split())  # Replace all whitespace/newlines with single space
                logging.debug(f"[Retrieval] Document: [{title}]({link}): {content_preview}")
                
                search_result = SearchResult(
                    title=title,
                    link=link,
                    content=content
                )
                results_list.append(search_result.model_dump())

            # If we found results, force cache to non-empty so routing can recover
            # immediately from any stale empty-cache state.
            if results_list:
                _global_index_empty_cache[self.index_name] = {
                    "is_empty": False,
                    "checked_at": time.time(),
                }
            
            logging.info(f"[Retrieval] Found {len(results_list)} results from Azure AI Search")
            return json.dumps({"results": results_list, "query": query})
            
        except Exception as e:
            logging.error(f"[Retrieval] Azure AI Search failed: {e}", exc_info=True)

            # In strict mode (ALLOW_ANONYMOUS=false), do not silently degrade.
            # Raise so the request fails early and the logs make the root cause obvious.
            if not self._allow_anonymous:
                raise

            logging.warning("[Retrieval] Falling back to empty results (ALLOW_ANONYMOUS=true)")
            return json.dumps({"results": [], "query": query, "error": "search_failed"})

    # ------------------------------------------------------------------
    # Paginated filter fetch: issues repeated search POSTs using `skip`
    # until all matching chunks are retrieved.  Required for datasets
    # larger than the Azure AI Search per-request ceiling of 1 000 rows.
    # A safety ceiling of 50 000 total rows prevents runaway loops on
    # very large indexes.
    # ------------------------------------------------------------------
    async def _fetch_all_by_filter(
        self,
        select_fields: str,
        filter_expr: str,
        *,
        page_size: int = 1000,
        max_results: int = 50_000,
        search_user_token: Optional[str] = None,
    ) -> list:
        """
        Fetches every document matching `filter_expr` from the index by
        issuing paginated search requests (skip/top) until no more pages
        are returned.  Returns a flat list of raw document dicts.
        """
        all_docs: list = []
        skip = 0
        while True:
            body: Dict[str, Any] = {
                "search": "*",
                "select": select_fields,
                "filter": filter_expr,
                "top": page_size,
                "skip": skip,
            }
            response = await self.search(
                index_name=self.index_name,
                body=body,
                search_user_token=search_user_token,
            )
            page = response.get("value", [])
            all_docs.extend(page)
            logging.info(
                "[_fetch_all_by_filter] page skip=%d returned %d row(s); total so far=%d",
                skip, len(page), len(all_docs),
            )
            if len(page) < page_size:
                break  # last page
            skip += page_size
            if skip >= max_results:
                logging.warning(
                    "[_fetch_all_by_filter] Safety ceiling reached at %d results — stopping pagination",
                    len(all_docs),
                )
                break
        return all_docs

    # ------------------------------------------------------------------
    # Helper: group multi-chunk results by employee to avoid duplicate
    # entries and stay well under the 512 KB Azure AI Foundry tool-output
    # limit.  All PEP fields (objectives, self_comments, competency, …)
    # live in the `content` column (a GitHub markdown table).
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_fields_from_table(content: str, fields: List[str]) -> str:
        """
        Parses a GitHub-flavored markdown table (tabulate tablefmt='github')
        and returns only the requested columns as compact 'col: val' lines.

        Matching is case-, space-, and underscore-insensitive so e.g.
        'self comments' matches the 'self_comments' column.
        emp_name and employee_email are always included for identification.
        """
        def _norm(s: str) -> str:
            return re.sub(r'[\s_\-]+', '', s.lower().strip())

        # Only pick lines that are proper markdown table rows (start with |).
        # This skips the structured-content EMPLOYEE SUMMARY line which uses
        # "key: value | key: value" format (no leading |) but contains | chars.
        table_lines = [l for l in content.splitlines() if l.strip().startswith('|')]
        if len(table_lines) < 3:
            return content[:2000]

        # First table line = headers
        raw_headers = [h.strip() for h in table_lines[0].strip(' |').split('|')]
        norm_headers = [_norm(h) for h in raw_headers]

        # Always include identifier columns (emp_name only — not employee_email)
        id_norms = {'empname', 'employeeid'}
        target_norms = {_norm(f) for f in fields if f.strip()} | id_norms

        # Find matching column indices (exact match after normalisation only)
        col_indices: List[int] = []
        col_names: List[str] = []
        for i, nh in enumerate(norm_headers):
            if not nh:
                continue
            if any(tf and tf == nh for tf in target_norms):
                if i not in col_indices:
                    col_indices.append(i)
                    col_names.append(raw_headers[i])

        if not col_indices:
            return content[:2000]

        result_rows: List[str] = []
        for line in table_lines[1:]:
            cells = [c.strip() for c in line.strip(' |').split('|')]
            # Skip separator line (cells are '---' or similar)
            if cells and all(not c.replace('-', '').replace(' ', '') for c in cells if c):
                continue
            row_parts = []
            for idx, col_name in zip(col_indices, col_names):
                val = cells[idx].strip() if idx < len(cells) else ''
                # Always emit the field, even if empty, so the LLM sees N/A
                row_parts.append(f'{col_name}: {val if val else "N/A"}')
            if row_parts:
                result_rows.append(' | '.join(row_parts))

        return '\n'.join(result_rows) if result_rows else content[:2000]

    @staticmethod
    def _merge_results_by_employee(
        raw_results: list,
        *,
        fields: Optional[List[str]] = None,
        total_budget_chars: int = 430_000,  # 430 KB leaves ~40 KB for JSON wrapper overhead
        max_per_employee_chars: int = 15_000,
    ) -> list:
        """
        Groups all index chunks for the same employee into one SearchResult.

        When `fields` is provided (e.g. ["competency", "objective"]), only
        those columns are extracted from the markdown table, producing
        compact output that fits even 500+ employees in the 512 KB limit:
          - "competency" for 500 employees  ->  ~20 KB total
          - "competency,objective" for 500  ->  ~100 KB total

        When `fields` is None ("all"), the full content is returned subject
        to the dynamic per-employee budget (470 KB / N employees):
          - 36  employees  ->  ~13 000 chars each  (470 KB total)
          - 200 employees  ->   ~2 350 chars each  (470 KB total)
          - 500 employees  ->     ~940 chars each  (470 KB total)
        """
        from collections import OrderedDict
        use_all = not fields  # None means return all fields

        grouped: "OrderedDict[str, dict]" = OrderedDict()
        for doc in raw_results:
            key = (doc.get("emp_name") or "").strip() or doc.get("title", "unknown")
            chunk_content = (doc.get("content") or "").strip()
            if key not in grouped:
                grouped[key] = {
                    "emp_name": key,
                    "title": doc.get("title", ""),
                    "link": doc.get("filepath") or doc.get("url", ""),
                    "content_parts": [],
                }
            if chunk_content:
                grouped[key]["content_parts"].append(chunk_content)

        n = max(1, len(grouped))
        cap = max(300, min(max_per_employee_chars, total_budget_chars // n))
        logging.info(
            "[_merge_results_by_employee] %d employee(s) — fields=%s — per-employee cap: %d chars",
            n,
            "all" if use_all else ",".join(fields),
            cap,
        )

        merged_list = []
        for emp_name, data in grouped.items():
            if use_all:
                merged_content = "\n---\n".join(data["content_parts"])
            else:
                # Extract only requested fields from each content chunk
                extracted = [
                    SearchClient._extract_fields_from_table(part, fields)
                    for part in data["content_parts"]
                ]
                merged_content = "\n".join(p for p in extracted if p)

            if len(merged_content) > cap:
                merged_content = merged_content[:cap] + "…[truncated]"
            merged_list.append(
                SearchResult(
                    title=data["title"],
                    link=data["link"],
                    content=f"Employee: {emp_name}\n{merged_content}" if merged_content else f"Employee: {emp_name}",
                ).model_dump()
            )
        return merged_list


    @staticmethod
    def _format_filter_result(result_json_str: str, *, names_only: bool = False) -> str:
        """
        Parses JSON returned by search_by_manager / search_by_practice_manager and
        builds a markdown table directly in Python. Called by the strategy to stream
        the table to the UI without routing through the LLM.

        When names_only=True, always returns a plain numbered list of employee names
        regardless of what fields appear in the content.
        """
        try:
            employees = json.loads(result_json_str)
        except Exception:
            return result_json_str

        if not isinstance(employees, list) or not employees:
            return result_json_str

        rows: list = []
        all_cols: list = []  # column order from first employee seen

        for item in employees:
            content = item.get("content", "")
            emp_name = ""
            fields: dict = {}
            for line in content.splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("Employee:"):
                    emp_name = line.replace("Employee:", "").strip()
                    continue
                if not names_only:
                    for pair in line.split(" | "):
                        pair = pair.strip()
                        if ":" not in pair:
                            continue
                        k, _, v = pair.partition(":")
                        k, v = k.strip(), v.strip() or "N/A"
                        norm = re.sub(r'[\s_\-]+', '', k.lower())
                        if k and norm not in ("empname", "employeeid"):
                            if k not in fields:
                                fields[k] = v
                            if k not in all_cols:
                                all_cols.append(k)
            if not emp_name:
                emp_name = item.get("emp_name", "Unknown")
            rows.append((emp_name, fields))

        if not rows:
            return result_json_str

        count = len(rows)

        # names_only=True or no extra columns found → plain numbered list
        if names_only or not [c for c in all_cols if c.strip()]:
            list_rows = [f"{i}. {emp}" for i, (emp, _) in enumerate(rows, 1)]
            return f"Found **{count}** employees.\n\n" + "\n".join(list_rows)
        else:
            # Defensive: drop any blank/whitespace-only column names
            all_cols = [c for c in all_cols if c.strip()]
            header = "| Employee Name | " + " | ".join(all_cols) + " |"
            separator = "|---|" + "|".join(["---"] * len(all_cols)) + "|"
            data_rows = [
                "| " + emp + " | " + " | ".join(flds.get(c, "N/A") for c in all_cols) + " |"
                for emp, flds in rows
            ]
            return f"Found **{count}** employees.\n\n" + "\n".join([header, separator] + data_rows)

    async def search_by_employee(self, employee_name: str, fields: str = "all", assessment_year: str = "") -> str:
        """
        Returns PEP data for a specific employee looked up by name.
        Uses $filter with search.ismatch so the lookup is case-insensitive and
        does not depend on vector relevance scoring.

        Use this when the user asks about ONE specific person by name
        (e.g. "goals for Adarsh A", "Adarsh's ratings", "show competency of John").

        :param employee_name: The employee's name (or partial name) as the user typed it.
        :param fields: Comma-separated PEP column names to return.
            Examples: "objective,objective_description" for goals/weightage,
            "competency", "self_rating,direct_manager_rating,achievement",
            "self_comments,manager_comments".
            Use "all" to get full PEP.
            Common fields: competency, BU, objective, objective_description,
            self_comments, manager_comments, practice_manager_comments,
            self_rating, direct_manager_rating, practice_manager_rating,
            achievement, achievement_percentage, category_band, promotion_date,
            joined_date, separation_status.
        :param assessment_year: Filter to a specific PEP cycle year, e.g. "2026" or "2025".
            Pass an empty string (default) to return data across ALL years — use this
            for historical/trend queries or consolidated summaries spanning multiple cycles.
        :return: JSON string with one merged entry per matched employee.
        """
        logging.info("[Retrieval] search_by_employee: employee_name=%s fields=%s assessment_year=%s", employee_name, fields, assessment_year)

        # Sanitise: escape single-quotes to prevent OData injection
        safe_name = employee_name.strip().replace("'", "''")
        field_list = None if fields.strip().lower() == "all" else [
            f.strip() for f in fields.split(",") if f.strip()
        ]

        # Security: restrict results to employees who report to the logged-in user
        # (either as direct manager or practice manager). This prevents any user
        # from querying PEP data for employees outside their reportee chain.
        principal = getattr(self, "_principal_email", None)
        if principal:
            safe_principal = principal.strip().lower().replace("'", "''")
            manager_guard = (
                f" and (manager_email eq '{safe_principal}'"
                f" or practice_manager_email eq '{safe_principal}')"
            )
        else:
            manager_guard = ""

        # Optional year filter — scopes query to a single PEP cycle
        year = assessment_year.strip()
        year_filter = f" and assessment_year eq '{year.replace(chr(39), chr(39)*2)}'" if year else ""

        # Use search.ismatch with phrase search ("...") for case-insensitive exact phrase match
        # on the emp_name field. This works even when emp_name uses a language analyzer.
        safe_phrase = safe_name.replace('"', '\\"')
        filter_expr = f'search.ismatch(\'"{safe_phrase}"\', \'emp_name\'){manager_guard}{year_filter}'

        try:
            search_user_token = await self._get_search_user_token_for_trimming()
            raw = await self._fetch_all_by_filter(
                select_fields="title,url,filepath,chunk_id,emp_name,manager_email,manager_name,practice_manager_email,practice_manager_name,content",
                filter_expr=filter_expr,
                search_user_token=search_user_token,
            )

            if not raw:
                # Fallback: try startswith-style match using search.ismatch without phrase quotes
                logging.info("[Retrieval] search_by_employee: phrase match returned 0 rows; retrying with token match")
                filter_expr_loose = f"search.ismatch('{safe_name}', 'emp_name'){manager_guard}{year_filter}"
                raw = await self._fetch_all_by_filter(
                    select_fields="title,url,filepath,chunk_id,emp_name,manager_email,manager_name,practice_manager_email,practice_manager_name,content",
                    filter_expr=filter_expr_loose,
                    search_user_token=search_user_token,
                )

            # Single-employee query: give the full 430KB budget to one person
            # so all objective rows fit (each row ~2-3KB, 9 rows = ~20KB).
            results_list = self._merge_results_by_employee(
                raw, fields=field_list, max_per_employee_chars=430_000
            )
            n = len(results_list)
            logging.info(
                "[Retrieval] search_by_employee: %d raw chunk(s) merged into %d employee(s) for name '%s' (fields=%s)",
                len(raw), n, employee_name, fields,
            )

            # If no results and a manager guard was applied, return a clear
            # authorization error so the LLM can tell the user the right thing.
            if n == 0 and manager_guard:
                logging.warning(
                    "[Retrieval] search_by_employee: 0 results with manager guard active — '%s' is not a reportee of '%s'",
                    employee_name, principal,
                )
                return json.dumps({
                    "error": "not_authorized",
                    "message": f"'{employee_name}' is not found in your reportee chain. You can only access PEP data for your direct or practice reportees.",
                })

            return json.dumps(results_list)

        except Exception as e:
            logging.error("[Retrieval] search_by_employee failed: %s", e, exc_info=True)
            return json.dumps({"error": str(e), "employee_name": employee_name})

    async def search_by_manager(self, manager_email: str, fields: str = "all", assessment_year: str = "") -> str:
        """
        Returns ALL employees whose manager_email matches exactly.
        Uses $filter so no employee is missed regardless of relevance score.

        Use this whenever the user asks about a manager's reportees, direct
        reports, team members, or any employee-level PEP field for that team.

        :param manager_email: The manager's exact email address.
        :param fields: Comma-separated PEP column names to return per employee.
            Extract the field names the user is asking about from their question.
            Examples: "competency", "competency,BU", "objective,self_comments",
            "self_rating,direct_manager_rating,achievement".
            If the user just wants a list of names, use "emp_name".
            Use "all" only for small teams (fewer than 50 people).
            Common fields: competency, BU, objective, objective_description,
            self_comments, manager_comments, practice_manager_comments,
            self_rating, direct_manager_rating, achievement,
            achievement_percentage, category_band, promotion_date.
        :param assessment_year: Filter to a specific PEP cycle year, e.g. "2026" or "2025".
            Pass an empty string (default) to return data across ALL years — use this
            for historical/trend queries or consolidated summaries spanning multiple cycles.
        :return: JSON string with one merged entry per employee.
        """
        logging.info("[Retrieval] search_by_manager: manager_email=%s fields=%s assessment_year=%s", manager_email, fields, assessment_year)

        # Security: enforce that users can only query their own reportees.
        # If the logged-in user's email is known, ignore whatever email the LLM
        # passed and always use the principal's email instead.
        principal = getattr(self, "_principal_email", None)
        if principal:
            if manager_email.strip().lower() != principal:
                logging.warning(
                    "[Retrieval] search_by_manager: LLM requested email '%s' but principal is '%s'; overriding.",
                    manager_email, principal,
                )
            manager_email = principal

        # Sanitise to prevent OData injection
        safe_email = manager_email.strip().lower().replace("'", "''")
        field_list = None if fields.strip().lower() == "all" else [
            f.strip() for f in fields.split(",") if f.strip()
        ]

        # Optional year filter
        year = assessment_year.strip()
        year_filter = f" and assessment_year eq '{year.replace(chr(39), chr(39)*2)}'" if year else ""

        try:
            search_user_token = await self._get_search_user_token_for_trimming()
            raw = await self._fetch_all_by_filter(
                select_fields="title,url,filepath,chunk_id,emp_name,manager_email,manager_name,content",
                filter_expr=f"manager_email eq '{safe_email}'{year_filter}",
                search_user_token=search_user_token,
            )

            results_list = self._merge_results_by_employee(raw, fields=field_list)
            n = len(results_list)
            logging.info(
                "[Retrieval] search_by_manager: %d raw chunk(s) merged into %d employee(s) for manager '%s' (fields=%s, year=%s)",
                len(raw), n, manager_email, fields, year or "all",
            )
            return json.dumps(results_list)

        except Exception as e:
            logging.error("[Retrieval] search_by_manager failed: %s", e, exc_info=True)
            return json.dumps({"error": str(e), "manager_email": manager_email})

    async def search_by_practice_manager(self, practice_manager_email: str, fields: str = "all", assessment_year: str = "") -> str:
        """
        Returns ALL employees whose practice_manager_email matches exactly.
        Uses $filter so no employee is missed regardless of relevance score.

        Use this for dotted-line / practice manager reportee queries, or when
        the user asks about any employee-level PEP field for that practice team.

        :param practice_manager_email: The practice manager's exact email address.
        :param fields: Comma-separated PEP column names to return per employee.
            Extract the field names the user is asking about from their question.
            Examples: "competency", "competency,BU", "objective,self_comments",
            "self_rating,direct_manager_rating,achievement".
            If the user just wants a list of names, use "emp_name".
            Use "all" only for small teams (fewer than 50 people).
            Common fields: competency, BU, objective, objective_description,
            self_comments, manager_comments, practice_manager_comments,
            self_rating, direct_manager_rating, achievement,
            achievement_percentage, category_band, promotion_date.
        :param assessment_year: Filter to a specific PEP cycle year, e.g. "2026" or "2025".
            Pass an empty string (default) to return data across ALL years.
        :return: JSON string with one merged entry per employee.
        """
        logging.info("[Retrieval] search_by_practice_manager: practice_manager_email=%s fields=%s assessment_year=%s", practice_manager_email, fields, assessment_year)

        # Security: enforce that users can only query their own practice team.
        principal = getattr(self, "_principal_email", None)
        if principal:
            if practice_manager_email.strip().lower() != principal:
                logging.warning(
                    "[Retrieval] search_by_practice_manager: LLM requested email '%s' but principal is '%s'; overriding.",
                    practice_manager_email, principal,
                )
            practice_manager_email = principal

        safe_email = practice_manager_email.strip().lower().replace("'", "''")
        field_list = None if fields.strip().lower() == "all" else [
            f.strip() for f in fields.split(",") if f.strip()
        ]

        # Optional year filter
        year = assessment_year.strip()
        year_filter = f" and assessment_year eq '{year.replace(chr(39), chr(39)*2)}'" if year else ""

        try:
            search_user_token = await self._get_search_user_token_for_trimming()
            raw = await self._fetch_all_by_filter(
                select_fields="title,url,filepath,chunk_id,emp_name,manager_email,manager_name,practice_manager_email,practice_manager_name,content",
                filter_expr=f"practice_manager_email eq '{safe_email}'{year_filter}",
                search_user_token=search_user_token,
            )

            results_list = self._merge_results_by_employee(raw, fields=field_list)
            n = len(results_list)
            logging.info(
                "[Retrieval] search_by_practice_manager: %d raw chunk(s) merged into %d employee(s) for practice manager '%s' (fields=%s, year=%s)",
                len(raw), n, practice_manager_email, fields, year or "all",
            )
            return json.dumps(results_list)

        except Exception as e:
            logging.error("[Retrieval] search_by_practice_manager failed: %s", e, exc_info=True)
            return json.dumps({"error": str(e), "practice_manager_email": practice_manager_email})

    async def fetch_filepath_from_index(self, document_id: str) -> Optional[str]:
        """
        Fetch filepath directly from Azure AI Search index using document ID.
        
        Args:
            document_id: Document ID from Azure Search
            
        Returns:
            Filepath string from the index, or None if not found
        """
        try:
            logging.info("[Citations] 🔍 Fetching filepath from index for document_id: %s", document_id)
            
            document = await self.get_document(
                index_name=self.index_name,
                document_id=document_id,
                select_fields=['filepath', 'title']
            )
            
            if document:
                filepath = document.get('filepath')
                if filepath:
                    logging.info("[Citations] ✅ Found filepath in index: %s", filepath)
                    return filepath
                else:
                    logging.warning("[Citations] ⚠️ Document found but 'filepath' field is empty")
            else:
                logging.warning("[Citations] ⚠️ Document not found with ID: %s", document_id)
                
        except Exception as e:
            logging.error("[Citations] ❌ Error fetching document from index: %s", e, exc_info=True)

        return None


_search_client_instance = None

def get_search_client() -> SearchClient:
    """Returns a singleton SearchClient to reuse connections and config."""
    global _search_client_instance
    if _search_client_instance is None:
        _search_client_instance = SearchClient()
    return _search_client_instance
