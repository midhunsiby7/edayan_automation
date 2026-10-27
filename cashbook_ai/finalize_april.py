"""Finalize April using saved directions and approved transaction corrections."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from src.export.manual_review_exporter import ManualReviewExporter
from src.models import AccountSet, ProcessedRow, RecordType, TransactionType
from src.review.physical_column_review import extraction_sha256, source_key
from src.validation.head_master import HeadMasterRepository

from correct_april import (
    RAW_PATH,
    REVIEW_PATH,
    read_review_decisions,
)
from generate_physical_column_review import load_processed_rows


ROOT = Path(__file__).parent
OUTPUT_PATH = ROOT / "data/monthly/2026-04/2026-04_ready_for_entry.xlsx"
CORRECTIONS_PATH = ROOT / "data/monthly/2026-04/april_final_corrections.json"
RECONCILIATION_PATH = ROOT / "data/monthly/2026-04/april_ready_for_entry_reconciliation.json"
EXCEPTIONS_PATH = ROOT / "data/monthly/2026-04/april_ready_for_entry_exceptions.json"
MASTER_PATH = ROOT / "data/reference/active/accounting_heads_general_verified.csv"

HEAD_CORRECTIONS = {
    ("page_002.jpg", "2", 3): {
        "expected_voucher": "524",
        "expected_original_head": "80",
        "corrected_head": "20",
    },
    ("page_002.jpg", "2", 4): {
        "expected_voucher": "525",
        "expected_original_head": "80",
        "corrected_head": "40",
    },
}


def apply_head_corrections(
    processed_rows: list[ProcessedRow],
    raw_rows: list,
    repository: HeadMasterRepository,
) -> tuple[list[ProcessedRow], list[dict]]:
    raw_by_key = {source_key(row): row for row in raw_rows}
    result = []
    records = []
    for row in processed_rows:
        key = source_key(row)
        correction = HEAD_CORRECTIONS.get(key)
        if not correction:
            result.append(row)
            continue
        raw = raw_by_key.get(key)
        if raw is None:
            raise ValueError(f"Head correction source record is missing: {key}")
        if (
            str(raw.raw_voucher_number) != correction["expected_voucher"]
            or str(raw.raw_head_number) != correction["expected_original_head"]
        ):
            raise ValueError(
                f"Head correction identity mismatch for {key}: "
                f"voucher={raw.raw_voucher_number!r}, head={raw.raw_head_number!r}"
            )
        canonical = repository.get_head_name(AccountSet.GENERAL, correction["corrected_head"])
        if not canonical:
            raise ValueError(f"Corrected head is absent from verified master: {correction['corrected_head']}")
        updated = row.model_copy(
            update={
                "accounting_head_number": correction["corrected_head"],
                "accounting_head_name": canonical,
            }
        )
        result.append(updated)
        records.append(
            {
                "source_image": key[0],
                "source_page": key[1],
                "source_row": key[2],
                "voucher_number": raw.raw_voucher_number,
                "original_raw_head_number": raw.raw_head_number,
                "corrected_accounting_head_number": correction["corrected_head"],
                "canonical_human_final_name": canonical,
                "correction_source": "USER_PROVIDED_SOURCE_IMAGE_REVIEW",
                "verification": "Exact source image/page/row/voucher identity matched.",
            }
        )
    return result, records


def finalize(
    review_path: Path = REVIEW_PATH,
    output_path: Path = OUTPUT_PATH,
    corrections_path: Path = CORRECTIONS_PATH,
    reconciliation_path: Path = RECONCILIATION_PATH,
    exceptions_path: Path = EXCEPTIONS_PATH,
) -> dict:
    raw_rows, processed_rows = load_processed_rows()
    review_records, review_metadata = read_review_decisions(review_path)
    by_key = {source_key(row): row for row in processed_rows}
    review_by_key = {
        (item["source_image"], item["source_page"], item["source_row"]): item
        for item in review_records
    }
    if set(by_key) != set(review_by_key):
        raise ValueError("Saved review workbook identifiers do not match current processed records")

    directions = {}
    direction_records = []
    direction_exceptions = []
    for key, row in by_key.items():
        item = review_by_key[key]
        direction = item["human_direction"]
        if row.record_type != RecordType.TRANSACTION:
            continue
        if direction not in {"Receipt", "Payment"}:
            direction_exceptions.append(
                {
                    "source_image": key[0],
                    "source_page": key[1],
                    "source_row": key[2],
                    "reason": "Saved review direction is missing or ambiguous.",
                }
            )
            continue
        directions[key] = TransactionType.RECEIPT if direction == "Receipt" else TransactionType.PAYMENT
        direction_records.append(
            {
                "source_image": key[0],
                "source_page": key[1],
                "source_row": key[2],
                "original_model_classification": row.transaction_type.value,
                "selected_direction": direction,
                "correction_source": "USER_SAVED_REVIEW_WORKBOOK",
                "correction_reason": item["correction_reason"],
            }
        )

    repository = HeadMasterRepository(MASTER_PATH)
    corrected_rows, head_corrections = apply_head_corrections(processed_rows, raw_rows, repository)
    unresolved_heads = [
        {
            "source_image": row.source_image,
            "source_page": row.source_page,
            "source_row": row.source_row,
            "accounting_head_number": row.accounting_head_number,
            "reason": "No canonical human_final_name exists in the verified master.",
        }
        for row in corrected_rows
        if row.record_type == RecordType.TRANSACTION and not row.accounting_head_name
    ]
    if direction_exceptions:
        raise ValueError(f"Cannot finalize with unresolved directions: {direction_exceptions}")

    canonical_names = [
        record.human_final_name or record.accounting_head_name
        for record in repository._records[AccountSet.GENERAL].values()
        if record.human_final_name or record.accounting_head_name
    ]
    ManualReviewExporter().export_with_directions(
        corrected_rows,
        directions,
        output_path,
        canonical_head_names=canonical_names,
    )

    source_hash = extraction_sha256(RAW_PATH)
    payload = {
        "schema_version": "april-final-corrections-1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_path": str(RAW_PATH),
        "source_sha256": source_hash,
        "review_workbook_path": str(review_path),
        "review_metadata": review_metadata,
        "direction_decisions": direction_records,
        "head_number_corrections": head_corrections,
    }
    corrections_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    receipt_keys = [key for key, value in directions.items() if value == TransactionType.RECEIPT]
    payment_keys = [key for key, value in directions.items() if value == TransactionType.PAYMENT]
    reconciliation = {
        "source_record_count": len(raw_rows),
        "processed_record_count": len(processed_rows),
        "eligible_transaction_count": sum(row.record_type == RecordType.TRANSACTION for row in processed_rows),
        "informational_excluded_count": sum(row.record_type != RecordType.TRANSACTION for row in processed_rows),
        "receipt_count": len(receipt_keys),
        "payment_count": len(payment_keys),
        "exported_count": len(receipt_keys) + len(payment_keys),
        "canonical_head_names_resolved": sum(
            row.record_type == RecordType.TRANSACTION and bool(row.accounting_head_name)
            for row in corrected_rows
        ),
        "head_name_exception_count": len(unresolved_heads),
        "direction_exception_count": len(direction_exceptions),
        "source_sha256": source_hash,
        "output_path": str(output_path),
        "receipt_source_keys": [f"{k[0]}|{k[1]}|{k[2]}" for k in receipt_keys],
        "payment_source_keys": [f"{k[0]}|{k[1]}|{k[2]}" for k in payment_keys],
        "all_eligible_exported_exactly_once": len(receipt_keys) + len(payment_keys)
        == sum(row.record_type == RecordType.TRANSACTION for row in processed_rows),
    }
    reconciliation_path.write_text(json.dumps(reconciliation, ensure_ascii=False, indent=2), encoding="utf-8")
    exceptions_path.write_text(
        json.dumps(
            {
                "source_sha256": source_hash,
                "direction_exceptions": direction_exceptions,
                "head_name_exceptions": unresolved_heads,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "output": str(output_path),
        "corrections": str(corrections_path),
        "reconciliation": str(reconciliation_path),
        "exceptions": str(exceptions_path),
        **reconciliation,
    }


if __name__ == "__main__":
    print(json.dumps(finalize(), ensure_ascii=False, indent=2))
