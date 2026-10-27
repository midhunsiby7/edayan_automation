"""Reusable monthly cashbook-to-entry-workbook workflow.

Uses an existing monthly raw_extractions.json when present. If it is absent,
the command invokes the existing configured extraction pipeline.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from generate_physical_column_review import load_processed_rows
from src.export.manual_review_exporter import ManualReviewExporter
from src.models import AccountSet, RecordType, TransactionType
from src.validation.head_master import HeadMasterRepository


ROOT = Path(__file__).parent
MASTER_PATH = ROOT / "data/reference/active/accounting_heads_general_verified.csv"


def canonical_names(master_path: Path) -> list[str]:
    repository = HeadMasterRepository(master_path)
    records = repository._records[AccountSet.GENERAL]
    return list(dict.fromkeys(
        record.human_final_name or record.accounting_head_name
        for record in records.values()
        if record.human_final_name or record.accounting_head_name
    ))


def process_month(
    month: str,
    raw_json: Path,
    output: Path,
    report: Path,
    *,
    input_dir: Path | None = None,
    provider: str = "gemini",
) -> dict:
    if not raw_json.exists():
        if input_dir is None or not input_dir.exists():
            raise FileNotFoundError(f"{raw_json} is missing and no valid monthly image directory was provided")
        raw_json = _extract_month(input_dir, output, provider)
    raw_rows, processed_rows = load_processed_rows_from(raw_json)
    directions = {}
    unresolved = []
    for row in processed_rows:
        if row.record_type != RecordType.TRANSACTION:
            continue
        if row.transaction_type == TransactionType.RECEIPT:
            directions[(row.source_image, row.source_page, row.source_row)] = TransactionType.RECEIPT
        elif row.transaction_type == TransactionType.PAYMENT:
            directions[(row.source_image, row.source_page, row.source_row)] = TransactionType.PAYMENT
        else:
            unresolved.append({
                "source_image": row.source_image,
                "source_page": row.source_page,
                "source_row": row.source_row,
                "reason": "Physical-column direction is unresolved",
            })
    exporter = ManualReviewExporter()
    exporter.export_with_directions(
        processed_rows,
        directions,
        output,
        canonical_head_names=canonical_names(MASTER_PATH),
    )
    report_data = {
        "month": month,
        "raw_json": str(raw_json),
        "output": str(output),
        "raw_records": len(raw_rows),
        "processed_records": len(processed_rows),
        "eligible_transactions": sum(row.record_type == RecordType.TRANSACTION for row in processed_rows),
        "receipts": sum(value == TransactionType.RECEIPT for value in directions.values()),
        "payments": sum(value == TransactionType.PAYMENT for value in directions.values()),
        "unresolved": unresolved,
        "head_unresolved": [
            {
                "source_image": row.source_image,
                "source_page": row.source_page,
                "source_row": row.source_row,
                "head_number": row.accounting_head_number,
            }
            for row in processed_rows
            if row.record_type == RecordType.TRANSACTION and not row.accounting_head_name
        ],
    }
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(report_data, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_data


def _extract_month(input_dir: Path, output: Path, provider: str) -> Path:
    from config import DEFAULT_PROVIDER
    from main import CashbookPipeline
    from src.extraction.gemini_extractor import GeminiVisionExtractor
    from src.extraction.mock_extractor import MockExtractor

    image_files = sorted(
        path for path in input_dir.iterdir()
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    )
    if not image_files:
        raise FileNotFoundError(f"No cashbook photographs found in {input_dir}")
    selected_provider = provider or DEFAULT_PROVIDER
    extractor = MockExtractor() if selected_provider == "mock" else GeminiVisionExtractor()
    intermediate = output.with_name(f"{output.stem}_detailed_intermediate.xlsx")
    CashbookPipeline(extractor=extractor, enable_preprocessing=True).process_images(
        image_files,
        intermediate,
    )
    generated_raw = intermediate.parent / "raw_extractions.json"
    if not generated_raw.exists():
        raise RuntimeError(f"Extraction completed without producing {generated_raw}")
    return generated_raw


def load_processed_rows_from(raw_json: Path):
    from src.models import RawExtractedRow
    from src.normalization.date_normalizer import DateNormalizer
    from src.validation.accounting_rules import AccountingRuleEngine

    raw_rows = [RawExtractedRow.model_validate(row) for row in json.loads(raw_json.read_text(encoding="utf-8"))]
    engine = AccountingRuleEngine(
        date_normalizer=DateNormalizer(),
        head_master_repo=HeadMasterRepository(MASTER_PATH),
    )
    return raw_rows, [engine.process_row(row) for row in raw_rows]


def main() -> None:
    parser = argparse.ArgumentParser(description="Process one monthly cashbook extraction into a six-column workbook.")
    parser.add_argument("--month", required=True, help="Month in YYYY-MM format")
    parser.add_argument("--raw-json", type=Path, required=True)
    parser.add_argument("--input", type=Path, default=None, help="Monthly photograph directory used when raw JSON is absent")
    parser.add_argument("--provider", choices=["gemini", "mock"], default="gemini")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(
        process_month(
            args.month,
            args.raw_json,
            args.output,
            args.report,
            input_dir=args.input,
            provider=args.provider,
        ),
        indent=2,
    ))


if __name__ == "__main__":
    main()
