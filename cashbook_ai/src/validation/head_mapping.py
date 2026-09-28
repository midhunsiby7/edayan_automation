import csv
import logging
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import difflib
from pydantic import BaseModel
from src.models import AccountSet, AccountingHead, WebsiteHead, TransactionType

logger = logging.getLogger(__name__)

class MappingResult(BaseModel):
    account_set: AccountSet
    accounting_head_number: str
    accounting_head_name_original: str
    transaction_type: TransactionType
    website_head_value: Optional[str] = None
    website_head_label_exact: Optional[str] = None
    mapping_status: str = "PENDING"
    mapping_confidence: float = 0.0
    mapping_warning: str = ""
    source_accounting_image: str = ""
    source_website_source: str = "HTML"
    review_required: bool = False

class HeadMapper:
    """Maps physical accounting heads to website heads deterministically."""

    def __init__(self, accounting_heads: List[AccountingHead], website_heads: List[WebsiteHead]):
        self.accounting_heads = accounting_heads
        self.website_heads = website_heads

    def normalize_text(self, text: str) -> str:
        if not text:
            return ""
        # Remove punctuation, extra spaces, lowercase
        text = text.lower()
        for char in "()&,-/:":
            text = text.replace(char, " ")
        return " ".join(text.split())
        
    def _find_matches(self, acc_name: str, direction: TransactionType) -> List[Tuple[WebsiteHead, str, float]]:
        """Returns list of (website_head, status, confidence)"""
        norm_acc = self.normalize_text(acc_name)
        matches = []
        
        for w_head in self.website_heads:
            # Must match direction (or UNKNOWN/INFORMATIONAL handling if applicable, but instruction says direction is mandatory)
            if direction != TransactionType.UNKNOWN and w_head.direction != TransactionType.UNKNOWN:
                if direction != w_head.direction:
                    continue
                    
            norm_web = self.normalize_text(w_head.website_head_label)
            
            # Exact match (normalized)
            if norm_acc == norm_web:
                matches.append((w_head, "MATCHED", 1.0))
                continue
                
            # Semantic/Wording variation match
            # "Rented Building Expense" vs "Rented Building Expenses"
            # "Bank Interest" vs "Bank Interest / Other Interest Received"
            # Since difflib doesn't have token_set_ratio, we do a basic ratio on substrings if needed,
            # or just simple ratio. Given the instructions, we can check basic ratio.
            score = difflib.SequenceMatcher(None, norm_acc, norm_web).ratio()
            # If one is a substring of another, boost the score
            if norm_acc in norm_web or norm_web in norm_acc:
                score = max(score, 0.86)
                
            if score >= 0.85:
                matches.append((w_head, "MATCHED_NAME_VARIATION", score))
                
        return sorted(matches, key=lambda x: x[2], reverse=True)

    def map_heads(self) -> List[MappingResult]:
        results = []
        
        for acc in self.accounting_heads:
            # We map twice per head: once for RECEIPT, once for PAYMENT, 
            # because some heads might theoretically have both (unlikely for specific expenses, but we process them as requested).
            # The prompt implies one mapping per head if it's strictly an expense/income.
            # In our list, image 1 is Income (RECEIPT), image 2 is Expenditure (PAYMENT).
            # We can infer direction from the source image or we can just try both.
            # I will infer from source_page since it was partitioned.
            direction = TransactionType.UNKNOWN
            if acc.source_page:
                direction = TransactionType.RECEIPT if "7.30.28" in acc.source_page else TransactionType.PAYMENT
            
            matches = self._find_matches(acc.accounting_head_name, direction)
            
            res = MappingResult(
                account_set=acc.account_set,
                accounting_head_number=acc.accounting_head_number,
                accounting_head_name_original=acc.accounting_head_name,
                transaction_type=direction,
                source_accounting_image=acc.source_page or ""
            )
            
            if not matches:
                res.mapping_status = "NOT_FOUND"
                res.review_required = True
                res.mapping_warning = "No website head matched."
            elif len(matches) == 1 or (matches[0][2] - matches[1][2] > 0.1): 
                # Clear single winner
                best_match, status, conf = matches[0]
                res.website_head_value = best_match.website_head_value
                res.website_head_label_exact = best_match.website_head_label
                res.mapping_status = status
                res.mapping_confidence = conf
            else:
                # Multiple close matches -> conflict
                res.mapping_status = "CONFLICT"
                res.review_required = True
                res.mapping_warning = f"Multiple possible matches: {[m[0].website_head_label for m in matches[:3]]}"
                
            results.append(res)
            
        return results

    def save_crosswalk(self, results: List[MappingResult], out_path: Path):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "account_set", "accounting_head_number", "accounting_head_name_original",
                "transaction_type", "website_head_value", "website_head_label_exact",
                "mapping_status", "mapping_confidence", "mapping_warning",
                "source_accounting_image", "source_website_source", "review_required"
            ])
            for r in results:
                writer.writerow([
                    r.account_set.value, r.accounting_head_number, r.accounting_head_name_original,
                    r.transaction_type.value, r.website_head_value or "", r.website_head_label_exact or "",
                    r.mapping_status, f"{r.mapping_confidence:.2f}", r.mapping_warning,
                    r.source_accounting_image, r.source_website_source, r.review_required
                ])

    def generate_report(self, results: List[MappingResult]):
        total = len(results)
        matched = sum(1 for r in results if r.mapping_status == "MATCHED")
        variation = sum(1 for r in results if r.mapping_status == "MATCHED_NAME_VARIATION")
        conflicts = sum(1 for r in results if r.mapping_status == "CONFLICT")
        not_found = sum(1 for r in results if r.mapping_status == "NOT_FOUND")
        review_req = sum(1 for r in results if r.review_required)
        
        print("="*50)
        print("HEAD MAPPING VALIDATION REPORT")
        print("="*50)
        print(f"Accounting heads compared: {total}")
        print(f"Exact matches: {matched}")
        print(f"Name-variation matches: {variation}")
        print(f"Conflicts: {conflicts}")
        print(f"Not found: {not_found}")
        print(f"Review-required mappings: {review_req}")
        
        # Also, calculate website heads not mapped
        mapped_web_vals = {r.website_head_value for r in results if r.website_head_value}
        unmapped_web = [w.website_head_label for w in self.website_heads if w.website_head_value not in mapped_web_vals]
        print(f"Website heads not yet mapped: {len(unmapped_web)}")
        print("="*50)
