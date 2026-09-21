from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field


class TransactionType(str, Enum):
    RECEIPT = "RECEIPT"
    PAYMENT = "PAYMENT"
    INFORMATIONAL = "INFORMATIONAL"
    UNKNOWN = "UNKNOWN"


class RecordType(str, Enum):
    OPENING_BALANCE = "OPENING_BALANCE"
    TRANSACTION = "TRANSACTION"
    CLOSING_BALANCE = "CLOSING_BALANCE"
    INFORMATIONAL = "INFORMATIONAL"
    UNKNOWN = "UNKNOWN"


class RawExtractedRow(BaseModel):
    """Raw extraction output directly from the OCR/vision provider before normalization."""
    source_image: str
    source_page: str
    source_row: int
    row_bbox: Optional[List[float]] = Field(
        default=None,
        description="Bounding box [ymin, xmin, ymax, xmax] normalized or pixel coordinates"
    )

    # Raw extracted text per cell
    raw_date: Optional[str] = None
    raw_head_number: Optional[str] = None
    raw_voucher_number: Optional[str] = None
    raw_narration: Optional[str] = None
    english_narration: Optional[str] = None

    # Physical amount column evidence
    raw_amount: Optional[str] = None
    raw_amount_col1: Optional[str] = Field(
        default=None,
        description="Nearer/first physical amount column (Receipt column)"
    )
    raw_amount_col2: Optional[str] = Field(
        default=None,
        description="Second/farther physical amount column (Payment column)"
    )
    detected_amount_column: Optional[int] = Field(
        default=None,
        description="1 = Column 1 (nearer/receipt), 2 = Column 2 (farther/payment), None = undetectable"
    )

    # Granular confidence scores (0.0 to 1.0)
    date_confidence: float = 1.0
    head_confidence: float = 1.0
    voucher_confidence: float = 1.0
    narration_confidence: float = 1.0
    amount_confidence: float = 1.0
    extraction_confidence: float = 1.0


class ProcessedRow(BaseModel):
    """Normalized, validated, accounting-aware row ready for audit and Excel export."""
    source_image: str
    source_page: str
    source_row: int
    row_bbox: Optional[List[float]] = None

    # Date fields
    raw_date: Optional[str] = None
    resolved_date: Optional[str] = None

    # Identifiers
    head_number: Optional[str] = None
    voucher_number: Optional[str] = None

    # Narrations
    raw_narration: Optional[str] = None
    english_narration: Optional[str] = None

    # Physical amount evidence preserved
    raw_amount: Optional[str] = None
    raw_amount_col1: Optional[str] = None
    raw_amount_col2: Optional[str] = None
    detected_amount_column: Optional[int] = None

    # Resolved amounts
    receipt_amount: Optional[float] = None
    payment_amount: Optional[float] = None

    # Accounting classifications
    transaction_type: TransactionType = TransactionType.UNKNOWN
    record_type: RecordType = RecordType.UNKNOWN

    # Confidence scores
    date_confidence: float = 1.0
    head_confidence: float = 1.0
    voucher_confidence: float = 1.0
    narration_confidence: float = 1.0
    amount_confidence: float = 1.0
    extraction_confidence: float = 1.0

    # Human audit flags
    review_required: bool = False
    review_reasons: List[str] = Field(default_factory=list)
