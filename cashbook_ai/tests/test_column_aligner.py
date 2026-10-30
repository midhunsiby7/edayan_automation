import json
import pytest
from pathlib import Path
from src.models import RawExtractedRow
from src.extraction.column_aligner import ColumnAligner, AlignedRow, ColumnShiftType

FIXTURES_DIR = Path("tests/fixtures")


@pytest.fixture
def aligner():
    return ColumnAligner()


def test_normal_correctly_aligned_row(aligner):
    """Test standard row with unambiguous date, head, voucher, and amount."""
    raw = RawExtractedRow(
        source_image="page_001.jpeg",
        source_page="1",
        source_row=2,
        row_bbox=[95.0, 45.0, 125.0, 955.0],
        raw_date="3.05.26",
        raw_head_number="20",
        raw_voucher_number="536",
        raw_narration="നിത്യ ചിലവിലേക്ക് സംഭാവന",
        english_narration="Donation for daily expenses",
        raw_amount_col1="5000",
        raw_amount_col2=None,
        detected_amount_column=1
    )

    aligned = aligner.align_row(raw)

    assert aligned.shift_type == ColumnShiftType.NONE
    assert aligned.alignment_confidence == 1.0
    assert aligned.needs_human_review is False
    assert aligned.aligned_date == "3.05.26"
    assert aligned.aligned_head_number == "20"
    assert aligned.aligned_voucher_number == "536"
    assert aligned.aligned_amount_col1 == "5000"
    # Verify raw row is untouched
    assert aligned.raw_row == raw


def test_staff_allowances_field_shift_page3(aligner):
    """Test Page 3 Rows 9-13: Folios 52-56 in date, Vouchers 71-75 in head, voucher empty."""
    prev_row = AlignedRow(
        raw_row=RawExtractedRow(
            source_image="page_003.jpeg",
            source_page="3",
            source_row=1,
            raw_date="31.5.2026",
            raw_head_number="20",
            raw_voucher_number="554",
            raw_amount_col1="6000"
        ),
        aligned_date="31.5.2026",
        aligned_head_number="20",
        aligned_voucher_number="554"
    )

    raw_allowance = RawExtractedRow(
        source_image="page_003.jpeg",
        source_page="3",
        source_row=9,
        row_bbox=[375.0, 25.0, 405.0, 900.0],
        raw_date="52",
        raw_head_number="71",
        raw_voucher_number="-",
        raw_narration="Allowance Rev. Fr. Vicar",
        english_narration="Allowance Rev. Fr. Vicar",
        raw_amount_col1=None,
        raw_amount_col2="15000",
        detected_amount_column=2
    )

    aligned = aligner.align_row(raw_allowance, prev_aligned_row=prev_row)

    # Must detect shift
    assert aligned.shift_type == ColumnShiftType.FOLIO_IN_DATE_VOUCHER_IN_HEAD
    assert aligned.alignment_confidence == 0.95
    assert aligned.needs_human_review is False

    # Folio moved from date -> head_number
    assert aligned.aligned_head_number == "52"

    # Voucher moved from head -> voucher_number
    assert aligned.aligned_voucher_number == "71"

    # Date inherited from previous row
    assert aligned.aligned_date == "31.5.2026"
    assert aligned.was_date_inherited is True

    # Payment amount preserved
    assert aligned.aligned_amount_col2 == "15000"
    assert aligned.detected_amount_column == 2

    # Raw row remains 100% untouched
    assert aligned.raw_row.raw_date == "52"
    assert aligned.raw_row.raw_head_number == "71"
    assert aligned.raw_row.raw_voucher_number == "-"


def test_receipt_vouchers_transposition_page3(aligner):
    """Test Page 3 Rows 2-8: Folio 28 in date, Voucher 556 in head, voucher empty."""
    prev_row = AlignedRow(
        raw_row=RawExtractedRow(
            source_image="page_003.jpeg",
            source_page="3",
            source_row=1,
            raw_date="31.5.2026"
        ),
        aligned_date="31.5.2026"
    )

    raw_nercha = RawExtractedRow(
        source_image="page_003.jpeg",
        source_page="3",
        source_row=3,
        row_bbox=[195.0, 25.0, 225.0, 900.0],
        raw_date="28",
        raw_head_number="556",
        raw_voucher_number="-",
        raw_narration="ലേലം വക (ഓടകൾ)",
        english_narration="Auction proceeds (drains)",
        raw_amount_col1="520",
        raw_amount_col2=None,
        detected_amount_column=1
    )

    aligned = aligner.align_row(raw_nercha, prev_aligned_row=prev_row)

    assert aligned.shift_type == ColumnShiftType.FOLIO_IN_DATE_VOUCHER_IN_HEAD
    assert aligned.aligned_head_number == "28"
    assert aligned.aligned_voucher_number == "556"
    assert aligned.aligned_date == "31.5.2026"
    assert aligned.aligned_amount_col1 == "520"


def test_ditto_date_inheritance_normal_row(aligner):
    """Test date inheritance when row has a recognized ditto mark in the date column."""
    prev = AlignedRow(
        raw_row=RawExtractedRow(source_image="p", source_page="1", source_row=1),
        aligned_date="18.05.26"
    )

    raw = RawExtractedRow(
        source_image="p",
        source_page="1",
        source_row=2,
        raw_date="''",
        raw_head_number="86",
        raw_voucher_number="50",
        raw_narration="Pastoral Letter",
        raw_amount_col2="800",
        detected_amount_column=2
    )

    aligned = aligner.align_row(raw, prev_aligned_row=prev)
    assert aligned.shift_type == ColumnShiftType.NONE
    assert aligned.aligned_date == "18.05.26"
    assert aligned.was_date_inherited is True
    assert aligned.aligned_head_number == "86"
    assert aligned.aligned_voucher_number == "50"


