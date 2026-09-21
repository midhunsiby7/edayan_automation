import logging
import re
from datetime import datetime
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Scoped ONLY to the date column. Never used on voucher or other fields.
DATE_DITTO_PATTERNS = {
    '"', '""', "''", "'", "do", "do.", "ditto", "ditt", "..", "-", "–", "—", "="
}


class DateNormalizer:
    """Accounting date normalizer with job-level stateful inheritance across pages."""

    def __init__(self):
        # Persists across pages within a single monthly processing job
        self.last_resolved_date: Optional[str] = None

    def reset_job_state(self):
        """Reset state when beginning a new independent batch/month."""
        self.last_resolved_date = None

    def is_ditto(self, raw_date: Optional[str]) -> bool:
        """Strictly scoped check for date continuation/ditto mark in date field."""
        if not raw_date:
            return False
        cleaned = raw_date.strip().lower()
        return cleaned in DATE_DITTO_PATTERNS

    def normalize(self, raw_date: Optional[str]) -> Tuple[Optional[str], bool, Optional[str]]:
        """Normalize raw date into YYYY-MM-DD.
        Returns:
            (resolved_date, was_inherited, warning_reason)
        """
        if not raw_date or not raw_date.strip():
            return None, False, "Missing date"

        cleaned = raw_date.strip()

        # Check if date contains a ditto/continuation mark
        if self.is_ditto(cleaned):
            if self.last_resolved_date:
                logger.debug(f"Date ditto mark '{cleaned}' inherited previous date: {self.last_resolved_date}")
                return self.last_resolved_date, True, None
            else:
                return None, False, f"Ditto mark '{cleaned}' encountered with no prior resolved date"

        # Attempt standard date parsing
        resolved = self._parse_explicit_date(cleaned)
        if resolved:
            self.last_resolved_date = resolved
            return resolved, False, None

        # If date cannot be parsed reliably, do not silently invent
        return None, False, f"Unparseable date format: '{raw_date}'"

    def _parse_explicit_date(self, text: str) -> Optional[str]:
        """Try parsing standard date formats commonly found in Indian cashbooks."""
        # Clean common OCR noise like trailing dots, commas
        sanitized = re.sub(r"[^\d/\-\.]", "", text).strip(".-/,")
        
        # Formats to try
        formats = [
            "%d/%m/%Y",
            "%d-%m-%Y",
            "%d.%m.%Y",
            "%d/%m/%y",
            "%d-%m-%y",
            "%d.%m.%y",
            "%Y-%m-%d",
            "%Y/%m/%d",
        ]

        for fmt in formats:
            try:
                dt = datetime.strptime(sanitized, fmt)
                # Adjust 2-digit years if needed
                if dt.year < 100:
                    dt = dt.replace(year=2000 + dt.year)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue

        # If it's just day of the month (e.g. "12") and we already have a last_resolved_date
        if re.fullmatch(r"\d{1,2}", sanitized) and self.last_resolved_date:
            try:
                day_num = int(sanitized)
                if 1 <= day_num <= 31:
                    last_dt = datetime.strptime(self.last_resolved_date, "%Y-%m-%d")
                    new_dt = last_dt.replace(day=day_num)
                    return new_dt.strftime("%Y-%m-%d")
            except ValueError:
                pass

        return None
