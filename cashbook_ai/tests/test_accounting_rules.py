import pytest
from src.models import (
    RawExtractedRow,
    RecordType,
    TransactionType,
)
from src.normalization.date_normalizer import DateNormalizer
from src.validation.accounting_rules import AccountingRuleEngine


def test_opening_balance_conventions():
    """Verify Opening Balance convention: no voucher, receipt column amount, informational record."""
    engine = AccountingRuleEngine(DateNormalizer())
    raw = RawExtractedRow(
        source_image="page1.jpg",
        source_page="1",
        source_row=1,
        raw_date="01/04/2024",
        raw_head_number=None,
        raw_voucher_number=None,
        raw_narration="തുടക്ക ബാക്കി",
        english_narration="Opening Balance",
        raw_amount="25000.00",
        raw_amount_col1="25000.00",
        detected_amount_column=1,
    )
    processed = engine.process_row(raw)
    assert processed.record_type == RecordType.OPENING_BALANCE
    assert processed.receipt_amount == 25000.00
    assert processed.payment_amount is None
    assert processed.transaction_type == TransactionType.INFORMATIONAL
    assert processed.voucher_number is None


def test_head_78_voucher_1_payment_700():
    """Verify user cashbook case: Head 78 / Voucher 1 / Payment 700."""
    engine = AccountingRuleEngine(DateNormalizer())
    raw = RawExtractedRow(
        source_image="page1.jpg",
        source_page="1",
        source_row=2,
        raw_date="02/04/2024",
        raw_head_number="78",
        raw_voucher_number="1",
        raw_narration="വൈദ്യുതി ചാർജ്ജ് അടച്ചത്",
        english_narration="Electricity bill payment",
        raw_amount="700.00",
        raw_amount_col1=None,
        raw_amount_col2="700.00",
        detected_amount_column=2,  # Column 2 = Payment
    )
    processed = engine.process_row(raw)
    assert processed.accounting_head_number == "78"
    assert processed.voucher_number == "1"
    assert processed.payment_amount == 700.00
    assert processed.receipt_amount is None
    assert processed.transaction_type == TransactionType.PAYMENT
    assert processed.record_type == RecordType.TRANSACTION


def test_head_78_voucher_2_payment_1080_with_ditto():
    """Verify user cashbook case: Head 78 / Voucher 2 / Payment 1080 with ditto date."""
    normalizer = DateNormalizer()
    engine = AccountingRuleEngine(normalizer)

    # First set the date
    raw1 = RawExtractedRow(
        source_image="page1.jpg",
        source_page="1",
        source_row=2,
        raw_date="02/04/2024",
        raw_head_number="78",
        raw_voucher_number="1",
        raw_amount_col2="700.00",
        detected_amount_column=2,
    )
    engine.process_row(raw1)

    # Second row inherits date
    raw2 = RawExtractedRow(
        source_image="page1.jpg",
        source_page="1",
        source_row=3,
        raw_date='"',  # ditto mark
        raw_head_number="78",
        raw_voucher_number="2",
        raw_amount="1080.00",
        raw_amount_col1=None,
        raw_amount_col2="1080.00",
        detected_amount_column=2,
    )
    processed2 = engine.process_row(raw2)
    assert processed2.resolved_date == "2024-04-02"
    assert processed2.accounting_head_number == "78"
    assert processed2.voucher_number == "2"
    assert processed2.payment_amount == 1080.00
    assert processed2.transaction_type == TransactionType.PAYMENT


def test_physical_column_overrides_narration_semantics():
    """Verify requirement 1: Physical column determines receipt/payment, never narration text."""
    engine = AccountingRuleEngine(DateNormalizer())

    # Even if narration says 'payment', if it's placed in column 1, physical column evidence holds!
    raw = RawExtractedRow(
        source_image="page1.jpg",
        source_page="1",
        source_row=5,
        raw_date="04/04/2024",
        raw_narration="ചിലവ് / payment",
        english_narration="Expense / payment",
        raw_amount_col1="500.00",
        detected_amount_column=1,  # Column 1 = Receipt
    )
    processed = engine.process_row(raw)
    assert processed.receipt_amount == 500.00
    assert processed.payment_amount is None
    assert processed.transaction_type == TransactionType.RECEIPT


def test_review_required_trigger_on_low_confidence():
    """Verify confidence triggers review_required."""
    engine = AccountingRuleEngine(DateNormalizer())
    raw = RawExtractedRow(
        source_image="page1.jpg",
        source_page="1",
        source_row=6,
        raw_date="05/04/2024",
        raw_head_number="78",
        raw_voucher_number="3",
        raw_amount="500.00",
        detected_amount_column=2,
        amount_confidence=0.45,  # Low amount confidence
        extraction_confidence=0.55,
    )
    processed = engine.process_row(raw)
    assert processed.review_required is True
    assert any("Low amount confidence" in r for r in processed.review_reasons)


def test_head_not_found_warning():
    """Verify that an unknown head number triggers HEAD_NOT_FOUND review."""
    from src.validation.head_master import HeadMasterRepository
    from pathlib import Path
    
    # Empty repo
    repo = HeadMasterRepository(Path("dummy.json"))
    engine = AccountingRuleEngine(DateNormalizer(), head_master_repo=repo)
    
    raw = RawExtractedRow(
        source_image="page1.jpg",
        source_page="1",
        source_row=10,
        raw_date="06/04/2024",
        raw_head_number="999",
        raw_voucher_number="10",
        raw_amount="500.00",
        detected_amount_column=2,
    )
    processed = engine.process_row(raw)
    assert processed.accounting_head_name is None
    assert processed.website_head_value is None
    assert processed.review_required is True
    assert "HEAD_NOT_FOUND" in processed.review_reasons
    assert "HEAD_MAPPING_NOT_FOUND" in processed.review_reasons
