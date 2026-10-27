import json
from pathlib import Path
import pytest
from src.models import (
    AccountSet,
    AccountingHead,
    MasterSourceType,
    RawExtractedRow,
    RecordType,
    TransactionType
)
from src.validation.accounting_rules import AccountingRuleEngine
from src.validation.head_master import HeadMasterRepository

@pytest.fixture
def temp_head_master(tmp_path):
    # Create temp active master
    active_path = tmp_path / "accounting_heads_general_verified.csv"
    import csv
    with open(active_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "account_set", "accounting_head_number", "extracted_head_name",
            "human_final_name", "review_status", "human_comment", "accounting_head_name",
            "normalized_accounting_head_name", "source_image", "source_page", "source_row",
            "extraction_confidence", "review_required", "source_type"
        ])
        writer.writerow([
            "GENERAL", "52", "Allowances : Rev. Fr. Vicar",
            "Allowances", "CORRECTED", "", "Allowances",
            "", "", "", "", "1.0", "False", "MANUAL_VERIFIED"
        ])
        writer.writerow([
            "GENERAL", "14", "Extracted Electricity Charges",
            "Corrected Electricity Charges", "CORRECTED", "", "Corrected Electricity Charges",
            "", "", "", "", "1.0", "False", "MANUAL_VERIFIED"
        ])
        
    # Create temp subhead rules
    rules_path = tmp_path / "data" / "reference"
    rules_path.mkdir(parents=True)
    rule_file = rules_path / "subhead_rules_general.json"
    rules = {
        "embedded_subheads": [
            {"accounting_head_number": "52", "rule_type": "extract_after_colon"}
        ],
        "narration_subheads": [
            {"accounting_head_number": "14", "rule_type": "extract_from_narration"}
        ]
    }
    with open(rule_file, "w") as f:
        json.dump(rules, f)
        
    # Monkeypatch the rules path in HeadMasterRepository
    import src.validation.head_master
    original_path = Path("data/reference/subhead_rules_general.json")
    def mock_load(self):
        if rule_file.exists():
            with open(rule_file, "r") as f:
                self.subhead_rules = json.load(f)
    
    # We will just patch the instance
    repo = HeadMasterRepository(active_path)
    repo._load_subhead_rules = mock_load.__get__(repo)
    repo._load_subhead_rules()
    
    return repo

def test_embedded_subhead_extraction(temp_head_master):
    engine = AccountingRuleEngine(head_master_repo=temp_head_master)
    raw = RawExtractedRow(
        source_image="img.jpg",
        source_page="1",
        source_row=1,
        raw_head_number="52",
        raw_voucher_number="123",
        raw_amount_col2="500.00"
    )
    processed = engine.process_row(raw)
    
    assert processed.accounting_head_number == "52"
    assert processed.accounting_head_name == "Allowances"
    assert processed.subhead == "Rev. Fr. Vicar"
    assert temp_head_master.get_extracted_head_name(AccountSet.GENERAL, "52") == "Allowances : Rev. Fr. Vicar"
    assert processed.transaction_type == TransactionType.PAYMENT

def test_narration_subhead_extraction(temp_head_master):
    engine = AccountingRuleEngine(head_master_repo=temp_head_master)
    raw = RawExtractedRow(
        source_image="img.jpg",
        source_page="1",
        source_row=1,
        raw_head_number="14",
        raw_voucher_number="124",
        raw_amount_col2="200.00",
        raw_narration="Some narration"
    )
    processed = engine.process_row(raw)
    
    assert processed.accounting_head_number == "14"
    assert processed.accounting_head_name == "Corrected Electricity Charges"
    assert temp_head_master.get_extracted_head_name(AccountSet.GENERAL, "14") == "Extracted Electricity Charges"
    assert processed.subhead is None  # Placeholder not implemented yet
    assert processed.transaction_type == TransactionType.PAYMENT

def test_unknown_head_handling(temp_head_master):
    engine = AccountingRuleEngine(head_master_repo=temp_head_master)
    raw = RawExtractedRow(
        source_image="img.jpg",
        source_page="1",
        source_row=1,
        raw_head_number="999",
        raw_voucher_number="125",
        raw_amount_col2="100.00"
    )
    processed = engine.process_row(raw)
    
    assert processed.accounting_head_number == "999"
    assert processed.accounting_head_name is None
    assert processed.review_required is True
    assert "HEAD_NOT_FOUND" in processed.review_reason
