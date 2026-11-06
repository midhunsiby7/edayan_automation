import datetime
from pathlib import Path

import openpyxl
from fastapi.testclient import TestClient

from src.app.main import app, DATA_DIR
from src.export.manual_review_exporter import (
    ManualReviewExporter,
    REVIEW_SHEET_COLUMNS,
    ALL_TRANSACTIONS_COLUMNS,
    parse_date_value,
)
from src.models import ProcessedRow, RecordType, TransactionType


def make_test_row(**kwargs):
    defaults = {
        "source_image": "page_001.jpg",
        "source_page": "1",
        "source_row": 2,
        "resolved_date": "2026-05-03",
        "raw_date": "3.05.26",
        "raw_voucher_number": "536",
        "voucher_number": "536",
        "raw_head_number": "20",
        "accounting_head_name": "Offerings for Daily Expense(C)",
        "subhead": None,
        "raw_narration": "നിത്യചിലവിലേക്ക് സംഭാവന",
        "narration_english": "Donation for daily expenses",
        "record_type": RecordType.TRANSACTION,
        "transaction_type": TransactionType.RECEIPT,
        "receipt_amount": 5000.0,
        "payment_amount": None,
    }
    defaults.update(kwargs)
    return ProcessedRow(**defaults)


def test_parse_date_value_various_formats():
    assert parse_date_value("2026-05-03") == datetime.date(2026, 5, 3)
    assert parse_date_value("3.05.26") == datetime.date(2026, 5, 3)
    assert parse_date_value("03-05-2026") == datetime.date(2026, 5, 3)
    assert parse_date_value("15/05/2026") == datetime.date(2026, 5, 15)
    assert parse_date_value("2026-05-03 00:00:00") == datetime.date(2026, 5, 3)
    assert parse_date_value("2026-05-03T10:15:00Z") == datetime.date(2026, 5, 3)
    assert parse_date_value(datetime.date(2026, 5, 3)) == datetime.date(2026, 5, 3)
    assert parse_date_value(datetime.datetime(2026, 5, 3, 14, 30)) == datetime.date(2026, 5, 3)
    assert parse_date_value("") is None
    assert parse_date_value(None) is None
    assert parse_date_value("-") is None


def test_ditto_mark_date_inheritance(tmp_path: Path):
    """Transactions with ditto marks or empty raw dates must export using the resolved date."""
    output = tmp_path / "ditto_test.xlsx"
    row_ditto = make_test_row(
        raw_date='"',
        resolved_date="2026-05-03",
        raw_voucher_number="537",
        receipt_amount=606.0,
    )
    row_empty_raw = make_test_row(
        raw_date=None,
        resolved_date="2026-05-04",
        raw_voucher_number="538",
        receipt_amount=850.0,
    )

    exporter = ManualReviewExporter()
    exporter.export_review_workbook([row_ditto, row_empty_raw], output)

    wb = openpyxl.load_workbook(output)
    ws = wb["Receipts"]
    
    # Check row 2 (ditto)
    cell_ditto = ws.cell(2, 1)
    assert cell_ditto.is_date is True
    assert cell_ditto.number_format == "DD-MM-YYYY"
    assert cell_ditto.value == datetime.datetime(2026, 5, 3, 0, 0)
    
    # Check row 3 (empty raw)
    cell_empty = ws.cell(3, 1)
    assert cell_empty.is_date is True
    assert cell_empty.number_format == "DD-MM-YYYY"
    assert cell_empty.value == datetime.datetime(2026, 5, 4, 0, 0)


def test_review_workbook_sheets_columns_and_styling(tmp_path: Path):
    """Test full review workbook typography, columns, sheet structure, and number formatting."""
    output = tmp_path / "styled_review.xlsx"
    rec_row = make_test_row(
        transaction_type=TransactionType.RECEIPT,
        receipt_amount=5000.0,
        raw_voucher_number="536",
        raw_head_number="20",
    )
    pay_row = make_test_row(
        transaction_type=TransactionType.PAYMENT,
        receipt_amount=None,
        payment_amount=400.0,
        raw_voucher_number="32",
        raw_head_number="85",
        accounting_head_name="Telephone & Postage",
        narration_english="Postage stamps",
    )

    exporter = ManualReviewExporter()
    exporter.export_review_workbook([rec_row, pay_row], output)

    wb = openpyxl.load_workbook(output)
    assert wb.sheetnames == ["Receipts", "Payments"]

    # Verify Receipts sheet
    ws_rec = wb["Receipts"]
    assert [c.value for c in ws_rec[1]] == [
        "Date", "Voucher Number", "Amount", "Head Name", "Subhead", "Narration"
    ]
    assert ws_rec.row_dimensions[1].height == 28.0
    assert ws_rec.cell(1, 1).font.bold is True
    assert ws_rec.cell(1, 1).font.size == 13.0
    assert ws_rec.cell(1, 1).fill.fgColor.rgb == "001F4E79"
    assert ws_rec.freeze_panes == "A2"
    assert ws_rec.auto_filter.ref is not None

    # Row 2 in Receipts
    assert ws_rec.row_dimensions[2].height == 24.0
    # Date (Col 1)
    c_date = ws_rec.cell(2, 1)
    assert c_date.is_date is True
    assert c_date.number_format == "DD-MM-YYYY"
    assert c_date.font.bold is True
    assert c_date.font.size == 12.0
    # Voucher Number (Col 2)
    c_v = ws_rec.cell(2, 2)
    assert c_v.value == "536"
    assert c_v.number_format == "@"
    assert c_v.font.bold is True
    assert c_v.font.size == 12.0
    # Amount (Col 3)
    c_amt = ws_rec.cell(2, 3)
    assert float(c_amt.value) == 5000.0
    assert c_amt.number_format == "#,##0.00"
    assert c_amt.font.bold is True
    assert c_amt.font.size == 12.0
    # Head Name (Col 4)
    c_head = ws_rec.cell(2, 4)
    assert c_head.value == "Offerings for Daily Expense(C)"
    assert c_head.font.bold is True
    assert c_head.font.size == 12.0
    # Subhead (Col 5)
    c_sub = ws_rec.cell(2, 5)
    assert c_sub.value is None
    assert c_sub.font.bold is False
    assert c_sub.font.size == 11.0
    # Narration (Col 6)
    c_narr = ws_rec.cell(2, 6)
    assert c_narr.value == "Donation for daily expenses"
    assert c_narr.font.bold is True
    assert c_narr.font.size == 12.0
    assert c_narr.alignment.wrap_text is True