def test_ambiguous_head_over_200_without_folio_flags_review(aligner):
    """Test that a head > 200 with normal date and empty voucher is flagged FIELD_SHIFT_SUSPECTED."""
    raw = RawExtractedRow(
        source_image="p",
        source_page="1",
        source_row=10,
        raw_date="31.5.2026",
        raw_head_number="555",
        raw_voucher_number="-",
        raw_narration="Some collection",
        raw_amount_col1="1000",
        detected_amount_column=1
    )

    aligned = aligner.align_row(raw)
    assert aligned.shift_type == ColumnShiftType.FIELD_SHIFT_SUSPECTED
    assert aligned.needs_human_review is True
    assert aligned.alignment_confidence <= 0.60


def test_malayalam_inside_voucher_column_flags_review(aligner):
    """Test that Malayalam script inside numeric column triggers FIELD_SHIFT_SUSPECTED."""
    raw = RawExtractedRow(
        source_image="p",
        source_page="1",
        source_row=5,
        raw_date="12.05.2026",
        raw_head_number="20",
        raw_voucher_number="വേദപുസ്തകം",  # Malayalam text in voucher field
        raw_amount_col1="500",
        detected_amount_column=1
    )

    aligned = aligner.align_row(raw)
    assert aligned.shift_type == ColumnShiftType.FIELD_SHIFT_SUSPECTED
    assert aligned.needs_human_review is True


def test_missing_bounding_box_handled_gracefully(aligner):
    """Test row without row_bbox processes without runtime error."""
    raw = RawExtractedRow(
        source_image="p",
        source_page="1",
        source_row=1,
        row_bbox=None,
        raw_date="01/05/2026",
        raw_head_number="10",
        raw_voucher_number="101"
    )

    aligned = aligner.align_row(raw)
    assert aligned.shift_type == ColumnShiftType.NONE
    assert aligned.row_bbox is None


def test_align_page_full_sequence_may_page3(aligner):
    """Test align_page on complete May Page 3 sequence, verifying all 13 rows."""
    raw_path = Path("data/output/2026-05/raw_extractions.json")
    if not raw_path.exists():
        pytest.skip("data/output/2026-05/raw_extractions.json not present")

    with open(raw_path, "r", encoding="utf-8") as f:
        all_raw = json.load(f)

    p3_raw = [RawExtractedRow(**r) for r in all_raw if r.get("source_image") == "page_003.jpeg"]
    assert len(p3_raw) == 13

    aligned_p3 = aligner.align_page(p3_raw)
    assert len(aligned_p3) == 13

    # Row 1: normal
    assert aligned_p3[0].shift_type == ColumnShiftType.NONE
    assert aligned_p3[0].aligned_head_number == "20"
    assert aligned_p3[0].aligned_voucher_number == "554"

    # Rows 2-8: receipt shifts resolved
    expected_folios = ["40", "28", "28", "28", "28", "28", "24"]
    expected_vouchers = ["555", "556", "557", "558", "559", "560", "561"]
    for idx, (exp_fol, exp_vouch) in enumerate(zip(expected_folios, expected_vouchers), start=1):
        assert aligned_p3[idx].shift_type == ColumnShiftType.FOLIO_IN_DATE_VOUCHER_IN_HEAD
        assert aligned_p3[idx].aligned_head_number == exp_fol
        assert aligned_p3[idx].aligned_voucher_number == exp_vouch
        assert aligned_p3[idx].aligned_date == "31.5.2026"

    # Rows 9-13: staff allowances shifts resolved
    expected_allowance_folios = ["52", "53", "54", "55", "56"]
    expected_allowance_vouchers = ["71", "72", "73", "74", "75"]
    for idx, (exp_fol, exp_vouch) in enumerate(zip(expected_allowance_folios, expected_allowance_vouchers), start=8):
        assert aligned_p3[idx].shift_type == ColumnShiftType.FOLIO_IN_DATE_VOUCHER_IN_HEAD
        assert aligned_p3[idx].aligned_head_number == exp_fol
        assert aligned_p3[idx].aligned_voucher_number == exp_vouch
        assert aligned_p3[idx].aligned_date == "31.5.2026"
        assert aligned_p3[idx].detected_amount_column == 2


def test_golden_verified_dataset_fixture_integrity():
    """Verify golden_verified_dataset.json loads and matches expected structure."""
    golden_path = FIXTURES_DIR / "golden_verified_dataset.json"
    assert golden_path.exists(), "Golden verified dataset must exist"

    with open(golden_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["record_count"] >= 65
    records = data["records"]
    assert len(records) == data["record_count"]

    # Verify every record has required audit fields
    for r in records:
        assert "transaction_ref" in r
        assert "source_month" in r
        assert "direction" in r
        assert r["direction"] in ("RECEIPT", "PAYMENT")
        assert "verified_voucher" in r
        assert "verified_amount" in r
        assert "expected_canonical_head_id" in r
        assert "verification_source" in r
