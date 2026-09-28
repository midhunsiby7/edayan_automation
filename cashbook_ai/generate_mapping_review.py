import csv
import difflib
from pathlib import Path
import openpyxl

def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = text.lower()
    for char in "()&,-/:":
        text = text.replace(char, " ")
    return " ".join(text.split())

def main():
    crosswalk_path = Path("data/reference/general_head_crosswalk.csv")
    website_heads_path = Path("data/reference/website_heads.csv")
    out_path = Path("data/reference/general_head_mapping_review.xlsx")
    
    # 1. Load website heads
    website_heads = []
    with open(website_heads_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            website_heads.append(row)
            
    # 2. Load crosswalk results
    crosswalk = []
    mapped_website_values = set()
    with open(crosswalk_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            crosswalk.append(row)
            if row.get("website_head_value"):
                mapped_website_values.add(row["website_head_value"])
                
    unmapped_crosswalk = [r for r in crosswalk if r["mapping_status"] == "NOT_FOUND"]
    mapped_crosswalk = [r for r in crosswalk if r["mapping_status"] != "NOT_FOUND"]
    conflicts = [r for r in crosswalk if r["mapping_status"] == "CONFLICT"]
    
    unused_website_heads = [w for w in website_heads if w["website_head_value"] not in mapped_website_values]
    
    wb = openpyxl.Workbook()
    
    # --- REVIEW SHEET ---
    ws_review = wb.active
    ws_review.title = "Review"
    ws_review.append([
        "accounting_head_number",
        "accounting_head_name_original",
        "expected_direction",
        "mapping_status",
        "reason_for_not_found",
        "candidate_website_heads",
        "candidate_website_values",
        "review_required"
    ])
    
    for row in unmapped_crosswalk:
        acc_name = row["accounting_head_name_original"]
        direction = row["transaction_type"]
        norm_acc = normalize_text(acc_name)
        
        # Find candidates (unused ones with matching direction)
        candidates = []
        for w in unused_website_heads:
            if direction != "UNKNOWN" and w["direction"] != "UNKNOWN" and w["direction"] != direction:
                continue
            norm_web = normalize_text(w["website_head_label"])
            score = difflib.SequenceMatcher(None, norm_acc, norm_web).ratio()
            candidates.append((w, score))
            
        candidates.sort(key=lambda x: x[1], reverse=True)
        top_candidates = candidates[:3]
        
        cand_labels = " | ".join(c[0]["website_head_label"] for c in top_candidates if c[1] > 0.3)
        cand_vals = " | ".join(c[0]["website_head_value"] for c in top_candidates if c[1] > 0.3)
        
        ws_review.append([
            row["accounting_head_number"],
            acc_name,
            direction,
            row["mapping_status"],
            row["mapping_warning"],
            cand_labels,
            cand_vals,
            "TRUE"
        ])
        
    # --- SUMMARY SHEET ---
    ws_summary = wb.create_sheet("Summary")
    ws_summary.append(["Metric", "Count"])
    ws_summary.append(["Total accounting heads supplied", len(crosswalk)])
    ws_summary.append(["Mapped", len(mapped_crosswalk)])
    ws_summary.append(["Unresolved", len(unmapped_crosswalk)])
    ws_summary.append(["Conflicts", len(conflicts)])
    ws_summary.append(["Website options available", len(website_heads)])
    ws_summary.append(["Website options used", len(mapped_website_values)])
    ws_summary.append(["Website options unused", len(unused_website_heads)])
    
    wb.save(out_path)
    print(f"Generated review report: {out_path.absolute()}")

if __name__ == "__main__":
    main()
