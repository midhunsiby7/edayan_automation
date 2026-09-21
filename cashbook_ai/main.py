import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List, Tuple

from config import (
    CONFIDENCE_THRESHOLD,
    DEFAULT_PROVIDER,
    INPUT_DIR,
    LOG_FORMAT,
    LOG_LEVEL,
    OUTPUT_DIR,
)
from src.export.excel_exporter import ExcelExporter
from src.extraction.base import BaseExtractor
from src.extraction.gemini_extractor import GeminiVisionExtractor
from src.extraction.mock_extractor import MockExtractor
from src.models import ProcessedRow, RawExtractedRow, RecordType, TransactionType
from src.normalization.date_normalizer import DateNormalizer
from src.preprocessing.image_preprocessor import ImagePreprocessor
from src.validation.accounting_rules import AccountingRuleEngine

# Setup logging
logging.basicConfig(level=getattr(logging, LOG_LEVEL.upper(), logging.INFO), format=LOG_FORMAT)
logger = logging.getLogger("CashbookAI")


class CashbookPipeline:
    """End-to-end pipeline orchestrating preprocessing, extraction, normalization, validation, and export."""

    def __init__(self, extractor: BaseExtractor, enable_preprocessing: bool = True):
        self.extractor = extractor
        self.enable_preprocessing = enable_preprocessing
        self.preprocessor = ImagePreprocessor() if enable_preprocessing else None
        
        # Stateful across pages within one monthly job
        self.date_normalizer = DateNormalizer()
        self.rule_engine = AccountingRuleEngine(date_normalizer=self.date_normalizer)
        self.exporter = ExcelExporter()

    def process_images(self, image_paths: List[Path], output_excel_path: Path) -> Tuple[Path, Path]:
        """Process a batch of images in chronological order."""
        logger.info(f"Starting Cashbook Processing Job: {len(image_paths)} images")
        self.date_normalizer.reset_job_state()
        all_processed_rows: List[ProcessedRow] = []
        all_raw_rows: List[RawExtractedRow] = []

        # Sort image paths chronologically (April 8.39.33 before August 8.39.28)
        def get_img_order(p: Path):
            name = p.name.lower()
            if "8.39.33" in name:
                return (0, name)
            if "8.39.28" in name:
                return (1, name)
            return (2, name)

        sorted_paths = sorted(image_paths, key=get_img_order)

        for img_idx, img_path in enumerate(sorted_paths, start=1):
            logger.info(f"--- Processing Page [{img_idx}/{len(sorted_paths)}]: {img_path.name} ---")

            preprocessed_path = None
            if self.preprocessor:
                try:
                    _, preprocessed_path = self.preprocessor.preprocess(img_path, save_debug=True)
                except Exception as e:
                    logger.warning(f"Preprocessing failed for {img_path.name}, falling back to raw image: {e}")

            # Extract raw structured rows
            raw_rows: List[RawExtractedRow] = self.extractor.extract(
                image_path=img_path,
                preprocessed_image_path=preprocessed_path,
            )
            logger.info(f"Extracted {len(raw_rows)} raw rows from {img_path.name}")

            # Ensure source_page reflects current page index in the multi-page job
            for r in raw_rows:
                r.source_page = str(img_idx)

            # Requirement 9: Sort extracted rows into actual reading order using vertical position / bounding box
            def get_sort_key(r: RawExtractedRow):
                if r.row_bbox and len(r.row_bbox) >= 2:
                    # Sort primarily by ymin (vertical position), then xmin
                    return (float(r.row_bbox[0]), float(r.row_bbox[1]))
                return (float(r.source_row), 0.0)

            sorted_raw_rows = sorted(raw_rows, key=get_sort_key)
            all_raw_rows.extend(sorted_raw_rows)

            # Normalize & apply accounting validation rules in reading order
            for sorted_row in sorted_raw_rows:
                processed = self.rule_engine.process_row(sorted_row)
                all_processed_rows.append(processed)

                if processed.review_required:
                    logger.warning(
                        f"Row {processed.source_row} [{processed.source_image}] flagged for review: "
                        f"{'; '.join(processed.review_reasons)}"
                    )

        # Requirement 11: Export raw extraction JSON for debugging
        raw_json_path = output_excel_path.parent / "raw_extractions.json"
        with open(raw_json_path, "w", encoding="utf-8") as f:
            json.dump([r.model_dump() for r in all_raw_rows], f, ensure_ascii=False, indent=2)
        logger.info(f"Saved raw extractions JSON to {raw_json_path}")

        # Requirement 11: Export results to 4-sheet Excel
        excel_path = self.exporter.export(all_processed_rows, output_excel_path)
        
        # Diagnostic Report
        self._print_diagnostic_report(sorted_paths, all_processed_rows, excel_path, raw_json_path)
        return excel_path, raw_json_path

    def _print_diagnostic_report(
        self,
        image_paths: List[Path],
        rows: List[ProcessedRow],
        excel_path: Path,
        raw_json_path: Path,
    ):
        """Prints the full diagnostic report to discover where real cashbook breaks assumptions."""
        total_pages = len(image_paths)
        total_rows = len(rows)

        unresolved_dates = sum(1 for r in rows if not r.resolved_date)
        unresolved_heads = sum(1 for r in rows if not r.head_number and r.record_type == RecordType.TRANSACTION)
        unresolved_vouchers = sum(1 for r in rows if not r.voucher_number and r.record_type == RecordType.TRANSACTION)
        unresolved_amounts = sum(
            1 for r in rows if (r.receipt_amount is None and r.payment_amount is None and r.record_type == RecordType.TRANSACTION)
        )
        low_narration_conf = sum(1 for r in rows if r.narration_confidence < 0.60)
        rows_requiring_review = sum(1 for r in rows if r.review_required)

        receipts = [
            r for r in rows
            if r.transaction_type == TransactionType.RECEIPT or (
                r.record_type == RecordType.OPENING_BALANCE and r.receipt_amount is not None
            )
        ]
        payments = [r for r in rows if r.transaction_type == TransactionType.PAYMENT]
        opening_bal_detected = any(r.record_type == RecordType.OPENING_BALANCE for r in rows)
        closing_bal_detected = any(r.record_type == RecordType.CLOSING_BALANCE for r in rows)

        total_receipts_sum = sum(r.receipt_amount or 0.0 for r in receipts)
        total_payments_sum = sum(r.payment_amount or 0.0 for r in payments)

        print("\n" + "=" * 70)
        print("               REAL CASHBOOK EXTRACTION DIAGNOSTIC REPORT               ")
        print("=" * 70)
        print(f" Total pages:                      {total_pages}")
        print(f" Total detected rows:              {total_rows}")
        print(f" Rows with unresolved dates:       {unresolved_dates}")
        print(f" Rows with unresolved heads:       {unresolved_heads}")
        print(f" Rows with unresolved vouchers:    {unresolved_vouchers}")
        print(f" Rows with unresolved amounts:     {unresolved_amounts}")
        print(f" Rows with low narration conf:     {low_narration_conf}")
        print(f" Rows requiring review:            {rows_requiring_review} ({rows_requiring_review / total_rows * 100:.1f}%)" if total_rows else "0")
        print("-" * 70)
        print(f" Number of receipts:               {len(receipts)}  (Sum: ₹{total_receipts_sum:,.2f})")
        print(f" Number of payments:               {len(payments)}  (Sum: ₹{total_payments_sum:,.2f})")
        print(f" Opening balance detected:         {'YES' if opening_bal_detected else 'NO'}")
        print(f" Closing balance detected:         {'YES' if closing_bal_detected else 'NO'}")
        print("-" * 70)
        print(f" Output Excel Workbook:            {excel_path}")
        print(f" Output Raw JSON:                  {raw_json_path}")
        print("=" * 70 + "\n")


