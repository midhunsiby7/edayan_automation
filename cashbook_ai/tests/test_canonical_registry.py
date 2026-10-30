import pytest
from pathlib import Path
from src.reference.canonical_registry import (
    CanonicalRegistry,
    SubheadPolicy,
    CanonicalHead,
    CanonicalSubhead,
)

REGISTRY_PATH = Path("data/reference/canonical_accounting_reference.json")


@pytest.fixture
def registry():
    return CanonicalRegistry(REGISTRY_PATH)


def test_registry_loads_and_passes_integrity_check(registry):
    """Test that canonical registry loads without error and passes all integrity checks."""
    assert len(registry.heads) >= 50
    assert len(registry.subheads) >= 20
    assert len(registry.folios) >= 50
    assert len(registry.aliases) >= 20
    assert len(registry.website_heads) >= 50

    errors = registry.validate_registry_integrity()
    assert errors == [], f"Registry integrity errors found: {errors}"


def test_bank_separation_integrity(registry):
    """Rule: South Indian Bank Kappad and Meenachil East Urban Co-operative Bank must remain distinct."""
    kappad = registry.get_head("GEN_HEAD_BANK_SIB_KAPPAD")
    meenachil = registry.get_head("GEN_HEAD_BANK_MEENACHIL_EAST")
    pala = registry.get_head("GEN_HEAD_BANK_SIB_PALA")

    assert kappad is not None
    assert meenachil is not None
    assert pala is not None

    # Distinct IDs and names
    assert kappad.canonical_head_id != meenachil.canonical_head_id
    assert kappad.canonical_name == "The South Indian Bank Kappad"
    assert meenachil.canonical_name == "Meenachil East urban cooperative bank"
    assert pala.canonical_name == "The South Indian Bank Pala"

    # Distinct website selectors
    web_kappad = registry.get_website_head_for_canonical("GEN_HEAD_BANK_SIB_KAPPAD", "RECEIPT")
    web_meenachil = registry.get_website_head_for_canonical("GEN_HEAD_BANK_MEENACHIL_EAST", "RECEIPT")
    web_pala = registry.get_website_head_for_canonical("GEN_HEAD_BANK_SIB_PALA", "RECEIPT")

    assert web_kappad.website_head_value == "4949"
    assert web_meenachil.website_head_value == "4948"
    assert web_pala.website_head_value == "4950"


def test_historical_folio_resolution(registry):
    """Test resolving legacy folio numbers to canonical heads and subheads."""
    # Folio 20 (General Receipts) -> Offerings for Daily Expense
    res_20 = registry.resolve_by_folio(20, "RECEIPT")
    assert res_20 is not None
    head_20, sub_20 = res_20
    assert head_20.canonical_head_id == "GEN_HEAD_OFFERINGS_DAILY"
    assert sub_20 is None

    # Folio 52 (Allowances Vicar) -> Head Allowances, Subhead Rev. Fr. Vicar
    res_52 = registry.resolve_by_folio(52, "PAYMENT")
    assert res_52 is not None
    head_52, sub_52 = res_52
    assert head_52.canonical_head_id == "GEN_HEAD_ALLOWANCES"
    assert sub_52 is not None
    assert sub_52.canonical_subhead_id == "GEN_SUB_ALLOW_VICAR"
    assert sub_52.canonical_name == "Rev. Fr. Vicar"

    # Folio 53 (Allowances Accountant) -> Head Allowances, Subhead Accountant
    res_53 = registry.resolve_by_folio(53, "PAYMENT")
    assert res_53 is not None
    head_53, sub_53 = res_53
    assert head_53.canonical_head_id == "GEN_HEAD_ALLOWANCES"
    assert sub_53 is not None
    assert sub_53.canonical_subhead_id == "GEN_SUB_ALLOW_ACCT"
    assert sub_53.canonical_name == "Accountant"

    # Folio 4 (Bank F.D. withdrawal) -> South Indian Bank Kappad (Receipt)
    res_4 = registry.resolve_by_folio(4, "RECEIPT")
    assert res_4 is not None
    head_4, _ = res_4
    assert head_4.canonical_head_id == "GEN_HEAD_BANK_SIB_KAPPAD"


