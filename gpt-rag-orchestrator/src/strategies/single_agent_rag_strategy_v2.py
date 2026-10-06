import logging
import json
import time
import traceback
from typing import Optional, Any, Dict, List, cast, AsyncIterator
from azure.ai.agents.models import AsyncAgentEventHandler, AsyncAgentRunStream

# Suppress Azure SDK HTTP logging BEFORE importing azure packages
for _azure_logger in [
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.identity",
    "azure.core",
    "azure"
]:
    logger = logging.getLogger(_azure_logger)
    logger.setLevel(logging.CRITICAL)
    logger.propagate = False
    logger.disabled = True
    logger.handlers.clear()

from azure.ai.agents.models import (
    BingGroundingTool,
    FunctionTool,
    ListSortOrder,
    MessageDeltaChunk,
    MessageDeltaTextUrlCitationAnnotation,
    MessageTextContent,
)
from azure.ai.agents.aio.operations._operations import RunsOperations as RunsOperationsGenerated
from azure.ai.agents.aio import AgentsClient

from .base_agent_strategy import BaseAgentStrategy
from .agent_strategies import AgentStrategies

from dependencies import get_config
from connectors.search import get_search_client
from connectors.aifoundry import get_genai_client

# Module-level singleton for AgentsClient — eliminates per-request TCP/TLS + token overhead
_agents_client: Optional[AgentsClient] = None
_cached_agent: Optional[Any] = None  # Pre-created/fetched agent reused across all requests


async def prewarm_agents_client() -> None:
    """Pre-warm the AgentsClient HTTP pipeline and create/fetch a reusable agent.

    1. Forces TCP/TLS handshake and token acquisition so the first real request
       doesn't pay the ~5-6s cold-start penalty.
    2. Creates (or fetches) the agent at startup so _setup_agent() returns in ~0ms.
    """
    global _agents_client, _cached_agent
    cfg = get_config()
    endpoint = cfg.get("AI_FOUNDRY_PROJECT_ENDPOINT")
    if not endpoint:
        logging.warning("[Startup] AI_FOUNDRY_PROJECT_ENDPOINT not set; skipping AgentsClient pre-warm")
        return

    from connectors.identity_manager import get_identity_manager
    credential = get_identity_manager().get_aio_credential()

    _agents_client = AgentsClient(endpoint=endpoint, credential=credential)
    try:
        # list_agents returns AsyncItemPaged (async iterable, not awaitable).
        # Iterate the first item to force HTTP connection + token acquisition.
        async for _ in _agents_client.list_agents(limit=1):
            break
        logging.info("[Startup] ✅ AgentsClient pre-warmed (HTTP pipeline ready)")
    except Exception as e:
        logging.warning("[Startup] ⚠️ AgentsClient pre-warm API call failed (client still created): %s", e)

    # --- Pre-create or fetch the agent so every request gets it in ~0ms ---
    agent_id = cfg.get("AGENT_ID", "") or None
    if agent_id:
        # AGENT_ID configured: fetch the pre-existing agent
        try:
            _cached_agent = await _agents_client.get_agent(agent_id)
            logging.info("[Startup] ✅ Persistent agent pre-fetched (AGENT_ID=%s)", agent_id)
        except Exception as e:
            logging.warning("[Startup] ⚠️ Could not pre-fetch agent %s: %s", agent_id, e)
    else:
        # No AGENT_ID: create a reusable agent at startup (eliminates create+delete per request)
        try:
            from connectors.search import get_search_client
            from pathlib import Path
            from jinja2 import Environment, FileSystemLoader, StrictUndefined

            tools_list = []
            tool_resources = {}

            # Add SearchClient function tool
            aisearch_enabled = cfg.get("SEARCH_RETRIEVAL_ENABLED", True, type=bool)
            if aisearch_enabled:
                try:
                    search_client = get_search_client()
                    retrieval_tool = FunctionTool(functions={
                        search_client.search_knowledge_base,
                        search_client.search_by_manager,
                        search_client.search_by_practice_manager,
                        search_client.search_by_employee,
                    })
                    tools_list.extend(retrieval_tool.definitions)
                except Exception as e:
                    logging.warning("[Startup] Could not add search tool: %s", e)

            # Add BingGroundingTool
            bing_enabled = cfg.get("BING_RETRIEVAL_ENABLED", False, type=bool)
            bing_conn = cfg.get("BING_CONNECTION_ID", "") if bing_enabled else ""
            if bing_conn:
                bing = BingGroundingTool(connection_id=bing_conn, count=5)
                tools_list.append(bing.definitions[0])

            # Render instructions via Jinja2
            src_folder = Path(__file__).resolve().parent.parent
            prompt_dir = src_folder / "prompts" / "single_agent_rag"
            env = Environment(
                loader=FileSystemLoader(str(prompt_dir)),
                undefined=StrictUndefined,
                trim_blocks=True,
                lstrip_blocks=True,
            )
            template = env.get_template("main.jinja2")
            instructions = template.render({
                "strategy": "single_agent_rag",
                "user_context": {},
                "bing_grounding_enabled": bool(bing_conn),
                "aisearch_enabled": aisearch_enabled,
            }).strip()

            model_name = cfg.get("CHAT_DEPLOYMENT_NAME")
            _cached_agent = await _agents_client.create_agent(
                model=model_name,
                name="gpt-rag-agent-v2",
                instructions=instructions,
                tools=tools_list,
                tool_resources=tool_resources,
            )
            logging.info("[Startup] ✅ Reusable agent created and cached (id=%s)", _cached_agent.id)
        except Exception as e:
            logging.warning("[Startup] ⚠️ Could not pre-create agent (will create per-request): %s", e)


