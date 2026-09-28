import csv
import logging
from pathlib import Path
from collections import defaultdict
import sys
import os

# Ensure the root folder is in the path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from src.extraction.head_master_extractor import HeadMasterExtractor
from src.models import AccountSet

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def main():
    candidate_models = ["gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.1-pro-preview", "gemini-pro-latest"]
    
    image1 = Path("data/reference/head_master_source/WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg")
    image2 = Path("data/reference/head_master_source/WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg")
    
    all_heads = []
    raw_data_all = {"files": {}}
    
    def try_extract_with_fallback(image_path: Path):
        for model in candidate_models:
            logger.info(f"Trying model {model} for {image_path.name}")
            try:
                extractor = HeadMasterExtractor(model_name=model)
                res, raw = extractor.extract(image_path, AccountSet.GENERAL)
                return res, raw
            except Exception as e:
                logger.error(f"Model {model} failed: {e}")
                continue
        raise RuntimeError(f"All candidate models failed for {image_path.name}")

    if image1.exists():
        res, raw = try_extract_with_fallback(image1)
        all_heads.extend(res)
        raw_data_all["files"][image1.name] = raw
    if image2.exists():
        res, raw = try_extract_with_fallback(image2)
        all_heads.extend(res)
        raw_data_all["files"][image2.name] = raw
        
    out_csv = Path("data/reference/accounting_heads_general.csv")
    out_raw = Path("data/reference/head_master_source/head_master_raw_extraction.json")
    
    import json
    out_raw.parent.mkdir(parents=True, exist_ok=True)
    with open(out_raw, "w", encoding="utf-8") as f:
        json.dump(raw_data_all, f, indent=2)
    
    # Analyze and validate
    number_to_names = defaultdict(set)
    name_to_numbers = defaultdict(set)
    
    # Assume heads are 1-101 for checking missing
    extracted_numbers = set()
    
    for h in all_heads:
        if h.accounting_head_number:
            extracted_numbers.add(h.accounting_head_number)
            number_to_names[h.accounting_head_number].add(h.accounting_head_name.lower().strip())
            name_to_numbers[h.accounting_head_name.lower().strip()].add(h.accounting_head_number)
            
    # Look for duplicates and conflicts
    duplicates = [num for num, names in number_to_names.items() if len(names) > 1]
    
    missing_numbers = []
    extra_numbers = []
    for num in extracted_numbers:
        try:
            val = int(num)
            if val < 1 or val > 101:
                extra_numbers.append(num)
        except ValueError:
            extra_numbers.append(num)
            
    # Check 1 to 101
    for i in range(1, 102):
        if str(i) not in extracted_numbers:
            missing_numbers.append(str(i))
            
    # Mark review required for conflicts and unclear items
    for h in all_heads:
        if h.accounting_head_number in duplicates:
            h.review_required = True
            if "Conflict" not in h.accounting_head_name:
                h.accounting_head_name = h.accounting_head_name + " (CONFLICT/REVIEW)"
            
    # Save CSV
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "account_set", "accounting_head_number", "accounting_head_name",
            "normalized_accounting_head_name", "source_image", "source_page",
            "source_row", "extraction_confidence", "review_required"
        ])
        for h in all_heads:
            writer.writerow([
                h.account_set.value,
                h.accounting_head_number,
                h.accounting_head_name,
                h.normalized_accounting_head_name or "",
                h.source_page or "",
                h.source_page or "",
                h.source_row or "",
                h.extraction_confidence,
                h.review_required
            ])
            
    # Output Report
    print("="*50)
    print("GENERAL ACCOUNTS HEAD MASTER INGESTION REPORT")
    print("="*50)
    print(f"Total entries extracted: {len(all_heads)}")
    print(f"Total unique head numbers: {len(extracted_numbers)}")
    print(f"Expected head count: 101")
    print(f"Missing head numbers (assuming 1-101 range): {missing_numbers}")
    print(f"Extra head numbers (outside 1-101 range): {extra_numbers}")
    print(f"Conflicting head definitions (duplicate numbers with different names): {duplicates}")
    
    review_rows = [h for h in all_heads if h.review_required or h.extraction_confidence < 0.6]
    print(f"Rows requiring manual review (conflict or low confidence): {len(review_rows)}")
    print(f"Output CSV path: {out_csv.absolute()}")
    print(f"Raw Extraction Output: {out_raw.absolute()}")
    print("="*50)
    
if __name__ == "__main__":
    main()
