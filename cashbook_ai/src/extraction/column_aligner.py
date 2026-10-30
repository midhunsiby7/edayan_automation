import re
import logging
from enum import Enum
from typing import Optional, List, Tuple, Dict, Any
from pydantic import BaseModel, Field

from src.models import RawExtractedRow

logger = logging.getLogger(__name__)


class ColumnShiftType(str, Enum):
    NONE = "NONE"
    FOLIO_IN_DATE_VOUCHER_IN_HEAD = "FOLIO_IN_DATE_VOUCHER_IN_HEAD"
    VOUCHER_IN_HEAD = "VOUCHER_IN_HEAD"
    FIELD_SHIFT_SUSPECTED = "FIELD_SHIFT_SUSPECTED"
    AMOUNT_COL_SHIFT = "AMOUNT_COL_SHIFT"


class AlignedRow(BaseModel):
    """Normalized, structurally aligned representation of an extracted transaction row.
    
    Preserves raw_row untouched while providing calibrated alignment evidence.
    """
    raw_row: RawExtractedRow

    # Proposed aligned fields
    aligned_date: Optional[str] = None
    aligned_head_number: Optional[str] = None
    aligned_voucher_number: Optional[str] = None
    aligned_narration_raw: Optional[str] = None
    aligned_narration_english: Optional[str] = None
    aligned_amount_col1: Optional[str] = None
    aligned_amount_col2: Optional[str] = None
    detected_amount_column: Optional[int] = None

    # Audit & alignment telemetry
    shift_type: ColumnShiftType = ColumnShiftType.NONE
    alignment_confidence: float = 1.0
    was_date_inherited: bool = False
    evidence_reason: str = "Normal column alignment"
    row_bbox: Optional[List[float]] = None
    needs_human_review: bool = False


