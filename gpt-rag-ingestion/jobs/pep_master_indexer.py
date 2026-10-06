"""
PEP Master Indexer
==================
Downloads six separate Excel files from Azure Blob Storage (one per data source),
merges them using the same logic as merge_to_master.py, and uploads the resulting
master Excel file back to blob storage.

Configuration keys (Azure App Configuration):
  PEP_BLOB_CONTAINER      – container that holds the source Excel files
                            (default: "pep-data")
  PEP_BLOB_PEP            – fallback PEP 2026 blob name used when auto-discovery finds nothing
                            (default: PEP_2026.xlsx)
  PEP_BLOB_CHECKINS       – blob name for sheet checkins-2026      (default: checkins-2026.xlsx)
  PEP_BLOB_PROJECTS       – blob name for sheet projects-2026      (default: projects-2026.xlsx)
  PEP_BLOB_AWARDS         – blob name for sheet awards-2026        (default: awards-2026.xlsx)
  PEP_BLOB_ADDL_MGR       – blob name for Additional_Manager_Data  (default: Additional_Manager_Data_2026.xlsx)
  PEP_BLOB_APPRECIATIONS  – blob name for appreciation-2026        (default: appreciation-2026.xlsx)
  PEP_OUTPUT_CONTAINER    – container for the merged output        (default: same as PEP_BLOB_CONTAINER)
  PEP_OUTPUT_BLOB         – blob name for the merged output        (default: PEP_master.xlsx)
"""

import asyncio
import gc
import hashlib
import io
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
from azure.core.exceptions import ResourceNotFoundError
from azure.identity.aio import AzureCliCredential, ChainedTokenCredential, ManagedIdentityCredential
from azure.storage.blob.aio import BlobServiceClient

from dependencies import get_config


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class PEPMasterIndexerConfig:
    storage_account_name: str

    pep_container: str = "pep-data"
    blob_pep: str = "PEP_2026.xlsx"  # fallback — used only when auto-discovery finds no PEP_2026*.xlsx
    blob_checkins: str = "checkins-2026.csv"
    blob_projects: str = "projects-2026.csv"
    blob_awards: str = "awards-2026.csv"
    blob_addl_mgr: str = "Additonal_Manager_Data_2026.xlsx"
    blob_appreciations: str = "appreciation_2026.csv"

    # Output goes to the documents container (watched by blob indexer) by default
    output_container: str = "documents"
    output_blob: str = "PEP_master.xlsx"

    # Historical PEP-only masters (optional — skipped if blob not found in pep-data)
    blob_pep_2025: str = "PEP_2025.csv"
    blob_pep_2024: str = "PEP_2024.csv"
    output_blob_2025: str = "PEP_master_2025.xlsx"
    output_blob_2024: str = "PEP_master_2024.xlsx"

    # Per-employee virtual folder prefix for current year output in documents container
    output_blob_prefix_2026: str = "PEP_2026"
    # Reference archive written to pep-data (NOT documents) — never indexed by blob indexer
    archive_blob: str = "PEP_master.xlsx"

    # SharePoint source (set PEP_SOURCE=sharepoint in App Config to enable)
    source: str = "blob"
    sp_site_id: str = ""
    sp_drive_id: str = ""
    sp_folder: str = "PEP Data"

    # Demo data injection (set PEP_INJECT_DEMO_DATA=true in App Config to enable)
    inject_demo_data: bool = False
    demo_blob_2026: str = "2026_PEP_demo.xlsx"
    demo_blob_2025: str = "2025_PEP_demo.xlsx"
    demo_blob_2024: str = "2024_PEP_demo.xlsx"

    @staticmethod
    def from_app_config() -> "PEPMasterIndexerConfig":
        app = get_config()
        pep_container = app.get("PEP_BLOB_CONTAINER", "pep-data")
        # Default output to the same container the blob indexer watches
        documents_container = app.get("DOCUMENTS_STORAGE_CONTAINER", "documents")
        return PEPMasterIndexerConfig(
            storage_account_name=app.get("STORAGE_ACCOUNT_NAME", ""),
            pep_container=pep_container,
            blob_pep=app.get("PEP_BLOB_PEP", "PEP_2026.xlsx"),
            blob_checkins=app.get("PEP_BLOB_CHECKINS", "checkins-2026.csv"),
            blob_projects=app.get("PEP_BLOB_PROJECTS", "projects-2026.csv"),
            blob_awards=app.get("PEP_BLOB_AWARDS", "awards-2026.csv"),
            blob_addl_mgr=app.get("PEP_BLOB_ADDL_MGR", "Additonal_Manager_Data_2026.xlsx"),
            blob_appreciations=app.get("PEP_BLOB_APPRECIATIONS", "appreciation_2026.csv"),
            output_container=app.get("PEP_OUTPUT_CONTAINER", documents_container),
            output_blob=app.get("PEP_OUTPUT_BLOB", "PEP_master.xlsx"),
            blob_pep_2025=app.get("PEP_BLOB_PEP_2025", "PEP_2025.csv"),
            blob_pep_2024=app.get("PEP_BLOB_PEP_2024", "PEP_2024.csv"),
            output_blob_2025=app.get("PEP_OUTPUT_BLOB_2025", "PEP_master_2025.xlsx"),
            output_blob_2024=app.get("PEP_OUTPUT_BLOB_2024", "PEP_master_2024.xlsx"),
            source=app.get("PEP_SOURCE", "blob"),
            sp_site_id=app.get("PEP_SHAREPOINT_SITE_ID", ""),
            sp_drive_id=app.get("PEP_SHAREPOINT_DRIVE_ID", ""),
            sp_folder=app.get("PEP_SHAREPOINT_FOLDER", "PEP Data"),
            inject_demo_data=(app.get("PEP_INJECT_DEMO_DATA", "false") or "false").lower() == "true",
            demo_blob_2026=app.get("PEP_DEMO_BLOB_2026", "2026_PEP_demo.xlsx"),
            demo_blob_2025=app.get("PEP_DEMO_BLOB_2025", "2025_PEP_demo.xlsx"),
            demo_blob_2024=app.get("PEP_DEMO_BLOB_2024", "2024_PEP_demo.xlsx"),
            output_blob_prefix_2026=app.get("PEP_OUTPUT_BLOB_PREFIX_2026", "PEP_2026"),
            archive_blob=app.get("PEP_ARCHIVE_BLOB", "PEP_master.xlsx"),
        )


