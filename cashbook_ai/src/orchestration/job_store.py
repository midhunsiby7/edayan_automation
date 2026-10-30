"""Persistent file-backed job store for Cashbook AI orchestration."""

import json
import logging
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StageStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class PipelineStage(BaseModel):
    id: str
    name: str
    status: StageStatus = StageStatus.PENDING
    progress_count: int = 0
    total_count: int = 0
    details: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error_message: Optional[str] = None


class JobRecord(BaseModel):
    job_id: str
    month: str
    status: JobStatus = JobStatus.PENDING
    current_stage: str = "source_ingestion"
    stages: List[PipelineStage] = Field(default_factory=list)
    created_at: str
    updated_at: str
    completed_at: Optional[str] = None
    error_message: Optional[str] = None
    result_summary: Dict[str, Any] = Field(default_factory=dict)
    cancel_requested: bool = False


STANDARD_STAGES = [
    ("source_ingestion", "Source ingestion"),
    ("page_validation", "Page validation"),
    ("ocr_extraction", "OCR extraction"),
    ("column_alignment", "Column alignment"),
    ("accounting_mapping", "Accounting mapping"),
    ("subhead_validation", "Subhead validation"),
    ("llm_assistance", "LLM assistance"),
    ("review_preparation", "Review preparation"),
    ("draft_workbook_generation", "Draft workbook generation"),
]


class JobStore:
    """Manages persistent job records on disk at `data/jobs/{job_id}.json`."""

    def __init__(self, jobs_dir: Optional[Path] = None):
        if jobs_dir is None:
            self.jobs_dir = Path(__file__).resolve().parent.parent.parent / "data" / "jobs"
        else:
            self.jobs_dir = Path(jobs_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._recover_stale_jobs()

    def _recover_stale_jobs(self):
        """On startup, any job left as RUNNING from a previous crash/kill is marked FAILED."""
        for job_file in self.jobs_dir.glob("job_*.json"):
            try:
                data = json.loads(job_file.read_text(encoding="utf-8"))
                if data.get("status") == JobStatus.RUNNING.value:
                    data["status"] = JobStatus.FAILED.value
                    data["error_message"] = "Process was interrupted by application restart."
                    data["updated_at"] = datetime.now(timezone.utc).isoformat()
                    # Atomically update
                    temp_file = job_file.with_suffix(".tmp")
                    temp_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
                    temp_file.replace(job_file)
            except Exception as e:
                logger.warning(f"Failed to check/recover stale job {job_file.name}: {e}")

    def create_job(self, month: str) -> JobRecord:
        """Create a new job record for the specified accounting month."""
        now_iso = datetime.now(timezone.utc).isoformat()
        clean_month = month.replace("-", "")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        job_id = f"job_{clean_month}_{timestamp}"

        stages = [
            PipelineStage(id=sid, name=sname, status=StageStatus.PENDING)
            for sid, sname in STANDARD_STAGES
        ]

        record = JobRecord(
            job_id=job_id,
            month=month,
            status=JobStatus.PENDING,
            current_stage="source_ingestion",
            stages=stages,
            created_at=now_iso,
            updated_at=now_iso,
        )
        self.save_job(record)
        return record

    def get_job(self, job_id: str) -> Optional[JobRecord]:
        """Load a job record from disk."""
        job_file = self.jobs_dir / f"{job_id}.json"
        if not job_file.is_file():
            return None
        try:
            data = json.loads(job_file.read_text(encoding="utf-8"))
            return JobRecord(**data)
        except Exception as e:
            logger.error(f"Error loading job {job_id}: {e}")
            return None

    def save_job(self, record: JobRecord):
        """Atomically save a job record to disk."""
        record.updated_at = datetime.now(timezone.utc).isoformat()
        job_file = self.jobs_dir / f"{record.job_id}.json"
        temp_file = self.jobs_dir / f"{record.job_id}.tmp"
        temp_file.write_text(json.dumps(record.model_dump(), indent=2), encoding="utf-8")
        temp_file.replace(job_file)

    def list_jobs(self, month: Optional[str] = None, limit: int = 20) -> List[JobRecord]:
        """List recent jobs, optionally filtered by month, newest first."""
        records: List[JobRecord] = []
        files = sorted(self.jobs_dir.glob("job_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                rec = JobRecord(**data)
                if month is None or rec.month == month:
                    records.append(rec)
                    if len(records) >= limit:
                        break
            except Exception:
                continue
        return records

    def get_active_job(self, month: Optional[str] = None) -> Optional[JobRecord]:
        """Return the currently RUNNING job if any exists."""
        for rec in self.list_jobs(month=month, limit=10):
            if rec.status == JobStatus.RUNNING:
                return rec
        return None

    def request_cancellation(self, job_id: str) -> bool:
        """Mark a job as cancellation requested."""
        rec = self.get_job(job_id)
        if rec and rec.status in (JobStatus.PENDING, JobStatus.RUNNING):
            rec.cancel_requested = True
            rec.status = JobStatus.CANCELLED
            rec.error_message = "Cancelled by user."
            rec.completed_at = datetime.now(timezone.utc).isoformat()
            self.save_job(rec)
            return True
        return False
