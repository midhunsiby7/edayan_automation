import json
from collections import Counter, defaultdict
from pathlib import Path

from src.export.manual_review_exporter import ManualReviewExporter
from src.models import RawExtractedRow, RecordType, TransactionType
from src.normalization.date_normalizer import DateNormalizer
from src.validation.accounting_rules import AccountingRuleEngine
from src.validation.head_master import HeadMasterRepository


ROOT = Path(__file__).parent
RAW_PATH = ROOT / "data/monthly/raw_extractions.json"
MASTER_PATH = ROOT / "data/reference/active/accounting_heads_general_verified.csv"
OUTPUT_PATH = ROOT / "data/monthly/2026-04/2026-04_manual_review.xlsx"


def load_raw_rows():
    data = json.loads(RAW_PATH.read_text(encoding="utf-8"))
    return [RawExtractedRow(**row) for row in data]


def process_rows(raw_rows):
    repository = HeadMasterRepository(MASTER_PATH)
    engine = AccountingRuleEngine(
        date_normalizer=DateNormalizer(),
        head_master_repo=repository,
    )
    return [engine.process_row(row) for row in raw_rows]


def reconcile_rows(raw_rows, processed_rows):
    raw_keys = [(row.source_image, row.source_page, row.source_row) for row in raw_rows]
    processed_keys = [(row.source_image, row.source_page, row.source_row) for row in processed_rows]
    raw_counts = Counter(raw_keys)
    processed_counts = Counter(processed_keys)
    duplicates = sorted(key for key, count in raw_counts.items() if count > 1)
    missing = sorted(key for key in raw_counts if processed_counts[key] == 0)
    extra = sorted(key for key in processed_counts if raw_counts[key] == 0)
    mismatched = sorted(
        key for key in set(raw_counts) | set(processed_counts)
        if raw_counts[key] != processed_counts[key]
    )
    return duplicates, missing, extra, mismatched


def main():
    raw_rows = load_raw_rows()
    processed_rows = process_rows(raw_rows)
    duplicates, missing, extra, mismatched = reconcile_rows(raw_rows, processed_rows)
    if missing or extra or mismatched:
        raise RuntimeError(
            f"Source reconciliation failed: missing={missing}, extra={extra}, "
            f"mismatched={mismatched}"
        )

    suspicious = [
        row for row in processed_rows
        if "FIELD_SHIFT_SUSPECTED" in row.review_reason
        or "HEAD_NOT_FOUND" in row.review_reason
    ]
    unknown_heads = sorted({
        row.accounting_head_number
        for row in processed_rows
        if "HEAD_NOT_FOUND" in row.review_reason
    })

    ManualReviewExporter().export(processed_rows, OUTPUT_PATH)

    counts = Counter(
        row.transaction_type
        for row in processed_rows
        if row.record_type == RecordType.TRANSACTION
    )
    informational = sum(
        row.record_type in {RecordType.OPENING_BALANCE, RecordType.CLOSING_BALANCE}
        for row in processed_rows
    )
    print(f"Raw records: {len(raw_rows)}")
    print(f"Processed records: {len(processed_rows)}")
    print(f"Receipts exported: {counts[TransactionType.RECEIPT]}")
    print(f"Payments exported: {counts[TransactionType.PAYMENT]}")
    print(f"Informational balances excluded: {informational}")
    print(f"Duplicate source identifiers: {duplicates}")
    print(f"Suspicious records: {len(suspicious)}")
    print(f"Unknown head numbers: {unknown_heads}")
    print(f"Workbook: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
