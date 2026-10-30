"""Unit tests for Level 4 Candidate Shortlist Generator."""

import pytest
from pathlib import Path

from src.reference.canonical_registry import CanonicalRegistry
from src.mapping.candidate_generator import CandidateGenerator, CandidateOption


@pytest.fixture
def registry():
    ref_path = Path("data/reference/canonical_accounting_reference.json")
    return CanonicalRegistry(ref_path)


@pytest.fixture
def generator(registry):
    return CandidateGenerator(registry)


def test_candidate_generator_bounded_length(generator):
    """Ensure shortlist never exceeds max_candidates + abstain option."""
    for max_k in [3, 4, 5, 6]:
        candidates = generator.generate_shortlist(
            direction="RECEIPT",
            narration="Donation for Holy Mass",
            max_candidates=max_k,
            include_abstain=True
        )
        assert len(candidates) <= max_k + 1
        assert candidates[-1].canonical_head_id == "ABSTAIN"


def test_candidate_generator_direction_filtering(generator):
    """Ensure candidates strictly match transaction direction or BOTH."""
    receipt_candidates = generator.generate_shortlist(
        direction="RECEIPT",
        narration="Sunday collection",
        max_candidates=5,
        include_abstain=False
    )
    for c in receipt_candidates:
        head = generator.registry.get_head(c.canonical_head_id)
        assert head is not None
        assert head.primary_direction in ("RECEIPT", "BOTH"), (
            f"Candidate {c.canonical_head_id} has direction {head.primary_direction}, expected RECEIPT or BOTH"
        )

    payment_candidates = generator.generate_shortlist(
        direction="PAYMENT",
        narration="Electricity bill",
        max_candidates=5,
        include_abstain=False
    )
    for c in payment_candidates:
        head = generator.registry.get_head(c.canonical_head_id)
        assert head is not None
        assert head.primary_direction in ("PAYMENT", "BOTH"), (
            f"Candidate {c.canonical_head_id} has direction {head.primary_direction}, expected PAYMENT or BOTH"
        )


def test_candidate_generator_abstain_option(generator):
    """Ensure ABSTAIN option is properly formatted with zero score and prohibited policy."""
    candidates = generator.generate_shortlist(
        direction="RECEIPT",
        narration="Unclear transaction",
        include_abstain=True
    )
    abstain_opt = [c for c in candidates if c.canonical_head_id == "ABSTAIN"]
    assert len(abstain_opt) == 1
    assert abstain_opt[0].score == 0.0
    assert "Abstain" in abstain_opt[0].canonical_name


def test_candidate_generator_recall_on_core_transactions(generator):
    """Verify that expected canonical heads are included in the shortlist for core narrations."""
    test_cases = [
        ("RECEIPT", "Donation for Holy Mass", "GEN_HEAD_OFFERINGS_DAILY"),
        ("RECEIPT", "Catechism", "GEN_HEAD_SUNDAY_COLLECTION_INC"),
        ("RECEIPT", "As per order (Holy land)", "GEN_HEAD_ORDER_FROM_DIOCESE"),
        ("RECEIPT", "Shop rent", "GEN_HEAD_RENT_BUILDINGS"),
        ("PAYMENT", "Allowance - Rev. Fr Vicar", "GEN_HEAD_ALLOWANCES"),
        ("PAYMENT", "Allowance - Accountant", "GEN_HEAD_ALLOWANCES"),
        ("PAYMENT", "Advance to Agriculture Account", "GEN_HEAD_ADV_TO_AGRI"),
        ("PAYMENT", "Current bill (Church)", "GEN_HEAD_ELECTRICITY"),
        ("PAYMENT", "School Maintenance", "GEN_HEAD_SCHOOL_INST_EXP"),
        ("PAYMENT", "Rent Security (James)", "GEN_HEAD_RENT_SECURITY"),
    ]

    for direction, narration, expected_head in test_cases:
        candidates = generator.generate_shortlist(
            direction=direction,
            narration=narration,
            max_candidates=5,
            include_abstain=True
        )
        cand_ids = [c.canonical_head_id for c in candidates]
        assert expected_head in cand_ids, (
            f"Expected {expected_head} for '{narration}' ({direction}), but got {cand_ids}"
        )


def test_candidate_generator_fallback_padding(generator):
    """Ensure empty or nonsense narrations still produce a valid padded shortlist."""
    candidates = generator.generate_shortlist(
        direction="PAYMENT",
        narration="xyz123nonsense456",
        max_candidates=4,
        include_abstain=True
    )
    assert len(candidates) >= 4
    # All candidates except ABSTAIN must exist in CanonicalRegistry
    for c in candidates:
        if c.canonical_head_id != "ABSTAIN":
            assert generator.registry.get_head(c.canonical_head_id) is not None
