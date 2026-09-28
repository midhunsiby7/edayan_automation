import pytest
from src.models import AccountSet, AccountingHead, WebsiteHead, TransactionType
from src.validation.head_mapping import HeadMapper

@pytest.fixture
def dummy_accounting_heads():
    return [
        AccountingHead(account_set=AccountSet.GENERAL, accounting_head_number="10", accounting_head_name="Candle offerings"),
        AccountingHead(account_set=AccountSet.GENERAL, accounting_head_number="87", accounting_head_name="Rented Building Expense"),
        AccountingHead(account_set=AccountSet.GENERAL, accounting_head_number="5", accounting_head_name="Bank Interest"),
        AccountingHead(account_set=AccountSet.GENERAL, accounting_head_number="99", accounting_head_name="Not on website at all"),
        AccountingHead(account_set=AccountSet.GENERAL, accounting_head_number="50", accounting_head_name="Conflict Expense")
    ]

@pytest.fixture
def dummy_website_heads():
    return [
        WebsiteHead(website_head_value="1", website_head_label="Candle offerings (Credit)", direction=TransactionType.RECEIPT),
        WebsiteHead(website_head_value="2", website_head_label="Rented Building Expenses (Debit)", direction=TransactionType.PAYMENT),
        WebsiteHead(website_head_value="3", website_head_label="Bank Interest / Other Interest Received (Credit)", direction=TransactionType.RECEIPT),
        WebsiteHead(website_head_value="4", website_head_label="Conflict Expense 1 (Debit)", direction=TransactionType.PAYMENT),
        WebsiteHead(website_head_value="5", website_head_label="Conflict Expense 2 (Debit)", direction=TransactionType.PAYMENT),
    ]

def test_exact_name_match(dummy_accounting_heads, dummy_website_heads):
    # Candle offerings vs Candle offerings (Credit) -> should be exact match
    mapper = HeadMapper(dummy_accounting_heads, dummy_website_heads)
    # Mock source image to test RECEIPT mapping
    dummy_accounting_heads[0].source_page = "7.30.28" 
    results = mapper.map_heads()
    
    r = next(res for res in results if res.accounting_head_number == "10")
    assert r.mapping_status in ("MATCHED", "MATCHED_NAME_VARIATION")
    assert r.website_head_value == "1"
    assert r.website_head_label_exact == "Candle offerings (Credit)"
    assert r.accounting_head_name_original == "Candle offerings"

def test_spelling_variation(dummy_accounting_heads, dummy_website_heads):
    mapper = HeadMapper(dummy_accounting_heads, dummy_website_heads)
    # Rented Building Expense vs Rented Building Expenses
    dummy_accounting_heads[1].source_page = "7.31.00" # Payment
    results = mapper.map_heads()
    
    r = next(res for res in results if res.accounting_head_number == "87")
    assert r.mapping_status == "MATCHED_NAME_VARIATION"
    assert r.website_head_value == "2"

def test_direction_mismatch(dummy_accounting_heads, dummy_website_heads):
    mapper = HeadMapper(dummy_accounting_heads, dummy_website_heads)
    # Try mapping Bank Interest (RECEIPT) but provide wrong image (PAYMENT)
    dummy_accounting_heads[2].source_page = "7.31.00" 
    results = mapper.map_heads()
    
    r = next(res for res in results if res.accounting_head_number == "5")
    assert r.mapping_status == "NOT_FOUND" # Because direction didn't match

def test_multiple_possible_matches(dummy_accounting_heads, dummy_website_heads):
    mapper = HeadMapper(dummy_accounting_heads, dummy_website_heads)
    dummy_accounting_heads[4].source_page = "7.31.00" 
    results = mapper.map_heads()
    
    r = next(res for res in results if res.accounting_head_number == "50")
    assert r.mapping_status == "CONFLICT"
    assert r.review_required == True
    assert r.website_head_value is None

def test_no_match(dummy_accounting_heads, dummy_website_heads):
    mapper = HeadMapper(dummy_accounting_heads, dummy_website_heads)
    results = mapper.map_heads()
    
    r = next(res for res in results if res.accounting_head_number == "99")
    assert r.mapping_status == "NOT_FOUND"
    assert r.review_required == True
