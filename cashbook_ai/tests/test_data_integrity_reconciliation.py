"""Tests for Cashbook AI Data Integrity, Balance Reconciliation, and Human Verification."""

import json
from decimal import Decimal
from pathlib import Path
import openpyxl
import pytest
from fastapi.testclient import TestClient

from src.app.main import app, DATA_DIR
from src.models import RawExtractedRow
from src.extraction.column_aligner import AlignedRow
from src.reference.canonical_registry import CanonicalRegistry, SubheadPolicy
from src.mapping.hybrid_engine import MappingResult, MappingStatus
from src.mapping.review_manager import (
    ReviewManager,
    ReviewAction,
    ReviewItem,
    HumanVerificationStatus,
)
from src.validation.approved_workbook_validator import (
    ApprovedWorkbookValidator,
    ValidationCategoryStatus,
)


@pytest.fixture
def test_client():
    return TestClient(app)


@pytest.fixture
def validator():
    registry = CanonicalRegistry()
    return ApprovedWorkbookValidator(registry=registry)


def create_balanced_workbook(
    file_path: Path,
    receipts_amt: float = 516867.0,
    payments_amt: float = 475438.0,
    omit_date: bool = False,
    omit_voucher: bool = False,
    invalid_head: bool = False,
    missing_subhead: bool = False,
):
    """Create a test Excel workbook with Receipts and Payments sheets."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    ws_rec = wb.create_sheet("Receipts")
    ws_rec.append(["Date", "Receipt Number", "Amount", "Head Name", "Subhead", "Narration"])
    r_date = "" if omit_date else "2026-05-01"
    r_vouch = "" if omit_voucher else "501"
    r_head = "Unknown Invalid Head" if invalid_head else "Donation/Offering for Special Purposes(C)"
    ws_rec.append([r_date, r_vouch, receipts_amt, r_head, "", "Parishioner donation"])

    ws_pay = wb.create_sheet("Payments")
    ws_pay.append(["Date", "Receipt Number", "Amount", "Head Name", "Subhead", "Narration"])
    p_head = "Bank Charges" if not missing_subhead else "Donation/Offering for Special Purposes(C)"
    p_sub = ""
    ws_pay.append(["2026-05-02", "31", payments_amt, p_head, p_sub, "Parish payment"])

    file_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(file_path)


# =========================================================================
# 1. Exact Zero-Variance and Discrepancy Gate Tests
# =========================================================================

def test_exact_zero_variance_balance_passes(tmp_path, validator):
    """Verify exact ₹0.00 variance passes with READY_FOR_EXECUTION."""
    wb_file = tmp_path / "exact_balance.xlsx"
    create_balanced_workbook(wb_file, receipts_amt=516867.0, payments_amt=475438.0)

    summary = validator.validate_workbook(
        workbook_path=wb_file,
        expected_month="2026-05",
        expected_opening_balance=18506.60,
        expected_closing_balance=59935.60,
    )

    assert summary.is_valid is True
    assert summary.status == "READY_FOR_EXECUTION"
    assert summary.closing_balance_variance == 0.0
    assert summary.categories["accounting_reconciliation"].status == ValidationCategoryStatus.PASS
    assert summary.categories["accounting_reconciliation"].details["is_exact_match"] is True


def test_one_paisa_variance_blocks_execution(tmp_path, validator):
    """Verify even a 1-paisa variance (₹0.01) strictly blocks execution."""
    wb_file = tmp_path / "paisa_error.xlsx"
    # Receipts 516867.01 vs 516867.00 => computed closing is 59935.61 vs target 59935.60
    create_balanced_workbook(wb_file, receipts_amt=516867.01, payments_amt=475438.0)

    summary = validator.validate_workbook(
        workbook_path=wb_file,
        expected_month="2026-05",
        expected_opening_balance=18506.60,
        expected_closing_balance=59935.60,
    )

    assert summary.is_valid is False
    assert "BLOCKED" in summary.status
    assert abs(summary.closing_balance_variance) == pytest.approx(0.01, abs=0.001)
    assert summary.categories["accounting_reconciliation"].status == ValidationCategoryStatus.BLOCKED
    assert any("Closing balance does not match" in e.error_message for e in summary.errors)


def test_historical_40828_discrepancy_blocks_execution(tmp_path, validator):
    """Verify the ₹40,828.95 discrepancy produces an explicit BLOCKED verdict with difference."""
    wb_file = tmp_path / "mismatch_40828.xlsx"
    # Computed closing: 18506.60 + 1000 - 400 = 19106.60 vs target 59935.60 (difference: 40829.00)
    create_balanced_workbook(wb_file, receipts_amt=1000.0, payments_amt=400.0)

    summary = validator.validate_workbook(
        workbook_path=wb_file,
        expected_month="2026-05",
        expected_opening_balance=18506.60,
        expected_closing_balance=59935.60,
    )

    assert summary.is_valid is False
    assert "BLOCKED" in summary.status
    diff = abs(summary.closing_balance_variance)
    assert diff == pytest.approx(40829.00, abs=0.50)
    assert summary.categories["accounting_reconciliation"].status == ValidationCategoryStatus.BLOCKED
    assert any("EXECUTION BLOCKED — Closing balance does not match" in iss for iss in summary.categories["accounting_reconciliation"].issues)


# =========================================================================
# 2. Required Fields Validation Gate Tests
# =========================================================================

def test_missing_date_blocks_execution(tmp_path, validator):
    """Verify that a missing transaction date strictly blocks execution."""
    wb_file = tmp_path / "missing_date.xlsx"
    create_balanced_workbook(wb_file, omit_date=True)

    summary = validator.validate_workbook(
        workbook_path=wb_file,
        expected_month="2026-05",
    )

    assert summary.is_valid is False
    assert summary.categories["required_fields"].status == ValidationCategoryStatus.BLOCKED
    assert any("date is missing" in iss.lower() for iss in summary.categories["required_fields"].issues)


def test_missing_voucher_number_blocks_execution(tmp_path, validator):
    """Verify that a missing voucher number strictly blocks execution."""
    wb_file = tmp_path / "missing_voucher.xlsx"
    create_balanced_workbook(wb_file, omit_voucher=True)

    summary = validator.validate_workbook(
        workbook_path=wb_file,
        expected_month="2026-05",
    )

    assert summary.is_valid is False
    assert summary.categories["required_fields"].status == ValidationCategoryStatus.BLOCKED
    assert any("voucher / receipt number cannot be empty" in iss.lower() for iss in summary.categories["required_fields"].issues)


def test_invalid_accounting_head_blocks_execution(tmp_path, validator):
    """Verify that an unmapped/invalid accounting head strictly blocks execution."""
    wb_file = tmp_path / "invalid_head.xlsx"
    create_balanced_workbook(wb_file, invalid_head=True)

    summary = validator.validate_workbook(
        workbook_path=wb_file,
        expected_month="2026-05",
    )

    assert summary.is_valid is False
    assert summary.categories["canonical_mapping"].status == ValidationCategoryStatus.BLOCKED


# =========================================================================
# 3. Human Verification State Separation Tests
# =========================================================================

def test_human_verification_state_separation():
    """Verify that machine-verified items initialize as PENDING human status, and human actions transition cleanly."""
    registry = CanonicalRegistry()
    mgr = ReviewManager(registry=registry)

    # Machine marks VERIFIED
    res = MappingResult(
        transaction_id="TX_TEST_RECON_01",
        raw_narration="Special Holy Mass Donation",
        amount=500.0,
        direction="RECEIPT",
        canonical_head_id="GEN_HEAD_OFFERINGS_DAILY",
        canonical_head_name="Daily Offerings",
        mapping_status=MappingStatus.VERIFIED,
        confidence_score=0.99,
        mapping_method="EXACT_ALIAS",
        needs_human_review=False,
    )

    raw_row = RawExtractedRow(source_image="p1.jpg", source_page="1", source_row=1, raw_date="01-05-2026", raw_voucher_number="501")
    aligned_row = AlignedRow(raw_row=raw_row, aligned_date="2026-05-01", aligned_voucher_number="501")

    item = mgr.build_review_item(res, aligned_row=aligned_row)

    # 1. State must start as PENDING human verification despite machine VERIFIED status
    assert item.mapping_status == MappingStatus.VERIFIED
    assert item.human_verification_status == HumanVerificationStatus.PENDING

    # 2. Explicit user confirmation transitions to HUMAN_VERIFIED
    confirmed = mgr.apply_user_decision(
        item=item,
        action=ReviewAction.CONFIRM,
        reviewed_by="Fr. Vicar",
        timestamp="2026-11-06T12:00:00Z",
        validate_required=True,
    )
    assert confirmed.human_verification_status == HumanVerificationStatus.HUMAN_VERIFIED
    assert confirmed.reviewed_by == "Fr. Vicar"

    # 3. Edit invalidates confirmation back to HUMAN_CORRECTED
    edited = mgr.apply_edit(
        item=confirmed,
        date=confirmed.date,
        voucher_number=confirmed.voucher_number,
        direction=confirmed.direction,
        amount=600.0,
        canonical_head_id=confirmed.canonical_head_id,
        editor="Accountant",
        timestamp="2026-11-06T12:05:00Z",
    )
    assert edited.amount == 600.0
    assert edited.human_verification_status == HumanVerificationStatus.HUMAN_CORRECTED

    # 4. Confirming strict validation on missing date raises ValueError
    bad_item = mgr.build_review_item(res)
    bad_item.date = None
    with pytest.raises(ValueError, match="Date is missing"):
        mgr.apply_user_decision(
            item=bad_item,
            action=ReviewAction.CONFIRM,
            reviewed_by="Fr. Vicar",
            timestamp="2026-11-06T12:10:00Z",
            validate_required=True,
        )


# =========================================================================
# 4. Safe May 2026 Reset Endpoint Tests
# =========================================================================

def test_safe_may_reset_archives_data_and_manifest(test_client, tmp_path, monkeypatch):
    """Verify POST /api/month/2026-05/reset archives output, writes SHA-256 manifest, and protects images."""
    monkeypatch.setattr("src.app.main.DATA_DIR", tmp_path)

    # Set up May 2026 directory structure
    may_output = tmp_path / "output" / "2026-05"
    may_output.mkdir(parents=True)
    (may_output / "review_state.json").write_text(json.dumps({"test": "data"}), encoding="utf-8")

    may_images = tmp_path / "monthly" / "2026-05"
    may_images.mkdir(parents=True)
    (may_images / "p1.jpg").write_bytes(b"dummy_image_bytes")

    # 1. Reset May 2026
    resp = test_client.post("/api/month/2026-05/reset")
    assert resp.status_code == 200
    res_data = resp.json()
    assert res_data["status"] == "RESET_SUCCESS"
    assert res_data["archived_count"] >= 1

    # Verify original photos preserved
    assert (may_images / "p1.jpg").is_file()

    # Verify archive manifest created with SHA-256
    archive_dir = Path(res_data["archived_directory"])
    manifest_file = archive_dir / "archive_manifest.json"
    assert manifest_file.is_file()
    manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert manifest_data["month"] == "2026-05"
    assert "review_state.json" in manifest_data["files"]
    assert len(manifest_data["files"]["review_state.json"]) == 64  # SHA-256 hex length

    # 2. Reset other months strictly forbidden
    resp_apr = test_client.post("/api/month/2026-04/reset")
    assert resp_apr.status_code == 400
    assert "strictly restricted to May 2026" in resp_apr.json()["detail"]
