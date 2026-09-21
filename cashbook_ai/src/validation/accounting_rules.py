import logging
import re
from typing import List, Optional
from config import CONFIDENCE_THRESHOLD
from src.models import (
    ProcessedRow,
    RawExtractedRow,
    RecordType,
    TransactionType,
)
from src.normalization.amount_normalizer import AmountNormalizer
from src.normalization.date_normalizer import DateNormalizer
from src.normalization.text_normalizer import TextNormalizer

logger = logging.getLogger(__name__)

OPENING_BALANCE_KEYWORDS = {
    "opening balance",
    "opening bal",
    "തുടക്ക ബാക്കി",
    "തുടക്കബാക്കി",
    "ആരംഭ ബാക്കി",
    "മുൻ ബാക്കി",
    "മുൻബാക്കി",
    "b/f",
    "brought forward",
}

CLOSING_BALANCE_KEYWORDS = {
    "closing balance",
    "closing bal",
    "നീക്കിയിരിപ്പ്",
    "അവസാന ബാക്കി",
    "അവസാനബാക്കി",
    "c/f",
    "carried forward",
}


class AccountingRuleEngine:
    """Applies strict accounting rules, physical column-based receipt/payment mapping, and human audit flags."""

    def __init__(self, date_normalizer: Optional[DateNormalizer] = None):
        self.date_normalizer = date_normalizer or DateNormalizer()

    def process_row(self, raw_row: RawExtractedRow) -> ProcessedRow:
        reasons: List[str] = []

        # 1. Normalize Date using scoped date normalizer
        resolved_date, was_inherited, date_err = self.date_normalizer.normalize(raw_row.raw_date)
        if date_err:
            reasons.append(f"Date issue: {date_err}")

        # 2. Normalize Head & Voucher Numbers
        head_num, head_err = TextNormalizer.normalize_head_number(raw_row.raw_head_number)
        if head_err:
            reasons.append(f"Head number: {head_err}")

        voucher_num, voucher_err = TextNormalizer.normalize_voucher_number(raw_row.raw_voucher_number)
        if voucher_err:
            reasons.append(f"Voucher number: {voucher_err}")

        # 3. Clean Narrations
        clean_raw_narration = TextNormalizer.clean_narration(raw_row.raw_narration)
        clean_eng_narration = TextNormalizer.clean_narration(raw_row.english_narration)

        # 4. Identify Record Type (Opening Balance / Closing Balance / Transaction)
        record_type = self._determine_record_type(clean_raw_narration, clean_eng_narration, voucher_num)

        # 5. Amount & Physical Column Classification (Receipt vs Payment)
        receipt_amount: Optional[float] = None
        payment_amount: Optional[float] = None
        transaction_type = TransactionType.UNKNOWN

        # Parse amounts from physical columns
        amount_col1, err_col1 = AmountNormalizer.normalize(raw_row.raw_amount_col1)
        amount_col2, err_col2 = AmountNormalizer.normalize(raw_row.raw_amount_col2)
        generic_amount, err_generic = AmountNormalizer.normalize(raw_row.raw_amount)

        if err_col1:
            reasons.append(f"Col 1 Amount: {err_col1}")
        if err_col2:
            reasons.append(f"Col 2 Amount: {err_col2}")
        if err_generic and not (amount_col1 or amount_col2):
            reasons.append(f"Amount: {err_generic}")

        # Physical column positioning rule:
        # Col 1 (nearer/first) = Receipt
        # Col 2 (second/farther) = Payment
        col_pos = raw_row.detected_amount_column

        if amount_col1 is not None and amount_col2 is None:
            receipt_amount = amount_col1
            transaction_type = (
                TransactionType.INFORMATIONAL
                if record_type in (RecordType.OPENING_BALANCE, RecordType.CLOSING_BALANCE)
                else TransactionType.RECEIPT
            )
        elif amount_col2 is not None and amount_col1 is None:
            payment_amount = amount_col2
            transaction_type = (
                TransactionType.INFORMATIONAL
                if record_type in (RecordType.OPENING_BALANCE, RecordType.CLOSING_BALANCE)
                else TransactionType.PAYMENT
            )
        elif amount_col1 is not None and amount_col2 is not None:
            # Both columns populated: anomaly
            receipt_amount = amount_col1
            payment_amount = amount_col2
            transaction_type = TransactionType.UNKNOWN
            reasons.append("Both receipt and payment columns have amounts on the same row")
        elif generic_amount is not None:
            # Fall back to detected_amount_column flag if col-specific strings were empty
            if col_pos == 1:
                receipt_amount = generic_amount
                transaction_type = (
                    TransactionType.INFORMATIONAL
                    if record_type in (RecordType.OPENING_BALANCE, RecordType.CLOSING_BALANCE)
                    else TransactionType.RECEIPT
                )
            elif col_pos == 2:
                payment_amount = generic_amount
                transaction_type = (
                    TransactionType.INFORMATIONAL
                    if record_type in (RecordType.OPENING_BALANCE, RecordType.CLOSING_BALANCE)
                    else TransactionType.PAYMENT
                )
            else:
                reasons.append("Amount detected but physical column position (Col 1 vs Col 2) is unknown")
        else:
            if record_type == RecordType.TRANSACTION:
                reasons.append("Missing transaction amount")

        # 6. Evaluate Confidence Scores and Review Triggers
        review_required = False

        if raw_row.extraction_confidence < CONFIDENCE_THRESHOLD:
            reasons.append(
                f"Low extraction confidence ({raw_row.extraction_confidence:.2f} < {CONFIDENCE_THRESHOLD:.2f})"
            )

        if raw_row.amount_confidence < 0.60 and (raw_row.raw_amount or raw_row.raw_amount_col1 or raw_row.raw_amount_col2):
            reasons.append(f"Low amount confidence ({raw_row.amount_confidence:.2f})")

        if raw_row.date_confidence < 0.60 and raw_row.raw_date:
            reasons.append(f"Low date confidence ({raw_row.date_confidence:.2f})")

        if raw_row.head_confidence < 0.60 and raw_row.raw_head_number:
            reasons.append(f"Low head number confidence ({raw_row.head_confidence:.2f})")

        if raw_row.voucher_confidence < 0.60 and raw_row.raw_voucher_number:
            reasons.append(f"Low voucher confidence ({raw_row.voucher_confidence:.2f})")

        if len(reasons) > 0:
            review_required = True

        return ProcessedRow(
            source_image=raw_row.source_image,
            source_page=raw_row.source_page,
            source_row=raw_row.source_row,
            row_bbox=raw_row.row_bbox,
            raw_date=raw_row.raw_date,
            resolved_date=resolved_date,
            head_number=head_num,
            voucher_number=voucher_num,
            raw_narration=clean_raw_narration,
            english_narration=clean_eng_narration,
            raw_amount=raw_row.raw_amount,
            raw_amount_col1=raw_row.raw_amount_col1,
            raw_amount_col2=raw_row.raw_amount_col2,
            detected_amount_column=col_pos,
            receipt_amount=receipt_amount,
            payment_amount=payment_amount,
            transaction_type=transaction_type,
            record_type=record_type,
            date_confidence=raw_row.date_confidence,
            head_confidence=raw_row.head_confidence,
            voucher_confidence=raw_row.voucher_confidence,
            narration_confidence=raw_row.narration_confidence,
            amount_confidence=raw_row.amount_confidence,
            extraction_confidence=raw_row.extraction_confidence,
            review_required=review_required,
            review_reasons=reasons,
        )

    def _determine_record_type(
        self,
        raw_narration: Optional[str],
        eng_narration: Optional[str],
        voucher_num: Optional[str],
    ) -> RecordType:
        """Determines record type (OPENING_BALANCE, CLOSING_BALANCE, TRANSACTION, etc.)."""
        combined = f"{raw_narration or ''} {eng_narration or ''}".lower()

        for kw in OPENING_BALANCE_KEYWORDS:
            if kw in combined:
                return RecordType.OPENING_BALANCE

        for kw in CLOSING_BALANCE_KEYWORDS:
            if kw in combined:
                return RecordType.CLOSING_BALANCE

        if voucher_num and voucher_num.isdigit():
            return RecordType.TRANSACTION

        return RecordType.TRANSACTION