# ---------------------------------------------------------------------------
# Indexer
# ---------------------------------------------------------------------------

class PEPMasterIndexer:
    """
    Downloads six PEP Excel files from blob storage, merges them, and writes
    the master Excel back to blob storage.
    """

    def __init__(self, cfg: Optional[PEPMasterIndexerConfig] = None):
        self.cfg = cfg or PEPMasterIndexerConfig.from_app_config()
        self._credential: Optional[ChainedTokenCredential] = None
        self._blob_service: Optional[BlobServiceClient] = None
        self._app = get_config()

    # ------------------------------------------------------------------
    # Credential / client helpers
    # ------------------------------------------------------------------

    def _get_credential(self) -> ChainedTokenCredential:
        return ChainedTokenCredential(
            ManagedIdentityCredential(),
            AzureCliCredential(),
        )

    async def _get_blob_service(self) -> BlobServiceClient:
        if self._blob_service is None:
            self._credential = self._get_credential()
            account_url = f"https://{self.cfg.storage_account_name}.blob.core.windows.net"
            self._blob_service = BlobServiceClient(account_url=account_url, credential=self._credential)
        return self._blob_service

    async def _download_bytes(self, container: str, blob_name: str) -> bytes:
        """Download a single blob and return its raw bytes."""
        svc = await self._get_blob_service()
        blob_client = svc.get_blob_client(container=container, blob=blob_name)
        try:
            stream = await blob_client.download_blob()
            data = await stream.readall()
            logging.info(f"[pep-master] Downloaded '{blob_name}' from '{container}' ({len(data):,} bytes)")
            return data
        except ResourceNotFoundError:
            raise FileNotFoundError(
                f"[pep-master] Blob not found: container='{container}' blob='{blob_name}'"
            )

    async def _sp_download_bytes(self, filename: str) -> bytes:
        """Download a single file from SharePoint via Microsoft Graph API (direct path-based download)."""
        import msal
        import requests as _requests
        tenant_id     = self._app.get("PEP_SP_TENANT_ID", "") or self._app.get("AZURE_TENANT_ID", "")
        client_id     = self._app.get("PEP_SP_CLIENT_ID", "")
        client_secret = self._app.get("PEP_SP_CLIENT_SECRET", "")
        cfg = self.cfg

        def _sync_download() -> bytes:
            authority = f"https://login.microsoftonline.com/{tenant_id}"
            msal_app = msal.ConfidentialClientApplication(
                client_id=client_id,
                authority=authority,
                client_credential=client_secret,
            )
            token = msal_app.acquire_token_silent(["https://graph.microsoft.com/.default"], account=None)
            if not token:
                token = msal_app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
            if not token or "access_token" not in token:
                raise RuntimeError(
                    "[pep-master] SharePoint auth failed — check PEP_SP_CLIENT_ID / PEP_SP_CLIENT_SECRET in App Config"
                )
            url = (
                f"https://graph.microsoft.com/v1.0"
                f"/sites/{cfg.sp_site_id}/drives/{cfg.sp_drive_id}"
                f"/root:/{cfg.sp_folder}/{filename}:/content"
            )
            resp = _requests.get(
                url,
                headers={"Authorization": f"Bearer {token['access_token']}"},
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.content
            logging.info(f"[pep-master] SharePoint downloaded '{filename}' ({len(data):,} bytes)")
            return data

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _sync_download)

    async def _sp_list_files(self) -> list:
        """List all files in the configured SharePoint PEP folder.
        Returns list of (filename, lastModifiedDateTime_str) tuples."""
        import msal
        import requests as _requests
        tenant_id     = self._app.get("PEP_SP_TENANT_ID", "") or self._app.get("AZURE_TENANT_ID", "")
        client_id     = self._app.get("PEP_SP_CLIENT_ID", "")
        client_secret = self._app.get("PEP_SP_CLIENT_SECRET", "")
        cfg = self.cfg

        def _sync_list() -> list:
            authority = f"https://login.microsoftonline.com/{tenant_id}"
            msal_app = msal.ConfidentialClientApplication(
                client_id=client_id,
                authority=authority,
                client_credential=client_secret,
            )
            token = msal_app.acquire_token_silent(["https://graph.microsoft.com/.default"], account=None)
            if not token:
                token = msal_app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
            if not token or "access_token" not in token:
                raise RuntimeError(
                    "[pep-master] SharePoint auth failed — check PEP_SP_CLIENT_ID / PEP_SP_CLIENT_SECRET in App Config"
                )
            url = (
                f"https://graph.microsoft.com/v1.0"
                f"/sites/{cfg.sp_site_id}/drives/{cfg.sp_drive_id}"
                f"/root:/{cfg.sp_folder}:/children"
            )
            resp = _requests.get(
                url,
                headers={"Authorization": f"Bearer {token['access_token']}"},
                timeout=60,
            )
            resp.raise_for_status()
            items = resp.json().get("value", [])
            result = [
                (item["name"], item.get("lastModifiedDateTime", ""))
                for item in items
                if "file" in item  # exclude sub-folders
            ]
            logging.info(f"[pep-master] SharePoint listed {len(result)} file(s) in '{cfg.sp_folder}'")
            return result

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _sync_list)

    async def _resolve_pep_2026_file(self) -> str:
        """Dynamically resolve the latest PEP_2026*.xlsx file name.

        - SharePoint source: lists the configured folder via Graph API, filters
          files matching PEP_2026*.xlsx, picks the one with the newest
          lastModifiedDateTime.
        - Blob source: lists blobs in pep_container with prefix 'PEP_2026',
          filters *.xlsx, picks the newest by last_modified.
        - Falls back to cfg.blob_pep if no matching file is found or discovery
          fails for any reason (network error, permissions, etc.).
        """
        cfg = self.cfg
        try:
            if cfg.source == "sharepoint":
                files = await self._sp_list_files()
                candidates = [
                    (name, mod_time)
                    for name, mod_time in files
                    if name.startswith("PEP_2026") and name.lower().endswith(".xlsx")
                ]
                if not candidates:
                    logging.info(
                        f"[pep-master] No PEP_2026*.xlsx found in SharePoint folder '{cfg.sp_folder}'"
                        f" — using fallback '{cfg.blob_pep}'"
                    )
                    return cfg.blob_pep
                # ISO 8601 strings sort correctly lexicographically
                candidates.sort(key=lambda x: x[1], reverse=True)
                latest = candidates[0][0]
                logging.info(
                    f"[pep-master] Resolved PEP 2026 file (SharePoint): '{latest}'"
                    f" (from {len(candidates)} candidate(s))"
                )
                return latest
            else:
                # Blob storage: list blobs whose name starts with 'PEP_2026'
                svc = await self._get_blob_service()
                container_client = svc.get_container_client(cfg.pep_container)
                candidates = []
                async for blob in container_client.list_blobs(name_starts_with="PEP_2026"):
                    if blob.name.lower().endswith(".xlsx"):
                        candidates.append((blob.name, blob.last_modified))
                if not candidates:
                    logging.info(
                        f"[pep-master] No PEP_2026*.xlsx found in container '{cfg.pep_container}'"
                        f" — using fallback '{cfg.blob_pep}'"
                    )
                    return cfg.blob_pep
                # Sort by last_modified (datetime) descending
                candidates.sort(key=lambda x: x[1], reverse=True)
                latest = candidates[0][0]
                logging.info(
                    f"[pep-master] Resolved PEP 2026 file (blob): '{latest}'"
                    f" (from {len(candidates)} candidate(s))"
                )
                return latest
        except Exception as exc:
            logging.warning(
                f"[pep-master] PEP 2026 file discovery failed ({exc})"
                f" — using fallback '{cfg.blob_pep}'"
            )
            return cfg.blob_pep

    async def _upload_bytes(self, container: str, blob_name: str, data: bytes) -> None:
        """Upload bytes to a blob, overwriting if it already exists."""
        svc = await self._get_blob_service()
        blob_client = svc.get_blob_client(container=container, blob=blob_name)
        await blob_client.upload_blob(data, overwrite=True)
        logging.info(f"[pep-master] Uploaded '{blob_name}' to '{container}' ({len(data):,} bytes)")

    async def _delete_blob(self, container: str, blob_name: str) -> None:
        """Delete a blob; logs info if already absent (not fatal)."""
        try:
            svc = await self._get_blob_service()
            blob_client = svc.get_blob_client(container=container, blob=blob_name)
            await blob_client.delete_blob()
            logging.info(f"[pep-master] Deleted blob '{blob_name}' from '{container}'")
        except ResourceNotFoundError:
            logging.info(f"[pep-master] Blob '{blob_name}' already absent in '{container}'")
        except Exception as exc:
            logging.warning(f"[pep-master] Could not delete '{container}/{blob_name}': {exc}")

    # ------------------------------------------------------------------
    # File-level hash helpers  (to revert: swap _upload_if_changed → _upload_bytes in run())
    # ------------------------------------------------------------------
    _HASH_STORE_BLOB = "pep_master_file_hashes.json"
    _HASH_VERSION    = "v2"  # bumped: hash now covers source input bytes, not output Excel bytes

    @staticmethod
    def _hash_bytes(data: bytes) -> str:
        """Return SHA-256 hex digest of *data*."""
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _compute_source_hash(sources: list) -> str:
        """Return a single deterministic SHA-256 from a list of raw source byte blobs.

        Hashes each blob individually, then hashes the pipe-joined hex digests.
        Result only changes when any source file's *content* changes — it is
        time-invariant because blob bytes don't embed timestamps.
        """
        combined = "|".join(hashlib.sha256(b).hexdigest() for b in sources)
        return hashlib.sha256(combined.encode()).hexdigest()

    async def _load_hash_store(self) -> dict:
        """Load the per-file hash store from blob storage.  Returns {} on any error."""
        try:
            svc = await self._get_blob_service()
            blob = svc.get_blob_client(container=self.cfg.pep_container, blob=self._HASH_STORE_BLOB)
            stream = await blob.download_blob()
            raw = await stream.readall()
            store = json.loads(raw)
            if store.get("_version") != self._HASH_VERSION:
                logging.info("[pep-master] Hash store version mismatch — treating all files as changed.")
                return {}
            return store
        except Exception as exc:
            logging.info(f"[pep-master] Hash store not found or unreadable ({exc}) — treating all files as changed.")
            return {}

    async def _save_hash_store(self, store: dict) -> None:
        """Persist the per-file hash store back to blob storage."""
        try:
            svc = await self._get_blob_service()
            blob = svc.get_blob_client(container=self.cfg.pep_container, blob=self._HASH_STORE_BLOB)
            payload = json.dumps(store, indent=2).encode()
            await blob.upload_blob(payload, overwrite=True)
            logging.info(f"[pep-master] Hash store saved to '{self.cfg.pep_container}/{self._HASH_STORE_BLOB}'.")
        except Exception as exc:
            logging.warning(f"[pep-master] Could not save hash store: {exc}")

    async def _upload_if_changed(
        self, container: str, blob_name: str, data: bytes, store: dict
    ) -> bool:
        """
        Upload *data* to *blob_name* only when its SHA-256 digest differs from what
        is recorded in *store*.  Updates *store* in-place on upload.

        Returns True if the upload was performed (data changed or first run),
        False if the file was identical to the previous run (upload skipped).

        To revert to unconditional upload, replace calls to this method with
        _upload_bytes(container, blob_name, data).
        """
        new_hash = self._hash_bytes(data)
        old_hash = store.get(blob_name)
        if old_hash == new_hash:
            logging.info(
                f"[pep-master] '{blob_name}' is unchanged (SHA-256 matches). "
                "Skipping upload — blob indexer will also skip re-indexing."
            )
            return False
        await self._upload_bytes(container, blob_name, data)
        store[blob_name] = new_hash
        return True

    # ------------------------------------------------------------------
    # Download helpers
    # ------------------------------------------------------------------

    async def _read_excel(self, container: str, blob_name: str) -> pd.DataFrame:
        raw = await self._download_bytes(container, blob_name)
        ext = blob_name.rsplit(".", 1)[-1].lower()
        if ext == "csv":
            for enc in ("utf-8-sig", "latin-1"):
                try:
                    df = pd.read_csv(io.BytesIO(raw), encoding=enc, on_bad_lines="skip", low_memory=False)
                    break
                except Exception:
                    pass
            else:
                raise ValueError(f"[pep-master] Could not parse CSV '{blob_name}'")
        else:
            df = pd.read_excel(io.BytesIO(raw), engine="openpyxl")
        df.columns = df.columns.str.strip()
        logging.info(f"[pep-master] Loaded '{blob_name}': {df.shape[0]} rows × {df.shape[1]} cols")
        return df

    # ------------------------------------------------------------------
    # Merge logic  (mirrors merge_to_master.py exactly)
    # ------------------------------------------------------------------

    @staticmethod
    def _build_master(
        pep: pd.DataFrame,
        checkins: pd.DataFrame,
        projects: pd.DataFrame,
        awards: pd.DataFrame,
        addl_mgr: pd.DataFrame,
        appreciations: pd.DataFrame,
    ) -> pd.DataFrame:

        # ── Normalize join keys ───────────────────────────────────────────────
        if "employee_email" in pep.columns:
            pep["employee_email"] = pep["employee_email"].astype(str).str.strip().str.lower()
        if "objective_id" in pep.columns:
            pep["objective_id"] = pep["objective_id"].astype(str).str.strip()

        for df in [checkins, addl_mgr]:
            if "objective_id" in df.columns:
                df["objective_id"] = df["objective_id"].astype(str).str.strip()

        for df in [projects, awards]:
            if "emp_work_email" in df.columns:
                df["emp_work_email"] = df["emp_work_email"].astype(str).str.strip().str.lower()

        if "To" in appreciations.columns:
            appreciations["To"] = appreciations["To"].astype(str).str.strip().str.lower()

        # ── Rename legacy column names to canonical names if present ─────────
        # PEP_2026 already uses canonical names (manager_email, practice_manager_email).
        # These renames are kept as a no-op safety net for any older file formats.
        rename_map = {}
        if "manager_email_microsoft" in pep.columns and "manager_email" not in pep.columns:
            rename_map["manager_email_microsoft"] = "manager_email"
        if "practice_manager_email_id" in pep.columns and "practice_manager_email" not in pep.columns:
            rename_map["practice_manager_email_id"] = "practice_manager_email"
        if rename_map:
            pep = pep.rename(columns=rename_map)

        # ── Retain required PEP fields ────────────────────────────────────────
        KEEP_FROM_PEP = [
            "assessment_year", "employee_id", "employee_email", "emp_name",
            "separation_status", "BU", "competency", "joined_date",
            "manager_id", "manager_email",
            "manager_name", "practice_manager_id", "practice_manager_email",
            "practice_manager_name", "objective_id",
            "objective", "objective_description", "measurement", "self_comments",
            "manager_comments", "practice_manager_comments", "weightage",
            "threshold", "min_threshold", "max_threshold", "self_rating",
            "achievement", "achievement_percentage", "direct_manager_rating",
            "practice_manager_rating", "manager_validated_final_kra_score",
            "category_band", "self_overall_comments", "manager_overall_comments",
            "practice_overall_comments", "promotion_date",
        ]
        pep = pep[[c for c in KEEP_FROM_PEP if c in pep.columns]]

        for blank_col in ["separation_status", "objective_description", "threshold",
                           "min_threshold", "max_threshold"]:
            if blank_col not in pep.columns:
                pep[blank_col] = None

        # ── Aggregations (del each raw DF immediately after aggregating) ──────
        checkins_agg = checkins.groupby("objective_id").agg(
            checkin_count=("id", "count"),
            # cycle identifiers — take last value (most recent checkin)
            checkin_objective_cycle_id=("objective_cycle_id",   "last"),
            checkin_cycle_id=("checkins_cycle_id",              "last"),
            emp_checkins_cycle_id=("emp_checkins_cycle_id",     "last"),
            # self inputs
            checkin_self_comments=("self_comments", lambda x: " | ".join(x.dropna().astype(str))),
            checkin_achievement=("achievement", lambda x: " | ".join(x.dropna().astype(str))),
            # scores
            calculated_goal_score=("calculated_goal_score",     "last"),
            manager_goal_score=("manager_goal_score",           "last"),
            # manager inputs
            mgr_checkin_comments=("mgr_comments", lambda x: " | ".join(x.dropna().astype(str))),
            # status and overall comments
            latest_checkin_status=("status",                    "last"),
            checkin_self_overall_comments=("self_overall_comments", lambda x: " | ".join(x.dropna().astype(str))),
            mgr_checkin_overall_comments=("mgr_overall_comments",  lambda x: " | ".join(x.dropna().astype(str))),
            # last modified
            checkin_last_modified=("modified_at",               "last"),
        ).reset_index()
        del checkins

        addl_mgr_agg = addl_mgr.groupby("objective_id").agg(
            addl_mgr_data_ids=("addln_mgr_data_id",    lambda x: " | ".join(x.dropna().astype(str).unique())),
            addl_mgr_request_ids=("addln_mgr_request_id", lambda x: " | ".join(x.dropna().astype(str).unique())),
            addl_mgr_emails=("addln_mgr_email",        lambda x: " | ".join(x.dropna().astype(str).unique())),
            addl_mgr_comments=("comments",             lambda x: " | ".join(x.dropna().astype(str))),
            addl_mgr_last_updated=("last_updated",     "last"),
        ).reset_index()
        del addl_mgr

        projects_agg = projects.groupby("emp_work_email").agg(
            project_count=("project_code",      "count"),
            project_names=("project_name",      lambda x: " | ".join(x.dropna().astype(str))),
            project_managers=("project_manager", lambda x: " | ".join(x.dropna().astype(str).unique())),
            delivery_managers=("delivery_manager", lambda x: " | ".join(x.dropna().astype(str).unique())),
            billable_roles=("billable_role",    lambda x: " | ".join(x.dropna().astype(str).unique())),
            start_dates=("start_date",          lambda x: " | ".join(x.dropna().astype(str))),
            end_dates=("end_date",              lambda x: " | ".join(x.dropna().astype(str))),
            allocations=("allocation",          lambda x: " | ".join(x.dropna().astype(str))),
        ).reset_index()
        del projects

        awards_agg = awards.groupby("emp_work_email").agg(
            award_count=("awardType",      "count"),
            award_types=("awardType",      lambda x: " | ".join(x.dropna().astype(str).unique())),
            award_categories=("awardCategory", lambda x: " | ".join(x.dropna().astype(str))),
            award_years=("awardYear",      lambda x: " | ".join(x.dropna().astype(str))),
            award_citations=("citation",   lambda x: " | ".join(x.dropna().astype(str))),
        ).reset_index()
        del awards

        appreciations_agg = appreciations.groupby("To").agg(
            appreciation_count=("ID",           "count"),
            appreciation_ids=("ID",             lambda x: " | ".join(x.dropna().astype(str))),
            appreciation_titles=("Title",       lambda x: " | ".join(x.dropna().astype(str))),
            appreciation_messages=("Message",   lambda x: " | ".join(x.dropna().astype(str))),
            appreciation_subcategories=("SubCategory", lambda x: " | ".join(x.dropna().astype(str))),
            appreciation_first_created=("Created",  "first"),
            appreciation_last_modified=("Modified", "last"),
        ).reset_index()
        del appreciations
        gc.collect()  # all raw DFs freed; only compact aggregations remain

        # ── Merge (sequential to free each agg DF as soon as it is consumed) ─
        master = pep.merge(checkins_agg, on="objective_id", how="left")
        pep_nrows = len(pep)
        del pep, checkins_agg
        master = master.merge(addl_mgr_agg, on="objective_id", how="left")
        del addl_mgr_agg
        master = master.merge(projects_agg, left_on="employee_email", right_on="emp_work_email", how="left")
        del projects_agg
        master = master.merge(awards_agg, left_on="employee_email", right_on="emp_work_email", how="left")
        del awards_agg
        master = master.merge(appreciations_agg, left_on="employee_email", right_on="To", how="left")
        del appreciations_agg
        gc.collect()

        # Drop right-side join key duplicates
        cols_to_drop = [c for c in master.columns
                        if c.startswith("emp_work_email") or c.startswith("To")]
        master = master.drop(columns=cols_to_drop)

        # ── Row-count sanity check ────────────────────────────────────────────
        if len(master) != pep_nrows:
            raise ValueError(
                f"[pep-master] Row count changed after merges! "
                f"PEP: {pep_nrows}, Master: {len(master)} — check for duplicate join keys"
            )
        logging.info(f"[pep-master] Row count check passed — {len(master)} rows preserved")

        # ── Manager email consistency diagnostic ──────────────────────────────
        # Log any manager_name that maps to more than one distinct manager_email.
        # This reveals whether mixing originated in the source PEP data or in the merge.
        if "manager_name" in master.columns and "manager_email" in master.columns:
            mgr_email_map = (
                master.dropna(subset=["manager_name", "manager_email"])
                .groupby("manager_name")["manager_email"]
                .nunique()
            )
            mixed = mgr_email_map[mgr_email_map > 1]
            if mixed.empty:
                logging.info("[pep-master] ✅ Manager email consistency check passed — every manager_name maps to exactly one manager_email")
            else:
                for mgr_name, n_emails in mixed.items():
                    emails = master.loc[master["manager_name"] == mgr_name, "manager_email"].unique().tolist()
                    logging.warning(
                        f"[pep-master] ⚠️ manager_name='{mgr_name}' maps to {n_emails} distinct emails: {emails}"
                    )

        # ── Fill numeric nulls ────────────────────────────────────────────────
        for c in ["checkin_count", "project_count", "award_count", "appreciation_count"]:
            if c in master.columns:
                master[c] = master[c].fillna(0).astype(int)

        # ── Escape formula-injection characters ──────────────────────────────
        formula_chars = ("=", "+", "-", "@")
        for col in master.select_dtypes(include="object").columns:
            master[col] = master[col].apply(
                lambda v: "'" + v
                if isinstance(v, str) and v.startswith(formula_chars) else v
            )

        return master

    # ------------------------------------------------------------------
    # Serialise to Excel in-memory
    # ------------------------------------------------------------------

    @staticmethod
    def _to_excel_bytes(df: pd.DataFrame) -> bytes:
        buf = io.BytesIO()
        with pd.ExcelWriter(
            buf,
            engine="xlsxwriter",
            engine_kwargs={"options": {"strings_to_formulas": False}},
        ) as writer:
            df.to_excel(writer, index=False)
        return buf.getvalue()

    # ------------------------------------------------------------------
    # PEP-only master (no supplement merges) — for historical years
    # ------------------------------------------------------------------

    @staticmethod
    def _build_pep_only_master(pep: pd.DataFrame) -> pd.DataFrame:
        """Build a PEP-only master (no checkins/awards/appreciations) for historical years.

        Applies the same column rename and field-filter logic as _build_master so
        the resulting schema is consistent with the canonical PEP section of the
        2026 master.  Extra columns in the source (e.g. 'Legend') are silently
        dropped because they are not in KEEP_FROM_PEP.
        """
        # ── Normalize join key ────────────────────────────────────────────────
        if "employee_email" in pep.columns:
            pep["employee_email"] = pep["employee_email"].astype(str).str.strip().str.lower()
        if "objective_id" in pep.columns:
            pep["objective_id"] = pep["objective_id"].astype(str).str.strip()

        # ── Rename legacy column names to canonical names if present ─────────
        rename_map = {}
        if "manager_email_microsoft" in pep.columns and "manager_email" not in pep.columns:
            rename_map["manager_email_microsoft"] = "manager_email"
        if "practice_manager_email_id" in pep.columns and "practice_manager_email" not in pep.columns:
            rename_map["practice_manager_email_id"] = "practice_manager_email"
        if rename_map:
            pep = pep.rename(columns=rename_map)

        # ── Retain required PEP fields (identical list to _build_master) ─────
        KEEP_FROM_PEP = [
            "assessment_year", "employee_id", "employee_email", "emp_name",
            "separation_status", "BU", "competency", "joined_date",
            "manager_id", "manager_email",
            "manager_name", "practice_manager_id", "practice_manager_email",
            "practice_manager_name", "objective_id",
            "objective", "objective_description", "measurement", "self_comments",
            "manager_comments", "practice_manager_comments", "weightage",
            "threshold", "min_threshold", "max_threshold", "self_rating",
            "achievement", "achievement_percentage", "direct_manager_rating",
            "practice_manager_rating", "manager_validated_final_kra_score",
            "category_band", "self_overall_comments", "manager_overall_comments",
            "practice_overall_comments", "promotion_date",
        ]
        pep = pep[[c for c in KEEP_FROM_PEP if c in pep.columns]]

        for blank_col in ["separation_status", "objective_description", "threshold",
                           "min_threshold", "max_threshold"]:
            if blank_col not in pep.columns:
                pep[blank_col] = None

        # ── Manager email consistency diagnostic ──────────────────────────────
        if "manager_name" in pep.columns and "manager_email" in pep.columns:
            mgr_email_map = (
                pep.dropna(subset=["manager_name", "manager_email"])
                .groupby("manager_name")["manager_email"]
                .nunique()
            )
            mixed = mgr_email_map[mgr_email_map > 1]
            if mixed.empty:
                logging.info("[pep-master] ✅ Manager email consistency check passed")
            else:
                for mgr_name, n_emails in mixed.items():
                    emails = pep.loc[pep["manager_name"] == mgr_name, "manager_email"].unique().tolist()
                    logging.warning(
                        f"[pep-master] ⚠️ manager_name='{mgr_name}' maps to {n_emails} distinct emails: {emails}"
                    )

        # ── Escape formula-injection characters ──────────────────────────────
        formula_chars = ("=", "+", "-", "@")
        for col in pep.select_dtypes(include="object").columns:
            pep[col] = pep[col].apply(
                lambda v: "'" + v
                if isinstance(v, str) and v.startswith(formula_chars) else v
            )

        return pep

    # ------------------------------------------------------------------
    # Field audit / join diagnostics  (logged, not fatal)
    # ------------------------------------------------------------------

    @staticmethod
    def _audit(master: pd.DataFrame, pep: pd.DataFrame,
               projects_agg: pd.DataFrame, awards_agg: pd.DataFrame,
               appreciations_agg: pd.DataFrame) -> None:
        expected_fields = [
            # 36 PEP fields (BU, competency, joined_date added in updated schema)
            "assessment_year", "employee_id", "employee_email", "emp_name",
            "separation_status", "BU", "competency", "joined_date",
            "manager_id", "manager_email", "manager_name",
            "practice_manager_id", "practice_manager_email", "practice_manager_name",
            "objective_id", "objective", "objective_description", "measurement",
            "self_comments", "manager_comments", "practice_manager_comments",
            "weightage", "threshold", "min_threshold", "max_threshold",
            "self_rating", "achievement", "achievement_percentage",
            "direct_manager_rating", "practice_manager_rating",
            "manager_validated_final_kra_score", "category_band",
            "self_overall_comments", "manager_overall_comments",
            "practice_overall_comments", "promotion_date",
            # 13 from checkins (all 14 source fields → 13 aggregated, objective_id is join key)
            "checkin_count", "checkin_objective_cycle_id", "checkin_cycle_id",
            "emp_checkins_cycle_id", "checkin_self_comments", "checkin_achievement",
            "calculated_goal_score", "manager_goal_score", "mgr_checkin_comments",
            "latest_checkin_status", "checkin_self_overall_comments",
            "mgr_checkin_overall_comments", "checkin_last_modified",
            # 5 from addl_mgr (all 6 source fields → 5 aggregated, objective_id is join key)
            "addl_mgr_data_ids", "addl_mgr_request_ids",
            "addl_mgr_emails", "addl_mgr_comments", "addl_mgr_last_updated",
            # 8 from projects
            "project_count", "project_names", "project_managers", "delivery_managers",
            "billable_roles", "start_dates", "end_dates", "allocations",
            # 5 from awards
            "award_count", "award_types", "award_categories", "award_years", "award_citations",
            # 7 from appreciations (all 7 source fields → 7 aggregated, To is join key)
            "appreciation_count", "appreciation_ids", "appreciation_titles",
            "appreciation_messages", "appreciation_subcategories",
            "appreciation_first_created", "appreciation_last_modified",
        ]
        missing = [f for f in expected_fields if f not in master.columns]
        extra   = [f for f in master.columns  if f not in expected_fields]
        logging.info(
            f"[pep-master] Field audit — present: {len(expected_fields) - len(missing)}/{len(expected_fields)}"
            + (f" | missing: {missing}" if missing else "")
            + (f" | extra: {extra}"   if extra   else "")
        )

        pep_emails   = set(pep["employee_email"].dropna()) if "employee_email" in pep.columns else set()
        proj_emails  = set(projects_agg["emp_work_email"].dropna()) if "emp_work_email" in projects_agg.columns else set()
        award_emails = set(awards_agg["emp_work_email"].dropna()) if "emp_work_email" in awards_agg.columns else set()
        appre_emails = set(appreciations_agg["To"].dropna()) if "To" in appreciations_agg.columns else set()

        logging.info(
            f"[pep-master] Join diagnostics — "
            f"PEP emails: {len(pep_emails)} | "
            f"projects matched: {len(pep_emails & proj_emails)}/{len(proj_emails)} | "
            f"awards matched: {len(pep_emails & award_emails)}/{len(award_emails)} | "
            f"appreciations matched: {len(pep_emails & appre_emails)}/{len(appre_emails)}"
        )

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        cfg = self.cfg
        output_container = cfg.output_container or cfg.pep_container

        logging.info("[pep-master] Starting PEP master merge job")

        # ── Load hash store before any downloads ──────────────────────────────
        hash_store = await self._load_hash_store()
        hash_store["_version"] = self._HASH_VERSION

        # ── Resolve the active PEP 2026 file (dynamic discovery) ─────────────
        active_pep_file = await self._resolve_pep_2026_file()
        _prev_pep_file = hash_store.get("_active_pep_2026_file", cfg.blob_pep)
        if active_pep_file != _prev_pep_file:
            logging.info(
                f"[pep-master] PEP 2026 file changed: '{_prev_pep_file}' → '{active_pep_file}'"
            )
        else:
            logging.info(f"[pep-master] PEP 2026 active file: '{active_pep_file}'")

        # ── Source fetch helper — routes to SharePoint or blob ────────────────
        async def _fetch(filename: str) -> bytes:
            if cfg.source == "sharepoint":
                return await self._sp_download_bytes(filename)
            return await self._download_bytes(cfg.pep_container, filename)

        # ── Download all six sheets concurrently ─────────────────────────────
        (
            pep_raw, checkins_raw, projects_raw,
            awards_raw, addl_mgr_raw, appreciations_raw,
        ) = await asyncio.gather(
            _fetch(active_pep_file),  # resolved dynamically; falls back to cfg.blob_pep
            _fetch(cfg.blob_checkins),
            _fetch(cfg.blob_projects),
            _fetch(cfg.blob_awards),
            _fetch(cfg.blob_addl_mgr),
            _fetch(cfg.blob_appreciations),
        )

        # ── Demo data for 2026 (always from pep-data blob, not SharePoint) ────
        demo_raw_2026: Optional[bytes] = None
        if cfg.inject_demo_data:
            try:
                demo_raw_2026 = await self._download_bytes(cfg.pep_container, cfg.demo_blob_2026)
            except FileNotFoundError:
                logging.warning(
                    f"[pep-master] Demo blob '{cfg.demo_blob_2026}' not found — skipping demo injection for 2026"
                )

        # ── Source hash for 2026 master (time-invariant: raw input bytes) ─────
        _source_inputs_2026 = [pep_raw, checkins_raw, projects_raw, awards_raw, addl_mgr_raw, appreciations_raw]
        if demo_raw_2026 is not None:
            _source_inputs_2026.append(demo_raw_2026)
        source_hash_2026 = self._compute_source_hash(_source_inputs_2026)
        # ── Parse into DataFrames (CPU-bound — run in thread pool) ────────────
        loop = asyncio.get_event_loop()

        def _parse_all():
            def _read(raw: bytes, blob_name: str) -> pd.DataFrame:
                ext = blob_name.rsplit(".", 1)[-1].lower()
                if ext == "csv":
                    # Try utf-8-sig first; fall back to latin-1 for non-UTF files.
                    # on_bad_lines='skip' tolerates rows with inconsistent column counts.
                    for enc in ("utf-8-sig", "latin-1"):
                        try:
                            df = pd.read_csv(
                                io.BytesIO(raw),
                                encoding=enc,
                                on_bad_lines="skip",
                                low_memory=False,
                            )
                            break
                        except Exception:
                            pass
                    else:
                        raise ValueError(f"[pep-master] Could not parse CSV '{blob_name}' with any known encoding")
                else:
                    df = pd.read_excel(io.BytesIO(raw), engine="openpyxl")
                df.columns = df.columns.str.strip()
                return df

            pep           = _read(pep_raw,           active_pep_file)  # use resolved filename for extension detection
            checkins      = _read(checkins_raw,       cfg.blob_checkins)
            projects      = _read(projects_raw,       cfg.blob_projects)
            awards        = _read(awards_raw,         cfg.blob_awards)
            addl_mgr      = _read(addl_mgr_raw,       cfg.blob_addl_mgr)
            appreciations = _read(appreciations_raw,  cfg.blob_appreciations)

            # Inject demo rows before merge — demo employees get NULLs for
            # supplementary columns (checkins/projects/awards/appreciations)
            if demo_raw_2026 is not None:
                demo_df = _read(demo_raw_2026, cfg.demo_blob_2026)
                pep = pd.concat([pep, demo_df], ignore_index=True)
                logging.info(
                    f"[pep-master] Injected {len(demo_df)} demo rows into 2026 PEP ({len(pep)} total rows)"
                )

            logging.info(
                f"[pep-master] Loaded — "
                f"PEP:{len(pep)} checkins:{len(checkins)} projects:{len(projects)} "
                f"awards:{len(awards)} addl_mgr:{len(addl_mgr)} appreciations:{len(appreciations)}"
            )
            return pep, checkins, projects, awards, addl_mgr, appreciations

        pep, checkins, projects, awards, addl_mgr, appreciations = await loop.run_in_executor(
            None, _parse_all
        )

        # Free raw bytes — DataFrames are now the only copy of the data
        del pep_raw, checkins_raw, projects_raw, awards_raw, addl_mgr_raw, appreciations_raw
        if demo_raw_2026 is not None:
            del demo_raw_2026
        gc.collect()

        # ── Build small aggregations for audit log ───────────────────────────
        def _agg_for_audit():
            proj_agg  = (projects.groupby("emp_work_email")
                         .size().reset_index(name="_c")) if "emp_work_email" in projects.columns else pd.DataFrame(columns=["emp_work_email"])
            award_agg = (awards.groupby("emp_work_email")
                         .size().reset_index(name="_c")) if "emp_work_email" in awards.columns else pd.DataFrame(columns=["emp_work_email"])
            appr_agg  = (appreciations.groupby("To")
                         .size().reset_index(name="_c")) if "To" in appreciations.columns else pd.DataFrame(columns=["To"])
            return proj_agg, award_agg, appr_agg

        proj_agg_audit, award_agg_audit, appr_agg_audit = await loop.run_in_executor(
            None, _agg_for_audit
        )

        # ── Merge (CPU-bound) ─────────────────────────────────────────────────
        # Pass DataFrames as explicit args (not closure) so we can release the
        # outer references immediately — cuts peak memory roughly in half during
        # the merge because _build_master progressively deletes its own locals.
        _merge_future = loop.run_in_executor(
            None, self._build_master, pep, checkins, projects, awards, addl_mgr, appreciations
        )
        # Release outer refs now; _build_master holds the only remaining refs
        del checkins, projects, awards, addl_mgr, appreciations
        gc.collect()
        master = await _merge_future

        # ── Audit ─────────────────────────────────────────────────────────────
        self._audit(master, pep, proj_agg_audit, award_agg_audit, appr_agg_audit)
        del pep, proj_agg_audit, award_agg_audit, appr_agg_audit
        gc.collect()

        # ── 2026: per-employee granular upload ────────────────────────────────
        prefix = cfg.output_blob_prefix_2026  # e.g. "PEP_2026"
        _stored_source_2026 = hash_store.get("_source_hash_2026")
        hash_store["_active_pep_2026_file"] = active_pep_file  # always track, even when skipping

        if _stored_source_2026 == source_hash_2026:
            logging.info(
                "[pep-master] 2026 source data unchanged — "
                "skipping per-employee upload and archive."
            )
            del master
            gc.collect()
        else:
            # 1. Upload reference archive to pep-data (human reference, not indexed)
            archive_bytes = await loop.run_in_executor(None, lambda: self._to_excel_bytes(master))
            await self._upload_bytes(cfg.pep_container, cfg.archive_blob, archive_bytes)
            del archive_bytes
            logging.info(
                f"[pep-master] Reference archive uploaded to "
                f"'{cfg.pep_container}/{cfg.archive_blob}'"
            )

            # 2. Migrate: delete old single-master blob from documents on first run
            if cfg.output_blob in hash_store:
                await self._delete_blob(output_container, cfg.output_blob)
                hash_store.pop(cfg.output_blob, None)

            # 3. Delete blobs for employees explicitly marked separation_status = 2
            sep2_mask = master["separation_status"].astype(str).str.strip() == "2"
            sep2_ids = {
                str(eid).strip().replace("/", "_").replace("\\", "_")
                for eid in master.loc[sep2_mask, "employee_id"].dropna()
                if str(eid).strip() and str(eid).strip() != "nan"
            }
            for sep_id in sep2_ids:
                await self._delete_blob(output_container, f"{prefix}/{sep_id}.xlsx")
                hash_store.pop(f"{prefix}/{sep_id}", None)
            if sep2_ids:
                logging.info(f"[pep-master] Removed {len(sep2_ids)} separated (status=2) employee blob(s)")

            # Filter out separation_status=2 employees from master before per-employee upload
            master = master.loc[~sep2_mask].reset_index(drop=True)
            sep2_mask = None  # release reference

            # 4. Compute per-employee hashes; serialize only changed employees to Excel
            def _compute_and_serialize():
                if "employee_id" not in master.columns:
                    raise ValueError("[pep-master] 'employee_id' column not found in master")
                all_ids, serialized = [], []
                for emp_id, group in master.groupby("employee_id", sort=False):
                    emp_id_str = str(emp_id).strip()
                    if not emp_id_str or emp_id_str == "nan":
                        continue
                    safe_id = emp_id_str.replace("/", "_").replace("\\", "_")
                    row_hash = hashlib.sha256(
                        group.to_json(orient="records", date_format="iso").encode()
                    ).hexdigest()
                    all_ids.append(emp_id_str)
                    if hash_store.get(f"{prefix}/{safe_id}") != row_hash:
                        serialized.append((safe_id, row_hash, self._to_excel_bytes(group)))
                return all_ids, serialized

            all_employees, serialized_employees = await loop.run_in_executor(
                None, _compute_and_serialize
            )
            del master
            gc.collect()

            total_count = len(all_employees)
            changed_count = len(serialized_employees)
            skipped_count = total_count - changed_count
            logging.info(
                f"[pep-master] Per-employee diff — "
                f"changed: {changed_count}, unchanged: {skipped_count}"
            )

            # Upload in concurrent batches of 20 to avoid overwhelming storage
            _UPLOAD_BATCH = 20
            for i in range(0, len(serialized_employees), _UPLOAD_BATCH):
                batch = serialized_employees[i : i + _UPLOAD_BATCH]
                await asyncio.gather(*[
                    self._upload_bytes(output_container, f"{prefix}/{safe_id}.xlsx", excel)
                    for safe_id, _, excel in batch
                ])
                for safe_id, row_hash, _ in batch:
                    hash_store[f"{prefix}/{safe_id}"] = row_hash

            del serialized_employees
            gc.collect()

            hash_store["_source_hash_2026"] = source_hash_2026
            logging.info(
                f"[pep-master] 2026 per-employee upload complete — "
                f"uploaded: {changed_count}, skipped: {skipped_count}, "
                f"separated removed: {len(sep2_ids)}"
            )

        # ── Historical PEP-only masters (2025, 2024) ─────────────────────────
        # Each is optional: if the source blob is absent we log and skip.
        for year, blob_name, output_blob in [
            ("2025", cfg.blob_pep_2025, cfg.output_blob_2025),
            ("2024", cfg.blob_pep_2024, cfg.output_blob_2024),
        ]:
            try:
                pep_hist_raw = await _fetch(blob_name)
            except FileNotFoundError:
                logging.info(f"[pep-master] {year} PEP blob '{blob_name}' not found — skipping")
                continue

            # Demo data for this historical year (always from pep-data blob, not SharePoint)
            demo_blob_hist = cfg.demo_blob_2025 if year == "2025" else cfg.demo_blob_2024
            demo_raw_hist: Optional[bytes] = None
            if cfg.inject_demo_data:
                try:
                    demo_raw_hist = await self._download_bytes(cfg.pep_container, demo_blob_hist)
                except FileNotFoundError:
                    logging.warning(
                        f"[pep-master] Demo blob '{demo_blob_hist}' not found — skipping demo injection for {year}"
                    )

            # Source hash — include demo bytes when present
            _source_hash_hist = (
                self._compute_source_hash([pep_hist_raw, demo_raw_hist])
                if demo_raw_hist is not None
                else self._hash_bytes(pep_hist_raw)
            )
            _stored_hist = hash_store.get(output_blob)
            if isinstance(_stored_hist, dict) and _stored_hist.get("source_hash") == _source_hash_hist:
                logging.info(
                    f"[pep-master] '{output_blob}' source data unchanged — "
                    "skipping build and upload."
                )
                del pep_hist_raw
                if demo_raw_hist is not None:
                    del demo_raw_hist
                continue

            def _build_hist(raw=pep_hist_raw, bname=blob_name, yr=year, demo_raw=demo_raw_hist):
                ext = bname.rsplit(".", 1)[-1].lower()
                if ext == "csv":
                    for enc in ("utf-8-sig", "latin-1"):
                        try:
                            df = pd.read_csv(io.BytesIO(raw), encoding=enc, on_bad_lines="skip", low_memory=False)
                            break
                        except Exception:
                            pass
                    else:
                        raise ValueError(f"[pep-master] Could not parse CSV '{bname}'")
                else:
                    df = pd.read_excel(io.BytesIO(raw), engine="openpyxl")
                df.columns = df.columns.str.strip()
                logging.info(f"[pep-master] {yr} PEP loaded: {df.shape[0]} rows × {df.shape[1]} cols")
                raw = None  # free raw bytes
                gc.collect()
                if demo_raw is not None:
                    demo_df = pd.read_excel(io.BytesIO(demo_raw), engine="openpyxl")
                    demo_df.columns = demo_df.columns.str.strip()
                    df = pd.concat([df, demo_df], ignore_index=True)
                    logging.info(
                        f"[pep-master] Injected {len(demo_df)} demo rows into {yr} PEP ({len(df)} total rows)"
                    )
                return self._build_pep_only_master(df)

            hist_master = await loop.run_in_executor(None, _build_hist)
            del pep_hist_raw  # no longer needed after parse
            if demo_raw_hist is not None:
                del demo_raw_hist
            hist_bytes = await loop.run_in_executor(
                None, lambda df=hist_master: self._to_excel_bytes(df)
            )
            del hist_master
            await self._upload_bytes(output_container, output_blob, hist_bytes)
            hash_store[output_blob] = {"source_hash": _source_hash_hist}
            logging.info(
                f"[pep-master] {year} master saved to "
                f"container='{output_container}' blob='{output_blob}'"
            )
            del hist_bytes
            gc.collect()

        # ── Persist updated hash store ────────────────────────────────────────
        await self._save_hash_store(hash_store)

        # ── Close credential / blob service ──────────────────────────────────
        if self._blob_service:
            await self._blob_service.close()
        if self._credential:
            await self._credential.close()
