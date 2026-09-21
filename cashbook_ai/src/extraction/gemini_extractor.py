import json
import logging
from pathlib import Path
from typing import List, Optional
from PIL import Image

from config import GEMINI_API_KEY, GEMINI_MODEL_NAME
from src.extraction.base import BaseExtractor
from src.models import RawExtractedRow

logger = logging.getLogger(__name__)


PROMPT_CASHBOOK_EXTRACTION = """
You are an expert handwritten accounting document analyst specializing in handwritten Malayalam and English cashbooks.
Analyze this cashbook page photograph and extract all transaction and balance rows into a JSON array.

CRITICAL ACCOUNTING & EXTRACTION RULES:
1. Column Layout of the Cashbook:
   - Leftmost column: Date (or ditto marks like ", .., -)
   - "Page" column: Head number / Folio number (e.g. 78, 14, 5, 59, 28, 52)
   - "V.No" column: Voucher number (e.g. 1, 2, 3, 514, 627, or '-' for no voucher)
   - Middle column: Particulars / Narration (Handwritten Malayalam, English, or mixed)
   - Nearer Amount column (first amount column): Receipts
   - Farther Amount column (second amount column): Payments

2. Physical Amount Columns:
   - You MUST report the physical position in `detected_amount_column` (1 or 2).
   - Place the raw text in `raw_amount_col1` if physically in Column 1 (Receipts).
   - Place the raw text in `raw_amount_col2` if physically in Column 2 (Payments).
   - Note on amounts: "33382 60" means 33382.60, "700 -" or "3600 -" means 700.00 or 3600.00.
   - NEVER classify receipts/payments based on narration keywords. Only record physical column evidence!

3. Malayalam & English Narrations:
   - Preserve the exact handwritten Malayalam narration in `raw_narration` without altering or simplifying it.
   - Provide an accurate English translation or interpretation in `english_narration`.

4. Date & Ditto Marks:
   - Record the exact handwritten characters in `raw_date`. If ditto marks (", .., do, -) appear, capture them verbatim.

5. Head & Voucher Numbers:
   - "Page" column value goes to `raw_head_number`.
   - "V.No" column value goes to `raw_voucher_number`. Opening/closing balance lines usually have '-' or blank voucher.

6. Confidence & Bounding Boxes:
   - Provide a confidence score between 0.0 and 1.0 for each field (date, head, voucher, narration, amount, overall).
   - If a number or word is smudged, crossed out, or ambiguous, assign low confidence (< 0.6) and do not invent digits.
   - Provide row bounding box as normalized coordinates [ymin, xmin, ymax, xmax] from 0 to 1000.

Return ONLY a valid JSON object matching this schema:
{
  "rows": [
    {
      "source_row": 1,
      "row_bbox": [ymin, xmin, ymax, xmax],
      "raw_date": "01/04/2024",
      "raw_head_number": "78",
      "raw_voucher_number": "1",
      "raw_narration": "വൈദ്യുതി ബിൽ",
      "english_narration": "Electricity bill",
      "raw_amount": "700.00",
      "raw_amount_col1": null,
      "raw_amount_col2": "700.00",
      "detected_amount_column": 2,
      "date_confidence": 0.95,
      "head_confidence": 0.95,
      "voucher_confidence": 0.98,
      "narration_confidence": 0.95,
      "amount_confidence": 0.95,
      "extraction_confidence": 0.95
    }
  ]
}
"""


class GeminiVisionExtractor(BaseExtractor):
    """Multimodal Vision Extractor using Google Gemini API for complex Malayalam/English cashbook pages."""

    def __init__(self, api_key: Optional[str] = None, model_name: str = GEMINI_MODEL_NAME):
        self.api_key = api_key or GEMINI_API_KEY
        self.model_name = model_name

    def extract(self, image_path: Path, preprocessed_image_path: Optional[Path] = None) -> List[RawExtractedRow]:
        if not self.api_key:
            raise ValueError(
                "GEMINI_API_KEY is not set. Please set the environment variable GEMINI_API_KEY "
                "or run with the '--mock' flag to test the pipeline using the built-in MockExtractor."
            )

        target_path = preprocessed_image_path or image_path
        logger.info(f"Extracting cashbook rows with Gemini ({self.model_name}) from {target_path.name}")

        try:
            from google import genai
            from google.genai import types
        except ImportError:
            raise ImportError(
                "google-genai package is required for GeminiVisionExtractor. "
                "Install it using: pip install google-genai"
            )

        client = genai.Client(api_key=self.api_key)
        pil_img = Image.open(target_path)

        import time
        response = None
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                response = client.models.generate_content(
                    model=self.model_name,
                    contents=[pil_img, PROMPT_CASHBOOK_EXTRACTION],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.1,
                    ),
                )
                break
            except Exception as e:
                err_msg = str(e)
                if ("503" in err_msg or "UNAVAILABLE" in err_msg or "429" in err_msg) and attempt < max_retries:
                    wait_time = attempt * 5
                    logger.warning(f"Gemini API transient error (attempt {attempt}/{max_retries}): {err_msg[:80]}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    raise

        raw_text = response.text or ""
        cleaned_text = raw_text.strip()
        if cleaned_text.startswith("```"):
            cleaned_text = cleaned_text.split("\n", 1)[-1]
            if cleaned_text.endswith("```"):
                cleaned_text = cleaned_text.rsplit("```", 1)[0].strip()

        try:
            data = json.loads(cleaned_text)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Gemini JSON output: {e}\nRaw output: {raw_text}")
            raise ValueError(f"Gemini returned invalid JSON for {image_path.name}: {e}")

        raw_rows = data.get("rows", [])
        if not isinstance(raw_rows, list):
            raw_rows = []

        results: List[RawExtractedRow] = []
        for idx, row in enumerate(raw_rows, start=1):
            results.append(
                RawExtractedRow(
                    source_image=image_path.name,
                    source_page=row.get("source_page", "1"),
                    source_row=row.get("source_row", idx),
                    row_bbox=row.get("row_bbox"),
                    raw_date=row.get("raw_date"),
                    raw_head_number=str(row.get("raw_head_number")) if row.get("raw_head_number") is not None else None,
                    raw_voucher_number=str(row.get("raw_voucher_number")) if row.get("raw_voucher_number") is not None else None,
                    raw_narration=row.get("raw_narration"),
                    english_narration=row.get("english_narration"),
                    raw_amount=str(row.get("raw_amount")) if row.get("raw_amount") is not None else None,
                    raw_amount_col1=str(row.get("raw_amount_col1")) if row.get("raw_amount_col1") is not None else None,
                    raw_amount_col2=str(row.get("raw_amount_col2")) if row.get("raw_amount_col2") is not None else None,
                    detected_amount_column=row.get("detected_amount_column"),
                    date_confidence=float(row.get("date_confidence", 0.9)),
                    head_confidence=float(row.get("head_confidence", 0.9)),
                    voucher_confidence=float(row.get("voucher_confidence", 0.9)),
                    narration_confidence=float(row.get("narration_confidence", 0.9)),
                    amount_confidence=float(row.get("amount_confidence", 0.9)),
                    extraction_confidence=float(row.get("extraction_confidence", 0.9)),
                )
            )

        logger.info(f"Gemini extracted {len(results)} rows from {image_path.name}")
        return results
