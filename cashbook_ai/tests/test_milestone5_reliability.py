"""Milestone 5 Reliability, Confidence Calibration, and Review Intelligence Tests."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.models import RawExtractedRow
from src.extraction.column_aligner import AlignedRow
from src.reference.canonical_registry import CanonicalRegistry
from src.mapping.hybrid_engine import (
    HybridMappingEngine,
    MappingResult,
    MappingStatus,
    SubheadStatus,
    MappingMethod
)
from src.mapping.candidate_generator import CandidateGenerator
from src.mapping.local_llm_assistant import (
    LocalLLMAssistant,
    LLMClassification,
    InsufficientMemoryError
)
from src.mapping.review_manager import (
    ReviewManager,
    ReviewItem,
    ReviewGroup,
    ReviewAction
)


@pytest.fixture
def registry():
    ref_path = Path("data/reference/canonical_accounting_reference.json")
    return CanonicalRegistry(ref_path)


@pytest.fixture
def engine(registry):
    return HybridMappingEngine(registry)


@pytest.fixture
def review_mgr(registry):
    return ReviewManager(registry)


# ---------------------------------------------------------------------------
# PHASE 3: Mapping Decision Status Hierarchy
# ---------------------------------------------------------------------------

def test_decision_status_hierarchy_level1_is_verified(engine):
    """Level 1 exact folio match must be marked VERIFIED and require no review."""
    raw = RawExtractedRow(source_image="p1.jpg", source_page="1", source_row=1, raw_head_number="20", raw_voucher_number="10", raw_amount_col1="1000")
    aligned = AlignedRow(raw_row=raw, aligned_head_number="20", aligned_voucher_number="10", aligned_amount_col1="1000")
    res = engine.map_transaction(aligned)

    assert res.mapping_method == MappingMethod.EXACT_FOLIO
    assert res.mapping_status == MappingStatus.VERIFIED
    assert res.needs_human_review is False


def test_decision_status_hierarchy_level2_is_suggested(engine):
    """Level 2 normalized alias must be marked SUGGESTED and require human review."""
    raw = RawExtractedRow(source_image="p1.jpg", source_page="1", source_row=2, raw_narration="Electricity charge", raw_amount_col2="500")
    aligned = AlignedRow(raw_row=raw, aligned_narration_raw="Electricity charge", aligned_amount_col2="500")
    res = engine.map_transaction(aligned)

    assert res.mapping_method == MappingMethod.NORMALIZED_ALIAS
    assert res.mapping_status == MappingStatus.SUGGESTED
    assert res.needs_human_review is True


def test_decision_status_hierarchy_level3_is_provisional(engine):
    """Level 3 contextual rules must be marked PROVISIONAL and require human review."""
    raw = RawExtractedRow(source_image="p1.jpg", source_page="1", source_row=3, raw_narration="Painting wages", raw_amount_col2="1500")
    aligned = AlignedRow(raw_row=raw, aligned_narration_raw="Painting wages", aligned_amount_col2="1500")
    res = engine.map_transaction(aligned)

    assert res.mapping_method == MappingMethod.CONTEXTUAL_RULE
    assert res.mapping_status == MappingStatus.PROVISIONAL
    assert res.needs_human_review is True


def test_decision_status_hierarchy_level4_high_confidence_is_suggested(engine):
    """Level 4 LLM predictions with high confidence (>=0.90) must be SUGGESTED, requiring review."""
    raw = RawExtractedRow(source_image="p1.jpg", source_page="1", source_row=4, raw_narration="Donation for Holy Mass", raw_amount_col1="500")
    aligned = AlignedRow(raw_row=raw, aligned_narration_raw="Donation for Holy Mass", aligned_amount_col1="500")

    mock_llm = MagicMock()
    mock_llm.classify_batch.return_value = [
        LLMClassification(
            transaction_ref="TX_1_4",
            canonical_head_id="GEN_HEAD_OFFERINGS_DAILY",
            canonical_subhead_id=None,
            confidence=0.95,
            reasoning="Routine daily offering",
            abstain=False,
            subhead_status=SubheadStatus.VALID_EMPTY
        )
    ]

    results = engine.map_batch([aligned], enable_llm=True, llm_assistant=mock_llm)
    assert len(results) == 1
    assert results[0].mapping_status == MappingStatus.SUGGESTED
    assert results[0].needs_human_review is True


def test_decision_status_hierarchy_level4_moderate_confidence_is_provisional(engine):
    """Level 4 LLM predictions with moderate confidence (0.75 <= c < 0.90) must be PROVISIONAL."""
    raw = RawExtractedRow(source_image="p1.jpg", source_page="1", source_row=5, raw_narration="Special liturgical item", raw_amount_col2="700")
    aligned = AlignedRow(raw_row=raw, aligned_narration_raw="Special liturgical item", aligned_amount_col2="700")

    mock_llm = MagicMock()
    mock_llm.classify_batch.return_value = [
        LLMClassification(
            transaction_ref="TX_1_5",
            canonical_head_id="GEN_HEAD_CHURCH_ARTICLES_VEST",
            canonical_subhead_id=None,
            confidence=0.82,
            reasoning="Plausible liturgical fixture",
            abstain=False,
            subhead_status=SubheadStatus.VALID_EMPTY
        )
    ]

    results = engine.map_batch([aligned], enable_llm=True, llm_assistant=mock_llm)
    assert len(results) == 1
    assert results[0].mapping_status == MappingStatus.PROVISIONAL
    assert results[0].needs_human_review is True


# ---------------------------------------------------------------------------
# PHASE 4: Confidence-Gate and Calibration Behavior
# ---------------------------------------------------------------------------

def test_confidence_gate_rejects_below_threshold(registry):
    """Verify that predictions below the confidence gate (0.75) are marked UNRESOLVED."""
    assistant = LocalLLMAssistant(registry, confidence_threshold=0.75)
    assistant._is_running = True
    assistant._port = 8888

    # Mock response returning a valid candidate (GEN_HEAD_SUNDAY_COLLECTION_INC) with low confidence
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{
            "message": {
                "content": '{"selected_head_id": "GEN_HEAD_SUNDAY_COLLECTION_INC", "confidence": 0.60, "reasoning": "Uncertain guess"}'
            }
        }]
    }

    with patch("requests.post", return_value=mock_resp):
        res = assistant.classify_transaction(
            direction="RECEIPT",
            narration="Sunday collection from parish",
            transaction_ref="TEST_GATE"
        )
        assert res.abstain is True
        assert res.mapping_status == MappingStatus.UNRESOLVED
        assert "below threshold" in res.reasoning


# ---------------------------------------------------------------------------
# PHASE 5: Candidate Generator Institution Separation & Recall
# ---------------------------------------------------------------------------

def test_candidate_generator_institution_deduplication(registry):
    """Ensure generic payments mentioning 'bank' or 'interest' do not monopolize shortlist with 6 bank heads."""
    cg = CandidateGenerator(registry)
    candidates = cg.generate_shortlist(
        direction="PAYMENT",
        narration="Interest income (25 Oct - 26 March)",
        max_candidates=5,
        include_abstain=True
    )
    cand_ids = [c.canonical_head_id for c in candidates]

    # Verify that generic bank contra heads are capped so GEN_HEAD_MISC_EXPENSES is present
    bank_contra = [cid for cid in cand_ids if cid.startswith("GEN_HEAD_BANK_") and cid not in ("GEN_HEAD_BANK_INTEREST", "GEN_HEAD_BANK_CHARGES")]
    assert len(bank_contra) <= 2
    assert "GEN_HEAD_MISC_EXPENSES" in cand_ids
    assert "ABSTAIN" in cand_ids


# ---------------------------------------------------------------------------
# PHASE 6: Subhead Quality and Policy Enforcement
# ---------------------------------------------------------------------------

def test_subhead_quality_mandatory_missing_subhead_flagged(engine):
    """Verify that a head with MANDATORY subhead policy flags MISSING_MANDATORY when no role is found."""
    head = engine.registry.get_head("GEN_HEAD_ALLOWANCES")
    sub, status, err = engine._resolve_subhead_policy(head, None, "Generic Unspecified Staff Payment")

    assert sub is None
    assert status == SubheadStatus.MISSING_MANDATORY
    assert "mandatory subhead" in err


def test_subhead_quality_prohibited_subhead_intentional_empty(engine):
    """Verify that a head with PROHIBITED policy produces None subhead and VALID_EMPTY status."""
    # Head 20 (Daily Expense Offerings) strictly prohibits subheads
    raw = RawExtractedRow(source_image="p1.jpg", source_page="1", source_row=7, raw_head_number="20", raw_voucher_number="21", raw_amount_col1="3000")
    aligned = AlignedRow(raw_row=raw, aligned_head_number="20", aligned_voucher_number="21", aligned_amount_col1="3000")
    res = engine.map_transaction(aligned)

    assert res.canonical_head_id == "GEN_HEAD_OFFERINGS_DAILY"
    assert res.canonical_subhead_id is None
    assert res.subhead_status == SubheadStatus.VALID_EMPTY
    assert res.mapping_status == MappingStatus.VERIFIED


# ---------------------------------------------------------------------------
# PHASE 8: Review Intelligence & Alias Lifecycle
# ---------------------------------------------------------------------------

def test_review_manager_build_and_group(review_mgr, engine):
    """Verify ReviewManager builds ReviewItems and clusters identical narrations."""
    items = []
    # Create 3 rows with identical narration 'Donation for Holy Mass'
    for i in range(3):
        raw = RawExtractedRow(source_image="p1.jpg", source_page="1", source_row=i+1, raw_narration="Donation for Holy Mass", raw_amount_col1="500")
        aligned = AlignedRow(raw_row=raw, aligned_narration_raw="Donation for Holy Mass", aligned_amount_col1="500")
        res = engine.map_transaction(aligned, transaction_id=f"TX_MASS_{i}")
        res.mapping_status = MappingStatus.SUGGESTED
        res.canonical_head_id = "GEN_HEAD_OFFERINGS_DAILY"
        res.needs_human_review = True
        items.append(review_mgr.build_review_item(res, aligned_row=aligned))

    groups = review_mgr.group_similar_transactions(items)
    assert len(groups) == 1
    assert groups[0].group_type == "IDENTICAL_NARRATION"
    assert groups[0].transaction_count == 3
    assert groups[0].total_amount == 1500.0


def test_review_manager_user_decisions(review_mgr, engine):
    """Verify CONFIRM, REJECT, and OVERRIDE review status transitions."""
    raw = RawExtractedRow(source_image="p1.jpg", source_page="1", source_row=1, raw_narration="Current bill (Church)", raw_amount_col2="1200")
    aligned = AlignedRow(raw_row=raw, aligned_narration_raw="Current bill (Church)", aligned_amount_col2="1200")
    res = engine.map_transaction(aligned, transaction_id="TX_REV_01")
    res.mapping_status = MappingStatus.SUGGESTED
    res.canonical_head_id = "GEN_HEAD_ELECTRICITY"
    res.needs_human_review = True

    item = review_mgr.build_review_item(res, aligned_row=aligned)
    assert item.mapping_status == MappingStatus.SUGGESTED

    # 1. User confirms
    confirmed_item = review_mgr.apply_user_decision(
        item=item,
        action=ReviewAction.CONFIRM,
        reviewed_by="Fr. Vicar",
        timestamp="2026-10-30T21:00:00Z"
    )
    assert confirmed_item.mapping_status == MappingStatus.VERIFIED
    assert confirmed_item.needs_human_review is False
    assert confirmed_item.confirmed_head_id == "GEN_HEAD_ELECTRICITY"

    # 2. User overrides
    override_item = review_mgr.apply_user_decision(
        item=item,
        action=ReviewAction.OVERRIDE,
        reviewed_by="Auditor",
        timestamp="2026-10-30T21:05:00Z",
        confirmed_head_id="GEN_HEAD_MISC_EXPENSES",
        notes="Reclassified to misc"
    )
    assert override_item.mapping_status == MappingStatus.VERIFIED
    assert override_item.canonical_head_id == "GEN_HEAD_MISC_EXPENSES"

    # 3. User rejects
    rejected_item = review_mgr.apply_user_decision(
        item=item,
        action=ReviewAction.REJECT,
        reviewed_by="Auditor",
        timestamp="2026-10-30T21:10:00Z"
    )
    assert rejected_item.mapping_status == MappingStatus.UNRESOLVED
    assert rejected_item.canonical_head_id is None
    assert rejected_item.needs_human_review is True


def test_confirmed_alias_persistence_only_on_explicit_human_action(review_mgr, engine):
    """Verify that aliases are persisted into CanonicalRegistry ONLY when explicitly requested by human."""
    raw = RawExtractedRow(source_image="p1.jpg", source_page="1", source_row=1, raw_narration="Special Custom Donation", raw_amount_col1="1000")
    aligned = AlignedRow(raw_row=raw, aligned_narration_raw="Special Custom Donation", aligned_amount_col1="1000")
    res = engine.map_transaction(aligned, transaction_id="TX_ALIAS_TEST")
    res.mapping_status = MappingStatus.SUGGESTED
    res.canonical_head_id = "GEN_HEAD_OFFERINGS_DAILY"

    item = review_mgr.build_review_item(res, aligned_row=aligned)
    initial_alias_count = len(review_mgr.registry.aliases)

    # 1. Normal confirmation without promote_to_alias -> no new alias
    review_mgr.apply_user_decision(
        item=item,
        action=ReviewAction.CONFIRM,
        reviewed_by="Auditor",
        timestamp="2026-10-30T21:00:00Z",
        promote_to_alias=False
    )
    assert len(review_mgr.registry.aliases) == initial_alias_count

    # 2. Explicit confirmation WITH promote_to_alias -> new approved alias added
    review_mgr.apply_user_decision(
        item=item,
        action=ReviewAction.CONFIRM,
        reviewed_by="Auditor",
        timestamp="2026-10-30T21:05:00Z",
        promote_to_alias=True
    )
    assert len(review_mgr.registry.aliases) == initial_alias_count + 1
    new_alias = review_mgr.registry.aliases[-1]
    assert new_alias.alias_text == "Special Custom Donation"
    assert new_alias.target_id == "GEN_HEAD_OFFERINGS_DAILY"


# ---------------------------------------------------------------------------
# PHASE 7: Hardware Reliability & Error Recovery
# ---------------------------------------------------------------------------

def test_hardware_reliability_graceful_memory_fallback(engine):
    """Ensure that insufficient memory triggers graceful fallback to UNRESOLVED without crashing."""
    raw = RawExtractedRow(source_image="p1.jpg", source_page="1", source_row=1, raw_narration="Unknown item", raw_amount_col2="200")
    aligned = AlignedRow(raw_row=raw, aligned_narration_raw="Unknown item", aligned_amount_col2="200")

    mock_assistant = MagicMock()
    mock_assistant.classify_batch.side_effect = InsufficientMemoryError("Available RAM 0.5 GB below safety threshold")

    results = engine.map_batch([aligned], enable_llm=True, llm_assistant=mock_assistant)
    assert len(results) == 1
    assert results[0].mapping_status == MappingStatus.UNRESOLVED
    assert "SKIPPED_LLM_INSUFFICIENT_MEMORY" in results[0].unresolved_reason


def test_hardware_reliability_error_safe_model_teardown(registry):
    """Ensure that exceptions during batch processing guarantee assistant.stop() is executed."""
    assistant = LocalLLMAssistant(registry)
    assistant.start = MagicMock()
    assistant.stop = MagicMock()
    assistant.classify_transaction = MagicMock(side_effect=RuntimeError("Subprocess crashed"))

    with pytest.raises(RuntimeError):
        assistant.classify_batch([{"direction": "RECEIPT", "narration": "Test"}])

    assistant.stop.assert_called_once()
