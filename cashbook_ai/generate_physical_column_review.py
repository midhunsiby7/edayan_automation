import json
from pathlib import Path

from src.models import RawExtractedRow
from src.normalization.date_normalizer import DateNormalizer
from src.validation.accounting_rules import AccountingRuleEngine
from src.validation.head_master import HeadMasterRepository
from src.review.physical_column_review import (
    build_review_records,
    extraction_sha256,
    write_review_workbook,
)


ROOT = Path(__file__).parent
RAW_PATH = ROOT / "data/monthly/raw_extractions.json"
MASTER_PATH = ROOT / "data/reference/active/accounting_heads_general_verified.csv"
REVIEW_WORKBOOK_PATH = ROOT / "data/monthly/2026-04/2026-04_physical_column_review.xlsx"
SIDECAR_PATH = ROOT / "data/monthly/2026-04/physical_column_review.json"


def load_processed_rows():
    raw_rows = [
        RawExtractedRow.model_validate(row)
        for row in json.loads(RAW_PATH.read_text(encoding="utf-8"))
    ]
    engine = AccountingRuleEngine(
        date_normalizer=DateNormalizer(),
        head_master_repo=HeadMasterRepository(MASTER_PATH),
    )
    return raw_rows, [engine.process_row(row) for row in raw_rows]


def main():
    raw_rows, processed_rows = load_processed_rows()
    records = build_review_records(processed_rows, RAW_PATH)
    source_hash = extraction_sha256(RAW_PATH)
    write_review_workbook(
        records,
        REVIEW_WORKBOOK_PATH,
        source_path=RAW_PATH,
        source_hash=source_hash,
    )
    sidecar = {
        "review_schema_version": "1.0",
        "source_path": str(RAW_PATH),
        "source_sha256": source_hash,
        "generated_at_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "records": [
            {
                "source_image": record["source_image"],
                "source_page": record["source_page"],
                "source_row": record["source_row"],
                "original_model_detected_amount_column": record["detected_amount_column"],
                "original_model_classification": record["model_classification"],
                "human_verified_direction": record["human_verified_direction"],
                "review_status": record["review_status"],
                "correction_reason": "",
                "reviewer": None,
                "reviewed_at_utc": None,
                "source_extraction_sha256": source_hash,
                "review_schema_version": "1.0",
            }
            for record in records
        ],
    }
    sidecar["counts"] = {
        "UNREVIEWED": sum(1 for r in records if r["review_status"] == "UNREVIEWED"),
        "NOT_REQUIRED": sum(1 for r in records if r["review_status"] == "NOT_REQUIRED"),
    }
    SIDECAR_PATH.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Raw records: {len(raw_rows)}")
    print(f"Processed records: {len(processed_rows)}")
    print(f"Review workbook: {REVIEW_WORKBOOK_PATH}")
    print(f"Review sidecar: {SIDECAR_PATH}")
    print(f"Source SHA-256: {source_hash}")
    print("Eligible transactions require explicit HUMAN_VERIFIED Receipt/Payment decisions.")


if __name__ == "__main__":
    main()
