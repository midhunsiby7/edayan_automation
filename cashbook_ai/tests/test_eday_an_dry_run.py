import csv
import json
from pathlib import Path

import openpyxl

from automation.edayan_dry_run import REQUIRED_COLUMNS, run_dry_run


def write_workbook(path: Path, rows: dict[str, list[tuple]]):
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for sheet_name, sheet_rows in rows.items():
        sheet = workbook.create_sheet(sheet_name)
        sheet.append(list(REQUIRED_COLUMNS))
        for row in sheet_rows:
            sheet.append(row)
    workbook.save(path)


def write_crosswalk(path: Path):
    fields = [
        "transaction_type",
        "website_head_value",
        "website_head_label_exact",
        "accounting_head_name_original",
        "mapping_status",
        "mapping_confidence",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow(
            {
                "transaction_type": "PAYMENT",
                "website_head_value": "126",
                "website_head_label_exact": "Miscellaneous Expenses ( Debit )",
                "accounting_head_name_original": "Miscellaneous Expenses",
                "mapping_status": "MATCHED_NAME_VARIATION",
                "mapping_confidence": "0.9",
            }
        )


def test_dry_run_maps_verified_head_and_updates_journal(tmp_path: Path):
    workbook = tmp_path / "ready.xlsx"
    crosswalk = tmp_path / "crosswalk.csv"
    journal = tmp_path / "journal.json"
    write_workbook(
        workbook,
        {
            "Receipts": [],
            "Payments": [
                (
                    "2026-04-01",
                    "1",
                    700,
                    "Miscellaneous Expenses",
                    "",
                    "Electricity charge",
                )
            ],
        },
    )
    write_crosswalk(crosswalk)

    report = run_dry_run(workbook, crosswalk, journal)

    assert report["entry_count"] == 1
    assert report["blocked_count"] == 0
    assert report["entries"][0]["head_resolution"]["website_value"] == "126"
    assert report["entries"][0]["mode"] == "cash"
    assert "exception_groups" in report
    assert json.loads(journal.read_text(encoding="utf-8"))["entries"]


def test_dry_run_blocks_unverified_head_and_missing_subhead(tmp_path: Path):
    workbook = tmp_path / "ready.xlsx"
    crosswalk = tmp_path / "crosswalk.csv"
    journal = tmp_path / "journal.json"
    write_workbook(
        workbook,
        {
            "Receipts": [
                (
                    "2026-04-01",
                    "1",
                    100,
                    "Unknown Head",
                    "",
                    "Narration",
                )
            ],
            "Payments": [],
        },
    )
    write_crosswalk(crosswalk)

    report = run_dry_run(workbook, crosswalk, journal)

    assert report["blocked_count"] == 1
    assert any("Head mapping blocked" in issue for issue in report["entries"][0]["issues"])


def test_dry_run_accepts_observed_no_subhead_head(tmp_path: Path):
    workbook = tmp_path / "ready.xlsx"
    crosswalk = tmp_path / "crosswalk.csv"
    journal = tmp_path / "journal.json"
    write_workbook(
        workbook,
        {
            "Receipts": [],
            "Payments": [
                ("2026-04-01", "1", 100, "Miscellaneous Expenses", "", "Expense")
            ],
        },
    )
    write_crosswalk(crosswalk)

    report = run_dry_run(workbook, crosswalk, journal)

    assert report["ready_count"] == 1
    assert report["entries"][0]["status"] == "READY"


def test_dry_run_blocks_blank_subhead_when_observed_options_exist(tmp_path: Path):
    workbook = tmp_path / "ready.xlsx"
    crosswalk = tmp_path / "crosswalk.csv"
    journal = tmp_path / "journal.json"
    write_workbook(
        workbook,
        {
            "Receipts": [
                (
                    "2026-04-01",
                    "1",
                    100,
                    "Collections as per Order from the Diocese",
                    "",
                    "Collection",
                )
            ],
            "Payments": [],
        },
    )
    write_crosswalk(crosswalk)
    with crosswalk.open("a", encoding="utf-8", newline="") as handle:
        handle.write(
            "RECEIPT,143,Collections as per Order from the Diocese ( Credit ),"
            "Collection As Per the Order From the Diocese,MATCHED_EXACT_WEBSITE_MASTER,1.00\n"
        )

    report = run_dry_run(workbook, crosswalk, journal)

    assert report["blocked_count"] == 1
    assert "Subhead is required" in report["entries"][0]["issues"][0]
