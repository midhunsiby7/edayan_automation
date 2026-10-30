import pytest
from pathlib import Path

from src.models import RawExtractedRow
from src.extraction.column_aligner import AlignedRow, ColumnShiftType
from src.reference.canonical_registry import CanonicalRegistry, SubheadPolicy
from src.mapping.hybrid_engine import (
    HybridMappingEngine,
    MappingResult,
    MappingStatus,
    SubheadStatus,
    MappingMethod
)

REGISTRY_PATH = Path("data/reference/canonical_accounting_reference.json")


@pytest.fixture
def registry():
    return CanonicalRegistry(REGISTRY_PATH)


@pytest.fixture
def engine(registry):
    return HybridMappingEngine(registry)


def test_level1_exact_folio_resolution(engine):
    """Test Level 1: exact folio resolution maps to canonical head and resolves website selector."""
    raw = RawExtractedRow(
        source_image="p1.jpg", source_page="1", source_row=2,
        raw_head_number="20", raw_voucher_number="536",
        raw_amount_col1="5000", raw_narration="Donation"
    )
    aligned = AlignedRow(raw_row=raw, aligned_head_number="20", aligned_voucher_number="536", aligned_amount_col1="5000")
    res = engine.map_transaction(aligned)

    assert res.mapping_method == MappingMethod.EXACT_FOLIO
    assert res.canonical_head_id == "GEN_HEAD_OFFERINGS_DAILY"
    assert res.canonical_head_name == "Offerings for Daily Expense(C)"
    assert res.confidence_score == 1.0
    assert res.mapping_status == MappingStatus.RESOLVED
    assert res.subhead_status == SubheadStatus.VALID_EMPTY
    assert res.website_head_value == "128"


def test_level1_exact_malayalam_alias_resolution(engine):
    """Test Level 1: exact approved Malayalam alias resolution."""
    raw = RawExtractedRow(
        source_image="p1.jpg", source_page="1", source_row=5,
        raw_head_number=None, raw_voucher_number="550",
        raw_amount_col1="2000", raw_narration="നിത്യ ചിലവിലേക്ക് സംഭാവന"
    )
    aligned = AlignedRow(raw_row=raw, aligned_narration_raw="നിത്യ ചിലവിലേക്ക് സംഭാവന", aligned_amount_col1="2000")
    res = engine.map_transaction(aligned)

    assert res.mapping_method == MappingMethod.EXACT_ALIAS
    assert res.canonical_head_id == "GEN_HEAD_OFFERINGS_DAILY"
    assert res.confidence_score == 1.0
    assert res.mapping_status == MappingStatus.RESOLVED


def test_level1_allowance_subhead_from_folio(engine):
    """Test Level 1: Folio 52 accurately maps to Allowances and Rev. Fr. Vicar subhead."""
    raw = RawExtractedRow(
        source_image="p3.jpg", source_page="3", source_row=9,
        raw_head_number="71", raw_voucher_number="-", raw_date="52",
        raw_amount_col2="15000", raw_narration="Allowance Rev. Fr. Vicar"
    )
    # AlignedRow produced by ColumnAligner
    aligned = AlignedRow(
        raw_row=raw,
        aligned_head_number="52",
        aligned_voucher_number="71",
        aligned_date="2026-05-31",
        aligned_amount_col2="15000",
        aligned_narration_raw="Allowance Rev. Fr. Vicar"
    )
    res = engine.map_transaction(aligned)

    assert res.mapping_method == MappingMethod.EXACT_FOLIO
    assert res.canonical_head_id == "GEN_HEAD_ALLOWANCES"
    assert res.canonical_subhead_id == "GEN_SUB_ALLOW_VICAR"
    assert res.canonical_subhead_name == "Rev. Fr. Vicar"
    assert res.subhead_status == SubheadStatus.RESOLVED
    assert res.website_head_value == "108"
    assert res.website_subhead_value == "338"
    assert res.needs_human_review is False


def test_mandatory_subhead_enforcement_blocks_missing_subhead(engine):
    """Test Phase 3: Allowances without a resolvable subhead is flagged MISSING_MANDATORY."""
    raw = RawExtractedRow(
        source_image="p3.jpg", source_page="3", source_row=15,
        raw_head_number="108", raw_voucher_number="99",
        raw_amount_col2="5000", raw_narration="Generic Staff Payment"
    )
    aligned = AlignedRow(
        raw_row=raw,
        aligned_head_number=None,  # No folio
        aligned_amount_col2="5000",
        aligned_narration_english="Allowance Staff"
    )
    # Direct alias or context to head
    # Let's test calling _resolve_subhead_policy directly or via context
    head = engine.registry.get_head("GEN_HEAD_ALLOWANCES")
    sub, status, err = engine._resolve_subhead_policy(head, None, "Unknown Staff Member")

    assert sub is None
    assert status == SubheadStatus.MISSING_MANDATORY
    assert "mandatory subhead" in err


def test_prohibited_subhead_enforcement_rejects_subhead(engine):
    """Test Phase 3: Prohibited subhead head rejects an unexpected subhead."""
    head = engine.registry.get_head("GEN_HEAD_ELECTRICITY")
    sub, status, err = engine._resolve_subhead_policy(head, "GEN_SUB_ALLOW_VICAR", "KSEB electricity bill")

    assert sub is None
    assert status == SubheadStatus.PROHIBITED_VIOLATION
    assert "prohibits subheads" in err


