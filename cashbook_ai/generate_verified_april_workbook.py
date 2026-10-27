import argparse
import json
from pathlib import Path

from generate_physical_column_review import load_processed_rows
from src.review.physical_column_review import (
    build_reconciliation_report,
    extraction_sha256,
    export_verified_workbook,
    load_decisions,
)


ROOT = Path(__file__).parent
RAW_PATH = ROOT / "data/monthly/raw_extractions.json"
DEFAULT_SIDECAR = ROOT / "data/monthly/2026-04/physical_column_review.json"
DEFAULT_OUTPUT = ROOT / "data/monthly/2026-04/2026-04_verified.xlsx"
DEFAULT_REPORT = ROOT / "data/monthly/2026-04/physical_column_reconciliation.json"


def main():
    parser = argparse.ArgumentParser(description="Generate the final workbook only after all directions are approved.")
    parser.add_argument("--sidecar", type=Path, default=DEFAULT_SIDECAR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    _, processed_rows = load_processed_rows()
    decisions = load_decisions(args.sidecar, RAW_PATH, processed_rows)
    report = build_reconciliation_report(processed_rows, decisions)
    report.update(
        {
            "source_path": str(RAW_PATH),
            "source_sha256": extraction_sha256(RAW_PATH),
            "processed_record_count": len(processed_rows),
            "exported_receipt_count": 0,
            "exported_payment_count": 0,
            "exported_source_keys": [],
        }
    )
    if not report["finalization_allowed"]:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        raise SystemExit("Finalization blocked: review all eligible transactions explicitly.")
    export_verified_workbook(processed_rows, decisions, args.output)
    report["exported_receipt_count"] = report["receipt_count"]
    report["exported_payment_count"] = report["payment_count"]
    report["exported_source_keys"] = report["receipt_source_keys"] + report["payment_source_keys"]
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"Verified workbook: {args.output}")


if __name__ == "__main__":
    main()
