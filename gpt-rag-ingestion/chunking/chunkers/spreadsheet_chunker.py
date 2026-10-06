import gc
import logging 
import os
import time
from collections import OrderedDict

from io import BytesIO

from openpyxl import load_workbook
from tabulate import tabulate

from .base_chunker import BaseChunker
from dependencies import get_config

app_config_client = get_config()

class SpreadsheetChunker(BaseChunker):
    """
    SpreadsheetChunker processes and chunks spreadsheet content, such as Excel files, into manageable pieces for analysis and summarization. 
    It handles both chunking by rows or sheets, allowing users to specify whether to include header rows in each chunk, and ensures that 
    the content size does not exceed a specified token limit.

    The class supports the following operations:
    - Converts spreadsheets into chunkable content.
    - Provides options to chunk either by row or by sheet.
    - Includes optional header rows in chunks.
    - Summarizes large sheets if the content exceeds the maximum chunk size.
    - Groups multiple rows that share the same value in a designated column (e.g., employee ID) into a single chunk.
    
    Attributes:
    -----------
    max_chunk_size (int): Maximum allowed size of each chunk in tokens.
    chunking_by_row (bool): Whether to chunk by row instead of by sheet.
    include_header_in_chunks (bool): Whether to include header rows in each row-based chunk.
    group_by_column (str): Column header name to group rows by (e.g., "Employee ID"). When set, all rows
        sharing the same value in this column are combined into one chunk. Requires chunking_by_row=True.
    document_content (str): Processed spreadsheet content ready for chunking.

    Methods:
    --------
    - get_chunks(): Splits the spreadsheet content into manageable chunks, based on the configuration.
    - _spreadsheet_process(): Extracts and processes data from each sheet, including summaries if necessary.
    - _get_sheet_data(sheet): Retrieves data and headers from the given sheet, handling empty cells.
    - _clean_markdown_table(table_str): Cleans up Markdown table strings by removing excessive whitespace.
    """

    # Columns that carry the SAME value in every row for a given employee group.
    # In grouped-employee chunks these are shown ONCE in section headers and are
    # excluded from the per-row embedding text so the vector focuses on KPI data.
    _DEDUP_COLS: frozenset = frozenset({
        # Identity
        "assessment_year", "employee_id", "employee_email", "emp_name",
        "separation_status",
        # Manager / practice-manager identity
        "manager_id", "manager_email", "manager_name",
        "practice_manager_id", "practice_manager_email", "practice_manager_name",
        # Overall summary comments (not per-objective)
        "self_overall_comments", "manager_overall_comments",
        "practice_overall_comments",
        # Final assessment results
        "manager_validated_final_kra_score", "category_band", "promotion_date",
        # Employee profile fields (same value for all KPI rows)
        "competency", "BU", "joined_date",
        # Appreciations (employee-level aggregate)
        "appreciation_count", "appreciation_ids", "appreciation_titles",
        "appreciation_messages", "appreciation_subcategories",
        "appreciation_first_created", "appreciation_last_modified",
        # Awards (employee-level aggregate)
        "award_count", "award_types", "award_categories",
        "award_years", "award_citations",
        # Projects (employee-level aggregate)
        "project_count", "project_names", "project_managers",
        "delivery_managers", "billable_roles", "start_dates",
        "end_dates", "allocations",
        # Additional manager cross-references
        "addl_mgr_data_ids", "addl_mgr_request_ids", "addl_mgr_emails",
        "addl_mgr_comments", "addl_mgr_last_updated",
        # Check-in rollups (employee-level, not per-objective)
        "checkin_count", "checkin_objective_cycle_id", "checkin_cycle_id",
        "emp_checkins_cycle_id", "checkin_self_comments", "checkin_achievement",
        "calculated_goal_score", "manager_goal_score", "mgr_checkin_comments",
        "latest_checkin_status", "checkin_self_overall_comments",
        "mgr_checkin_overall_comments", "checkin_last_modified",
        # Computed rating averages and gap (derived from per-objective ratings at ingestion time)
        "self_rating_avg", "manager_rating_avg", "practice_manager_rating_avg",
        "rating_self_mgr_gap",
    })

    def __init__(self, data, max_chunk_size=None, chunking_by_row=None, include_header_in_chunks=None, group_by_column=None):
        """
        Initializes the SpreadsheetChunker with the provided data and environment configurations.
        
        Args:
            data (str): The spreadsheet content to be chunked.
            max_chunk_size (int, optional): Maximum allowed size of each chunk in tokens. Defaults to an environment variable 'SPREADSHEET_CHUNKING_NUM_TOKENS' or 0 if not set.
            chunking_by_row (bool, optional): Whether to chunk by row instead of by sheet. Defaults to an environment variable 'CHUNKING_BY_ROW' or False.
            include_header_in_chunks (bool, optional): Whether to include the header row in each chunk if chunking by row. Defaults to 'INCLUDE_HEADER_IN_CHUNKS' environment variable or False.
            group_by_column (str, optional): Column header name to group rows by. When set, all rows sharing
                the same value in this column are combined into a single chunk (requires chunking_by_row=True).
                Defaults to 'SPREADSHEET_CHUNKING_GROUP_BY_COLUMN' environment variable or empty string (disabled).
        """
        super().__init__(data)
        
        if max_chunk_size is None:
            self.max_chunk_size = int(app_config_client.get("SPREADSHEET_CHUNKING_NUM_TOKENS", 0))
        else:
            self.max_chunk_size = int(max_chunk_size)
        
        if chunking_by_row is None:
            chunking_env = app_config_client.get("SPREADSHEET_CHUNKING_BY_ROW", "true").lower()
            self.chunking_by_row = chunking_env in ["true", "1", "yes"]
        else:
            self.chunking_by_row = bool(chunking_by_row)
        
        if include_header_in_chunks is None:
            include_header_env = app_config_client.get("SPREADSHEET_CHUNKING_BY_ROW_INCLUDE_HEADER", "true").lower()
            self.include_header_in_chunks = include_header_env in ["true", "1", "yes"]
        else:
            self.include_header_in_chunks = bool(include_header_in_chunks)

        if group_by_column is None:
            self.group_by_column = app_config_client.get("SPREADSHEET_CHUNKING_GROUP_BY_COLUMN", "").strip()
        else:
            self.group_by_column = str(group_by_column).strip() if group_by_column else ""
        logging.info(f"[spreadsheet_chunker][{self.filename}] group_by_column='{self.group_by_column}' chunking_by_row={self.chunking_by_row}")

    def get_chunks(self):
        """
        Splits the spreadsheet content into smaller chunks. Depending on the configuration, chunks can be created by sheet or by row.
        - If chunking by sheet, the method summarizes content that exceeds the maximum chunk size.
        - If chunking by row, each row is processed into its own chunk, optionally including the header row.
        
        Returns:
            List[dict]: A list of dictionaries representing the chunks created from the spreadsheet.
        """
        return list(self.iter_chunks())

    def iter_chunks(self):
        """Yield chunks one-by-one to avoid buffering all chunks/vectors in memory."""
        logging.info(f"[spreadsheet_chunker][{self.filename}][iter_chunks] Running iter_chunks.")
        total_start_time = time.time()

        blob_stream = BytesIO(self.document_bytes)
        workbook = load_workbook(blob_stream, data_only=True)
        logging.info(
            f"[spreadsheet_chunker][{self.filename}][iter_chunks] Workbook has {len(workbook.sheetnames)} sheets"
        )

        chunk_id = 0
        if not self.chunking_by_row:
            # Original behavior: Chunk per sheet
            for sheet_name in workbook.sheetnames:
                start_time = time.time()
                current_chunk_id = chunk_id
                sheet = workbook[sheet_name]
                logging.debug(
                    f"[spreadsheet_chunker][{self.filename}][iter_chunks][{sheet_name}] "
                    f"Starting processing chunk {current_chunk_id} (sheet)."
                )

                data, headers = self._get_sheet_data(sheet)
                table_content = tabulate(data, headers=headers, tablefmt="grid")
                table_content = self._clean_markdown_table(table_content)
                table_tokens = self.token_estimator.estimate_tokens(table_content)

                prompt = (
                    f"Summarize the table with data in it, by understanding the information clearly.\n "
                    f"table_data:{table_content}"
                )
                summary = self.aoai_client.get_completion(prompt, max_tokens=2048)

                if self.max_chunk_size > 0 and table_tokens > self.max_chunk_size:
                    logging.info(
                        f"[spreadsheet_chunker][{self.filename}][iter_chunks][{sheet_name}] "
                        f"Table has {table_tokens} tokens. Max tokens is {self.max_chunk_size}. Using summary."
                    )
                    table_content = summary

                chunk_dict = self._create_chunk(
                    chunk_id=current_chunk_id,
                    content=table_content,
                    summary=summary,
                    embedding_text=summary if summary else table_content,
                    title=sheet_name,
                )
                yield chunk_dict
                chunk_id += 1
                elapsed_time = time.time() - start_time
                logging.debug(
                    f"[spreadsheet_chunker][{self.filename}][iter_chunks][{sheet_name}] "
                    f"Processed chunk {current_chunk_id} in {elapsed_time:.2f} seconds."
                )
        else:
            # New behavior: Chunk per row (streaming)
            for sheet_name in workbook.sheetnames:
                sheet = workbook[sheet_name]
                logging.info(
                    f"[spreadsheet_chunker][{self.filename}][iter_chunks][{sheet_name}] "
                    "Starting row-wise chunking."
                )

                try:
                    header_row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True))
                except StopIteration:
                    continue
                headers = ["" if v is None else str(v) for v in (header_row or [])]

                # Determine if we should group rows by a column value
                group_col_idx = None
                if self.group_by_column:
                    col_names_lower = [h.lower() for h in headers]
                    target = self.group_by_column.lower()
                    if target in col_names_lower:
                        group_col_idx = col_names_lower.index(target)
                    else:
                        logging.warning(
                            f"[spreadsheet_chunker][{self.filename}][iter_chunks][{sheet_name}] "
                            f"Group-by column '{self.group_by_column}' not found in headers {headers}. "
                            "Falling back to row-by-row chunking."
                        )

                if group_col_idx is not None:
                    # --- Grouped chunking: collect all rows per group key, yield one chunk per group ---
                    groups = OrderedDict()
                    for row_values in sheet.iter_rows(min_row=2, values_only=True):
                        row = ["" if v is None else str(v) for v in (row_values or [])]
                        if not any(cell.strip() for cell in row):
                            continue
                        key_val = row[group_col_idx] if group_col_idx < len(row) else ""
                        if key_val not in groups:
                            groups[key_val] = []
                        groups[key_val].append(row)

                    logging.info(
                        f"[spreadsheet_chunker][{self.filename}][iter_chunks][{sheet_name}] "
                        f"Grouped {sum(len(v) for v in groups.values())} rows into "
                        f"{len(groups)} group(s) by column '{self.group_by_column}'."
                    )

                    # Free workbook from memory before yielding — groups dict has all data needed
                    del sheet
                    try:
                        workbook.close()
                    except Exception:
                        pass
                    del workbook, blob_stream
                    gc.collect()

                    for group_key, rows in groups.items():
                        start_time = time.time()
                        current_chunk_id = chunk_id
                        logging.debug(
                            f"[spreadsheet_chunker][{self.filename}][iter_chunks][{sheet_name}] "
                            f"Processing chunk {current_chunk_id} for group '{group_key}' ({len(rows)} row(s))."
                        )

                        table = tabulate(rows, headers=headers, tablefmt="github")
                        table = self._clean_markdown_table(table)
                        content = self._grouped_rows_to_structured_content(headers=headers, rows=rows)
                        embedding_text = self._grouped_rows_to_embedding_text(
                            headers=headers,
                            rows=rows,
                            sheet_name=sheet_name,
                            group_key=group_key,
                            group_col=self.group_by_column,
                        )

                        if self.max_chunk_size and self.max_chunk_size > 0:
                            content_tokens = self.token_estimator.estimate_tokens(content)
                            if content_tokens > self.max_chunk_size:
                                logging.info(
                                    f"[spreadsheet_chunker][{self.filename}][iter_chunks][{sheet_name}] "
                                    f"Grouped content for '{group_key}' has {content_tokens} tokens. "
                                    f"Max tokens is {self.max_chunk_size}. Truncating."
                                )
                                content = self._truncate_chunk(content)
                            embed_tokens = self.token_estimator.estimate_tokens(embedding_text)
                            if embed_tokens > self.max_chunk_size:
                                embedding_text = self._truncate_chunk(embedding_text)

                        # Extract manager_email and manager_name from the first row that has them
                        col_names_lower = [h.lower() for h in headers]
                        def _extract_field(col_name):
                            if col_name in col_names_lower:
                                idx = col_names_lower.index(col_name)
                                for r in rows:
                                    val = r[idx] if idx < len(r) else ""
                                    if val.strip():
                                        return val.strip()
                            return ""

                        emp_name = _extract_field("emp_name")
                        assessment_year = _extract_field("assessment_year") or None

                        chunk_dict = self._create_chunk(
                            chunk_id=current_chunk_id,
                            content=content,
                            summary="",
                            embedding_text=embedding_text,
                            title=f"{sheet_name} - {self.group_by_column}: {group_key} - {emp_name}" if emp_name else f"{sheet_name} - {self.group_by_column}: {group_key}",
                        )

                        chunk_dict["emp_name"]               = emp_name
                        chunk_dict["assessment_year"]        = assessment_year
                        chunk_dict["manager_email"]          = _extract_field("manager_email")
                        chunk_dict["manager_name"]           = _extract_field("manager_name")
                        chunk_dict["practice_manager_email"] = _extract_field("practice_manager_email")
                        chunk_dict["practice_manager_name"]  = _extract_field("practice_manager_name")

                        yield chunk_dict
                        chunk_id += 1
                        elapsed_time = time.time() - start_time
                        logging.debug(
                            f"[spreadsheet_chunker][{self.filename}][iter_chunks][{sheet_name}] "
                            f"Processed chunk {current_chunk_id} in {elapsed_time:.2f} seconds."
                        )
                else:
                    # --- Original row-by-row chunking ---
                    for row_index, row_values in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=1):
                        row = ["" if v is None else str(v) for v in (row_values or [])]
                        if not any(cell.strip() for cell in row):
                            continue

                        start_time = time.time()
                        current_chunk_id = chunk_id
                        logging.debug(
                            f"[spreadsheet_chunker][{self.filename}][iter_chunks][{sheet_name}] "
                            f"Processing chunk {current_chunk_id} for row {row_index}."
                        )

                        if self.include_header_in_chunks:
                            table = tabulate([headers, row], headers="firstrow", tablefmt="github")
                        else:
                            table = tabulate([row], headers=headers, tablefmt="github")

                        table = self._clean_markdown_table(table)
                        content = table
                        embedding_text = self._row_to_embedding_text(
                            headers=headers,
                            row=row,
                            sheet_name=sheet_name,
                            row_index=row_index,
                            include_header_in_embedding=self.include_header_in_chunks,
                        )

                        if self.max_chunk_size and self.max_chunk_size > 0:
                            content_tokens = self.token_estimator.estimate_tokens(content)
                            if content_tokens > self.max_chunk_size:
                                logging.info(
                                    f"[spreadsheet_chunker][{self.filename}][iter_chunks][{sheet_name}] "
                                    f"Row content has {content_tokens} tokens. Max tokens is {self.max_chunk_size}. Truncating."
                                )
                                content = self._truncate_chunk(content)
                            embed_tokens = self.token_estimator.estimate_tokens(embedding_text)
                            if embed_tokens > self.max_chunk_size:
                                embedding_text = self._truncate_chunk(embedding_text)

                        chunk_dict = self._create_chunk(
                            chunk_id=current_chunk_id,
                            content=content,
                            summary="",
                            embedding_text=embedding_text,
                            title=f"{sheet_name} - Row {row_index}",
                        )
                        yield chunk_dict
                        chunk_id += 1
                        elapsed_time = time.time() - start_time
                        logging.debug(
                            f"[spreadsheet_chunker][{self.filename}][iter_chunks][{sheet_name}] "
                            f"Processed chunk {current_chunk_id} in {elapsed_time:.2f} seconds."
                        )

        total_elapsed_time = time.time() - total_start_time
        logging.debug(
            f"[spreadsheet_chunker][{self.filename}][iter_chunks] Finished iter_chunks in {total_elapsed_time:.2f} seconds."
        )

    def _spreadsheet_process(self):
        """
        Extracts and processes each sheet from the spreadsheet, converting the content into Markdown table format. 
        If chunking by sheet, a summary is generated if the sheet's content exceeds the maximum token size.

        Returns:
            List[dict]: A list of dictionaries, where each dictionary contains sheet metadata, headers, rows, table content, and a summary if applicable.
        """
        logging.debug(f"[spreadsheet_chunker][{self.filename}][spreadsheet_process] Starting blob download.")        
        blob_data = self.document_bytes
        blob_stream = BytesIO(blob_data)
        logging.debug(f"[spreadsheet_chunker][{self.filename}][spreadsheet_process] Starting openpyxl load_workbook.")                    
        workbook = load_workbook(blob_stream, data_only=True)

        sheets = []
        total_start_time = time.time()
    
        for sheet_name in workbook.sheetnames:
            logging.info(f"[spreadsheet_chunker][{self.filename}][spreadsheet_process][{sheet_name}] Started processing.")                  
            start_time = time.time()
            sheet_dict = {}            
            sheet_dict['name'] = sheet_name
            sheet = workbook[sheet_name]
            data, headers = self._get_sheet_data(sheet)
            sheet_dict["headers"] = headers
            sheet_dict["data"] = data

            # Only build the full-sheet table/summary when chunking by sheet.
            if not self.chunking_by_row:
                table = tabulate(data, headers=headers, tablefmt="grid")
                table = self._clean_markdown_table(table)
                sheet_dict["table"] = table

                prompt = f"Summarize the table with data in it, by understanding the information clearly.\n table_data:{table}"
                summary = self.aoai_client.get_completion(prompt, max_tokens=2048)
                sheet_dict["summary"] = summary
                logging.debug(
                    f"[spreadsheet_chunker][{self.filename}][spreadsheet_process][{sheet_dict['name']}] Generated summary."
                )
            else:
                sheet_dict["table"] = ""
                sheet_dict["summary"] = ""
                logging.debug(
                    f"[spreadsheet_chunker][{self.filename}][spreadsheet_process][{sheet_dict['name']}] "
                    "Skipped table/summary generation (chunking by row)."
                )
            
            elapsed_time = time.time() - start_time
            logging.debug(f"[spreadsheet_chunker][{self.filename}][spreadsheet_process][{sheet_dict['name']}] Processed in {elapsed_time:.2f} seconds.")
            sheets.append(sheet_dict)
    
        total_elapsed_time = time.time() - total_start_time
        logging.debug(f"[spreadsheet_chunker][{self.filename}][spreadsheet_process] Total processing time: {total_elapsed_time:.2f} seconds.")

        return sheets

    def _row_to_embedding_text(
        self,
        headers,
        row,
        sheet_name,
        row_index,
        include_header_in_embedding,
    ):
        """Build a compact per-row text for embeddings.

        Goals:
        - Dramatically smaller than markdown tables (lower TPM pressure).
        - If include_header_in_embedding=True, ensure the header schema is present in the embedding.

        Format (when include_header_in_embedding=True):
            file=<filename>\n
            sheet=<sheet>\n
            row=<row_index>\n
            cols=<h1>|<h2>|...\n
            vals=<v1>|<v2>|...
        """

        def _norm(value: object) -> str:
            if value is None:
                return ""
            text = str(value)
            text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
            return text.strip()

        safe_headers = [_norm(h) for h in (headers or [])]
        safe_row = [_norm(v) for v in (row or [])]

        width = max(len(safe_headers), len(safe_row))
        if len(safe_headers) < width:
            safe_headers += [""] * (width - len(safe_headers))
        if len(safe_row) < width:
            safe_row += [""] * (width - len(safe_row))

        # Keep alignment; but drop trailing fully-empty columns.
        last_nonempty = -1
        for i, (h, v) in enumerate(zip(safe_headers, safe_row)):
            if h or v:
                last_nonempty = i
        if last_nonempty >= 0:
            safe_headers = safe_headers[: last_nonempty + 1]
            safe_row = safe_row[: last_nonempty + 1]

        # We keep empty values to preserve positional alignment between cols and vals.
        # To reduce tokens, use tight separators and collapsed whitespace.
        cols = "|".join(safe_headers)
        vals = "|".join(safe_row)

        parts = [
            f"file={_norm(self.filename)}",
            f"sheet={_norm(sheet_name)}",
            f"row={row_index}",
        ]
        if include_header_in_embedding:
            parts.append(f"cols={cols}")
        parts.append(f"vals={vals}")
        return "\n".join(parts)

    def _grouped_rows_to_structured_content(self, headers, rows):
        """Build structured content for a grouped employee chunk.

        Employee-level fields (_DEDUP_COLS) are shown ONCE in labelled sections
        at the top. Per-objective fields are rendered as a compact markdown table
        (one row per objective). This eliminates repetition of overall comments
        and appreciation messages across every KPI row, dramatically reducing the
        content byte footprint for employees with many appreciations/objectives.
        """
        def _norm(value):
            if value is None:
                return ""
            text = str(value)
            text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
            return text.strip()

        col_lower = [h.lower() for h in (headers or [])]

        def _first_val(col_name):
            """Return the first non-empty value for a column across all rows."""
            if col_name not in col_lower:
                return ""
            idx = col_lower.index(col_name)
            for r in rows:
                val = _norm(r[idx] if idx < len(r) else "")
                if val:
                    return val
            return ""

        def _avg_rating(col_name):
            """Return the average of all numeric non-null values for a column across rows.

            Returns a string like "3.75" rounded to 2 decimal places, or "" if no
            numeric values are present (e.g. column absent or all \\N).
            """
            if col_name not in col_lower:
                return ""
            idx = col_lower.index(col_name)
            total, count = 0.0, 0
            for r in rows:
                raw = _norm(r[idx] if idx < len(r) else "")
                if not raw or raw in (r"\N", "N/A", "NA", "-", "none"):
                    continue
                try:
                    total += float(raw)
                    count += 1
                except ValueError:
                    pass
            if count == 0:
                return ""
            return f"{total / count:.2f}"

        sections = []

        # ── 1. EMPLOYEE SUMMARY ───────────────────────────────────────────────
        summary_cols = [
            "assessment_year", "employee_id", "emp_name", "separation_status",
            "manager_name", "manager_email",
            "practice_manager_name", "practice_manager_email",
            "category_band", "manager_validated_final_kra_score",
            "competency", "BU", "joined_date",
        ]
        summary_parts = [f"{c}: {_first_val(c)}" for c in summary_cols if _first_val(c)]

        # Compute and append average rating fields
        self_rating_avg = _avg_rating("self_rating")
        manager_rating_avg = _avg_rating("direct_manager_rating")
        practice_manager_rating_avg = _avg_rating("practice_manager_rating")
        if self_rating_avg:
            summary_parts.append(f"self_rating_avg: {self_rating_avg}")
        if manager_rating_avg:
            summary_parts.append(f"manager_rating_avg: {manager_rating_avg}")
        if practice_manager_rating_avg:
            summary_parts.append(f"practice_manager_rating_avg: {practice_manager_rating_avg}")
        if self_rating_avg and manager_rating_avg:
            gap = float(self_rating_avg) - float(manager_rating_avg)
            summary_parts.append(f"rating_self_mgr_gap: {gap:.2f}")

        sections.append("=== EMPLOYEE SUMMARY ===")
        sections.append(" | ".join(summary_parts) if summary_parts else "(no identity data)")

        # ── 2. OVERALL COMMENTS (shown once) ─────────────────────────────────
        self_overall = _first_val("self_overall_comments")
        if self_overall:
            sections.append("\n=== OVERALL SELF ASSESSMENT ===")
            sections.append(self_overall)

        mgr_overall = _first_val("manager_overall_comments")
        if mgr_overall:
            sections.append("\n=== OVERALL MANAGER ASSESSMENT ===")
            sections.append(mgr_overall)

        pm_overall = _first_val("practice_overall_comments")
        if pm_overall:
            sections.append("\n=== OVERALL PRACTICE MANAGER ASSESSMENT ===")
            sections.append(pm_overall)

        # ── 3. APPRECIATIONS (shown once) ────────────────────────────────────
        appr_messages = _first_val("appreciation_messages")
        if appr_messages:
            appr_count = _first_val("appreciation_count")
            hdr = f"\n=== APPRECIATIONS ({appr_count} total) ===" if appr_count else "\n=== APPRECIATIONS ==="
            sections.append(hdr)
            sections.append(appr_messages)

        # ── 4. AWARDS (shown once) ───────────────────────────────────────────
        award_types = _first_val("award_types")
        if award_types:
            award_count = _first_val("award_count")
            hdr = f"\n=== AWARDS ({award_count} total) ===" if award_count else "\n=== AWARDS ==="
            sections.append(hdr)
            award_parts = [
                f"{c}: {_first_val(c)}"
                for c in ["award_types", "award_categories", "award_years", "award_citations"]
                if _first_val(c)
            ]
            sections.append(" | ".join(award_parts))

        # ── 5. PROJECTS (shown once) ─────────────────────────────────────────
        project_names = _first_val("project_names")
        if project_names:
            proj_count = _first_val("project_count")
            hdr = f"\n=== PROJECTS ({proj_count} total) ===" if proj_count else "\n=== PROJECTS ==="
            sections.append(hdr)
            proj_parts = [
                f"{c}: {_first_val(c)}"
                for c in ["project_names", "project_managers", "delivery_managers",
                          "billable_roles", "allocations", "start_dates", "end_dates"]
                if _first_val(c)
            ]
            sections.append(" | ".join(proj_parts))

        # ── 6. KPI PERFORMANCE TABLE (per-objective, dedup cols excluded) ─────
        kpi_indices = [i for i, h in enumerate(col_lower) if h not in self._DEDUP_COLS]
        kpi_headers = [headers[i] for i in kpi_indices]
        kpi_rows = [
            [_norm(r[i] if i < len(r) else "") for i in kpi_indices]
            for r in rows
        ]
        sections.append("\n=== KPI PERFORMANCE ===")
        if kpi_rows:
            kpi_table = tabulate(kpi_rows, headers=kpi_headers, tablefmt="github")
            kpi_table = self._clean_markdown_table(kpi_table)
            sections.append(kpi_table)
        else:
            sections.append("(no KPI data)")

        return "\n".join(sections)

    def _grouped_rows_to_embedding_text(self, headers, rows, sheet_name, group_key, group_col):
        """Build a compact embedding text for a group of rows that share the same group-by column value.

        Employee-level columns (_DEDUP_COLS) are excluded from the per-row entries
        so the embedding vector focuses on KPI-specific content (objectives, ratings,
        comments) rather than repeating appreciation messages / overall comments for
        every row. A brief identity prefix is included so the vector still captures
        who this chunk belongs to.
        """
        def _norm(value: object) -> str:
            if value is None:
                return ""
            text = str(value)
            text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
            return text.strip()

        col_lower = [h.lower() for h in (headers or [])]

        def _first_val(col_name):
            if col_name not in col_lower:
                return ""
            idx = col_lower.index(col_name)
            for r in rows:
                val = _norm(r[idx] if idx < len(r) else "")
                if val:
                    return val
            return ""

        # Brief identity prefix so the vector knows who/what this chunk is about
        parts = [
            f"file={_norm(self.filename)}",
            f"sheet={_norm(sheet_name)}",
            f"group_col={_norm(group_col)}",
            f"group_key={_norm(group_key)}",
        ]
        for field in ("emp_name", "assessment_year", "category_band",
                      "manager_validated_final_kra_score"):
            val = _first_val(field)
            if val:
                parts.append(f"{field}={val}")

        # Per-row entries with ONLY the KPI columns (dedup cols excluded)
        include_indices = [i for i, h in enumerate(col_lower) if h not in self._DEDUP_COLS]
        kpi_headers = [_norm(headers[i]) for i in include_indices]
        parts.append(f"cols={'|'.join(kpi_headers)}")

        for row in rows:
            safe_row = [_norm(row[i] if i < len(row) else "") for i in include_indices]
            parts.append(f"row={'|'.join(safe_row)}")

        return "\n".join(parts)

    def _get_sheet_data(self, sheet):
        """
        Retrieves data and headers from the given sheet. Each row's data is processed into a list format, ensuring that empty rows are excluded.

        Args:
            sheet (Worksheet): The worksheet object to extract data from.

        Returns:
            Tuple[List[List[str]], List[str]]: A tuple containing a list of row data and a list of headers.
        """
        data = []
        for row in sheet.iter_rows(min_row=2):  # Start from the second row to skip headers
            row_data = []
            for cell in row:
                cell_value = cell.value
                if cell_value is None:
                    cell_value = ""
                cell_text = str(cell_value)
                row_data.append(cell_text)
            if "".join(row_data).strip() != "":
                data.append(row_data)

        headers = [cell.value if cell.value is not None else "" for cell in sheet[1]]
        return data, headers
    
    def _clean_markdown_table(self, table_str):
        """
        Cleans up a Markdown table string by removing excessive whitespace from each cell.

        Args:
            table_str (str): The Markdown table string to be cleaned.

        Returns:
            str: The cleaned Markdown table string with reduced whitespace.
        """
        cleaned_lines = []
        lines = table_str.splitlines()

        for line in lines:
            if set(line.strip()) <= set('-| '):
                cleaned_lines.append(line)
                continue

            cells = line.split('|')
            stripped_cells = [cell.strip() for cell in cells[1:-1]]
            cleaned_line = '| ' + ' | '.join(stripped_cells) + ' |'
            cleaned_lines.append(cleaned_line)

        return '\n'.join(cleaned_lines)