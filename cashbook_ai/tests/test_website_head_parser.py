import pytest
from src.models import TransactionType
from src.validation.website_head_parser import WebsiteHeadParser

def test_website_head_parser_html():
    parser = WebsiteHeadParser()
    html_content = """
    <select name="head_id" id="head_id">
        <option value="">-- Select --</option>
        <option value="106">Bank Charges ( Debit )</option>
        <option value="107">Bank Interest ( Credit )</option>
        <option value="112">Church Feast - Expenses( D )</option>
        <option value="116">Electricity Charges ( C )</option>
        <option value="999">Unknown Direction</option>
    </select>
    """
    
    heads = parser.parse_html(html_content)
    
    # 5 options matched (empty value is skipped)
    assert len(heads) == 5
    
    # Check specific mappings
    bank_charges = next(h for h in heads if h.website_head_value == "106")
    assert bank_charges.website_head_label == "Bank Charges ( Debit )"
    assert bank_charges.direction == TransactionType.PAYMENT
    
    bank_interest = next(h for h in heads if h.website_head_value == "107")
    assert bank_interest.website_head_label == "Bank Interest ( Credit )"
    assert bank_interest.direction == TransactionType.RECEIPT

    feast_expense = next(h for h in heads if h.website_head_value == "112")
    assert feast_expense.direction == TransactionType.PAYMENT

    electricity = next(h for h in heads if h.website_head_value == "116")
    assert electricity.direction == TransactionType.RECEIPT
    
    unknown = next(h for h in heads if h.website_head_value == "999")
    assert unknown.direction == TransactionType.UNKNOWN
