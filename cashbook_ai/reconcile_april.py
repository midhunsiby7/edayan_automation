import json
import logging
from pathlib import Path
from src.models import RawExtractedRow, ProcessedRow, RecordType, TransactionType
from src.validation.accounting_rules import AccountingRuleEngine
from src.normalization.date_normalizer import DateNormalizer
from src.validation.head_master import HeadMasterRepository
from src.export.excel_exporter import ExcelExporter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def main():
    raw_file = Path("data/monthly/raw_extractions.json")
    if not raw_file.exists():
        logger.error(f"Cannot find {raw_file}")
        return
        
    with open(raw_file, "r", encoding="utf-8") as f:
        raw_data = json.load(f)
        
    raw_rows = [RawExtractedRow(**r) for r in raw_data]
    
    # Process
    date_normalizer = DateNormalizer()
    head_master_repo = HeadMasterRepository(Path("data/reference/active/accounting_heads_general_verified.csv"))
    rule_engine = AccountingRuleEngine(date_normalizer=date_normalizer, head_master_repo=head_master_repo)
    
    processed_rows = []
    dropped = []
    
    # Group by page
    from collections import defaultdict
    pages = defaultdict(list)
    for r in raw_rows:
        pages[r.source_page].append(r)
        
    for page_num in sorted(pages.keys()):
        for raw in pages[page_num]:
            try:
                proc = rule_engine.process_row(raw)
                processed_rows.append(proc)
            except Exception as e:
                import traceback
                traceback.print_exc()
                dropped.append({"source_page": raw.source_page, "source_row": raw.source_row, "reason": str(e)})

    # Recalculate metrics
    receipts = [r for r in processed_rows if r.transaction_type == TransactionType.RECEIPT]
    payments = [r for r in processed_rows if r.transaction_type == TransactionType.PAYMENT]
    informational = [r for r in processed_rows if r.record_type in (RecordType.OPENING_BALANCE, RecordType.CLOSING_BALANCE, RecordType.INFORMATIONAL)]
    review_req = [r for r in processed_rows if r.data_review_required]
    web_pending = [r for r in processed_rows if r.website_mapping_pending]
    
    counts = {
        "RAW_EXTRACTED": len(raw_rows),
        "NORMALIZED": len(processed_rows),
        "ACCOUNTING_VALIDATED": len(processed_rows) - len(review_req),
        "PROCESSED": len(processed_rows),
        "ALL_TRANSACTIONS_EXPORT": len(processed_rows),
        "RECEIPTS_EXPORT": len(receipts),
        "PAYMENTS_EXPORT": len(payments),
        "REVIEW_REQUIRED": len(review_req),
        "INFORMATIONAL": len(informational),
        "WEBSITE_MAPPING_PENDING": len(web_pending),
        "DROPPED": len(dropped)
    }
    
    # Missing/Duplicate trace
    trace_status = []
    for raw in raw_rows:
        matching_procs = [p for p in processed_rows if p.source_image == raw.source_image and p.source_row == raw.source_row]
        if len(matching_procs) == 0:
            status = "MISSING_FROM_OUTPUT"
        elif len(matching_procs) > 1:
            status = "DUPLICATE_OUTPUT"
        else:
            p = matching_procs[0]
            if p.record_type == RecordType.INFORMATIONAL or p.record_type == RecordType.OPENING_BALANCE:
                status = "INFORMATIONAL"
            elif p.data_review_required:
                status = "REVIEW_REQUIRED"
            else:
                status = "MATCHED"
        trace_status.append({
            "source_image": raw.source_image,
            "source_page": raw.source_page,
            "source_row": raw.source_row,
            "status": status
        })
        
    # Field Shift warnings
    field_shifts = [
        {"source_page": p.source_page, "source_row": p.source_row, "raw_head": p.raw_head_number, "raw_voucher": p.raw_voucher_number, "raw_date": p.raw_date, "reasons": p.review_reason}
        for p in processed_rows if "FIELD_SHIFT_SUSPECTED" in p.review_reason
    ]
    
    # Subheads 52-56 check
    subheads_check = [
        {"source_page": p.source_page, "source_row": p.source_row, "head_no": p.accounting_head_number, "head_name": p.accounting_head_name, "subhead": p.subhead}
        for p in processed_rows if p.accounting_head_number in ["52", "53", "54", "55", "56"]
    ]
    
    # Website mapping status
    web_mapping = []
    for p in processed_rows:
        if p.record_type == RecordType.TRANSACTION:
            if not p.accounting_head_name:
                s = "C. ACCOUNTING_HEAD_NOT_FOUND"
            elif p.website_mapping_pending:
                s = "B. ACCOUNTING_HEAD_FOUND + WEBSITE_MAPPING_NOT_FOUND"
            else:
                s = "A. ACCOUNTING_HEAD_FOUND + WEBSITE_MAPPING_FOUND"
            web_mapping.append({
                "source_page": p.source_page,
                "source_row": p.source_row,
                "head_no": p.accounting_head_number,
                "status": s
            })
            
    # Write to Excel using openpyxl
    out_path = Path("data/monthly/2026-04/april_reconciliation_report.xlsx")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    import openpyxl
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    
    def write_sheet(wb, sheet_name, list_of_dicts):
        ws = wb.create_sheet(sheet_name)
        if not list_of_dicts:
            return
        headers = list(list_of_dicts[0].keys())
        ws.append(headers)
        for row in list_of_dicts:
            ws.append([row.get(h) for h in headers])
            
    write_sheet(wb, "Pipeline_Counts", [counts])
    write_sheet(wb, "Source_Traceability", trace_status)
    write_sheet(wb, "Missing_Rows", dropped if dropped else [{"status": "None"}])
    write_sheet(wb, "Field_Shift_Warnings", field_shifts if field_shifts else [{"status": "None"}])
    write_sheet(wb, "Website_Mapping_Status", web_mapping if web_mapping else [{"status": "None"}])
    write_sheet(wb, "Subhead_Check", subheads_check if subheads_check else [{"status": "None"}])
    
    wb.save(out_path)
        
    print(f"Report generated: {out_path}")
    for k, v in counts.items():
        print(f"{k}: {v}")

    # Re-export the main excel file using updated rule engine logic so that it matches new columns
    exporter = ExcelExporter()
    exporter.export(processed_rows, Path("data/monthly/2026-04_processed.xlsx"))

if __name__ == "__main__":
    main()