def test_optional_all_transactions_and_folio(tmp_path: Path):
    """Test optional multi-sheet export with Folio and All_Transactions."""
    output = tmp_path / "all_txs_review.xlsx"
    rec_row = make_test_row(
        transaction_type=TransactionType.RECEIPT,
        receipt_amount=5000.0,
        raw_voucher_number="536",
        raw_head_number="20",
    )
    pay_row = make_test_row(
        transaction_type=TransactionType.PAYMENT,
        receipt_amount=None,
        payment_amount=400.0,
        raw_voucher_number="32",
        raw_head_number="85",
        accounting_head_name="Telephone & Postage",
        narration_english="Postage stamps",
    )

    exporter = ManualReviewExporter()
    exporter.export(
        [rec_row, pay_row],
        output,
        include_all_transactions=True,
        include_folio=True,
    )

    wb = openpyxl.load_workbook(output)
    assert wb.sheetnames == ["Receipts", "Payments", "All_Transactions"]

    # Verify All_Transactions sheet
    ws_all = wb["All_Transactions"]
    assert [c.value for c in ws_all[1]] == ALL_TRANSACTIONS_COLUMNS
    assert ws_all.max_row == 3  # 1 header + 2 transactions

    # Check direction styling
    rec_dir_cell = ws_all.cell(2, 4)
    assert rec_dir_cell.value == "RECEIPT"
    assert rec_dir_cell.font.bold is True
    assert rec_dir_cell.font.color.rgb == "00065F46"  # emerald green
    assert rec_dir_cell.fill.fgColor.rgb == "00D1FAE5"

    pay_dir_cell = ws_all.cell(3, 4)
    assert pay_dir_cell.value == "PAYMENT"
    assert pay_dir_cell.font.bold is True
    assert pay_dir_cell.font.color.rgb == "00991B1B"  # red
    assert pay_dir_cell.fill.fgColor.rgb == "00FEE2E2"


def test_api_download_endpoints_and_content():
    """Verify GET/POST /api/export/draft and /api/export/raw serve the verified 2-sheet workbook with anti-cache headers."""
    client = TestClient(app)

    # 1. Refresh via POST /api/export/draft
    post_res = client.post("/api/export/draft", data={"month": "2026-05"})
    assert post_res.status_code == 200
    assert post_res.json()["rows_count"] == 74

    # 2. Download via GET /api/export/draft (with cache-buster query parameter)
    get_res = client.get("/api/export/draft?month=2026-05&t=123456789")
    assert get_res.status_code == 200
    assert get_res.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert "no-cache" in get_res.headers.get("cache-control", "")
    assert "no-store" in get_res.headers.get("cache-control", "")
    assert len(get_res.content) > 1000

    # 3. Test raw alias routes
    post_raw = client.post("/api/export/raw", data={"month": "2026-05"})
    assert post_raw.status_code == 200
    get_raw = client.get("/api/export/raw?month=2026-05")
    assert get_raw.status_code == 200
    assert len(get_raw.content) == len(get_res.content)

    # 4. Verify downloaded workbook structure
    tmp_wb_path = DATA_DIR / "output" / "2026-05" / "2026-05_draft_review.xlsx"
    wb = openpyxl.load_workbook(tmp_wb_path)
    assert set(wb.sheetnames) == {"Receipts", "Payments"}

    # Verify Receipts sheet has 30 rows + 1 header = 31, sum = 516,867.00
    ws_rec = wb["Receipts"]
    assert ws_rec.max_row == 31
    rec_sum = sum(float(ws_rec.cell(r, 3).value or 0) for r in range(2, ws_rec.max_row + 1))
    assert rec_sum == 516867.0

    # Verify reference transaction Voucher 536 in Receipts
    found_536 = False
    for r in range(2, ws_rec.max_row + 1):
        if str(ws_rec.cell(r, 2).value).strip() == "536":
            found_536 = True
            c_date = ws_rec.cell(r, 1)
            assert c_date.is_date is True
            assert c_date.number_format == "DD-MM-YYYY"
            assert c_date.font.bold is True
            assert c_date.font.size == 12.0
            assert float(ws_rec.cell(r, 3).value) == 5000.0
            assert ws_rec.cell(r, 4).value == "Offerings for Daily Expense(C)"
            break
    assert found_536 is True

    # Verify Payments sheet has 44 rows + 1 header = 45, sum = 475,438.00
    ws_pay = wb["Payments"]
    assert ws_pay.max_row == 45
    pay_sum = sum(float(ws_pay.cell(r, 3).value or 0) for r in range(2, ws_pay.max_row + 1))
    assert pay_sum == 475438.0

    # Ensure every single date cell across Receipts and Payments is a valid date
    for ws in (ws_rec, ws_pay):
        for r in range(2, ws.max_row + 1):
            d_cell = ws.cell(r, 1)
            assert d_cell.is_date is True
            assert d_cell.number_format == "DD-MM-YYYY"
            assert d_cell.value is not None
