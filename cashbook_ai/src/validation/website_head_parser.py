import csv
import logging
import re
from pathlib import Path
from typing import List

from src.models import TransactionType, WebsiteHead

logger = logging.getLogger(__name__)

class WebsiteHeadParser:
    """Parses website HTML source to extract authoritative website head options."""

    def parse_html(self, html_content: str) -> List[WebsiteHead]:
        heads = []
        # Pattern to match <option value="xxx">Label</option>
        pattern = re.compile(r'<option[^>]*value=["\']([^"\']*)["\'][^>]*>(.*?)</option>', re.IGNORECASE)
        
        for match in pattern.finditer(html_content):
            val = match.group(1).strip()
            label = match.group(2).strip()
            
            if not val:
                continue
            
            direction = TransactionType.UNKNOWN
            lower_label = label.lower()
            
            # Map website label hints to accounting transaction types
            if "debit" in lower_label or "(d)" in lower_label or "( d )" in lower_label:
                direction = TransactionType.PAYMENT
            elif "credit" in lower_label or "(c)" in lower_label or "( c )" in lower_label:
                direction = TransactionType.RECEIPT
                
            heads.append(WebsiteHead(
                website_head_value=val,
                website_head_label=label,
                direction=direction,
                source_type="HTML",
                review_required=False
            ))
            
        return heads

    def parse_file(self, filepath: Path) -> List[WebsiteHead]:
        if not filepath.exists():
            logger.warning(f"Website head source file not found: {filepath}")
            return []
            
        with open(filepath, "r", encoding="utf-8") as f:
            return self.parse_html(f.read())

    def export_csv(self, heads: List[WebsiteHead], out_path: Path):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["website_head_value", "website_head_label", "direction", "review_required"])
            for h in heads:
                writer.writerow([h.website_head_value, h.website_head_label, h.direction.value, h.review_required])