class SingleAgentRAGStrategyV2(BaseAgentStrategy):
    """
    Implements a latency-optimized V2 Retrieve-Augmented Generation strategy.
    
    This strategy prioritizes bypassing heavy cloud Agent services when possible (e.g. empty indexes),
    instead relying on local Execution via `GenAIModelClient`.
    
    When an index has data or complex tools are needed, this uses Microsoft Agent Framework (MAF)
    with explicit Event Handlers for streaming and local orchestration, avoiding polling loops.
    """

    @classmethod
    async def create(cls):
        """
        Factory method to create an instance of SingleAgentRAGStrategyV2.
        """
        logging.debug("[Agent Flow V2] Creating SingleAgentRAGStrategyV2 instance...")
        return cls()
        
    def __init__(self):
        super().__init__()
        
        logging.debug("[Init] Initializing SingleAgentRAGStrategyV2...")
        self.cfg = get_config()
        self.strategy_type = AgentStrategies.SINGLE_AGENT_RAG
        
        self.existing_agent_id = self.cfg.get("AGENT_ID", "") or None
        self.tools_list = []
        self.tool_resources = {}

        aisearch_enabled = self.cfg.get("SEARCH_RETRIEVAL_ENABLED", True, type=bool)
        if not aisearch_enabled:
            logging.warning("[Init V2] SEARCH_RETRIEVAL_ENABLED set to false. SearchClient will not be available.")
            self.search_client = None
        else:
            try:
                self.search_client = get_search_client()
                logging.info("[Init V2] ✅ SearchClient initialized (singleton)")
            except Exception as e:
                logging.error("[Init V2] ❌ Could not initialize SearchClient: %s", e)
                raise

        # --- Initialize BingGroundingTool ---
        bing_enabled = self.cfg.get("BING_RETRIEVAL_ENABLED", False, type=bool)
        if bing_enabled:
            bing_conn = self.cfg.get("BING_CONNECTION_ID", "")
            if bing_conn:
                bing = BingGroundingTool(connection_id=bing_conn, count=5)
                bing_def = bing.definitions[0]
                self.tools_list.append(bing_def)
                logging.debug(f"[Init V2] Added BingGroundingTool: {bing_def}")
            else:
                logging.error("[Init V2] BING_CONNECTION_ID not set")

        # Initialize Direct LLM Client for Bypass scenario (singleton)
        self.llm_client = get_genai_client()

    async def initiate_agent_flow(self, user_message: str):
        """
        V2 Latency-Optimized Agent Flow.
        Routes locally based on cached index state:
        - If empty: Direct streaming chat completion via GenAIModelClient (Bypass)
        - If not empty: MAF / Agent V2 streaming with event handlers
        """
        flow_start = time.time()

        # Inject logged-in user context so the model can resolve self-referential queries
        # (e.g. "my reportees", "my manager") without requiring the user to type their email.
        # This works for both the direct-LLM and MAF/cached-agent paths.
        principal_name = (self.user_context or {}).get("principal_name", "")
        if principal_name and principal_name != "anonymous":
            logging.info("[Agent Flow V2] Injecting logged-in user context for principal: %s", principal_name)
            user_message = (
                f"[Context: the logged-in user's email is {principal_name}. "
                f"When the user refers to 'me', 'my', 'I', or 'myself', use this email as the identity.]\n\n"
                f"{user_message}"
            )
        else:
            logging.debug("[Agent Flow V2] No authenticated principal; proceeding without user context injection.")
        
        is_empty = False
        if self.search_client:
            is_empty = await self.search_client.is_index_empty()
            
        logging.info(f"[Agent Flow V2] Index Empty Check Result: {is_empty} ({round(time.time() - flow_start, 2)}s)")
        
        # 1. Bypass Routing: Empty Index goes directly to Chat Completion for ~Instant TTFB
        if is_empty and not self.cfg.get("BING_RETRIEVAL_ENABLED", False, type=bool):
            logging.info("[Agent Flow V2] ⚡ Routing locally directly to LLM (Search Index is empty, bypassing Agents)")
            async for chunk in self._stream_direct_llm(user_message):
                yield chunk
        else:
            # 2. Complex Routing: Use Agent Framework V2
            logging.info("[Agent Flow V2] 🤖 Routing to Microsoft Agent Framework (Search Index has data or Bing enabled)")
            async for chunk in self._stream_maf_agent(user_message):
                yield chunk
                
        logging.info(f"[Agent Flow V2] Total flow time: {round(time.time() - flow_start, 2)}s")

    async def _stream_direct_llm(self, user_message: str):
        """
        Direct lightweight GenAI client stream, skipping all MAF overhead.
        Used for basic Q&A when no search data exists.
        """
        stream_start = time.time()

        aisearch_enabled = self.cfg.get("SEARCH_RETRIEVAL_ENABLED", True, type=bool)
        bing_enabled = self.cfg.get("BING_RETRIEVAL_ENABLED", False, type=bool)
        
        # Build prompt
        prompt_context = {
            "strategy": self.strategy_type.value,
            "user_context": self.user_context or {},
            "aisearch_enabled": aisearch_enabled,
            "bing_grounding_enabled": bing_enabled,
        }
        system_prompt = await self._read_prompt("main", use_jinja2=True, jinja2_context=prompt_context)
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ]
        
        # Optional: Append history from CosmosDB conversation if we want context
        history = self.conversation.get("messages", [])
        if history:
             for msg in history[-5:]: # Keep last 5 for speed
                 messages.insert(1, msg)
                 
        try:
             # Fast streaming completion
             response_stream = await self.llm_client.openai_client.chat.completions.create(
                 model=self.llm_client.chat_deployment,
                 messages=messages,
                 stream=True,
             )
             
             first_token = False
             async for update in response_stream:
                 if not first_token:
                     logging.info(f"[Agent Flow V2] ⚡ Direct LLM First Token: {time.time() - stream_start:.2f}s")
                     first_token = True
                      
                 if update.choices and update.choices[0].delta.content:
                     yield update.choices[0].delta.content
                     
        except Exception as e:
             logging.error(f"[Agent Flow V2] Direct LLM Streaming failed: {e}", exc_info=True)
             raise
             
        # Add assistant response to history
        self.conversation.setdefault("messages", []).extend([
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": "Assistant generated response directly via stream."}
        ])

    # Keywords that indicate the user wants analysis/aggregation/filtering rather than a plain list.
    _ANALYTICAL_KEYWORDS = {
        # Aggregation / ranking
        "highest", "lowest", "maximum", "minimum", "max", "min",
        "top", "bottom", "most", "least", "average", "avg",
        "count", "how many", "which", "who has", "who have",
        "best", "worst", "rank", "compare",
        "greater than", "less than", "above", "below", "exceed",
        "sort", "order by", "percentage", "percent",
        # Filtering intent
        "filter", "only", "with", "having", "who were", "who was",
        "that have", "that has", "that were", "that was",
        "promoted", "promotion", "joined", "separated", "active",
        "not null", "non-null", "available", "missing", "empty",
        # Band / performance category
        "ec", "sc", "cc", "ic", "nc",
        "exceptional", "significant", "inconsistent", "non-contributor",
        "band", "performance band", "performer", "performers",
        "distribution", "band distribution",
        # Achievement / targets / KRA
        "achievement", "achieved", "exceeding", "exceeded", "overachieved",
        "on track", "below target", "not entered", "target",
        "kra", "score",
        # Self vs manager rating
        "self rating", "self assessment", "rating gap",
        "over-rated", "under-rated", "discrepancy", "aligned",
        # Active / separation / attrition
        "resigned", "attrition", "current employees", "active only",
        # Business unit / competency
        "business unit", "bu", "grade", "competency level",
    }

    async def _process_stream(self, agents_client, stream, thread_id, user_message: str = ""):
        tool_outputs_to_submit = None
        run_id_to_submit = None
        first_token = False
        direct_output_yielded = False  # True when filter tool result was streamed directly
        _user_msg_lower = user_message.lower()
        async for event_type, event_data, raw in stream:
            
            if event_type == "thread.message.delta":
                # Suppress LLM message when we already streamed the formatted table directly
                if direct_output_yielded:
                    continue
                if not first_token:
                    start_t = getattr(self, '_stream_start_time', time.time())
                    logging.info(f"[Agent Flow V2] 🤖 MAF First Token: {time.time() - start_t:.2f}s")
                    first_token = True
                    
                chunk = ""
                delta = getattr(event_data, "delta", None)
                if delta is not None:
                    # delta could be a dict or a MessageDelta object
                    content = delta.get("content") if isinstance(delta, dict) else getattr(delta, "content", None)
                    if content and isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict):
                                text_obj = block.get("text", {})
                                if isinstance(text_obj, dict) and text_obj.get("value"):
                                    chunk += text_obj["value"]
                            else:
                                text_obj = getattr(block, "text", None)
                                val = getattr(text_obj, "value", None) if text_obj else None
                                if val:
                                    chunk += val
                else:
                    chunk = getattr(event_data, "text", getattr(raw, "text", ""))
                    
                # Minimal inline Bing citation processor to avoid V1 regex overhead if possible
                if chunk and '【' in chunk:
                    from .single_agent_rag_strategy_v1 import process_bing_citations
                    chunk = process_bing_citations(event_data)
                    
                if chunk:
                    yield chunk

            elif event_type == "thread.run.requires_action":
                logging.info(f"[Agent Flow V2] Run requires action. Executing tools natively...")
                
                tool_calls = event_data.required_action.submit_tool_outputs.tool_calls
                tool_outputs = []
                _retrieval_tools = {"search_knowledge_base", "search_by_manager", "search_by_practice_manager", "search_by_employee"}
                _filter_tools = {"search_by_manager", "search_by_practice_manager"}
                for tc in tool_calls:
                    if tc.function.name in _retrieval_tools and self.search_client:
                        try:
                            args = json.loads(tc.function.arguments)
                            t0 = time.time()
                            fn = getattr(self.search_client, tc.function.name)
                            result = await fn(**args)
                            result_str = result if isinstance(result, str) else json.dumps(result)
                            logging.info(f"[Agent Flow V2] {tc.function.name} executed in {time.time()-t0:.2f}s")

                            if tc.function.name in _filter_tools:
                                # Detect empty result — skip direct-format path so LLM generates a natural no-results response
                                try:
                                    _parsed_result = json.loads(result_str)
                                    _is_empty_result = isinstance(_parsed_result, list) and len(_parsed_result) == 0
                                except Exception:
                                    _is_empty_result = False

                                # Detect analytical intent — let LLM process data instead of streaming table directly
                                # If only emp_name is requested, it's never analytical — force direct-stream path
                                _fields_arg = args.get("fields", "emp_name").strip().lower()
                                _names_only = _fields_arg in ("emp_name", "")
                                _is_analytical = (not _names_only) and any(kw in _user_msg_lower for kw in self._ANALYTICAL_KEYWORDS)

                                if _is_empty_result:
                                    logging.info(f"[Agent Flow V2] {tc.function.name} returned 0 results; letting LLM generate no-results response")
                                    tool_outputs.append({
                                        "tool_call_id": tc.id,
                                        "output": "No employees were found for this query."
                                    })
                                elif _is_analytical:
                                    # Analytical query — send raw JSON to LLM so it can aggregate/filter/rank
                                    logging.info(f"[Agent Flow V2] {tc.function.name} analytical query detected; forwarding data to LLM for analysis")
                                    max_bytes = 470_000
                                    if len(result_str.encode("utf-8")) > max_bytes:
                                        result_str = result_str.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
                                        logging.warning(f"[Agent Flow V2] {tc.function.name} analytical output truncated to 470KB")
                                    tool_outputs.append({
                                        "tool_call_id": tc.id,
                                        "output": result_str
                                    })
                                else:
                                    # Display-all query — format and stream the table directly, bypass LLM entirely
                                    _employee_count = len(_parsed_result) if isinstance(_parsed_result, list) else "some"
                                    try:
                                        # Build contextual intro based on tool and fields requested
                                        if _names_only:
                                            _fields_label = None
                                        else:
                                            _fields_label = _fields_arg.replace(",", ", ")
                                        if tc.function.name == "search_by_practice_manager":
                                            _intro = "Here are the employees in your practice team"
                                        else:
                                            _intro = "Here are the employees reporting to you"
                                        if _fields_label:
                                            _intro += f", along with their {_fields_label}"
                                        _intro += ":\n\n"

                                        formatted = self.search_client._format_filter_result(result_str, names_only=_names_only)
                                        logging.info(f"[Agent Flow V2] {tc.function.name} direct output: {len(formatted)} chars")
                                        yield _intro + formatted
                                        direct_output_yielded = True
                                    except Exception as fmt_err:
                                        logging.warning(f"[Agent Flow V2] _format_filter_result failed ({fmt_err}); falling back to LLM")
                                    # Submit stub: LLM only needs to add a short follow-up question
                                    tool_outputs.append({
                                        "tool_call_id": tc.id,
                                        "output": f"Table with {_employee_count} employee(s) already displayed to the user. Add one short helpful follow-up question (e.g. ask if they want ratings, band, objectives, or competency for this team)."
                                    })
                            else:
                                # search_knowledge_base: send full result to LLM as before
                                max_bytes = 470_000
                                if len(result_str.encode("utf-8")) > max_bytes:
                                    result_str = result_str.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
                                    logging.warning(f"[Agent Flow V2] {tc.function.name} output truncated to 470KB (512KB API limit)")
                                tool_outputs.append({
                                    "tool_call_id": tc.id,
                                    "output": result_str
                                })
                        except Exception as e:
                            logging.error(f"Error executing tool {tc.function.name}: {e}")
                            tool_outputs.append({
                                "tool_call_id": tc.id,
                                "output": json.dumps({"error": str(e)})
                            })
                
                if tool_outputs:
                    tool_outputs_to_submit = tool_outputs
                    run_id_to_submit = event_data.id
                    # Let the stream iterate to completion naturally (so the server confirms closure) and proceed.
                    pass
                            
            elif event_type == "thread.run.failed":
                err_msg = getattr(event_data, "last_error", None)
                err_txt = getattr(err_msg, "message", "Unknown Error") if err_msg else "Unknown Error"
                logging.error(f"[Agent Flow V2] Run failed: {err_txt}")
                yield f"\n\n[ERROR]: The agent encountered an unexpected error: {err_txt}"
                
        if tool_outputs_to_submit and run_id_to_submit:
            # Poll the run status to ensure it has transitioned to requires_action on the backend
            # before submitting tool outputs to prevent the 'in_progress' conflict error.
            import asyncio
            for _ in range(15):
                run_state = await agents_client.runs.get(thread_id=thread_id, run_id=run_id_to_submit)
                logging.info(f"[Agent Flow V2] Run {run_id_to_submit} status is {run_state.status}")
                if run_state.status in ["requires_action", "failed", "completed", "cancelled"]:
                    break
                await asyncio.sleep(2)
                
            if run_state.status != "requires_action":
                logging.warning(f"[Agent Flow V2] Run transitioned to {run_state.status} natively. Bypassing manual tool submission.")
                return

                
            # We bypass submit_tool_outputs_stream and the telemetry patched instance 
            # method because the telemetry instrumentor crashes on `stream=True`.
            kwargs_stream = {"stream_parameter": True, "stream": True}
            response = await RunsOperationsGenerated.submit_tool_outputs(
                agents_client.runs,
                thread_id=thread_id,
                run_id=run_id_to_submit,
                tool_outputs=tool_outputs_to_submit,
                **kwargs_stream
            )
            response_iterator = cast(AsyncIterator[bytes], response)
            event_handler = AsyncAgentEventHandler()
            stream2 = AsyncAgentRunStream(response_iterator, agents_client.runs._handle_submit_tool_outputs, event_handler)
            
            async with stream2 as s2:
                async for chunk in self._process_stream(agents_client, s2, thread_id, user_message=user_message):
                    yield chunk

    async def _stream_maf_agent(self, user_message: str):
        """
        Microsoft Agent Framework V2 implementation.
        Uses Event Handlers in azure-ai-projects v2 SDK for real-time streaming,
        avoiding polling loops entirely to guarantee sub-millisecond retrieval impact.
        """
        stream_start = time.time()
        conv = self.conversation
        thread_id = conv.get("thread_id")
        
        if self.search_client:
            # Add retrieval via SearchClient
            retrieval_functions = {self.search_client.search_knowledge_base}
            retrieval_tool = FunctionTool(functions=retrieval_functions)
            for tool_def in retrieval_tool.definitions:
                if tool_def not in self.tools_list:
                    self.tools_list.append(tool_def)
        
        # Reuse singleton AgentsClient — eliminates per-request TCP/TLS + token acquisition overhead
        global _agents_client
        if _agents_client is None:
            _agents_client = AgentsClient(
                endpoint=self.project_endpoint,
                credential=self.credential
            )
        agents_client = _agents_client

        # MAF Features: Auto-functions
        if self.search_client:
            # OBO prep
            allow_anonymous = self.cfg.get("ALLOW_ANONYMOUS", default=True, type=bool)
            request_access_token = getattr(self, "request_access_token", None)
            principal_email = (self.user_context or {}).get("principal_name", "") or None
            try:
                self.search_client.set_request_context(
                    api_access_token=request_access_token,
                    allow_anonymous=allow_anonymous,
                    principal_email=principal_email,
                )
            except Exception:
                pass

        # Determine if we need to create an agent this request
        # Priority: _cached_agent (pre-warmed) > existing_agent_id (config) > create new
        create_agent = not self.existing_agent_id and _cached_agent is None
        instructions = None
        if create_agent:
            # Fallback: pre-warm didn't cache an agent — render instructions for per-request creation
            bing_enabled = bool(self.cfg.get("BING_CONNECTION_ID", ""))
            aisearch_enabled = self.cfg.get("SEARCH_RETRIEVAL_ENABLED", True, type=bool)
            prompt_context = {
                "strategy": self.strategy_type.value,
                "user_context": self.user_context or {},
                "bing_grounding_enabled": bing_enabled,
                "aisearch_enabled": aisearch_enabled,
            }
            instructions = await self._read_prompt("main", use_jinja2=True, jinja2_context=prompt_context)

        # Parallel thread + agent setup — agent resolves from cache in ~0ms when pre-warmed
        import asyncio
        t0 = time.time()

        async def _setup_thread():
            if thread_id:
                return await agents_client.threads.get(thread_id)
            return await agents_client.threads.create()

        async def _setup_agent():
            # 1. Pre-warmed cache (created or fetched at startup) — ~0ms
            if _cached_agent is not None:
                return _cached_agent
            # 2. AGENT_ID configured but not cached (pre-warm failed) — GET ~2s
            if self.existing_agent_id:
                return await agents_client.get_agent(self.existing_agent_id)
            # 3. No cache, no AGENT_ID — create per-request (fallback) — POST ~3s
            return await agents_client.create_agent(
                model=self.model_name,
                name="gpt-rag-agent-v2",
                instructions=instructions,
                tools=self.tools_list,
                tool_resources=self.tool_resources,
            )

        thread, agent = await asyncio.gather(_setup_thread(), _setup_agent())

        if not thread_id:
            conv["thread_id"] = thread.id
        if create_agent:
            conv["agent_id"] = agent.id
        logging.info(f"[Agent Flow V2][Telemetry] Thread + Agent parallel setup took: {time.time() - t0:.2f}s")

        # Send message
        t2 = time.time()
        await agents_client.messages.create(
            thread_id=thread.id,
            role="user",
            content=user_message
        )
        logging.info(f"[Agent Flow V2][Telemetry] Message creation took: {time.time() - t2:.2f}s")

        logging.info(f"[Agent Flow V2] Streaming from MAF SDK V2 Event Handlers...")
        self._stream_start_time = stream_start
        t_stream = time.time()

        # Build per-run additional_instructions to inject the logged-in user's identity.
        # This is more reliable than a [Context: ...] message prefix because it goes
        # directly into the system context for this specific run, even when using a
        # pre-warmed / cached agent whose static instructions don't know who is logged in.
        _principal_name = (self.user_context or {}).get("principal_name", "")
        if _principal_name and _principal_name != "anonymous":
            _additional_instructions = (
                f"IMPORTANT: The currently authenticated user's email is {_principal_name}. "
                f"Whenever the user uses words like 'me', 'my', 'I', or 'myself' to refer to themselves "
                f"(e.g. 'my reportees', 'my manager', 'my team', 'who reports to me'), "
                f"you MUST use {_principal_name} as their identity in all tool call arguments. "
                f"For example, if asked for 'my reportees', call search_by_manager with "
                f"manager_email=\"{_principal_name}\".\n\n"
                f"SECURITY RESTRICTION: You are only permitted to retrieve reportee or team data for "
                f"the logged-in user ({_principal_name}). "
                f"If the user asks about another person's reportees or team (e.g. 'who reports to other@company.com'), "
                f"do NOT call any search tool. Instead respond: "
                f"'I can only retrieve reportee information for your own account. "
                f"I'm not able to look up another person\\'s team.'"
            )
            logging.info(
                "[Agent Flow V2] Injecting user identity via additional_instructions: %s",
                _principal_name,
            )
        else:
            _additional_instructions = None
            logging.warning(
                "[Agent Flow V2] principal_name is '%s' — user identity will NOT be injected. "
                "Self-referential queries (e.g. 'my reportees') cannot be resolved. "
                "Ensure the UI sends a Bearer token and OAUTH_AZURE_AD_TENANT_ID / "
                "OAUTH_AZURE_AD_CLIENT_ID are configured.",
                _principal_name or "empty",
            )

        try:
            # MAF Feature: Native Real-time event streaming
            async with await agents_client.runs.stream(
                thread_id=thread.id,
                agent_id=agent.id,
                additional_instructions=_additional_instructions,
                max_completion_tokens=16000,
            ) as stream:
                async for chunk in self._process_stream(agents_client, stream, thread.id, user_message=user_message):
                    yield chunk

        except Exception as e:
            err_msg = traceback.format_exc()
            logging.error(f"[Agent Flow V2] MAF Streaming failed: {err_msg}")
            yield f"[ERROR in MAF Streaming]: {e}\n{err_msg}"

        # Cleanup Agent — only if created per-request (not cached/persistent)
        if create_agent:
            try:
                await agents_client.delete_agent(agent.id)
            except Exception:
                pass
