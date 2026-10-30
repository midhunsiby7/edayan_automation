import json
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from src.app.main import app
from src.mapping.review_manager import ReviewManager, ReviewItem, ReviewAction, MappingStatus
from src.reference.canonical_registry import CanonicalRegistry
from src.mapping.hybrid_engine import MappingMethod


@pytest.fixture
def test_registry():
    return CanonicalRegistry(Path("data/reference/canonical_accounting_reference.json"))


@pytest.fixture
def isolated_review_setup(tmp_path, monkeypatch):
    """Sets up an isolated month environment without touching May 2026 data."""
    test_month = "2026-99"
    month_data_dir = tmp_path / "data"
    month_dir = month_data_dir / "output" / test_month
    month_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = month_dir / "intake"
    staging_dir.mkdir(parents=True, exist_ok=True)

    # Monkeypatch DATA_DIR in main
    monkeypatch.setattr("src.app.main.DATA_DIR", month_data_dir)

    # Initial mock review items
    item1 = ReviewItem(
        transaction_id="TX_TEST_001",
        source_image="test_page1.jpg",
        source_page="1",
        source_row=1,
        raw_head_number="12",
        raw_voucher_number="V101",
        voucher_number="V101",
        raw_date="2026-05-01",
        date="2026-05-01",
        raw_narration="Candle counter sale",
        english_narration="Candle offerings",
        direction="RECEIPT",
        amount=1500.0,
        mapping_status=MappingStatus.PROVISIONAL,
        mapping_method=MappingMethod.CONTEXTUAL_RULE,
        canonical_head_id="GEN_HEAD_CANDLE_OFFERINGS",
        canonical_head_name="Candle Offerings(C)",
        canonical_subhead_id=None,
        canonical_subhead_name=None,
        confidence_score=0.75,
        evidence="Rule matched",
        needs_human_review=True,
    )

    item2 = ReviewItem(
        transaction_id="TX_TEST_002",
        source_image="test_page1.jpg",
        source_page="1",
        source_row=2,
        raw_head_number="52",
        raw_voucher_number="V102",
        voucher_number="V102",
        raw_date="2026-05-02",
        date="2026-05-02",
        raw_narration="Fr. Vicar monthly allowance",
        english_narration="Priest allowance",
        direction="PAYMENT",
        amount=5000.0,
        mapping_status=MappingStatus.SUGGESTED,
        mapping_method=MappingMethod.EXACT_FOLIO,
        canonical_head_id="GEN_HEAD_ALLOWANCES",
        canonical_head_name="Allowances",
        canonical_subhead_id="GEN_SUB_ALLOW_VICAR",
        canonical_subhead_name="Rev. Fr. Vicar",
        confidence_score=0.95,
        evidence="Folio 52 matched",
        needs_human_review=True,
    )

    state_data = {
        "month": test_month,
        "updated_at": "2026-05-02T10:00:00Z",
        "items": [item1.model_dump(), item2.model_dump()],
        "groups": [
            {
                "group_key": "grp_allowances",
                "canonical_head_id": "GEN_HEAD_ALLOWANCES",
                "suggested_head_id": "GEN_HEAD_ALLOWANCES",
                "suggested_head_name": "Allowances",
                "sample_narrations": ["Fr. Vicar monthly allowance"],
                "transaction_count": 1,
                "total_amount": 5000.0,
                "transaction_ids": ["TX_TEST_002"]
            }
        ]
    }
    state_file = month_dir / "review_state.json"
    state_file.write_text(json.dumps(state_data, indent=2), encoding="utf-8")

    # Also create a dummy approved preflight summary
    preflight_file = staging_dir / f"{test_month}_preflight_summary.json"
    preflight_data = {
        "month": test_month,
        "file_name": "test_approved.xlsx",
        "file_hash_sha256": "fakehash123456",
        "is_valid": True,
        "status": "VALIDATION_PASSED",
        "receipts_count": 1,
        "receipts_total": 1500.0,
        "payments_count": 1,
        "payments_total": 5000.0,
        "net_movement": -3500.0,
        "errors": []
    }
    preflight_file.write_text(json.dumps(preflight_data, indent=2), encoding="utf-8")

    return test_month, state_file, preflight_file


