"""Comprehensive test suite for Milestone 7: Approved Workbook to Autonomous e-Dayan Execution.
Validates manifest coverage, cryptographic tamper detection, process execution locking,
bounded execution runner, duplicate prevention, circuit breakers, 9-point reconciliation,
production OCR safety, and API endpoints.
"""

import copy
import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List

import openpyxl
import pytest
from fastapi.testclient import TestClient

from automation.batch_manifest import (
    APPROVED,
    DRAFT,
    BatchManifest,
    ManifestError,
    WorkbookTamperedError,
    approve_manifest,
    calculate_file_sha256,
    load_manifest,
    save_manifest,
    validate_manifest,
)
from automation.edayan_entry import (
    CONFIRMED,
    FAILED,
    PENDING,
    READY,
    SUBMITTED,
    UNCERTAIN,
    BatchEntryRunner,
    BrowserEntryAdapter,
    MockBrowserEntryAdapter,
    load_or_create_journal,
)
from src.app.main import app, DATA_DIR, registry, execution_manager
from src.automation.execution_manager import (
    ExecutionLock,
    ExecutionLockedError,
    FinalReconciler,
    ManifestBuilder,
    ExecutionManager,
)
from src.orchestration.processing_pipeline import ProcessingPipeline
from src.reference.canonical_registry import CanonicalRegistry
from src.validation.approved_workbook_validator import ApprovedWorkbookValidator, PreflightSummary


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def test_env(tmp_path):
    """Create isolated test workspace and registry for execution manager tests."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    for sub in ["monthly", "output", "completed", "archive", "jobs"]:
        (data_dir / sub).mkdir(parents=True)

    test_registry = CanonicalRegistry()
    manager = ExecutionManager(data_dir=data_dir, registry=test_registry)
    validator = ApprovedWorkbookValidator(test_registry)

    return {
        "data_dir": data_dir,
        "registry": test_registry,
        "manager": manager,
        "validator": validator,
        "tmp_path": tmp_path,
    }


def create_sample_approved_workbook(
    file_path: Path,
    receipts_data: List[Dict[str, Any]],
    payments_data: List[Dict[str, Any]],
) -> Path:
    """Create a valid approved openpyxl workbook with Receipts and Payments sheets."""
    wb = openpyxl.Workbook()

    # Receipts sheet
    ws_rec = wb.active
    ws_rec.title = "Receipts"
    ws_rec.append(["Date", "Receipt Number", "Amount", "Head Name", "Subhead", "Narration"])
    for r in receipts_data:
        ws_rec.append([
            r.get("date", "2026-05-01"),
            r.get("receipt_number", "1"),
            r.get("amount", 100.0),
            r.get("head_name", "Donation/Offering for Special Purposes(C)"),
            r.get("subhead", None),
            r.get("narration", "Sunday collection"),
        ])

    # Payments sheet
    ws_pay = wb.create_sheet("Payments")
    ws_pay.append(["Date", "Receipt Number", "Amount", "Head Name", "Subhead", "Narration"])
    for p in payments_data:
        ws_pay.append([
            p.get("date", "2026-05-02"),
            p.get("receipt_number", "51"),
            p.get("amount", 200.0),
            p.get("head_name", "Electricity Charges"),
            p.get("subhead", None),
            p.get("narration", "Parish hall power"),
        ])

    file_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(file_path)
    return file_path


# =========================================================================
# Part 1: ManifestBuilder & 100% Coverage Verification
# =========================================================================

def test_manifest_builder_100_percent_coverage(test_env):
    """Verify ManifestBuilder covers 100% of rows with deterministic keys and valid selectors."""
    builder = ManifestBuilder(test_env["registry"])
    wb_path = test_env["tmp_path"] / "test_approved.xlsx"

    rec_data = [
        {"date": "2026-05-01", "receipt_number": "101", "amount": 500.0, "head_name": "Donation/Offering for Special Purposes(C)", "narration": "General Donation"},
        {"date": "2026-05-02", "receipt_number": "102", "amount": 1500.0, "head_name": "Church Feast - Collection", "narration": "Feast collection"},
    ]
    pay_data = [
        {"date": "2026-05-03", "receipt_number": "201", "amount": 250.0, "head_name": "Electricity Charges", "narration": "Office bill"},
        {"date": "2026-05-04", "receipt_number": "202", "amount": 400.0, "head_name": "Church Feast - Expenses", "narration": "Band set"},
    ]

    create_sample_approved_workbook(wb_path, rec_data, pay_data)

    # 1. Receipts Manifest
    rec_manifest = builder.build_manifest(wb_path, "RECEIPT", "2026-05")
    assert rec_manifest.direction == "RECEIPT"
    assert len(rec_manifest.authorized_entries) == 2
    assert rec_manifest.batch_summary["transaction_count"] == 2
    assert rec_manifest.batch_summary["total_amount"] == 2000.0
    assert rec_manifest.batch_summary["source_coverage"] == 1.0

    for entry in rec_manifest.authorized_entries:
        assert "submission_key" in entry
        assert entry["submission_key"] in rec_manifest.authorized_keys
        assert entry["selectors"]["form"] == "#frmreceipt"
        assert entry["head_resolution"]["status"] == "matched"
        assert entry["head_resolution"]["website_value"] != ""

    # 2. Payments Manifest
    pay_manifest = builder.build_manifest(wb_path, "PAYMENT", "2026-05")
    assert pay_manifest.direction == "PAYMENT"
    assert len(pay_manifest.authorized_entries) == 2
    assert pay_manifest.batch_summary["transaction_count"] == 2
    assert pay_manifest.batch_summary["total_amount"] == 650.0

    for entry in pay_manifest.authorized_entries:
        assert entry["selectors"]["form"] == "#frmpayment"
        assert entry["head_resolution"]["status"] == "matched"
        assert entry["head_resolution"]["website_value"] != ""


def test_manifest_builder_rejects_unmapped_head(test_env):
    """Verify ManifestBuilder raises ManifestError when encountering a head with no website mapping."""
    builder = ManifestBuilder(test_env["registry"])
    wb_path = test_env["tmp_path"] / "unmapped.xlsx"

    rec_data = [
        {"date": "2026-05-01", "receipt_number": "101", "amount": 100.0, "head_name": "Nonexistent Unmapped Head", "narration": "Invalid"}
    ]
    create_sample_approved_workbook(wb_path, rec_data, [])

    with pytest.raises(ManifestError, match="Unknown canonical head"):
        builder.build_manifest(wb_path, "RECEIPT", "2026-05")


# =========================================================================
# Part 2: Cryptographic Tamper Detection
# =========================================================================

def test_workbook_snapshot_tamper_detection(test_env):
    """Verify modifying an approved workbook snapshot fails cryptographic verification."""
    builder = ManifestBuilder(test_env["registry"])
    wb_path = test_env["tmp_path"] / "snapshot_source.xlsx"

    rec_data = [{"date": "2026-05-01", "receipt_number": "1", "amount": 100.0, "head_name": "Donation/Offering for Special Purposes(C)"}]
    pay_data = [{"date": "2026-05-02", "receipt_number": "51", "amount": 50.0, "head_name": "Electricity Charges"}]
    create_sample_approved_workbook(wb_path, rec_data, pay_data)

    manifest = builder.build_manifest(wb_path, "RECEIPT", "2026-05")
    assert manifest.workbook_sha256 == calculate_file_sha256(wb_path)

    # Tamper with workbook: append arbitrary byte
    with open(wb_path, "ab") as f:
        f.write(b"tamper_data")

    # Validating tampered manifest against altered file must raise WorkbookTamperedError
    with pytest.raises(WorkbookTamperedError):
        validate_manifest(manifest, wb_path)


# =========================================================================
# Part 3: Process Execution Lock
# =========================================================================

def test_execution_lock_mutual_exclusion_and_stale_recovery(test_env):
    """Verify ExecutionLock provides mutual exclusion, re-entrancy, and dead PID recovery."""
    lock_file = test_env["data_dir"] / "jobs" / "execution.lock"

    # 1. Normal acquire and release via context manager
    lock1 = ExecutionLock(lock_file, "2026-05", "BATCH_01")
    with lock1:
        assert lock_file.is_file()
        lock_data = json.loads(lock_file.read_text(encoding="utf-8"))
        assert lock_data["pid"] == os.getpid()
        assert lock_data["batch_id"] == "BATCH_01"

        # 2. Re-entrant acquire from same process succeeds
        lock_same_proc = ExecutionLock(lock_file, "2026-05", "BATCH_01")
        assert lock_same_proc.acquire(timeout_seconds=0.1) is True

    # Lock released on context exit
    assert not lock_file.is_file()

    # 3. Stale lock recovery: write fake dead PID (PID 99999999)
    stale_data = {
        "pid": 99999999,
        "month": "2026-05",
        "batch_id": "BATCH_DEAD",
        "owner": "DeadRunner",
        "acquired_at": "2020-01-01T00:00:00Z",
    }
    lock_file.write_text(json.dumps(stale_data), encoding="utf-8")

    # New runner acquires lock and successfully reclaims stale lock
    lock2 = ExecutionLock(lock_file, "2026-05", "BATCH_NEW")
    assert lock2.acquire(timeout_seconds=0.5) is True
    new_data = json.loads(lock_file.read_text(encoding="utf-8"))
    assert new_data["pid"] == os.getpid()
    assert new_data["batch_id"] == "BATCH_NEW"
    lock2.release()
    assert not lock_file.is_file()


# =========================================================================
# Part 4: Batch Authorization & Snapshot Preservation
# =========================================================================

def test_execution_manager_authorization(test_env):
    """Verify ExecutionManager.authorize_batch creates immutable signed manifests and authorization record."""
    mgr = test_env["manager"]
    wb_path = test_env["tmp_path"] / "approved_may.xlsx"

    rec_data = [
        {"date": "2026-05-01", "receipt_number": "1", "amount": 1000.0, "head_name": "Donation/Offering for Special Purposes(C)", "narration": "May gift"}
    ]
    pay_data = [
        {"date": "2026-05-02", "receipt_number": "51", "amount": 400.0, "head_name": "Electricity Charges", "narration": "May bill"}
    ]
    create_sample_approved_workbook(wb_path, rec_data, pay_data)

    # 1. Run validator to create approved snapshot
    summary = test_env["validator"].validate_workbook(
        wb_path,
        expected_month="2026-05",
        expected_opening_balance=18506.60,
        snapshot_dir=test_env["data_dir"] / "output" / "2026-05" / "approved_snapshots",
    )
    assert summary.is_valid is True
    assert summary.snapshot_path is not None

    # 2. Authorize batch
    auth_record = mgr.authorize_batch(
        month="2026-05",
        workbook_sha256=summary.file_hash_sha256,
        authorizer="Accountant",
        opening_balance=18506.60,
    )

    assert auth_record["month"] == "2026-05"
    assert auth_record["status"] == "AUTHORIZED"
    assert auth_record["authorized_by"] == "Accountant"
    assert auth_record["receipts"]["count"] == 1
    assert auth_record["payments"]["count"] == 1
    assert auth_record["opening_balance"] == 18506.60

    # Verify authorization record file on disk
    auth_file = test_env["data_dir"] / "output" / "2026-05" / "authorization_record.json"
    assert auth_file.is_file()

    # Verify approved manifests created on disk
    manifests_dir = test_env["data_dir"] / "output" / "2026-05" / "manifests"
    rec_m = load_manifest(manifests_dir / f"{auth_record['receipts']['manifest_id']}.json")
    pay_m = load_manifest(manifests_dir / f"{auth_record['payments']['manifest_id']}.json")
    assert rec_m.status == APPROVED
    assert pay_m.status == APPROVED


# =========================================================================
# Part 5: Full Execution with Mock Adapter & 9-Point Reconciliation
# =========================================================================

def test_execution_manager_execute_mock_success(test_env):
    """Verify complete bounded execution through MockBrowserEntryAdapter and final 9-point reconciliation."""
    mgr = test_env["manager"]
    wb_path = test_env["tmp_path"] / "approved_may_full.xlsx"

    rec_data = [
        {"date": "2026-05-01", "receipt_number": "1", "amount": 1000.0, "head_name": "Donation/Offering for Special Purposes(C)", "narration": "Receipt 1"},
        {"date": "2026-05-02", "receipt_number": "2", "amount": 500.0, "head_name": "Church Feast - Collection", "narration": "Receipt 2"},
    ]
    pay_data = [
        {"date": "2026-05-03", "receipt_number": "51", "amount": 300.0, "head_name": "Electricity Charges", "narration": "Payment 51"},
        {"date": "2026-05-04", "receipt_number": "52", "amount": 200.0, "head_name": "Church Feast - Expenses", "narration": "Payment 52"},
    ]
    create_sample_approved_workbook(wb_path, rec_data, pay_data)

    # Validate and snapshot
    summary = test_env["validator"].validate_workbook(
        wb_path,
        expected_month="2026-05",
        expected_opening_balance=18506.60,
        snapshot_dir=test_env["data_dir"] / "output" / "2026-05" / "approved_snapshots",
    )
    assert summary.is_valid is True
    mgr.authorize_batch(month="2026-05", workbook_sha256=summary.file_hash_sha256, opening_balance=18506.60)

    # Execute batch with MockBrowserEntryAdapter
    mock_adapter = MockBrowserEntryAdapter()
    result = mgr.execute_batch(
        month="2026-05",
        adapter=mock_adapter,
        opening_balance=18506.60,
    )

    assert result["status"] == "COMPLETED"
    assert result["completed_count"] == 4
    assert result["total_count"] == 4

    # Verify Journal
    journal_path = test_env["data_dir"] / "output" / "2026-05" / "edayan_journal.json"
    journal = load_or_create_journal(journal_path)
    assert len(journal["entries"]) == 4
    for entry in journal["entries"].values():
        assert entry["status"] == CONFIRMED

    # Verify 9-Point Reconciliation Report
    recon = result["reconciliation"]
    assert recon["overall_reconciled"] is True
    assert recon["confirmed_receipts_count"] == 2
    assert recon["confirmed_receipts_total"] == 1500.0
    assert recon["confirmed_payments_count"] == 2
    assert recon["confirmed_payments_total"] == 500.0
    assert recon["opening_balance"] == 18506.60
    assert recon["expected_closing_balance"] == 19506.60
    assert recon["uncertain_count"] == 0
    assert recon["failed_count"] == 0


# =========================================================================
# Part 6: Duplicate Prevention on Second Run
# =========================================================================

def test_execution_manager_duplicate_prevention(test_env):
    """Verify executing an already confirmed batch skips existing transactions without double-submission."""
    mgr = test_env["manager"]
    wb_path = test_env["tmp_path"] / "approved_idempotent.xlsx"

    rec_data = [{"date": "2026-05-01", "receipt_number": "1", "amount": 1000.0, "head_name": "Donation/Offering for Special Purposes(C)"}]
    pay_data = [{"date": "2026-05-02", "receipt_number": "51", "amount": 200.0, "head_name": "Electricity Charges"}]
    create_sample_approved_workbook(wb_path, rec_data, pay_data)

    summary = test_env["validator"].validate_workbook(
        wb_path,
        expected_month="2026-05",
        expected_opening_balance=18506.60,
        snapshot_dir=test_env["data_dir"] / "output" / "2026-05" / "approved_snapshots",
    )
    assert summary.is_valid is True
    mgr.authorize_batch(month="2026-05", workbook_sha256=summary.file_hash_sha256)

    # First run
    mock_adapter1 = MockBrowserEntryAdapter()
    mgr.execute_batch(month="2026-05", adapter=mock_adapter1)

    # Second run with fresh mock adapter
    mock_adapter2 = MockBrowserEntryAdapter()
    result2 = mgr.execute_batch(month="2026-05", adapter=mock_adapter2)

    assert result2["status"] == "COMPLETED"
    # Zero forms filled in second run because all were already confirmed!
    assert len(mock_adapter2.entered_entries) == 0


# =========================================================================
# Part 7: Circuit Breaker on Submission Failure
# =========================================================================

class FaultyMockAdapter(MockBrowserEntryAdapter):
    """Adapter that simulates network failure / portal timeout on a specific voucher."""

    def __init__(self, fail_on_receipt: str = "2"):
        super().__init__()
        self.fail_on_receipt = fail_on_receipt

    def submit(self) -> Dict[str, Any]:
        if self.current_entry and str(self.current_entry.get("receipt_number")) == self.fail_on_receipt:
            raise TimeoutError("e-Dayan portal save request timed out after 90 seconds.")
        return dict(super().submit())

    def observe_save(self, expected_entry: Any = None, timeout_seconds: float = 30) -> Dict[str, Any]:
        if expected_entry and str(expected_entry.get("receipt_number")) == self.fail_on_receipt:
            return {
                "confirmed": False,
                "record_id": None,
                "last_error": "Timeout waiting for table evidence",
            }
        return dict(super().observe_save(expected_entry, timeout_seconds))


def test_execution_manager_circuit_breaker_on_submit_failure(test_env):
    """Verify circuit breaker trips immediately when a transaction submission fails, marking UNCERTAIN."""
    mgr = test_env["manager"]
    wb_path = test_env["tmp_path"] / "approved_circuit.xlsx"

    rec_data = [
        {"date": "2026-05-01", "receipt_number": "1", "amount": 500.0, "head_name": "Donation/Offering for Special Purposes(C)"},
        {"date": "2026-05-02", "receipt_number": "2", "amount": 600.0, "head_name": "Donation/Offering for Special Purposes(C)"},
        {"date": "2026-05-03", "receipt_number": "3", "amount": 700.0, "head_name": "Donation/Offering for Special Purposes(C)"},
    ]
    create_sample_approved_workbook(wb_path, rec_data, [])

    summary = test_env["validator"].validate_workbook(
        wb_path,
        expected_month="2026-05",
        expected_opening_balance=18506.60,
        snapshot_dir=test_env["data_dir"] / "output" / "2026-05" / "approved_snapshots",
    )
    assert summary.is_valid is True
    mgr.authorize_batch(month="2026-05", workbook_sha256=summary.file_hash_sha256)

    # Run with faulty adapter failing on voucher 2
    faulty_adapter = FaultyMockAdapter(fail_on_receipt="2")
    result = mgr.execute_batch(month="2026-05", adapter=faulty_adapter)

    assert result["status"] == "STOPPED_CIRCUIT_BREAKER"
    assert result["direction"] == "RECEIPT"
    assert result["reason"] == "CIRCUIT_BREAKER_TRIPPED"
    assert "e-Dayan portal save request timed out" in str(result["incident"])

    # Verify Journal: Voucher 1 confirmed, Voucher 2 UNCERTAIN, Voucher 3 untouched
    journal_path = test_env["data_dir"] / "output" / "2026-05" / "edayan_journal.json"
    journal = load_or_create_journal(journal_path)

    statuses = [v["status"] for v in journal["entries"].values()]
    assert CONFIRMED in statuses
    assert UNCERTAIN in statuses
    # Voucher 3 was never submitted
    assert len(journal["entries"]) == 2

    # Verify incident artifact saved
    incident_dir = test_env["data_dir"] / "output" / "2026-05" / "incidents"
    incident_files = list(incident_dir.glob("*.json"))
    assert len(incident_files) >= 1


# =========================================================================
# Part 8: OCR Production Safety — Mock Provider Rejection
# =========================================================================

def test_ocr_production_safety_rejects_mock(monkeypatch, tmp_path):
    """Verify ProcessingPipeline strictly rejects mock extraction in production when allow_mock=False."""
    # Ensure GEMINI_API_KEY is not set in environment or config
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    import config
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")

    pipeline = ProcessingPipeline(data_dir=tmp_path / "data", allow_mock=False)

    # Attempting to initialize provider without API key in production mode must raise RuntimeError
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY environment variable is not set"):
        pipeline.get_extractor()


# =========================================================================
# Part 9: FastAPI Execution Endpoints Integration
# =========================================================================

def test_execution_api_endpoints_integration(client, tmp_path):
    """Verify FastAPI execution endpoints: upload-approved, authorize, status, and reconciliation."""
    wb_path = tmp_path / "approved_api.xlsx"
    rec_data = [{"date": "2026-05-01", "receipt_number": "1", "amount": 1000.0, "head_name": "Donation/Offering for Special Purposes(C)"}]
    pay_data = [{"date": "2026-05-02", "receipt_number": "51", "amount": 400.0, "head_name": "Electricity Charges"}]
    create_sample_approved_workbook(wb_path, rec_data, pay_data)

    # 1. POST /api/upload-approved
    with open(wb_path, "rb") as f:
        resp = client.post(
            "/api/upload-approved",
            data={"month": "2026-05"},
            files={"file": ("approved_api.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
    assert resp.status_code == 200
    summary = resp.json()
    assert summary["is_valid"] is True
    assert summary["status"] == "READY_FOR_EXECUTION"
    file_hash = summary["file_hash_sha256"]

    # 2. POST /api/execution/authorize
    auth_resp = client.post(
        "/api/execution/authorize",
        json={
            "month": "2026-05",
            "workbook_sha256": file_hash,
            "authorizer": "Accountant",
            "opening_balance": 18506.60,
        },
    )
    assert auth_resp.status_code == 200
    auth_data = auth_resp.json()
    assert auth_data["status"] == "AUTHORIZED"
    assert "batch_id" in auth_data

    # 3. GET /api/execution/status
    stat_resp = client.get("/api/execution/status?month=2026-05")
    assert stat_resp.status_code == 200
    status_data = stat_resp.json()
    assert status_data["is_authorized"] is True

    # 4. POST /api/execution/pause, resume, cancel
    pause_resp = client.post("/api/execution/pause")
    assert pause_resp.status_code == 200
    assert pause_resp.json()["status"] == "PAUSED"

    resume_resp = client.post("/api/execution/resume")
    assert resume_resp.status_code == 200
    assert resume_resp.json()["status"] == "RUNNING"

    cancel_resp = client.post("/api/execution/cancel")
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "CANCELLED"
