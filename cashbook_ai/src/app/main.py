"""Cashbook AI - Local Web Application Server.

Bound strictly to localhost (127.0.0.1:8000).
Integrates all backend components: CanonicalRegistry, Image Ingestion, ColumnAligner,
HybridMappingEngine, LocalLLMAssistant, ReviewManager, Excel Exporter, and ApprovedWorkbookValidator.
"""

import hashlib
import json
import logging
import os
import re
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image

from src.models import ProcessedRow, TransactionType, RecordType
from src.reference.canonical_registry import CanonicalRegistry
from src.extraction.column_aligner import ColumnAligner, AlignedRow
from src.mapping.hybrid_engine import HybridMappingEngine, MappingStatus, MappingMethod
from src.mapping.local_llm_assistant import LocalLLMAssistant
from src.mapping.review_manager import ReviewManager, ReviewAction, ReviewItem, ReviewGroup
from src.export.manual_review_exporter import ManualReviewExporter
from src.validation.approved_workbook_validator import ApprovedWorkbookValidator, PreflightSummary
from src.orchestration.job_store import JobStore, JobRecord, JobStatus, StageStatus
from src.orchestration.processing_pipeline import ProcessingPipeline
from src.automation.execution_manager import ExecutionManager, ExecutionLockedError

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("cashbook_ai.app")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