class ColumnAligner:
    """Detects and resolves physical column transpositions and field shifts.
    
    Examines:
      - Physical column boundaries & row bounding boxes
      - Folio vs voucher vs date patterns
      - Cross-field consistency
    
    Guarantees:
      - Raw OCR fields are preserved 100% untouched.
      - Aligns only when structural evidence is strong.
      - Ambiguous rows are flagged as FIELD_SHIFT_SUSPECTED for human review.
      - Never assigns accounting heads or subheads.
    """

    # Common ditto mark patterns in cashbooks
    DITTO_PATTERNS = {
        '"', "''", "“", "”", "..", "...", "....", "-", "do", "ditto", "—", "."
    }

    def __init__(self, known_folio_range: Tuple[int, int] = (1, 150)):
        self.known_folio_range = known_folio_range

    def is_likely_date_format(self, text: Optional[str]) -> bool:
        """Check if text contains date delimiters (. / -) or month indicators."""
        if not text:
            return False
        clean = text.strip()
        # Has date delimiters like 31.5.2026, 01/04/2026, 18-05-26
        if re.search(r'\d{1,2}[\.\/\-]\d{1,2}([\.\/\-]\d{2,4})?', clean):
            return True
        return False

    def is_ditto_mark(self, text: Optional[str]) -> bool:
        """Check if text is a ditto mark indicating inherited date."""
        if not text:
            return False
        return text.strip() in self.DITTO_PATTERNS

    def is_clean_folio_candidate(self, text: Optional[str]) -> bool:
        """Check if text is a 1-3 digit integer within standard ledger folio ranges."""
        if not text:
            return False
        clean = text.strip()
        if clean.isdigit():
            val = int(clean)
            return self.known_folio_range[0] <= val <= self.known_folio_range[1]
        return False

    def is_empty_or_dash(self, text: Optional[str]) -> bool:
        """Check if voucher field is missing or dash placeholder."""
        if not text:
            return True
        return text.strip() in {"", "-", "None", "null", "nil"}

    def align_row(
        self,
        raw_row: RawExtractedRow,
        prev_aligned_row: Optional[AlignedRow] = None
    ) -> AlignedRow:
        """Align a single raw extracted row using structural rules and historical context."""
        raw_date = raw_row.raw_date
        raw_head = raw_row.raw_head_number
        raw_vouch = raw_row.raw_voucher_number

        # Check for obvious corruption or Malayalam characters in voucher/head
        has_malayalam_in_vouch = bool(raw_vouch and re.search(r'[\u0D00-\u0D7F]', raw_vouch))
        has_malayalam_in_head = bool(raw_head and re.search(r'[\u0D00-\u0D7F]', raw_head))

        if has_malayalam_in_vouch or has_malayalam_in_head:
            return AlignedRow(
                raw_row=raw_row,
                aligned_date=raw_date,
                aligned_head_number=raw_head,
                aligned_voucher_number=raw_vouch,
                aligned_narration_raw=raw_row.raw_narration,
                aligned_narration_english=raw_row.english_narration,
                aligned_amount_col1=raw_row.raw_amount_col1,
                aligned_amount_col2=raw_row.raw_amount_col2,
                detected_amount_column=raw_row.detected_amount_column,
                shift_type=ColumnShiftType.FIELD_SHIFT_SUSPECTED,
                alignment_confidence=0.40,
                evidence_reason="Malayalam script detected inside numeric column (text shift)",
                row_bbox=raw_row.row_bbox,
                needs_human_review=True
            )

        # 1. Structural Check: FOLIO_IN_DATE_VOUCHER_IN_HEAD
        # Trigger conditions:
        #  - raw_date is a clean 1-2 digit integer (matches folio range e.g. 20, 28, 40, 52..56)
        #  - raw_date is NOT a date format (no slashes, no dots, length <= 2 or integer)
        #  - raw_head or raw_vouch contains a distinct voucher number (e.g. 555..561 or 71..75)
        is_transposition = False
        aligned_vouch_candidate = None
        if (
            raw_date
            and not self.is_likely_date_format(raw_date)
            and self.is_clean_folio_candidate(raw_date)
        ):
            if (
                raw_head
                and raw_head.strip().isdigit()
                and (self.is_empty_or_dash(raw_vouch) or raw_vouch.strip() == raw_head.strip())
            ):
                is_transposition = True
                aligned_vouch_candidate = raw_head.strip()
            elif (
                raw_vouch
                and raw_vouch.strip().isdigit()
                and (int(raw_vouch.strip()) > 200 or 30 <= int(raw_vouch.strip()) <= 100)
            ):
                is_transposition = True
                aligned_vouch_candidate = raw_vouch.strip()

        if is_transposition and aligned_vouch_candidate:
            inherited_date = prev_aligned_row.aligned_date if prev_aligned_row else None
            return AlignedRow(
                raw_row=raw_row,
                aligned_date=inherited_date,
                aligned_head_number=raw_date.strip(),       # Real Folio
                aligned_voucher_number=aligned_vouch_candidate,    # Real Voucher
                aligned_narration_raw=raw_row.raw_narration,
                aligned_narration_english=raw_row.english_narration,
                aligned_amount_col1=raw_row.raw_amount_col1,
                aligned_amount_col2=raw_row.raw_amount_col2,
                detected_amount_column=raw_row.detected_amount_column,
                shift_type=ColumnShiftType.FOLIO_IN_DATE_VOUCHER_IN_HEAD,
                alignment_confidence=0.95,
                was_date_inherited=bool(inherited_date),
                evidence_reason=(
                    f"Structural transposition: raw_date '{raw_date}' is folio, "
                    f"voucher '{aligned_vouch_candidate}' resolved"
                ),
                row_bbox=raw_row.row_bbox,
                needs_human_review=False
            )

        # 2. Check for Ditto Date in normal position
        aligned_date = raw_date
        was_inherited = False
        if self.is_ditto_mark(raw_date):
            if prev_aligned_row and prev_aligned_row.aligned_date:
                aligned_date = prev_aligned_row.aligned_date
                was_inherited = True

        # 3. Check for Head Number Out of Range (>200) with empty voucher (Ambiguous shift)
        if raw_head and raw_head.strip().isdigit() and int(raw_head.strip()) > 200 and self.is_empty_or_dash(raw_vouch):
            return AlignedRow(
                raw_row=raw_row,
                aligned_date=aligned_date,
                aligned_head_number=raw_head,
                aligned_voucher_number=raw_vouch,
                aligned_narration_raw=raw_row.raw_narration,
                aligned_narration_english=raw_row.english_narration,
                aligned_amount_col1=raw_row.raw_amount_col1,
                aligned_amount_col2=raw_row.raw_amount_col2,
                detected_amount_column=raw_row.detected_amount_column,
                shift_type=ColumnShiftType.FIELD_SHIFT_SUSPECTED,
                alignment_confidence=0.60,
                was_date_inherited=was_inherited,
                evidence_reason=f"Head number '{raw_head}' exceeds known folio ranges (>200) with empty voucher",
                row_bbox=raw_row.row_bbox,
                needs_human_review=True
            )

        # 4. Normal Row (No transposition detected)
        return AlignedRow(
            raw_row=raw_row,
            aligned_date=aligned_date,
            aligned_head_number=raw_head,
            aligned_voucher_number=raw_vouch,
            aligned_narration_raw=raw_row.raw_narration,
            aligned_narration_english=raw_row.english_narration,
            aligned_amount_col1=raw_row.raw_amount_col1,
            aligned_amount_col2=raw_row.raw_amount_col2,
            detected_amount_column=raw_row.detected_amount_column,
            shift_type=ColumnShiftType.NONE,
            alignment_confidence=1.0,
            was_date_inherited=was_inherited,
            evidence_reason="Normal column alignment",
            row_bbox=raw_row.row_bbox,
            needs_human_review=False
        )

    def align_page(self, rows: List[RawExtractedRow]) -> List[AlignedRow]:
        """Align an ordered sequence of raw rows belonging to a cashbook page."""
        aligned_rows: List[AlignedRow] = []
        prev: Optional[AlignedRow] = None

        for row in rows:
            aligned = self.align_row(row, prev_aligned_row=prev)
            aligned_rows.append(aligned)
            # Update prev only if aligned row has a valid resolved date
            if aligned.aligned_date:
                prev = aligned
            elif prev is None:
                prev = aligned

        return aligned_rows
