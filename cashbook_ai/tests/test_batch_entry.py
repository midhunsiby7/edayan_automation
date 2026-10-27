"""Tests for Guarded Batch Execution Mode and Manifests."""

import json
from pathlib import Path
from typing import Any, Mapping

import openpyxl
import pytest

from automation.batch_manifest import (
    APPROVED,
    DRAFT,
    BatchManifest,
    ManifestError,
    ManifestStateError,
    UnauthorizedTransactionError,
    WorkbookTamperedError,
    approve_manifest,
    calculate_file_sha256,
    generate_batch_manifest,
    load_manifest,
    save_manifest,
    validate_manifest,
)
from automation.edayan_entry import (
    CONFIRMED,
    FAILED,
    PENDING,
    SUBMITTED,
    UNCERTAIN,
    BatchEntryRunner,
    check_live_duplicate,
    load_or_create_journal,
    transition,
    verify_readback,
)


class MockBrowserEntryAdapter:
    """Mock adapter simulating browser interactions without network/DOM access."""

    def __init__(
        self,
        *,
        readback_override: Mapping[str, Any] | None = None,
        table_text_override: str | None = None,
        save_confirmed: bool = True,
        save_error: str | None = None,
        fail_submit: bool = False,
    ):
        self.readback_override = readback_override
        self.table_text_override = table_text_override
        self.save_confirmed = save_confirmed
        self.save_error = save_error
        self.fail_submit = fail_submit
        self.entered_entries: list[dict[str, Any]] = []
        self.submitted_count = 0
        self.current_entry: dict[str, Any] | None = None

    def enter(self, entry: Mapping[str, Any]) -> Mapping[str, Any]:
        self.current_entry = dict(entry)
        self.entered_entries.append(dict(entry))
        return self.readback(entry)

    def readback(self, entry: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        target = dict(entry or self.current_entry or {})
        if self.readback_override is not None:
            res = dict(self.readback_override)
            if "table_text" not in res:
                res["table_text"] = self.table_text_override or ""
            return res

        date_val = str(target.get("date", "2026-04-04"))
        if len(date_val) == 10 and date_val[4] == "-" and date_val[7] == "-":
            date_val = f"{date_val[8:10]}-{date_val[5:7]}-{date_val[0:4]}"

        return {
            "date": date_val,
            "receipt_number": str(target.get("receipt_number", "515")),
            "amount": str(target.get("amount", "1000")),
            "mode_cash": True,
            "head": target.get("head_name", "Test Head"),
            "subhead": target.get("subhead", ""),
            "narration": str(target.get("narration", "")),
            "table_text": self.table_text_override or "",
            "dom": {},
        }

    def submit(self) -> Mapping[str, Any]:
        if self.fail_submit:
            raise RuntimeError("Browser submit error")
        self.submitted_count += 1
        return {"clicked": True}

    def observe_save(
        self, expected_entry: Mapping[str, Any] | None = None, timeout_seconds: float = 30
    ) -> Mapping[str, Any]:
        v_num = str((expected_entry or {}).get("receipt_number", "515"))
        if not self.save_confirmed:
            return {
                "confirmed": False,
                "record_id": None,
                "last_error": self.save_error or "Timeout waiting for table evidence",
            }
        return {
            "confirmed": True,
            "record_id": v_num,
            "success_text": f"Saved voucher {v_num}",
            "table_evidence": True,
        }


def make_dummy_workbook(path: Path) -> Path:
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "Receipts"
    ws1.append(["Date", "Receipt Number", "Amount", "Head Name", "Subhead", "Narration"])
    ws1.append(["2026-04-03", "514", 13245, "Head A", "Sub A", "Narr 514"])
    ws1.append(["2026-04-03", "1", 725, "Bank Interest", "", "Narr 1"])
    ws1.append(["2026-04-04", "515", 6405, "Head A", "Sub B", "Narr 515"])
    ws1.append(["2026-04-05", "516", 8000, "Head B", "", "Narr 516"])
    ws2 = wb.create_sheet(title="Payments")
    ws2.append(["Date", "Receipt Number", "Amount", "Head Name", "Subhead", "Narration"])
    ws2.append(["2026-04-01", "1", 700, "Expense Head", "", "Payment 1"])
    wb.save(path)
    return path


def make_sample_report(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"mode": "dry-run", "entries": entries}


def test_batch_manifest_generation_and_exclusions(tmp_path: Path):
    wb_file = make_dummy_workbook(tmp_path / "test_wb.xlsx")
    journal_data = {
        "entries": {
            "key-514": {"status": "confirmed", "evidence": {"record_id": "514"}},
        }
    }
    entries = [
        {
            "submission_key": "key-514",
            "direction": "RECEIPT",
            "sheet": "Receipts",
            "row": 2,
            "date": "2026-04-03",
            "receipt_number": "514",
            "amount": 13245,
            "head_name": "Head A",
            "subhead": "Sub A",
            "narration": "Narr 514",
            "status": "READY",
        },
        {
            "submission_key": "key-1",
            "direction": "RECEIPT",
            "sheet": "Receipts",
            "row": 3,
            "date": "2026-04-03",
            "receipt_number": "1",
            "amount": 725,
            "head_name": "Bank Interest",
            "subhead": "",
            "narration": "Narr 1",
            "status": "READY",
        },
        {
            "submission_key": "key-515",
            "direction": "RECEIPT",
            "sheet": "Receipts",
            "row": 4,
            "date": "2026-04-04",
            "receipt_number": "515",
            "amount": 6405,
            "head_name": "Head A",
            "subhead": "Sub B",
            "narration": "Narr 515",
            "status": "READY",
        },
        {
            "submission_key": "key-516",
            "direction": "RECEIPT",
            "sheet": "Receipts",
            "row": 5,
            "date": "2026-04-05",
            "receipt_number": "516",
            "amount": 8000,
            "head_name": "Head B",
            "subhead": "",
            "narration": "Narr 516",
            "status": "READY",
        },
        {
            "submission_key": "key-pay-1",
            "direction": "PAYMENT",
            "sheet": "Payments",
            "row": 2,
            "date": "2026-04-01",
            "receipt_number": "1",
            "amount": 700,
            "head_name": "Expense Head",
            "subhead": "",
            "narration": "Payment 1",
            "status": "READY",
        },
        {
            "submission_key": "key-blocked",
            "direction": "RECEIPT",
            "sheet": "Receipts",
            "row": 6,
            "date": "2026-04-06",
            "receipt_number": "517",
            "amount": 500,
            "head_name": "Unmapped Head",
            "subhead": "",
            "narration": "Blocked",
            "status": "BLOCKED",
            "issues": ["No website mapping"],
        },
    ]

    report = make_sample_report(entries)
    manifest = generate_batch_manifest(
        report, journal_data, wb_file, direction="RECEIPT", target_voucher_start=515, target_voucher_end=516
    )

    assert manifest.status == DRAFT
    assert manifest.batch_summary["transaction_count"] == 2
    assert manifest.batch_summary["total_amount"] == 14405.0  # 6405 + 8000
    assert manifest.authorized_keys == ["key-515", "key-516"]

    # Verify exclusions
    excluded_keys = {e["submission_key"]: e["reason"] for e in manifest.excluded_entries}
    assert "ALREADY_CONFIRMED" in excluded_keys["key-514"]
    assert "VOUCHER_DISCONTINUITY" in excluded_keys["key-1"]
    assert "DIRECTION_EXCLUDED" in excluded_keys["key-pay-1"]
    assert "BLOCKED" in excluded_keys["key-blocked"]


def test_batch_manifest_approval_lifecycle(tmp_path: Path):
    wb_file = make_dummy_workbook(tmp_path / "test_wb.xlsx")
    manifest = generate_batch_manifest(
        make_sample_report([]), {}, wb_file, direction="RECEIPT", target_voucher_start=1, target_voucher_end=1
    )
    assert manifest.status == DRAFT
    assert manifest.approval_metadata is None

    # Approve
    approved = approve_manifest(manifest, approved_by="auditor_test")
    assert approved.status == APPROVED
    assert approved.approval_metadata is not None
    assert approved.approval_metadata["approved_by"] == "auditor_test"

    # Cannot approve twice
    with pytest.raises(ManifestStateError, match="already approved"):
        approve_manifest(approved, approved_by="auditor_test")


def test_batch_manifest_rejects_altered_workbook(tmp_path: Path):
    wb_file = make_dummy_workbook(tmp_path / "test_wb.xlsx")
    manifest = generate_batch_manifest(
        make_sample_report([]), {}, wb_file, direction="RECEIPT", target_voucher_start=1, target_voucher_end=1
    )
    validate_manifest(manifest, wb_file)

    # Modify workbook byte
    with wb_file.open("ab") as handle:
        handle.write(b"corruption")

    with pytest.raises(WorkbookTamperedError):
        validate_manifest(manifest, wb_file)


def test_unapproved_draft_manifest_cannot_be_run(tmp_path: Path):
    wb_file = make_dummy_workbook(tmp_path / "test_wb.xlsx")
    manifest = generate_batch_manifest(
        make_sample_report([]), {}, wb_file, direction="RECEIPT", target_voucher_start=1, target_voucher_end=1
    )
    journal_path = tmp_path / "journal.json"
    runner = BatchEntryRunner(manifest, MockBrowserEntryAdapter(), journal_path, wb_file)

    with pytest.raises(ManifestStateError, match="must be APPROVED"):
        runner.run_batch()


def test_whitelist_enforcement_rejects_unauthorized_entry(tmp_path: Path):
    wb_file = make_dummy_workbook(tmp_path / "test_wb.xlsx")
    entry = {
        "submission_key": "key-515",
        "direction": "RECEIPT",
        "sheet": "Receipts",
        "row": 4,
        "date": "2026-04-04",
        "receipt_number": "515",
        "amount": 6405,
        "head_name": "Head A",
        "subhead": "",
        "narration": "Narr 515",
        "status": "READY",
    }
    manifest = generate_batch_manifest(
        make_sample_report([entry]), {}, wb_file, direction="RECEIPT", target_voucher_start=515, target_voucher_end=515
    )
    approved = approve_manifest(manifest, approved_by="user")

    # Infiltrate an unauthorized transaction into approved.authorized_entries (scope expansion attempt)
    approved.authorized_entries.append({
        "submission_key": "unauthorized-injected-key",
        "sheet": "Receipts",
        "row": 10,
        "receipt_number": "999",
        "amount": 100,
    })

    journal_path = tmp_path / "journal.json"
    runner = BatchEntryRunner(approved, MockBrowserEntryAdapter(), journal_path, wb_file)

    with pytest.raises(UnauthorizedTransactionError):
        runner.run_batch()


def test_pre_submit_readback_mismatch_halts_batch(tmp_path: Path):
    wb_file = make_dummy_workbook(tmp_path / "test_wb.xlsx")
    entry = {
        "submission_key": "key-515",
        "direction": "RECEIPT",
        "sheet": "Receipts",
        "row": 4,
        "date": "2026-04-04",
        "receipt_number": "515",
        "amount": 6405,
        "head_name": "Collections as per Order from the Diocese",
        "subhead": "Holy Land",
        "narration": "As per order (Holy land)",
        "status": "READY",
    }
    report = make_sample_report([entry])
    manifest = generate_batch_manifest(
        report, {}, wb_file, direction="RECEIPT", target_voucher_start=515, target_voucher_end=515
    )
    approved = approve_manifest(manifest, approved_by="user")
    journal_path = tmp_path / "journal.json"

    # Adapter simulates readback mismatch (amount typed as 500 instead of 6405)
    adapter = MockBrowserEntryAdapter(
        readback_override={
            "date": "04-04-2026",
            "receipt_number": "515",
            "amount": "500",  # MISMATCH
            "mode_cash": True,
            "head": "Collections as per Order from the Diocese",
            "subhead": "Holy Land",
            "narration": "As per order (Holy land)",
        }
    )

    runner = BatchEntryRunner(approved, adapter, journal_path, wb_file, incident_dir=tmp_path)
    result = runner.run_batch()

    assert result["status"] == "STOPPED"
    assert result["reason"] == "CIRCUIT_BREAKER_TRIPPED"
    assert adapter.submitted_count == 0  # SUBMIT WAS NEVER CLICKED!

    saved_journal = load_or_create_journal(journal_path)
    assert saved_journal["entries"]["key-515"]["status"] == FAILED


def test_duplicate_conflict_in_live_table_halts_batch(tmp_path: Path):
    wb_file = make_dummy_workbook(tmp_path / "test_wb.xlsx")
    entry = {
        "submission_key": "key-515",
        "direction": "RECEIPT",
        "sheet": "Receipts",
        "row": 4,
        "date": "2026-04-04",
        "receipt_number": "515",
        "amount": 6405,
        "head_name": "Head A",
        "subhead": "",
        "narration": "Narr 515",
        "status": "READY",
    }
    manifest = generate_batch_manifest(
        make_sample_report([entry]), {}, wb_file, direction="RECEIPT", target_voucher_start=515, target_voucher_end=515
    )
    approved = approve_manifest(manifest, approved_by="user")
    journal_path = tmp_path / "journal.json"

    # Conflicting line in live table (voucher 515 exists with DIFFERENT amount 9999)
    table_conflict = "1 | 04-04-2026 | Head A | 515 | Cash | Conflict | ₹9,999.00"
    adapter = MockBrowserEntryAdapter(table_text_override=table_conflict)

    runner = BatchEntryRunner(approved, adapter, journal_path, wb_file, incident_dir=tmp_path)
    result = runner.run_batch()

    assert result["status"] == "STOPPED"
    assert adapter.submitted_count == 0  # Did not submit into conflict!

    saved_journal = load_or_create_journal(journal_path)
    assert saved_journal["entries"]["key-515"]["status"] == FAILED


def test_save_timeout_marks_uncertain_and_halts_batch(tmp_path: Path):
    wb_file = make_dummy_workbook(tmp_path / "test_wb.xlsx")
    entry1 = {
        "submission_key": "key-515",
        "direction": "RECEIPT",
        "sheet": "Receipts",
        "row": 4,
        "date": "2026-04-04",
        "receipt_number": "515",
        "amount": 6405,
        "head_name": "Head A",
        "subhead": "",
        "narration": "Narr 515",
        "status": "READY",
    }
    entry2 = {
        "submission_key": "key-516",
        "direction": "RECEIPT",
        "sheet": "Receipts",
        "row": 5,
        "date": "2026-04-05",
        "receipt_number": "516",
        "amount": 8000,
        "head_name": "Head B",
        "subhead": "",
        "narration": "Narr 516",
        "status": "READY",
    }
    manifest = generate_batch_manifest(
        make_sample_report([entry1, entry2]), {}, wb_file, direction="RECEIPT", target_voucher_start=515, target_voucher_end=516
    )
    approved = approve_manifest(manifest, approved_by="user")
    journal_path = tmp_path / "journal.json"

    # Save observation fails/times out
    adapter = MockBrowserEntryAdapter(save_confirmed=False, save_error="Timeout waiting for row")

    runner = BatchEntryRunner(approved, adapter, journal_path, wb_file, incident_dir=tmp_path)
    result = runner.run_batch()

    assert result["status"] == "STOPPED"
    assert result["reason"] == "CIRCUIT_BREAKER_TRIPPED"
    assert adapter.submitted_count == 1  # Only submitted first

    saved_journal = load_or_create_journal(journal_path)
    # First transaction marked UNCERTAIN
    assert saved_journal["entries"]["key-515"]["status"] == UNCERTAIN
    # Second transaction was NOT touched
    assert "key-516" not in saved_journal["entries"]


def test_successful_sequential_batch_execution(tmp_path: Path):
    wb_file = make_dummy_workbook(tmp_path / "test_wb.xlsx")
    entries = [
        {
            "submission_key": "key-515",
            "direction": "RECEIPT",
            "sheet": "Receipts",
            "row": 4,
            "date": "2026-04-04",
            "receipt_number": "515",
            "amount": 6405,
            "head_name": "Head A",
            "subhead": "Sub B",
            "narration": "Narr 515",
            "status": "READY",
        },
        {
            "submission_key": "key-516",
            "direction": "RECEIPT",
            "sheet": "Receipts",
            "row": 5,
            "date": "2026-04-05",
            "receipt_number": "516",
            "amount": 8000,
            "head_name": "Head B",
            "subhead": "",
            "narration": "Narr 516",
            "status": "READY",
        },
    ]
    manifest = generate_batch_manifest(
        make_sample_report(entries), {}, wb_file, direction="RECEIPT", target_voucher_start=515, target_voucher_end=516
    )
    approved = approve_manifest(manifest, approved_by="user")
    journal_path = tmp_path / "journal.json"

    adapter = MockBrowserEntryAdapter(save_confirmed=True)
    runner = BatchEntryRunner(approved, adapter, journal_path, wb_file, incident_dir=tmp_path)
    result = runner.run_batch()

    assert result["status"] == "COMPLETED"
    assert result["confirmed_count"] == 2
    assert adapter.submitted_count == 2

    saved_journal = load_or_create_journal(journal_path)
    assert saved_journal["entries"]["key-515"]["status"] == CONFIRMED
    assert saved_journal["entries"]["key-516"]["status"] == CONFIRMED


def test_exact_duplicate_already_in_table_marks_confirmed_without_resubmit(tmp_path: Path):
    wb_file = make_dummy_workbook(tmp_path / "test_wb.xlsx")
    entry = {
        "submission_key": "key-515",
        "direction": "RECEIPT",
        "sheet": "Receipts",
        "row": 4,
        "date": "2026-04-04",
        "receipt_number": "515",
        "amount": 6405,
        "head_name": "Head A",
        "subhead": "Sub B",
        "narration": "Narr 515",
        "status": "READY",
    }
    manifest = generate_batch_manifest(
        make_sample_report([entry]), {}, wb_file, direction="RECEIPT", target_voucher_start=515, target_voucher_end=515
    )
    approved = approve_manifest(manifest, approved_by="user")
    journal_path = tmp_path / "journal.json"

    # Exact matching line already visible in live table
    matching_table_text = "1 | 04-04-2026 | Head A | 515 | Cash | Narr 515 | ₹6,405.00"
    adapter = MockBrowserEntryAdapter(table_text_override=matching_table_text)

    runner = BatchEntryRunner(approved, adapter, journal_path, wb_file, incident_dir=tmp_path)
    result = runner.run_batch()

    assert result["status"] == "COMPLETED"
    assert result["confirmed_count"] == 1
    assert adapter.submitted_count == 0  # NOT re-submitted! Form submit was skipped safely!

    saved_journal = load_or_create_journal(journal_path)
    assert saved_journal["entries"]["key-515"]["status"] == CONFIRMED


def test_verify_readback_field_validations():
    entry = {
        "date": "2026-04-04",
        "receipt_number": "515",
        "amount": 6405,
        "head_name": "Collections as per Order from the Diocese",
        "subhead": "Holy Land",
        "narration": "As per order (Holy land)",
        "head_resolution": {"website_label": "Collections as per Order from the Diocese ( Credit )"},
    }
    # 1. Exact match
    valid_rb = {
        "date": "04-04-2026",
        "receipt_number": "515",
        "amount": "6405",
        "mode_cash": True,
        "head": "Collections as per Order from the Diocese ( Credit )",
        "subhead": "Holy Land",
        "narration": "As per order (Holy land)",
    }
    is_valid, issues = verify_readback(valid_rb, entry)
    assert is_valid
    assert len(issues) == 0

    # 2. Date mismatch
    bad_date = dict(valid_rb, date="05-04-2026")
    is_valid, issues = verify_readback(bad_date, entry)
    assert not is_valid
    assert any("Date mismatch" in i for i in issues)

    # 3. Voucher mismatch
    bad_v = dict(valid_rb, receipt_number="516")
    is_valid, issues = verify_readback(bad_v, entry)
    assert not is_valid
    assert any("Voucher number mismatch" in i for i in issues)

    # 4. Mode cash unchecked
    bad_mode = dict(valid_rb, mode_cash=False)
    is_valid, issues = verify_readback(bad_mode, entry)
    assert not is_valid
    assert any("Mode cash is not selected" in i for i in issues)


def test_manifest_save_and_load_roundtrip(tmp_path: Path):
    wb_file = make_dummy_workbook(tmp_path / "test_wb.xlsx")
    manifest = generate_batch_manifest(
        make_sample_report([]), {}, wb_file, direction="RECEIPT", target_voucher_start=1, target_voucher_end=1
    )
    manifest_path = tmp_path / "manifest.json"
    save_manifest(manifest, manifest_path)
    loaded = load_manifest(manifest_path)

    assert loaded.manifest_id == manifest.manifest_id
    assert loaded.workbook_sha256 == manifest.workbook_sha256
    assert loaded.status == manifest.status
    assert loaded.batch_summary == manifest.batch_summary

