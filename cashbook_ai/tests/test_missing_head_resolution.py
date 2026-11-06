import datetime
from decimal import Decimal
from pathlib import Path
import openpyxl
import pytest

from src.models import RawExtractedRow, ProcessedRow, RecordType, TransactionType
from src.extraction.column_aligner import ColumnAligner, ColumnShiftType
from src.reference.canonical_registry import CanonicalRegistry
from src.mapping.hybrid_engine import HybridMappingEngine, MappingStatus
from src.export.manual_review_exporter import ManualReviewExporter
from src.validation.approved_workbook_validator import ApprovedWorkbookValidator, ValidationError
from src.automation.execution_manager import ManifestBuilder, ManifestError


@pytest.fixture
def registry():
    return CanonicalRegistry(Path("data/reference/canonical_accounting_reference.json"))


def test_bishop_pala_and_good_shepherd_end_to_end_resolution(registry):
    """Test tracing and resolving Bishop Pala (V.# 8) and Good Shepherd (V.# 9).
    
    Validates:
      1. ColumnAligner resolves VOUCHER_IN_HEAD from sequence 7 -> 8 -> 9.
      2. Aligner inherits Folio 6.
      3. HybridMappingEngine maps Folio 6 to 'The South Indian Bank Kappad' with 1.0 confidence.
      4. Vouchers 8 and 9 are preserved.
    """
    row16 = RawExtractedRow(
        source_image="page_002.jpeg",
        source_page="2",
        source_row=16,
        raw_date="16.05.26",
        raw_head_number="6",
        raw_voucher_number="7",
        raw_narration="S.B withdrawal South Indian Bank",
        raw_amount_col1="53749",
        detected_amount_column=1,
    )
    row17 = RawExtractedRow(
        source_image="page_002.jpeg",
        source_page="2",
        source_row=17,
        raw_date="..",
        raw_head_number="8",
        raw_voucher_number="-",
        raw_narration="Bishop Pala",
        raw_amount_col1="104180",
        detected_amount_column=1,
    )
    row18 = RawExtractedRow(
        source_image="page_002.jpeg",
        source_page="2",
        source_row=18,
        raw_date="..",
        raw_head_number="9",
        raw_voucher_number="-",
        raw_narration="Good Shepherd Minor Seminary",
        raw_amount_col1="13245",
        detected_amount_column=1,
    )

    aligner = ColumnAligner()
    aligned = aligner.align_page([row16, row17, row18])

    # 1. Verify alignment
    assert aligned[1].shift_type == ColumnShiftType.VOUCHER_IN_HEAD
    assert aligned[1].aligned_head_number == "6"
    assert aligned[1].aligned_voucher_number == "8"

    assert aligned[2].shift_type == ColumnShiftType.VOUCHER_IN_HEAD
    assert aligned[2].aligned_head_number == "6"
    assert aligned[2].aligned_voucher_number == "9"

    # 2. Verify mapping engine maps Folio 6
    engine = HybridMappingEngine(registry)
    res17 = engine.map_transaction(aligned[1], financial_year="2026-2027")
    assert res17.canonical_head_id == "GEN_HEAD_BANK_SIB_KAPPAD"
    assert res17.canonical_head_name == "The South Indian Bank Kappad"
    assert res17.mapping_status == MappingStatus.VERIFIED
    assert res17.confidence_score == 1.0

    res18 = engine.map_transaction(aligned[2], financial_year="2026-2027")
    assert res18.canonical_head_id == "GEN_HEAD_BANK_SIB_KAPPAD"
    assert res18.canonical_head_name == "The South Indian Bank Kappad"
    assert res18.mapping_status == MappingStatus.VERIFIED
    assert res18.confidence_score == 1.0


def test_missing_mandatory_head_blocks_validator(tmp_path: Path, registry):
    """A transaction workbook with a missing/blank Head Name must be BLOCKED by ApprovedWorkbookValidator."""
    wb_path = tmp_path / "missing_head_workbook.xlsx"
    wb = openpyxl.Workbook()
    ws_r = wb.active
    ws_r.title = "Receipts"
    ws_r.append(["Date", "Voucher Number", "Amount", "Head Name", "Subhead", "Narration"])
    # Row 2 has empty Head Name
    ws_r.append(["2026-05-18", "8", 104180.0, None, None, "Bishop Pala"])
    
    ws_p = wb.create_sheet("Payments")
    ws_p.append(["Date", "Voucher Number", "Amount", "Head Name", "Subhead", "Narration"])
    ws_p.append(["2026-05-04", "32", 400.0, "Telephone & Postage", None, "Cement labor"])
    wb.save(wb_path)

    validator = ApprovedWorkbookValidator(registry)
    summary = validator.validate_workbook(wb_path, expected_month="2026-05")

    # Must be BLOCKED
    assert summary.is_valid is False
    assert "READY" not in summary.status
    # Check that required_fields has the specific error
    req_issues = summary.categories["required_fields"].issues
    assert any("Receipts row 2: Canonical Head Name cannot be blank" in issue for issue in req_issues)


def test_missing_mandatory_head_blocks_manifest_builder(tmp_path: Path, registry):
    """ManifestBuilder must raise ManifestError if an approved workbook row has a blank head."""
    wb_path = tmp_path / "missing_head_manifest.xlsx"
    wb = openpyxl.Workbook()
    ws_r = wb.active
    ws_r.title = "Receipts"
    ws_r.append(["Date", "Voucher Number", "Amount", "Head Name", "Subhead", "Narration"])
    ws_r.append(["2026-05-18", "8", 104180.0, "", None, "Bishop Pala"])
    wb.save(wb_path)

    builder = ManifestBuilder(registry)
    with pytest.raises(ManifestError) as exc_info:
        builder.build_manifest(wb_path, month="2026-05", direction="RECEIPT")
    assert "Canonical Head Name is blank" in str(exc_info.value)


def test_manual_review_exporter_highlights_unresolved_heads(tmp_path: Path):
    """ManualReviewExporter must highlight unresolved heads with yellow warn_fill and leave resolved heads clean."""
    out_path = tmp_path / "highlight_test.xlsx"
    row_resolved = ProcessedRow(
        source_image="page_002.jpeg",
        source_page="2",
        source_row=16,
        resolved_date="2026-05-18",
        voucher_number="7",
        accounting_head_name="The South Indian Bank Kappad",
        transaction_type=TransactionType.RECEIPT,
        record_type=RecordType.TRANSACTION,
        receipt_amount=53749.0,
        narration_english="S.B withdrawal South Indian Bank",
    )
    row_unresolved = ProcessedRow(
        source_image="page_002.jpeg",
        source_page="2",
        source_row=17,
        resolved_date="2026-05-18",
        voucher_number="8",
        accounting_head_name=None,  # MISSING
        transaction_type=TransactionType.RECEIPT,
        record_type=RecordType.TRANSACTION,
        receipt_amount=104180.0,
        narration_english="Bishop Pala",
    )

    exporter = ManualReviewExporter()
    exporter.export_review_workbook([row_resolved, row_unresolved], out_path)

    wb = openpyxl.load_workbook(out_path)
    ws = wb["Receipts"]

    # Row 2 is resolved
    c_head2 = ws.cell(2, 4)
    assert c_head2.value == "The South Indian Bank Kappad"
    assert c_head2.fill.fgColor.rgb != "00FFF3CD"

    # Row 3 is unresolved
    c_head3 = ws.cell(3, 4)
    assert c_head3.value is None
    assert c_head3.fill.fgColor.rgb == "00FFF3CD"  # YELLOW HIGHLIGHT