class TestReviewEditing:
    """Test suite for manual review editing capabilities, validations, and UI interactions."""

    def test_apply_edit_preserves_original_extracted_data(self, test_registry):
        manager = ReviewManager(test_registry)
        item = ReviewItem(
            transaction_id="TX_001",
            source_image="pg1.jpg",
            source_page="1",
            source_row=1,
            raw_head_number="10",
            raw_voucher_number="V1",
            voucher_number="V1",
            raw_date="01.05.2026",
            date="2026-05-01",
            raw_narration="Original Malayalam Text",
            direction="RECEIPT",
            amount=500.0,
            mapping_status=MappingStatus.VERIFIED,
            mapping_method=MappingMethod.EXACT_FOLIO,
            canonical_head_id="GEN_HEAD_CANDLE_OFFERINGS",
            canonical_head_name="Candle Offerings(C)",
            confidence_score=1.0,
            evidence="Original evidence",
            needs_human_review=False
        )

        # Apply edit modifying amount, date, narration, and voucher
        edited = manager.apply_edit(
            item=item,
            date="2026-05-02",
            voucher_number="V101",
            amount=650.0,
            canonical_head_id="GEN_HEAD_CANDLE_OFFERINGS",
            canonical_subhead_id=None,
            narration="Updated Candle Sales",
            direction="RECEIPT",
            editor="Accountant"
        )

        # Verify original extraction is untouched
        orig = edited.original_extracted_data
        assert orig is not None
        assert orig["amount"] == 500.0
        assert orig["voucher_number"] == "V1"
        assert orig["date"] == "2026-05-01"
        assert orig["narration"] == "Original Malayalam Text"

        # Verify edited fields updated
        assert edited.amount == 650.0
        assert edited.voucher_number == "V101"
        assert edited.date == "2026-05-02"
        assert edited.raw_narration == "Updated Candle Sales"
        assert edited.needs_human_review is True  # Must remain awaiting human review!
        assert edited.review_action == ReviewAction.EDIT

        # Verify audit history captured all changes
        assert len(edited.audit_history) >= 4
        fields_changed = {a["field_changed"] for a in edited.audit_history}
        assert "amount" in fields_changed
        assert "voucher_number" in fields_changed
        assert "date" in fields_changed
        assert "narration" in fields_changed

    def test_apply_edit_rejects_non_positive_amount(self, test_registry):
        manager = ReviewManager(test_registry)
        item = ReviewItem(
            transaction_id="TX_001",
            source_image="pg1.jpg",
            source_page="1",
            source_row=1,
            direction="RECEIPT",
            amount=100.0,
            mapping_status=MappingStatus.PROVISIONAL,
            mapping_method=MappingMethod.UNRESOLVED,
            canonical_head_id="GEN_HEAD_CANDLE_OFFERINGS",
            confidence_score=0.5,
            evidence="",
        )

        with pytest.raises(ValueError, match="Amount must be a positive number"):
            manager.apply_edit(
                item=item,
                date="2026-05-01",
                voucher_number="1",
                amount=0.0,
                canonical_head_id="GEN_HEAD_CANDLE_OFFERINGS",
                direction="RECEIPT"
            )

        with pytest.raises(ValueError, match="Amount must be a positive number"):
            manager.apply_edit(
                item=item,
                date="2026-05-01",
                voucher_number="1",
                amount=-50.0,
                canonical_head_id="GEN_HEAD_CANDLE_OFFERINGS",
                direction="RECEIPT"
            )

    def test_apply_edit_rejects_invalid_date_format(self, test_registry):
        manager = ReviewManager(test_registry)
        item = ReviewItem(
            transaction_id="TX_001",
            source_image="pg1.jpg",
            source_page="1",
            source_row=1,
            direction="RECEIPT",
            amount=100.0,
            mapping_status=MappingStatus.PROVISIONAL,
            mapping_method=MappingMethod.UNRESOLVED,
            canonical_head_id="GEN_HEAD_CANDLE_OFFERINGS",
            confidence_score=0.5,
            evidence="",
        )

        with pytest.raises(ValueError, match="Invalid date format"):
            manager.apply_edit(
                item=item,
                date="invalid-date",
                voucher_number="1",
                amount=100.0,
                canonical_head_id="GEN_HEAD_CANDLE_OFFERINGS",
                direction="RECEIPT"
            )

    def test_apply_edit_enforces_direction_compatibility(self, test_registry):
        manager = ReviewManager(test_registry)
        item = ReviewItem(
            transaction_id="TX_001",
            source_image="pg1.jpg",
            source_page="1",
            source_row=1,
            direction="RECEIPT",
            amount=100.0,
            mapping_status=MappingStatus.PROVISIONAL,
            mapping_method=MappingMethod.UNRESOLVED,
            canonical_head_id="GEN_HEAD_CANDLE_OFFERINGS",
            confidence_score=0.5,
            evidence="",
        )

        # Allowances is a PAYMENT head, attempting to apply to a RECEIPT transaction
        with pytest.raises(ValueError, match="incompatible with transaction direction 'RECEIPT'"):
            manager.apply_edit(
                item=item,
                date="2026-05-01",
                voucher_number="1",
                amount=100.0,
                canonical_head_id="GEN_HEAD_ALLOWANCES",
                direction="RECEIPT"
            )

    def test_apply_edit_enforces_mandatory_subhead_policy(self, test_registry):
        manager = ReviewManager(test_registry)
        item = ReviewItem(
            transaction_id="TX_001",
            source_image="pg1.jpg",
            source_page="1",
            source_row=1,
            direction="PAYMENT",
            amount=1000.0,
            mapping_status=MappingStatus.PROVISIONAL,
            mapping_method=MappingMethod.UNRESOLVED,
            canonical_head_id="GEN_HEAD_ALLOWANCES",
            confidence_score=0.5,
            evidence="",
        )

        # Missing subhead on mandatory policy head
        with pytest.raises(ValueError, match="requires a mandatory subhead selection"):
            manager.apply_edit(
                item=item,
                date="2026-05-01",
                voucher_number="1",
                amount=1000.0,
                canonical_head_id="GEN_HEAD_ALLOWANCES",
                canonical_subhead_id=None,
                direction="PAYMENT"
            )

        # Valid subhead for head
        edited = manager.apply_edit(
            item=item,
            date="2026-05-01",
            voucher_number="1",
            amount=1000.0,
            canonical_head_id="GEN_HEAD_ALLOWANCES",
            canonical_subhead_id="GEN_SUB_ALLOW_VICAR",
            direction="PAYMENT"
        )
        assert edited.canonical_subhead_id == "GEN_SUB_ALLOW_VICAR"

    def test_apply_edit_enforces_prohibited_subhead_policy(self, test_registry):
        manager = ReviewManager(test_registry)
        item = ReviewItem(
            transaction_id="TX_001",
            source_image="pg1.jpg",
            source_page="1",
            source_row=1,
            direction="RECEIPT",
            amount=1000.0,
            mapping_status=MappingStatus.PROVISIONAL,
            mapping_method=MappingMethod.UNRESOLVED,
            canonical_head_id="GEN_HEAD_CANDLE_OFFERINGS",
            confidence_score=0.5,
            evidence="",
        )

        # Candle Offerings prohibits subheads
        with pytest.raises(ValueError, match="strictly prohibits subheads"):
            manager.apply_edit(
                item=item,
                date="2026-05-01",
                voucher_number="1",
                amount=1000.0,
                canonical_head_id="GEN_HEAD_CANDLE_OFFERINGS",
                canonical_subhead_id="GEN_SUB_ALLOW_VICAR",
                direction="RECEIPT"
            )

    def test_edit_endpoint_persists_and_invalidates_preflight(self, isolated_review_setup):
        test_month, state_file, preflight_file = isolated_review_setup
        client = TestClient(app)

        # Preflight was originally valid
        pref_before = json.loads(preflight_file.read_text(encoding="utf-8"))
        assert pref_before["is_valid"] is True

        payload = {
            "month": test_month,
            "date": "2026-05-05",
            "direction": "RECEIPT",
            "voucher_number": "V999",
            "amount": 2200.50,
            "canonical_head_id": "GEN_HEAD_CANDLE_OFFERINGS",
            "canonical_subhead_id": None,
            "narration": "Corrected Candle Offerings",
            "editor": "Senior Accountant"
        }

        resp = client.post("/api/review/TX_TEST_001/edit", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["transaction_id"] == "TX_TEST_001"
        assert data["item"]["amount"] == 2200.50
        assert data["item"]["needs_human_review"] is True

        # Check state file persisted
        state_after = json.loads(state_file.read_text(encoding="utf-8"))
        saved_item = next(it for it in state_after["items"] if it["transaction_id"] == "TX_TEST_001")
        assert saved_item["amount"] == 2200.50
        assert saved_item["voucher_number"] == "V999"
        assert saved_item["raw_narration"] == "Corrected Candle Offerings"
        assert len(saved_item["audit_history"]) >= 4

        # Crucial Requirement: Preflight must be invalidated!
        pref_after = json.loads(preflight_file.read_text(encoding="utf-8"))
        assert pref_after["is_valid"] is False
        assert pref_after["status"] == "INVALIDATED_BY_DRAFT_EDIT"

    def test_api_review_heads_includes_direction_and_policy(self):
        client = TestClient(app)
        resp = client.get("/api/review/heads")
        assert resp.status_code == 200
        data = resp.json()
        assert "heads" in data
        assert "subheads" in data
        assert len(data["heads"]) == 60
        assert len(data["subheads"]) == 25

        head = data["heads"][0]
        assert "direction" in head
        assert head["direction"] in ("RECEIPT", "PAYMENT", "BOTH")
        assert "policy" in head
        assert head["policy"] in ("MANDATORY", "OPTIONAL", "PROHIBITED")
        assert "allowed_subheads" in head
        assert isinstance(head["allowed_subheads"], list)
