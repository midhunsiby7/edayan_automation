import csv
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import openpyxl


ROOT = Path(__file__).parent
REVIEW_WORKBOOK = ROOT / "data/reference/accounting_heads_general_human_review.xlsx"
ACTIVE_CSV = ROOT / "data/reference/active/accounting_heads_general_verified.csv"
BACKUP_DIR = ROOT / "data/reference/backups"
REQUIRED_COLUMNS = {
    "accounting_head_number",
    "extracted_head_name",
    "human_final_name",
    "review_status",
    "human_comment",
    "source_image",
    "source_page",
    "source_row",
}


def read_review_rows():
    workbook = openpyxl.load_workbook(REVIEW_WORKBOOK, read_only=True, data_only=True)
    worksheet = workbook["Human Review"]
    rows = list(worksheet.iter_rows(values_only=True))
    headers = [str(value).strip() if value is not None else "" for value in rows[0]]
    missing_columns = REQUIRED_COLUMNS - set(headers)
    if missing_columns:
        raise ValueError(f"Human review workbook is missing columns: {sorted(missing_columns)}")

    records = []
    for row_number, values in enumerate(rows[1:], start=2):
        record = dict(zip(headers, values))
        record["_workbook_row"] = row_number
        records.append(record)
    return records


def validate_rows(records):
    by_number = defaultdict(list)
    errors = []
    for record in records:
        number = str(record.get("accounting_head_number") or "").strip()
        extracted = record.get("extracted_head_name")
        final = record.get("human_final_name")
        if not number:
            errors.append(f"Workbook row {record['_workbook_row']}: missing accounting_head_number")
        if not str(final or "").strip():
            errors.append(f"Workbook row {record['_workbook_row']}: missing human_final_name")
        if not str(extracted or "").strip():
            errors.append(f"Workbook row {record['_workbook_row']}: missing extracted_head_name")
        for field in ("source_image", "source_page", "source_row"):
            if record.get(field) in (None, ""):
                errors.append(f"Workbook row {record['_workbook_row']}: missing {field}")
        by_number[number].append(record)

    duplicates = {number: rows for number, rows in by_number.items() if number and len(rows) > 1}
    conflicts = {}
    for number, rows in duplicates.items():
        definitions = {
            (str(row.get("extracted_head_name") or ""), str(row.get("human_final_name") or ""))
            for row in rows
        }
        if len(definitions) > 1:
            conflicts[number] = definitions
    if errors:
        raise ValueError("\n".join(errors))
    if duplicates:
        raise ValueError(f"Duplicate accounting head numbers: {sorted(duplicates)}")
    return duplicates, conflicts


def write_active_csv(records):
    ACTIVE_CSV.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "account_set",
        "accounting_head_number",
        "extracted_head_name",
        "human_final_name",
        "review_status",
        "human_comment",
        "accounting_head_name",
        "normalized_accounting_head_name",
        "source_image",
        "source_page",
        "source_row",
        "extraction_confidence",
        "review_required",
        "source_type",
    ]
    with ACTIVE_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            final_name = str(record["human_final_name"]).strip()
            writer.writerow(
                {
                    "account_set": "GENERAL",
                    "accounting_head_number": str(record["accounting_head_number"]).strip(),
                    "extracted_head_name": record["extracted_head_name"],
                    "human_final_name": final_name,
                    "review_status": record["review_status"] or "",
                    "human_comment": record["human_comment"] or "",
                    "accounting_head_name": final_name,
                    "normalized_accounting_head_name": "",
                    "source_image": record["source_image"],
                    "source_page": record["source_page"],
                    "source_row": record["source_row"],
                    "extraction_confidence": "",
                    "review_required": str(record["review_status"]).upper() == "UNCERTAIN",
                    "source_type": "MANUAL_VERIFIED",
                }
            )


def main():
    records = read_review_rows()
    duplicates, conflicts = validate_rows(records)
    if len(records) != 61:
        raise ValueError(f"Expected 61 reviewed rows, found {len(records)}")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUP_DIR / (
        f"accounting_heads_general_verified_pre_reconcile_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    )
    shutil.copy2(ACTIVE_CSV, backup_path)
    write_active_csv(records)

    print(f"Active CSV: {ACTIVE_CSV}")
    print(f"Backup CSV: {backup_path}")
    print(f"Verified rows: {len(records)}")
    print(f"Duplicate numbers: {sorted(duplicates)}")
    print(f"Conflicting numbers: {sorted(conflicts)}")


if __name__ == "__main__":
    main()