# Application singletons
registry = CanonicalRegistry()
job_store = JobStore(DATA_DIR / "jobs")
pipeline = ProcessingPipeline(registry=registry, job_store=job_store, data_dir=DATA_DIR)
approved_validator = ApprovedWorkbookValidator(registry=registry)
review_manager = ReviewManager(registry=registry)
manual_exporter = ManualReviewExporter()
execution_manager = ExecutionManager(data_dir=DATA_DIR, registry=registry)
executor = ThreadPoolExecutor(max_workers=2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Cashbook AI Local Web Application...")
    logger.info(f"Canonical Registry loaded: {len(registry.heads)} heads, {len(registry.subheads)} subheads")
    yield
    logger.info("Shutting down Cashbook AI Local Web Application...")
    executor.shutdown(wait=False)


app = FastAPI(
    title="Cashbook AI",
    description="Unified Local Accounting Assistant & Intelligent Review Interface",
    version="1.0.0",
    lifespan=lifespan,
)

# Strict localhost-only CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _sanitize_filename(name: str) -> str:
    cleaned = Path(name).name
    return re.sub(r"[^a-zA-Z0-9_\-\.]", "_", cleaned)


def _validate_month_format(month: str) -> str:
    if not re.match(r"^\d{4}-\d{2}$", month):
        raise HTTPException(status_code=400, detail="Invalid month format. Expected YYYY-MM (e.g. 2026-05).")
    return month


def _check_relay_connectivity() -> str:
    """Check if the local browser relay server is reachable on port 11800."""
    try:
        req = urllib.request.Request("http://127.0.0.1:11800/json/version", headers={"User-Agent": "CashbookAI"})
        with urllib.request.urlopen(req, timeout=0.8) as resp:
            if resp.status == 200:
                return "ONLINE"
    except Exception:
        pass
    return "STANDBY"


# =========================================================================
# System & Status Endpoints
# =========================================================================

@app.get("/api/status")
def get_system_status():
    """Return application health, active accounting period, hardware, and service status."""
    mem_info = LocalLLMAssistant.get_memory_snapshot()
    model_avail = LocalLLMAssistant.is_available()

    # Determine default active month
    months = _get_all_months()
    active_m = "2026-05" if "2026-05" in months else (months[0] if months else "2026-05")

    return {
        "status": "ONLINE",
        "host": "127.0.0.1:8000",
        "active_month": active_m,
        "available_months": months,
        "browser_relay_status": _check_relay_connectivity(),
        "local_llm_model": {
            "name": "Qwen3.5-4B-Instruct-Q4_K_M",
            "available": model_avail,
            "backend": "llama.cpp (CUDA 12.4)",
        },
        "system_resources": mem_info,
        "canonical_registry": {
            "heads_count": len(registry.heads),
            "subheads_count": len(registry.subheads),
            "aliases_count": len(registry.aliases),
        },
    }


def _get_all_months() -> List[str]:
    found = set()
    for sub in ["monthly", "output", "completed", "archive"]:
        d = DATA_DIR / sub
        if d.is_dir():
            for p in d.iterdir():
                if p.is_dir() and re.match(r"^\d{4}-\d{2}$", p.name):
                    found.add(p.name)
    return sorted(list(found), reverse=True)


@app.get("/api/months")
def list_months():
    """List all available accounting periods with operational status."""
    months = _get_all_months()
    result = []
    for m in months:
        is_completed = (DATA_DIR / "completed" / m / f"{m}_state.json").is_file()
        is_archived = (DATA_DIR / "archive" / m).is_dir()
        has_output = (DATA_DIR / "output" / m).is_dir()

        status_str = "IN_PROGRESS"
        if is_archived:
            status_str = "ARCHIVED"
        elif is_completed:
            status_str = "COMPLETED"
        elif has_output:
            status_str = "PROCESSING_READY"

        result.append({
            "month": m,
            "status": status_str,
            "is_completed": is_completed,
            "is_archived": is_archived,
        })
    return {"months": result}


@app.post("/api/months")
def create_month(month: str = Form(...)):
    """Initialize a new monthly workspace directory partition."""
    _validate_month_format(month)
    monthly_dir = DATA_DIR / "monthly" / month
    output_dir = DATA_DIR / "output" / month

    monthly_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest_file = monthly_dir / "pages_manifest.json"
    if not manifest_file.is_file():
        manifest_file.write_text(json.dumps([], indent=2), encoding="utf-8")

    return {"message": f"Initialized workspace for {month}", "month": month}


# =========================================================================
# Image Upload & Page Ordering Endpoints
# =========================================================================

class PageManifestItem(BaseModel):
    page_number: str
    original_filename: str
    stored_filename: str
    sha256: str
    path: str
    rotation_degrees: int = 0
    file_size_bytes: int = 0
    is_duplicate: bool = False


@app.post("/api/upload")
async def upload_cashbook_images(
    month: str = Form(...),
    files: List[UploadFile] = File(...),
):
    """Multi-image upload with non-destructive preservation, corruption checks, and duplicate detection."""
    _validate_month_format(month)
    month_dir = DATA_DIR / "monthly" / month
    month_dir.mkdir(parents=True, exist_ok=True)

    manifest_file = month_dir / "pages_manifest.json"
    existing_manifest: List[Dict[str, Any]] = []
    if manifest_file.is_file():
        try:
            existing_manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except Exception:
            existing_manifest = []

    seen_hashes = {item.get("sha256") for item in existing_manifest if item.get("sha256")}
    new_pages = []

    for file in files:
        safe_name = _sanitize_filename(file.filename or "upload.jpg")
        ext = Path(safe_name).suffix.lower()
        if ext not in [".jpg", ".jpeg", ".png"]:
            raise HTTPException(status_code=400, detail=f"Unsupported file type '{ext}' for {safe_name}. Use JPG or PNG.")

        content = await file.read()
        if len(content) == 0:
            raise HTTPException(status_code=400, detail=f"Empty file uploaded: {safe_name}")

        # Compute SHA-256
        sha256_hash = hashlib.sha256(content).hexdigest()
        is_dup = sha256_hash in seen_hashes

        # Save to month directory non-destructively
        dest_path = month_dir / safe_name
        if dest_path.is_file():
            # If same name exists, check if content identical
            existing_sha = hashlib.sha256(dest_path.read_bytes()).hexdigest()
            if existing_sha != sha256_hash:
                # Add timestamp suffix to preserve both without overwriting
                ts = datetime.now(timezone.utc).strftime("%H%M%S")
                dest_path = month_dir / f"{dest_path.stem}_{ts}{dest_path.suffix}"

        dest_path.write_bytes(content)

        # Validate image integrity with PIL
        try:
            with Image.open(dest_path) as img:
                img.verify()
        except Exception as e:
            if dest_path.is_file():
                dest_path.unlink()
            raise HTTPException(status_code=400, detail=f"Corrupted or invalid image file {safe_name}: {str(e)}")

        # Assign logical page number
        assigned_page_num = f"{len(existing_manifest) + len(new_pages) + 1:03d}"

        page_record = {
            "page_number": assigned_page_num,
            "original_filename": file.filename or safe_name,
            "stored_filename": dest_path.name,
            "sha256": sha256_hash,
            "path": str(dest_path.resolve()),
            "rotation_degrees": 0,
            "file_size_bytes": len(content),
            "is_duplicate": is_dup,
        }
        new_pages.append(page_record)
        seen_hashes.add(sha256_hash)

    updated_manifest = existing_manifest + new_pages
    manifest_file.write_text(json.dumps(updated_manifest, indent=2), encoding="utf-8")

    return {
        "uploaded_count": len(new_pages),
        "total_pages": len(updated_manifest),
        "pages": updated_manifest,
    }


@app.get("/api/pages")
def get_pages(month: str):
    """Retrieve ordered cashbook pages for the specified accounting month."""
    _validate_month_format(month)
    month_dir = DATA_DIR / "monthly" / month
    manifest_file = month_dir / "pages_manifest.json"

    if not manifest_file.is_file():
        # Auto-discover any existing images in month_dir if manifest doesn't exist
        discovered = []
        if month_dir.is_dir():
            img_files = sorted(
                [f for f in month_dir.iterdir() if f.is_file() and f.suffix.lower() in [".jpg", ".jpeg", ".png"]],
                key=lambda x: x.name
            )
            for idx, f in enumerate(img_files):
                sha = hashlib.sha256(f.read_bytes()).hexdigest()
                discovered.append({
                    "page_number": f"{idx + 1:03d}",
                    "original_filename": f.name,
                    "stored_filename": f.name,
                    "sha256": sha,
                    "path": str(f.resolve()),
                    "rotation_degrees": 0,
                    "file_size_bytes": f.stat().st_size,
                    "is_duplicate": False,
                })
            manifest_file.write_text(json.dumps(discovered, indent=2), encoding="utf-8")
        return {"month": month, "pages": discovered}

    try:
        pages = json.loads(manifest_file.read_text(encoding="utf-8"))
        return {"month": month, "pages": pages}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read page manifest: {str(e)}")


class ReorderRequest(BaseModel):
    month: str
    pages: List[Dict[str, Any]]


@app.post("/api/pages/reorder")
def reorder_pages(req: ReorderRequest):
    """Update logical page ordering and rotation non-destructively."""
    _validate_month_format(req.month)
    month_dir = DATA_DIR / "monthly" / req.month
    manifest_file = month_dir / "pages_manifest.json"

    # Re-assign continuous logical numbers: 001, 002, 003...
    reordered = []
    for idx, p in enumerate(req.pages):
        p["page_number"] = f"{idx + 1:03d}"
        reordered.append(p)

    manifest_file.write_text(json.dumps(reordered, indent=2), encoding="utf-8")
    return {"message": "Pages reordered successfully", "pages": reordered}


@app.get("/api/pages/image/{month}/{filename}")
def serve_page_image(month: str, filename: str):
    """Safely serve page image for browser preview."""
    _validate_month_format(month)
    safe_name = _sanitize_filename(filename)
    month_dir = (DATA_DIR / "monthly" / month).resolve()
    target_file = (month_dir / safe_name).resolve()

    # Prevent path traversal
    if not str(target_file).startswith(str(month_dir)) or not target_file.is_file():
        raise HTTPException(status_code=404, detail="Image not found")

    media_type = "image/jpeg"
    if safe_name.lower().endswith(".png"):
        media_type = "image/png"

    return FileResponse(target_file, media_type=media_type)


# =========================================================================
# Processing Orchestration Endpoints
# =========================================================================

class ProcessRequest(BaseModel):
    month: str
    use_llm: bool = True
    force_ocr: bool = False


@app.post("/api/process")
def start_processing(req: ProcessRequest, background_tasks: BackgroundTasks):
    """Trigger background end-to-end processing pipeline."""
    _validate_month_format(req.month)

    # Enforce single active job constraint
    active_job = job_store.get_active_job(month=req.month)
    if active_job:
        raise HTTPException(
            status_code=409,
            detail=f"An active processing job ({active_job.job_id}) is already RUNNING for {req.month}. Please wait or cancel it."
        )

    # Load pages from manifest
    month_dir = DATA_DIR / "monthly" / req.month
    manifest_file = month_dir / "pages_manifest.json"
    if not manifest_file.is_file():
        # Trigger page discovery
        get_pages(req.month)

    pages = json.loads(manifest_file.read_text(encoding="utf-8"))
    if not pages:
        raise HTTPException(status_code=400, detail=f"No cashbook images found for {req.month}. Please upload images first.")

    # Create job in store
    job_record = job_store.create_job(month=req.month)

    # Run in background executor
    def _execute():
        try:
            pipeline.run_pipeline(
                month=req.month,
                job_id=job_record.job_id,
                page_manifest=pages,
                use_llm=req.use_llm,
                force_ocr=req.force_ocr,
            )
        except Exception as e:
            logger.error(f"Job {job_record.job_id} failed in background: {e}")

    background_tasks.add_task(_execute)

    return {
        "message": f"Processing started for {req.month}",
        "job_id": job_record.job_id,
        "status": job_record.status.value,
        "stages": [s.model_dump() for s in job_record.stages],
    }


@app.get("/api/jobs")
def list_jobs(month: Optional[str] = None):
    """List recent orchestration jobs."""
    jobs = job_store.list_jobs(month=month)
    return {"jobs": [j.model_dump() for j in jobs]}


@app.get("/api/jobs/{job_id}")
def get_job_status(job_id: str):
    """Retrieve live stage status, real counts, and diagnostics for a job."""
    job = job_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job.model_dump()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    """Request safe cancellation of an active processing job."""
    success = job_store.request_cancellation(job_id)
    if not success:
        raise HTTPException(status_code=400, detail=f"Job {job_id} cannot be cancelled (not RUNNING or PENDING).")
    return {"message": f"Cancellation requested for {job_id}", "status": "CANCELLED"}


# =========================================================================
# Intelligent Review Interface Endpoints
# =========================================================================

@app.get("/api/review")
def get_review_state(month: str):
    """Return clustered review groups, individual review items, and status statistics."""
    _validate_month_format(month)
    state_file = DATA_DIR / "output" / month / "review_state.json"
    if not state_file.is_file():
        return {
            "month": month,
            "has_data": False,
            "items": [],
            "groups": [],
            "stats": {"total": 0, "verified": 0, "suggested": 0, "provisional": 0, "unresolved": 0},
        }

    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        items = data.get("items", [])
        groups = data.get("groups", [])

        # Ensure multi-item clusters are available for review intelligence
        if not any(g.get("transaction_count", 0) > 1 for g in groups) and items:
            all_items = [ReviewItem(**it) for it in items]
            full_groups = review_manager.group_similar_transactions(all_items, only_needing_review=False)
            multi_groups = [g for g in full_groups if g.transaction_count > 1]
            if multi_groups:
                groups = [g.model_dump() for g in multi_groups]

        stats = {
            "total": len(items),
            "verified": sum(1 for it in items if it.get("mapping_status") == "VERIFIED"),
            "suggested": sum(1 for it in items if it.get("mapping_status") == "SUGGESTED"),
            "provisional": sum(1 for it in items if it.get("mapping_status") == "PROVISIONAL"),
            "unresolved": sum(1 for it in items if it.get("mapping_status") == "UNRESOLVED"),
        }
        return {
            "month": month,
            "has_data": True,
            "updated_at": data.get("updated_at"),
            "stats": stats,
            "groups": groups,
            "items": items,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load review state: {str(e)}")


@app.get("/api/review/heads")
def get_canonical_heads_and_subheads():
    """Return canonical accounting heads and subheads for manual review dropdowns."""
    heads = [
        {
            "id": h.canonical_head_id,
            "name": h.canonical_name,
            "account_set": h.account_set if isinstance(h.account_set, str) else getattr(h.account_set, "value", str(h.account_set)),
            "direction": h.primary_direction,
            "policy": h.subhead_policy.value if hasattr(h.subhead_policy, "value") else str(h.subhead_policy),
            "allowed_subheads": [s.canonical_subhead_id for s in registry.get_subheads_for_head(h.canonical_head_id)],
        }
        for h in sorted(registry.heads.values(), key=lambda x: x.canonical_name)
        if h.is_active
    ]
    subheads = [
        {
            "id": s.canonical_subhead_id,
            "name": s.canonical_name,
            "parent_head_id": s.parent_head_id,
        }
        for s in sorted(registry.subheads.values(), key=lambda x: x.canonical_name)
        if s.is_active
    ]
    return {"heads": heads, "subheads": subheads}


class EditTransactionRequest(BaseModel):
    month: str
    date: str
    voucher_number: str
    amount: float
    canonical_head_id: str
    canonical_subhead_id: Optional[str] = None
    narration: str
    direction: str
    editor: str = "Accountant"


@app.post("/api/review/{transaction_id}/edit")
def edit_transaction(transaction_id: str, req: EditTransactionRequest):
    """Apply human edits to transaction fields with strict canonical validation.
    
    Does NOT auto-approve the transaction; keeps it awaiting human confirmation.
    If an existing approved preflight summary exists for the month, it is invalidated.
    """
    _validate_month_format(req.month)
    state_file = DATA_DIR / "output" / req.month / "review_state.json"
    if not state_file.is_file():
        raise HTTPException(status_code=404, detail="Review state file not found for this month")

    data = json.loads(state_file.read_text(encoding="utf-8"))
    items = [ReviewItem(**it) for it in data.get("items", [])]

    target_item = None
    for it in items:
        if it.transaction_id == transaction_id:
            target_item = it
            break

    if not target_item:
        raise HTTPException(status_code=404, detail=f"Transaction {transaction_id} not found in review queue")

    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        review_manager.apply_edit(
            item=target_item,
            date=req.date,
            voucher_number=req.voucher_number,
            amount=req.amount,
            canonical_head_id=req.canonical_head_id,
            canonical_subhead_id=req.canonical_subhead_id,
            narration=req.narration,
            direction=req.direction,
            editor=req.editor,
            timestamp=now_iso,
        )
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))

    # Invalidate existing preflight summary if present because draft data changed
    staging_dir = DATA_DIR / "output" / req.month / "intake"
    preflight_report_file = staging_dir / f"{req.month}_preflight_summary.json"
    if preflight_report_file.is_file():
        try:
            summary_data = json.loads(preflight_report_file.read_text(encoding="utf-8"))
            summary_data["is_valid"] = False
            summary_data["status"] = "INVALIDATED_BY_DRAFT_EDIT"
            preflight_report_file.write_text(json.dumps(summary_data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to invalidate preflight summary: {e}")

    # Re-group
    groups = review_manager.group_similar_transactions(items)

    # Save updated review state
    data["updated_at"] = now_iso
    data["items"] = [it.model_dump() for it in items]
    data["groups"] = [g.model_dump() for g in groups]
    state_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    return {
        "message": f"Saved edits for {transaction_id}",
        "transaction_id": transaction_id,
        "item": target_item.model_dump(),
    }


class ReviewDecisionRequest(BaseModel):
    month: str
    action: ReviewAction
    confirmed_head_id: Optional[str] = None
    confirmed_subhead_id: Optional[str] = None
    notes: str = ""
    promote_to_alias: bool = False
    reviewer: str = "Accountant"


@app.post("/api/review/{transaction_id}/decision")
def apply_review_decision(transaction_id: str, req: ReviewDecisionRequest):
    """Apply human review decision (Confirm, Override, Reject, Defer) to a transaction."""
    _validate_month_format(req.month)
    state_file = DATA_DIR / "output" / req.month / "review_state.json"
    if not state_file.is_file():
        raise HTTPException(status_code=404, detail="Review state file not found for this month")

    data = json.loads(state_file.read_text(encoding="utf-8"))
    items = [ReviewItem(**it) for it in data.get("items", [])]

    target_item = None
    for it in items:
        if it.transaction_id == transaction_id:
            target_item = it
            break

    if not target_item:
        raise HTTPException(status_code=404, detail=f"Transaction {transaction_id} not found in review queue")

    now_iso = datetime.now(timezone.utc).isoformat()
    review_manager.apply_user_decision(
        item=target_item,
        action=req.action,
        reviewed_by=req.reviewer,
        timestamp=now_iso,
        confirmed_head_id=req.confirmed_head_id,
        confirmed_subhead_id=req.confirmed_subhead_id,
        notes=req.notes,
        promote_to_alias=req.promote_to_alias,
    )

    # Re-group
    groups = review_manager.group_similar_transactions(items)

    # Save updated review state
    data["updated_at"] = now_iso
    data["items"] = [it.model_dump() for it in items]
    data["groups"] = [g.model_dump() for g in groups]
    state_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    return {
        "message": f"Applied {req.action.value} to {transaction_id}",
        "transaction_id": transaction_id,
        "status": target_item.mapping_status.value,
        "item": target_item.model_dump(),
    }


class GroupDecisionRequest(BaseModel):
    month: str
    group_key: str
    action: ReviewAction
    confirmed_head_id: Optional[str] = None
    confirmed_subhead_id: Optional[str] = None
    notes: str = ""
    promote_to_alias: bool = False
    reviewer: str = "Accountant"


@app.post("/api/review/group/decision")
def apply_group_decision(req: GroupDecisionRequest):
    """Batch apply human review decision to all transactions in a clustered group."""
    _validate_month_format(req.month)
    state_file = DATA_DIR / "output" / req.month / "review_state.json"
    if not state_file.is_file():
        raise HTTPException(status_code=404, detail="Review state file not found for this month")

    data = json.loads(state_file.read_text(encoding="utf-8"))
    items = [ReviewItem(**it) for it in data.get("items", [])]

    now_iso = datetime.now(timezone.utc).isoformat()
    affected_count = 0

    for it in items:
        # Check if transaction matches group key
        item_key = it.canonical_head_id or (it.raw_narration or "").strip().lower()
        if item_key == req.group_key or it.raw_narration.strip().lower() == req.group_key.lower():
            review_manager.apply_user_decision(
                item=it,
                action=req.action,
                reviewed_by=req.reviewer,
                timestamp=now_iso,
                confirmed_head_id=req.confirmed_head_id,
                confirmed_subhead_id=req.confirmed_subhead_id,
                notes=req.notes,
                promote_to_alias=req.promote_to_alias,
            )
            affected_count += 1

    # Re-group
    groups = review_manager.group_similar_transactions(items)

    data["updated_at"] = now_iso
    data["items"] = [it.model_dump() for it in items]
    data["groups"] = [g.model_dump() for g in groups]
    state_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    return {
        "message": f"Batch applied {req.action.value} to {affected_count} transactions in group '{req.group_key}'",
        "affected_count": affected_count,
    }


# =========================================================================
# Draft Excel Export & Intake Endpoints
# =========================================================================

@app.post("/api/export/draft")
def refresh_draft_workbook(month: str = Form(...)):
    """Re-generate draft review Excel workbook reflecting current review decisions."""
    _validate_month_format(month)
    state_file = DATA_DIR / "output" / month / "review_state.json"
    if not state_file.is_file():
        raise HTTPException(status_code=404, detail=f"No review state found for {month}. Run processing first.")

    data = json.loads(state_file.read_text(encoding="utf-8"))
    items = [ReviewItem(**it) for it in data.get("items", [])]

    processed_rows = []
    for it in items:
        t_type = TransactionType.RECEIPT if it.direction == "RECEIPT" else TransactionType.PAYMENT
        rec_type = RecordType.TRANSACTION
        if "opening balance" in (it.raw_narration or "").lower():
            rec_type = RecordType.OPENING_BALANCE
        elif "closing balance" in (it.raw_narration or "").lower():
            rec_type = RecordType.CLOSING_BALANCE

        p_row = ProcessedRow(
            source_image=it.source_image,
            source_page=it.source_page,
            source_row=it.source_row,
            raw_date=None,
            resolved_date=None,
            raw_head_number=it.raw_head_number,
            accounting_head_name=it.canonical_head_name,
            subhead=it.canonical_subhead_name,
            raw_voucher_number=it.raw_voucher_number,
            voucher_number=it.raw_voucher_number,
            raw_narration=it.raw_narration,
            narration_english=it.english_narration,
            raw_amount=str(it.amount) if it.amount is not None else None,
            receipt_amount=it.amount if t_type == TransactionType.RECEIPT else None,
            payment_amount=it.amount if t_type == TransactionType.PAYMENT else None,
            transaction_type=t_type,
            record_type=rec_type,
            overall_confidence=it.confidence_score,
            review_required=it.needs_human_review,
        )
        processed_rows.append(p_row)

    draft_path = DATA_DIR / "output" / month / f"{month}_draft_review.xlsx"
    try:
        manual_exporter.export(processed_rows, draft_path)
    except PermissionError:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        draft_path = DATA_DIR / "output" / month / f"{month}_draft_review_{ts}.xlsx"
        manual_exporter.export(processed_rows, draft_path)

    return {
        "message": f"Successfully generated draft review workbook for {month}",
        "filename": draft_path.name,
        "rows_count": len(processed_rows),
    }


@app.get("/api/export/draft")
def download_draft_workbook(month: str):
    """Download the generated draft review Excel workbook."""
    _validate_month_format(month)
    draft_path = (DATA_DIR / "output" / month / f"{month}_draft_review.xlsx").resolve()
    if not draft_path.is_file():
        # If not existing, try generating it
        refresh_draft_workbook(month=month)

    if not draft_path.is_file():
        raise HTTPException(status_code=404, detail="Draft workbook not found. Please run processing first.")

    return FileResponse(
        path=draft_path,
        filename=draft_path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/api/upload-approved")
async def upload_approved_workbook(
    month: str = Form(...),
    file: UploadFile = File(...),
):
    """Upload human-approved Excel workbook, validate structure, canonical rules, and emit preflight report."""
    _validate_month_format(month)
    safe_name = _sanitize_filename(file.filename or "approved.xlsx")
    if not safe_name.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Invalid file type. Approved workbook must be an .xlsx file.")

    content = await file.read()
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    # Save to staging area
    staging_dir = DATA_DIR / "output" / month / "intake"
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged_path = staging_dir / safe_name
    staged_path.write_bytes(content)

    # Run comprehensive preflight validation and save to canonical approved_snapshots directory
    snapshot_dir = DATA_DIR / "output" / month / "approved_snapshots"
    summary: PreflightSummary = approved_validator.validate_workbook(
        workbook_path=staged_path,
        expected_month=month,
        snapshot_dir=snapshot_dir,
    )

    # Persist summary report
    preflight_report_file = staging_dir / f"{month}_preflight_summary.json"
    preflight_report_file.write_text(json.dumps(summary.model_dump(), indent=2), encoding="utf-8")

    return summary.model_dump()


@app.get("/api/preflight-summary")
def get_preflight_summary(month: str):
    """Retrieve existing preflight summary for the month if available."""
    _validate_month_format(month)
    staging_dir = DATA_DIR / "output" / month / "intake"
    preflight_report_file = staging_dir / f"{month}_preflight_summary.json"
    if preflight_report_file.is_file():
        try:
            return json.loads(preflight_report_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    raise HTTPException(status_code=404, detail="No preflight summary found for this month.")


# =========================================================================
# Milestone 7: Autonomous e-Dayan Execution Endpoints
# =========================================================================

class BatchAuthorizeRequest(BaseModel):
    month: str
    workbook_sha256: Optional[str] = None
    file_hash_sha256: Optional[str] = None
    authorizer: str = "Accountant"
    authorizer_role: Optional[str] = None
    opening_balance: Optional[float] = None


@app.post("/api/execution/authorize")
def authorize_execution_batch(req: BatchAuthorizeRequest):
    """Explicitly authorize a verified approved workbook batch."""
    _validate_month_format(req.month)
    sha = req.workbook_sha256 or req.file_hash_sha256
    if not sha:
        raise HTTPException(status_code=400, detail="Missing workbook SHA-256 hash.")
    authorizer_name = req.authorizer_role or req.authorizer
    try:
        auth_record = execution_manager.authorize_batch(
            month=req.month,
            workbook_sha256=sha,
            authorizer=authorizer_name,
            opening_balance=req.opening_balance,
        )
        return auth_record
    except Exception as e:
        logger.error(f"Failed to authorize batch: {e}")
        raise HTTPException(status_code=400, detail=str(e))


class ExecuteBatchRequest(BaseModel):
    month: str
    arm: bool = False
    save_timeout_seconds: float = 90.0
    live_closing_balance: Optional[float] = None


@app.post("/api/execution/start")
def start_execution_batch(req: ExecuteBatchRequest, background_tasks: BackgroundTasks):
    """Trigger autonomous live or dry-run execution under process lock."""
    _validate_month_format(req.month)
    status_info = execution_manager.get_status(req.month)
    if status_info.get("is_locked"):
        raise HTTPException(
            status_code=409,
            detail=f"Execution lock is already held by runner process (PID: {status_info.get('lock_info', {}).get('pid')})"
        )

    if not status_info.get("is_authorized"):
        raise HTTPException(
            status_code=400,
            detail="Batch authorization is required before starting execution. Click 'AUTHORIZE BATCH' first."
        )

    def _run():
        try:
            if req.arm:
                client = RelayBrowserClient(relay_url="ws://127.0.0.1:11800")
                adapter = RelayBrowserEntryAdapter(client=client)
            else:
                from automation.edayan_entry import MockBrowserEntryAdapter
                adapter = MockBrowserEntryAdapter()

            execution_manager.execute_batch(
                month=req.month,
                adapter=adapter,
                save_timeout_seconds=req.save_timeout_seconds,
                live_closing_balance=req.live_closing_balance,
            )
        except Exception as e:
            logger.error(f"Execution batch failed: {e}", exc_info=True)

    background_tasks.add_task(_run)
    return {"message": f"Execution initiated for {req.month}", "status": "RUNNING", "armed": req.arm}


@app.get("/api/execution/status")
def get_execution_telemetry(month: str):
    """Return live execution status, lock info, and journal summary."""
    _validate_month_format(month)
    return execution_manager.get_status(month)


@app.post("/api/execution/pause")
def pause_execution():
    """Request safe pause of active batch execution."""
    execution_manager.pause()
    return {"message": "Execution paused", "status": "PAUSED"}


@app.post("/api/execution/resume")
def resume_execution():
    """Resume paused batch execution."""
    execution_manager.resume()
    return {"message": "Execution resumed", "status": "RUNNING"}


@app.post("/api/execution/cancel")
def cancel_execution():
    """Request safe cancellation of active batch execution."""
    execution_manager.cancel()
    return {"message": "Execution cancellation requested", "status": "CANCELLED"}


@app.get("/api/execution/reconciliation")
def get_reconciliation_report(month: str):
    """Return final 9-point reconciliation report for the month."""
    _validate_month_format(month)
    recon_file = DATA_DIR / "output" / month / "final_reconciliation.json"
    if not recon_file.is_file():
        raise HTTPException(status_code=404, detail="Reconciliation report not found. Run execution first.")
    try:
        return json.loads(recon_file.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read reconciliation: {e}")


# =========================================================================
# Diagnostics & Static UI Serving
# =========================================================================

@app.get("/api/logs")
def get_recent_logs(lines: int = 50):
    """Return recent application log records for technical inspection."""
    # Read from task log if available or return recent events
    return {"message": "Application active and logging to standard output", "timestamp": datetime.now(timezone.utc).isoformat()}


# Serve static web frontend
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