def parse_args():
    parser = argparse.ArgumentParser(description="AI-Assisted Handwritten Cashbook Digitization (Prototype 1)")
    parser.add_argument("--mock", action="store_true", help="Run with deterministic mock cashbook extractor")
    parser.add_argument("--input", type=str, default=None, help="Path to input image or directory of images")
    parser.add_argument("--output", type=str, default=None, help="Path to output .xlsx file")
    parser.add_argument("--provider", choices=["mock", "gemini"], default=DEFAULT_PROVIDER, help="Vision extraction provider")
    parser.add_argument("--no-preprocess", action="store_true", help="Disable CLAHE/downsampling preprocessing")
    return parser.parse_args()


def main():
    args = parse_args()

    # Determine provider
    if args.mock or args.provider == "mock":
        extractor = MockExtractor()
        logger.info("Using MockExtractor (Prototype 1 test suite)")
    elif args.provider == "gemini":
        extractor = GeminiVisionExtractor()
        logger.info("Using GeminiVisionExtractor")
    else:
        extractor = MockExtractor()

    pipeline = CashbookPipeline(extractor=extractor, enable_preprocessing=not args.no_preprocess)

    # Output file
    output_file = Path(args.output) if args.output else (OUTPUT_DIR / "cashbook_digitized.xlsx")

    # Input handling
    if args.input:
        in_path = Path(args.input)
        if in_path.is_file():
            image_files = [in_path]
        elif in_path.is_dir():
            image_files = [p for p in in_path.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
            if not image_files:
                logger.error(f"No valid image files found in {in_path}")
                sys.exit(1)
        else:
            logger.error(f"Input path not found: {in_path}")
            sys.exit(1)
    else:
        # Check if user added real images into data/input
        existing_images = [
            p for p in INPUT_DIR.iterdir()
            if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        ]
        if existing_images:
            image_files = existing_images
            logger.info(f"Detected {len(image_files)} images in {INPUT_DIR}")
        else:
            logger.info("No images in data/input. Simulating multi-page run with test sample pages...")
            sample_page1 = INPUT_DIR / "cashbook_sample_page1.jpg"
            sample_page2 = INPUT_DIR / "cashbook_sample_page2.jpg"

            from PIL import Image, ImageDraw
            img1 = Image.new("RGB", (1200, 1600), color=(245, 240, 230))
            draw = ImageDraw.Draw(img1)
            draw.text((100, 100), "Sample Cashbook Page 1 (Simulated)", fill=(20, 20, 20))
            img1.save(sample_page1)

            img2 = Image.new("RGB", (1200, 1600), color=(245, 240, 230))
            draw = ImageDraw.Draw(img2)
            draw.text((100, 100), "Sample Cashbook Page 2 (Simulated)", fill=(20, 20, 20))
            img2.save(sample_page2)

            image_files = [sample_page1, sample_page2]

    pipeline.process_images(image_files, output_file)


if __name__ == "__main__":
    main()
