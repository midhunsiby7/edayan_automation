"""Unit tests for Level 4 Local LLM Assistant and integration guardrails."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.models import RawExtractedRow
from src.extraction.column_aligner import AlignedRow
from src.reference.canonical_registry import CanonicalRegistry
from src.mapping.hybrid_engine import HybridMappingEngine, MappingStatus, SubheadStatus, MappingMethod
from src.mapping.local_llm_assistant import (
    LocalLLMAssistant,
    LLMClassification,
    InsufficientMemoryError,
    MemorySnapshot
)


@pytest.fixture
def registry():
    ref_path = Path("data/reference/canonical_accounting_reference.json")
    return CanonicalRegistry(ref_path)


@pytest.fixture
def assistant(registry):
    return LocalLLMAssistant(
        registry=registry,
        model_path=Path("dummy_model.gguf"),
        server_path=Path("dummy_server.exe"),
        confidence_threshold=0.80
    )


def test_memory_snapshot_structure(assistant):
    """Verify native memory snapshot returns non-zero RAM and valid fields."""
    snapshot = assistant.get_memory_snapshot()
    assert snapshot.ram_total_gb > 0.0
    assert snapshot.ram_avail_gb > 0.0
    assert 0.0 <= snapshot.ram_percent <= 100.0


def test_clean_json_content(assistant):
    """Verify markdown fences and whitespace are stripped cleanly."""
    raw_md = "```json\n{\n  \"selected_head_id\": \"GEN_HEAD_OFFERINGS_DAILY\",\n  \"confidence\": 0.95\n}\n```"
    cleaned = assistant._clean_json_content(raw_md)
    assert cleaned == '{\n  "selected_head_id": "GEN_HEAD_OFFERINGS_DAILY",\n  "confidence": 0.95\n}'

    plain = '{"selected_head_id": "ABSTAIN", "confidence": 1.0}'
    assert assistant._clean_json_content(plain) == plain


def test_guardrail_rejects_hallucinated_head_id(assistant):
    """Ensure that any head ID not present in CanonicalRegistry is rejected as hallucination."""
    assistant._is_running = True
    assistant._port = 8888

    # Mock response with completely hallucinated head ID
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{
            "message": {
                "content": '{"selected_head_id": "GEN_HEAD_FICTIONAL_EXPENSE", "confidence": 0.99, "reasoning": "Fabricated head"}'
            }
        }]
    }

    with patch("requests.post", return_value=mock_resp):
        res = assistant.classify_transaction(
            direction="PAYMENT",
            narration="Mysterious payment",
            transaction_ref="TEST_01"
        )
        assert res.is_hallucinated is True
        assert res.abstain is True
        assert res.mapping_status == MappingStatus.UNRESOLVED
        assert res.canonical_head_id is None
        assert "not in candidate shortlist" in res.reasoning or "does not exist" in res.reasoning


def test_guardrail_rejects_out_of_shortlist_head_id(assistant):
    """Ensure that a valid head ID which was NOT in the presented candidate shortlist is rejected."""
    assistant._is_running = True
    assistant._port = 8888

    # Return a real head (GEN_HEAD_BANK_INTEREST) for a PAYMENT transaction where it wasn't shortlisted
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{
            "message": {
                "content": '{"selected_head_id": "GEN_HEAD_BANK_INTEREST", "confidence": 0.95, "reasoning": "Unsolicited pick"}'
            }
        }]
    }

    with patch("requests.post", return_value=mock_resp):
        res = assistant.classify_transaction(
            direction="PAYMENT",
            narration="Current bill",
            transaction_ref="TEST_02"
        )
        # GEN_HEAD_BANK_INTEREST is a RECEIPT head, cannot be in PAYMENT shortlist
        assert res.is_hallucinated is True
        assert res.abstain is True
        assert res.mapping_status == MappingStatus.UNRESOLVED


def test_guardrail_abstention_handling(assistant):
    """Ensure explicit ABSTAIN selection sets abstain=True and UNRESOLVED status."""
    assistant._is_running = True
    assistant._port = 8888

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{
            "message": {
                "content": '{"selected_head_id": "ABSTAIN", "confidence": 0.95, "reasoning": "Genuinely ambiguous"}'
            }
        }]
    }

    with patch("requests.post", return_value=mock_resp):
        res = assistant.classify_transaction(
            direction="RECEIPT",
            narration="Unknown incoming cash",
            transaction_ref="TEST_03"
        )
        assert res.abstain is True
        assert res.canonical_head_id is None
        assert res.mapping_status == MappingStatus.UNRESOLVED


def test_guardrail_confidence_threshold_filtering(assistant):
    """Ensure confidence below threshold marks transaction as provisional/unresolved."""
    assistant._is_running = True
    assistant._port = 8888

    # Confidence 0.65 is below the threshold of 0.80
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{
            "message": {
                "content": '{"selected_head_id": "GEN_HEAD_OFFERINGS_DAILY", "confidence": 0.65, "reasoning": "Low confidence guess"}'
            }
        }]
    }

    with patch("requests.post", return_value=mock_resp):
        res = assistant.classify_transaction(
            direction="RECEIPT",
            narration="Donation for Holy Mass",
            transaction_ref="TEST_04"
        )
        assert res.abstain is True
        assert res.mapping_status == MappingStatus.UNRESOLVED
        assert "below threshold" in res.reasoning


def test_subhead_policy_resolution_prohibited(assistant):
    """Ensure head with PROHIBITED policy produces None subhead and VALID_EMPTY."""
    # GEN_HEAD_OFFERINGS_DAILY prohibits subheads
    subhead_id, status = assistant._resolve_subhead_policy(
        "GEN_HEAD_OFFERINGS_DAILY",
        "Donation for Holy Mass"
    )
    assert subhead_id is None
    assert status == SubheadStatus.VALID_EMPTY


def test_subhead_policy_resolution_mandatory_matched(assistant):
    """Ensure head with MANDATORY policy resolves matching subhead from keywords."""
    # GEN_HEAD_ALLOWANCES requires mandatory subhead
    subhead_id, status = assistant._resolve_subhead_policy(
        "GEN_HEAD_ALLOWANCES",
        "Allowance - Rev. Fr Vicar"
    )
    assert subhead_id == "GEN_SUB_ALLOW_VICAR"
    assert status == SubheadStatus.RESOLVED

    subhead_id2, status2 = assistant._resolve_subhead_policy(
        "GEN_HEAD_ALLOWANCES",
        "Allowance - Accountant"
    )
    assert subhead_id2 == "GEN_SUB_ALLOW_ACCT"
    assert status2 == SubheadStatus.RESOLVED


def test_subhead_policy_resolution_mandatory_missing(assistant):
    """Ensure head with MANDATORY policy flags MISSING_MANDATORY when no keyword matches."""
    subhead_id, status = assistant._resolve_subhead_policy(
        "GEN_HEAD_ALLOWANCES",
        "General staff allowance with no role specified"
    )
    assert subhead_id is None
    assert status == SubheadStatus.MISSING_MANDATORY


def test_preflight_memory_safety_check(assistant):
    """Verify InsufficientMemoryError is raised when available RAM is below threshold."""
    low_ram_snap = MemorySnapshot(
        ram_total_gb=16.0,
        ram_avail_gb=0.5,  # 0.5 GB < 1.0 GB threshold
        ram_used_gb=15.5,
        ram_percent=96.8
    )
    with patch.object(assistant, "get_memory_snapshot", return_value=low_ram_snap):
        with patch("pathlib.Path.is_file", return_value=True):
            with pytest.raises(InsufficientMemoryError) as exc_info:
                assistant.check_preflight()
            assert "Insufficient RAM" in str(exc_info.value)


def test_hybrid_engine_map_batch_with_llm(registry):
    """Verify HybridMappingEngine.map_batch integrates Level 4 assistant seamlessly."""
    engine = HybridMappingEngine(registry)

    # Construct an aligned row that fails Levels 1-3 (unresolved)
    raw = RawExtractedRow(
        source_image="test.jpg",
        source_page="1",
        source_row=1,
        raw_head_number=None,
        raw_voucher_number="10",
        raw_narration="Donation for Holy Mass",
        raw_amount_col1="500",
        raw_amount_col2=None
    )
    aligned = AlignedRow(
        raw_row=raw,
        aligned_head_number=None,
        aligned_voucher_number="10",
        aligned_narration_raw="Donation for Holy Mass",
        aligned_amount_col1="500",
        aligned_amount_col2=None
    )

    # Mock assistant that resolves it
    mock_assistant = MagicMock()
    mock_assistant.classify_batch.return_value = [
        LLMClassification(
            transaction_ref="TX_1_1",
            canonical_head_id="GEN_HEAD_OFFERINGS_DAILY",
            canonical_subhead_id=None,
            confidence=0.95,
            reasoning="Routine daily offering",
            abstain=False,
            mapping_status=MappingStatus.RESOLVED,
            subhead_status=SubheadStatus.VALID_EMPTY,
            candidate_shortlist=["GEN_HEAD_OFFERINGS_DAILY", "ABSTAIN"]
        )
    ]

    results = engine.map_batch(
        aligned_rows=[aligned],
        financial_year="2026-2027",
        enable_llm=True,
        llm_assistant=mock_assistant
    )

    assert len(results) == 1
    res = results[0]
    assert res.mapping_status == MappingStatus.SUGGESTED
    assert res.mapping_method == MappingMethod.LLM_ASSISTANT
    assert res.canonical_head_id == "GEN_HEAD_OFFERINGS_DAILY"
    assert res.canonical_subhead_id is None
    assert res.subhead_status == SubheadStatus.VALID_EMPTY
    assert res.confidence_score == 0.95
    assert "Level 4 LLM Assistant" in res.evidence
