import logging
from pathlib import Path
from typing import List, Optional
from src.extraction.base import BaseExtractor
from src.models import RawExtractedRow

logger = logging.getLogger(__name__)


class MockExtractor(BaseExtractor):
    """Deterministic Mock Extractor reflecting authentic handwritten cashbook conventions:
    - Opening Balance with no voucher
    - Head 78 / Voucher 1 / payment 700
    - Head 78 / Voucher 2 / payment 1080
    - Ditto date continuation
    - Physical amount column indexing (Col 1 = Receipt, Col 2 = Payment)
    - Uncertain smudged row triggering review_required
    - Multi-page continuation support
    """

    def extract(self, image_path: Path, preprocessed_image_path: Optional[Path] = None) -> List[RawExtractedRow]:
        image_name = image_path.name
        logger.info(f"MockExtractor generating test extractions for {image_name}")

        page_str = "1"
        if "page_2" in image_name.lower() or "page2" in image_name.lower():
            page_str = "2"

        if page_str == "2":
            # Page 2 scenario: starts with ditto mark to test cross-page date inheritance
            return [
                RawExtractedRow(
                    source_image=image_name,
                    source_page=page_str,
                    source_row=1,
                    row_bbox=[80.0, 45.0, 140.0, 960.0],
                    raw_date='"',  # Ditto mark inherits last date from Page 1
                    raw_head_number="78",
                    raw_voucher_number="6",
                    raw_narration="സ്റ്റേഷനറി സാധനങ്ങൾ",
                    english_narration="Stationery items",
                    raw_amount="450.00",
                    raw_amount_col1=None,
                    raw_amount_col2="450.00",
                    detected_amount_column=2,
                    date_confidence=0.95,
                    head_confidence=0.96,
                    voucher_confidence=0.98,
                    narration_confidence=0.92,
                    amount_confidence=0.95,
                    extraction_confidence=0.95,
                ),
                RawExtractedRow(
                    source_image=image_name,
                    source_page=page_str,
                    source_row=2,
                    row_bbox=[145.0, 45.0, 205.0, 960.0],
                    raw_date="10/04/2024",
                    raw_head_number="05",
                    raw_voucher_number="7",
                    raw_narration="വാടക വരുമാനം",
                    english_narration="Rent income",
                    raw_amount="3500.00",
                    raw_amount_col1="3500.00",
                    raw_amount_col2=None,
                    detected_amount_column=1,
                    date_confidence=0.97,
                    head_confidence=0.95,
                    voucher_confidence=0.98,
                    narration_confidence=0.94,
                    amount_confidence=0.97,
                    extraction_confidence=0.96,
                ),
            ]

        # Default: Page 1 scenario
        return [
            # 1. Opening Balance with no voucher
            RawExtractedRow(
                source_image=image_name,
                source_page=page_str,
                source_row=1,
                row_bbox=[90.0, 50.0, 150.0, 950.0],
                raw_date="01/04/2024",
                raw_head_number=None,
                raw_voucher_number=None,
                raw_narration="തുടക്ക ബാക്കി",
                english_narration="Opening Balance",
                raw_amount="25000.00",
                raw_amount_col1="25000.00",
                raw_amount_col2=None,
                detected_amount_column=1,  # First amount column = Receipt
                date_confidence=0.98,
                head_confidence=1.0,
                voucher_confidence=1.0,
                narration_confidence=0.97,
                amount_confidence=0.98,
                extraction_confidence=0.98,
            ),
            # 2. Head 78 / Voucher 1 / payment 700
            RawExtractedRow(
                source_image=image_name,
                source_page=page_str,
                source_row=2,
                row_bbox=[155.0, 50.0, 215.0, 950.0],
                raw_date="02/04/2024",
                raw_head_number="78",
                raw_voucher_number="1",
                raw_narration="വൈദ്യുതി ചാർജ്ജ് അടച്ചത്",
                english_narration="Electricity bill paid",
                raw_amount="700.00",
                raw_amount_col1=None,
                raw_amount_col2="700.00",
                detected_amount_column=2,  # Second amount column = Payment
                date_confidence=0.96,
                head_confidence=0.95,
                voucher_confidence=0.98,
                narration_confidence=0.92,
                amount_confidence=0.96,
                extraction_confidence=0.95,
            ),
            # 3. Head 78 / Voucher 2 / payment 1080 with ditto date behavior
            RawExtractedRow(
                source_image=image_name,
                source_page=page_str,
                source_row=3,
                row_bbox=[220.0, 50.0, 280.0, 950.0],
                raw_date='"',  # Ditto mark, should inherit 02/04/2024
                raw_head_number="78",
                raw_voucher_number="2",
                raw_narration="ഓഫീസ് ചിലവുകൾ",
                english_narration="Office expenses",
                raw_amount="1080.00",
                raw_amount_col1=None,
                raw_amount_col2="1080.00",
                detected_amount_column=2,  # Second amount column = Payment
                date_confidence=0.94,
                head_confidence=0.95,
                voucher_confidence=0.98,
                narration_confidence=0.90,
                amount_confidence=0.96,
                extraction_confidence=0.94,
            ),
            # 4. Another ditto date ("do") with voucher "-" testing requirement 2
            RawExtractedRow(
                source_image=image_name,
                source_page=page_str,
                source_row=4,
                row_bbox=[285.0, 50.0, 345.0, 950.0],
                raw_date="do",  # Should inherit 02/04/2024
                raw_head_number="15",
                raw_voucher_number="-",  # Dash here must NOT be treated as ditto mark!
                raw_narration="പത്ര മാസികകൾ",
                english_narration="Newspapers and periodicals",
                raw_amount="150.00",
                raw_amount_col1=None,
                raw_amount_col2="150.00",
                detected_amount_column=2,
                date_confidence=0.91,
                head_confidence=0.90,
                voucher_confidence=0.92,
                narration_confidence=0.91,
                amount_confidence=0.95,
                extraction_confidence=0.92,
            ),
            # 5. Receipt transaction (Column 1)
            RawExtractedRow(
                source_image=image_name,
                source_page=page_str,
                source_row=5,
                row_bbox=[350.0, 50.0, 410.0, 950.0],
                raw_date="05/04/2024",
                raw_head_number="04",
                raw_voucher_number="3",
                raw_narration="വാർഷിക വരിസംഖ്യ",
                english_narration="Annual subscription",
                raw_amount="2500.00",
                raw_amount_col1="2500.00",
                raw_amount_col2=None,
                detected_amount_column=1,
                date_confidence=0.97,
                head_confidence=0.94,
                voucher_confidence=0.97,
                narration_confidence=0.93,
                amount_confidence=0.96,
                extraction_confidence=0.95,
            ),
            # 6. Uncertain row with low confidence triggering review_required
            RawExtractedRow(
                source_image=image_name,
                source_page=page_str,
                source_row=6,
                row_bbox=[415.0, 50.0, 475.0, 950.0],
                raw_date="05/04/2024",
                raw_head_number="?1",
                raw_voucher_number="4",
                raw_narration="അറ്റകുറ്റപ്പണി",
                english_narration="Repair and maintenance",
                raw_amount="8?0.00",
                raw_amount_col1=None,
                raw_amount_col2="8?0.00",
                detected_amount_column=2,
                date_confidence=0.88,
                head_confidence=0.45,  # Low confidence
                voucher_confidence=0.90,
                narration_confidence=0.85,
                amount_confidence=0.42,  # Low confidence
                extraction_confidence=0.52,  # Below threshold
            ),
        ]