def test_approved_alias_resolution(registry):
    """Test resolving handwritten Malayalam and English text against approved aliases."""
    # Malayalam alias for Daily Expenses
    res_ml = registry.resolve_by_alias("നിത്യ ചിലവിലേക്ക് സംഭാവന", "RECEIPT")
    assert res_ml is not None
    target_id, target_type, conf = res_ml
    assert target_id == "GEN_HEAD_OFFERINGS_DAILY"
    assert target_type == "HEAD"
    assert conf == 1.0

    # Malayalam alias for Auction proceeds / Nercha
    res_auction = registry.resolve_by_alias("ലേലം വക", "RECEIPT")
    assert res_auction is not None
    assert res_auction[0] == "GEN_HEAD_OFFERINGS_NERCHA_AUCTION"

    # Malayalam alias for Chapel collections
    res_chapel = registry.resolve_by_alias("പുവാർകോട് കുരിശുപള്ളി", "RECEIPT")
    assert res_chapel is not None
    assert res_chapel[0] == "GEN_HEAD_OFFERINGS_NERCHA_AUCTION"

    # Subhead alias (Malayalam)
    res_sub_ml = registry.resolve_by_alias("വികാരിയച്ചൻ അലവൻസ്", "PAYMENT")
    assert res_sub_ml is not None
    assert res_sub_ml[0] == "GEN_SUB_ALLOW_VICAR"
    assert res_sub_ml[1] == "SUBHEAD"


def test_subhead_policy_validation(registry):
    """Test strict subhead policy enforcement (MANDATORY, OPTIONAL, PROHIBITED)."""
    # 1. MANDATORY Policy: Allowances
    # Valid: with Vicar subhead
    valid, issues = registry.validate_transaction("GEN_HEAD_ALLOWANCES", "GEN_SUB_ALLOW_VICAR", "PAYMENT")
    assert valid is True
    assert issues == []

    # Invalid: missing subhead
    valid, issues = registry.validate_transaction("GEN_HEAD_ALLOWANCES", None, "PAYMENT")
    assert valid is False
    assert any("SUBHEAD_REQUIRED" in i for i in issues)

    # Invalid: wrong parent subhead (Holy Land belongs to Order from Diocese)
    valid, issues = registry.validate_transaction("GEN_HEAD_ALLOWANCES", "GEN_SUB_DIOCESE_HOLY_LAND", "PAYMENT")
    assert valid is False
    assert any("INVALID_SUBHEAD_PARENT" in i for i in issues)

    # 2. PROHIBITED Policy: Electricity Charges
    # Valid: without subhead
    valid, issues = registry.validate_transaction("GEN_HEAD_ELECTRICITY", None, "PAYMENT")
    assert valid is True

    # Invalid: with subhead
    valid, issues = registry.validate_transaction("GEN_HEAD_ELECTRICITY", "GEN_SUB_ALLOW_VICAR", "PAYMENT")
    assert valid is False
    assert any("SUBHEAD_PROHIBITED" in i for i in issues)


def test_direction_and_contra_validation(registry):
    """Test direction matching and contra-account bidirectional handling."""
    # Normal head: Electricity is PAYMENT only
    valid, issues = registry.validate_transaction("GEN_HEAD_ELECTRICITY", None, "RECEIPT")
    assert valid is False
    assert any("DIRECTION_MISMATCH" in i for i in issues)

    # Bank contra head: Allows both RECEIPT (withdrawal) and PAYMENT (deposit)
    valid_rec, issues_rec = registry.validate_transaction("GEN_HEAD_BANK_SIB_KAPPAD", None, "RECEIPT")
    assert valid_rec is True

    valid_pay, issues_pay = registry.validate_transaction("GEN_HEAD_BANK_SIB_KAPPAD", None, "PAYMENT")
    assert valid_pay is True