def test_invalid_parent_subhead_relationship(engine):
    """Test Phase 3: Subhead belonging to Diocese collection assigned to Allowances is caught."""
    head = engine.registry.get_head("GEN_HEAD_ALLOWANCES")
    sub, status, err = engine._resolve_subhead_policy(head, "GEN_SUB_DIOCESE_HOLY_LAND", "Payment")

    assert sub is None
    assert status == SubheadStatus.INVALID_PARENT


def test_diocesan_collection_subhead_resolution(engine):
    """Test Phase 3: Diocesan collection with Seminarians Aid Fund subhead resolves properly."""
    raw = RawExtractedRow(
        source_image="p2.jpg", source_page="2", source_row=10,
        raw_head_number="14", raw_voucher_number="58",
        raw_amount_col1="13245", raw_narration="Collections Paid Seminarians aid fund"
    )
    aligned = AlignedRow(
        raw_row=raw,
        aligned_head_number="14",
        aligned_voucher_number="58",
        aligned_amount_col1="13245",
        aligned_narration_raw="Collections Paid Seminarians aid fund"
    )
    res = engine.map_transaction(aligned)

    assert res.canonical_head_id == "GEN_HEAD_ORDER_FROM_DIOCESE"
    assert res.canonical_subhead_id == "GEN_SUB_DIOCESE_SEMINARIAN"
    assert res.subhead_status == SubheadStatus.RESOLVED
    assert res.website_head_value == "143"
    assert res.website_subhead_value == "467"


def test_bank_institution_separation_kappad_vs_meenachil(engine):
    """Test Phase 1 & 3: SIB Kappad and Meenachil East Urban Bank resolve to distinct heads & selectors."""
    # SIB Kappad
    raw_kappad = RawExtractedRow(
        source_image="p.jpg", source_page="1", source_row=1,
        raw_head_number="4", raw_amount_col1="50000", raw_narration="South Indian Bank Kappad FD close"
    )
    res_kappad = engine.map_transaction(AlignedRow(raw_row=raw_kappad, aligned_head_number="4", aligned_amount_col1="50000"))
    assert res_kappad.canonical_head_id == "GEN_HEAD_BANK_SIB_KAPPAD"
    assert res_kappad.website_head_value == "4949"

    # Meenachil East Urban Bank
    raw_meenachil = RawExtractedRow(
        source_image="p.jpg", source_page="1", source_row=2,
        raw_head_number=None, raw_amount_col1="36000", raw_narration="S.B. withdrawal Meenachil East Bank"
    )
    res_meenachil = engine.map_transaction(AlignedRow(raw_row=raw_meenachil, aligned_narration_raw="S.B. withdrawal Meenachil East Bank", aligned_amount_col1="36000"))
    assert res_meenachil.canonical_head_id == "GEN_HEAD_BANK_MEENACHIL_EAST"
    assert res_meenachil.website_head_value == "4948"
    assert res_meenachil.canonical_head_id != res_kappad.canonical_head_id


def test_unicode_and_zero_width_character_normalization(engine):
    """Test Level 2: Text with zero-width spaces (\u200b) and punctuation normalizes cleanly."""
    dirty_text = "വൈദ്യുതി\u200b ബിൽ / KSEB - Charges..."
    cleaned = engine.clean_text(dirty_text)
    assert "\u200b" not in cleaned
    assert "/" not in cleaned
    assert "വൈദ്യുതി ബിൽ kseb charges" == cleaned


def test_unresolved_candidate_handling(engine):
    """Test Level 5: Transaction with zero matching signals routes safely to UNRESOLVED without guessing."""
    raw = RawExtractedRow(
        source_image="p.jpg", source_page="1", source_row=99,
        raw_head_number=None, raw_voucher_number="999",
        raw_amount_col2="1234", raw_narration="xyz obscure obscure"
    )
    aligned = AlignedRow(raw_row=raw, aligned_amount_col2="1234", aligned_narration_raw="xyz obscure obscure")
    res = engine.map_transaction(aligned)

    assert res.mapping_status == MappingStatus.UNRESOLVED
    assert res.canonical_head_id is None
    assert res.confidence_score == 0.0
    assert res.needs_human_review is True
    assert res.unresolved_reason == "INSUFFICIENT_TRANSACTION_EVIDENCE"


def test_preservation_of_raw_ocr_data(engine):
    """Test that original raw extraction row is completely unaltered in MappingResult."""
    raw = RawExtractedRow(
        source_image="p.jpg", source_page="1", source_row=10,
        raw_date="31.5.2026", raw_head_number="20", raw_voucher_number="554",
        raw_narration="നിത്യ ചിലവിലേക്ക് സംഭാവന", raw_amount_col1="6000"
    )
    aligned = AlignedRow(raw_row=raw, aligned_head_number="20", aligned_voucher_number="554", aligned_amount_col1="6000")
    res = engine.map_transaction(aligned)

    assert res.raw_head_number == "20"
    assert res.raw_voucher_number == "554"
    assert res.raw_narration == "നിത്യ ചിലവിലേക്ക് സംഭാവന"
    assert aligned.raw_row.raw_date == "31.5.2026"
