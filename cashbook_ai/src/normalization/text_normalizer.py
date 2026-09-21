import re
from typing import Optional, Tuple


class TextNormalizer:
    """Normalizes text, head numbers, and voucher numbers while preserving authentic Malayalam script."""

    MALAYALAM_RANGE = re.compile(r"[\u0D00-\u0D7F]")

    @classmethod
    def contains_malayalam(cls, text: Optional[str]) -> bool:
        if not text:
            return False
        return bool(cls.MALAYALAM_RANGE.search(text))

    @staticmethod
    def clean_narration(raw_text: Optional[str]) -> Optional[str]:
        if not raw_text:
            return None
        # Normalize repeated whitespaces, strip leading/trailing whitespace
        cleaned = re.sub(r"\s+", " ", raw_text).strip()
        return cleaned if cleaned else None

    @staticmethod
    def normalize_head_number(raw_head: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        if not raw_head or not raw_head.strip():
            return None, None
        cleaned = raw_head.strip()
        if cleaned in {"-", "—", "nil", "n/a", "/"}:
            return None, None
        # Check for non-numeric or ambiguous characters
        if re.search(r"[\?\*xX#]", cleaned):
            return cleaned, f"Ambiguous head number: '{raw_head}'"
        # Extract digits
        digits = re.sub(r"\D", "", cleaned)
        if digits:
            return digits, None
        return cleaned, f"Non-numeric head number: '{raw_head}'"

    @staticmethod
    def normalize_voucher_number(raw_voucher: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        """Normalizes voucher number.
        Crucial: '-' or dash here indicates no voucher, NOT a ditto mark!
        """
        if not raw_voucher or not raw_voucher.strip():
            return None, None
        cleaned = raw_voucher.strip()
        # Explicit dash, nil, none, or slash indicates no voucher
        if cleaned in {"-", "–", "—", "nil", "n/a", "/", "null", "none"}:
            return None, None
        if re.search(r"[\?\*xX#]", cleaned):
            return cleaned, f"Ambiguous voucher number: '{raw_voucher}'"
        digits = re.sub(r"\D", "", cleaned)
        if digits:
            return digits, None
        return cleaned, f"Non-numeric voucher: '{raw_voucher}'"
