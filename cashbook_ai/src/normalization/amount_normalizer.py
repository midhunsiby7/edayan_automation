import logging
import re
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class AmountNormalizer:
    """Sanitizes and converts raw OCR amount strings into validated numeric floats without inventing digits."""

    @staticmethod
    def normalize(raw_amount: Optional[str]) -> Tuple[Optional[float], Optional[str]]:
        """Normalize raw amount string into float.
        Returns:
            (numeric_amount, error_or_warning)
        """
        if not raw_amount or not raw_amount.strip():
            return None, None

        cleaned = raw_amount.strip()

        # Detect ambiguous or unreadable characters (e.g. '?', '*', 'x')
        if re.search(r"[\?\*xX#]", cleaned):
            return None, f"Ambiguous characters in amount: '{raw_amount}'"

        # Remove currency symbols and common ledger endings like /=, /-, Rs, etc.
        cleaned = re.sub(r"(?i)(rs\.?|inr|₹|/-|/=)", "", cleaned).strip()

        # Handle trailing dash or dash representing 0 paise (e.g., "700 -", "3600-", "15000 -")
        cleaned = re.sub(r"\s*-\s*$", "", cleaned).strip()

        # Handle ledger format where space separates Rupees and Paise (e.g. "33382 60" -> "33382.60")
        match_paise = re.match(r"^(\d[\d,]*)\s+(\d{2})$", cleaned)
        if match_paise:
            cleaned = f"{match_paise.group(1)}.{match_paise.group(2)}"
        else:
            # Remove remaining spaces inside numbers
            cleaned = cleaned.replace(" ", "")

        # Handle OCR substitutions if obvious (e.g., uppercase O in numerical context)
        # Only replace O with 0 if surrounded by digits or at end of decimal
        cleaned = re.sub(r"(?<=\d)[oO]|[oO](?=\d)", "0", cleaned)

        # Handle Indian numbering format commas (e.g. 1,00,000.00 or 10,000.00)
        # If there are multiple commas and a single dot: remove commas
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(",", "")
        elif "," in cleaned and "." not in cleaned:
            # Check if comma was used as decimal separator (common in handwriting) or thousands separator
            parts = cleaned.split(",")
            if len(parts) == 2 and len(parts[1]) == 2:
                cleaned = f"{parts[0]}.{parts[1]}"
            else:
                cleaned = cleaned.replace(",", "")

        # Now extract the decimal number
        match = re.search(r"^\d+(?:\.\d{1,2})?$", cleaned)
        if match:
            try:
                val = float(match.group(0))
                return val, None
            except ValueError:
                pass

        return None, f"Failed to parse valid numeric amount from '{raw_amount}'"
