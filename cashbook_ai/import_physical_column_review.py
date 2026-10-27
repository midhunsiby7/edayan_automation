import argparse
import json
from pathlib import Path

from generate_physical_column_review import load_processed_rows
from src.review.physical_column_review import import_review_workbook


ROOT = Path(__file__).parent
RAW_PATH = ROOT / "data/monthly/raw_extractions.json"
DEFAULT_WORKBOOK = ROOT / "data/monthly/2026-04/2026-04_physical_column_review.xlsx"
DEFAULT_SIDECAR = ROOT / "data/monthly/2026-04/physical_column_review.json"


def main():
    parser = argparse.ArgumentParser(description="Import explicit physical-column decisions from a review workbook.")
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--sidecar", type=Path, default=DEFAULT_SIDECAR)
    parser.add_argument("--reviewer", default=None)
    args = parser.parse_args()

    _, processed_rows = load_processed_rows()
    payload = import_review_workbook(
        args.workbook,
        RAW_PATH,
        processed_rows,
        args.sidecar,
        reviewer=args.reviewer,
    )
    print(json.dumps(payload["counts"], indent=2))
    print(f"Imported sidecar: {args.sidecar}")


if __name__ == "__main__":
    main()
