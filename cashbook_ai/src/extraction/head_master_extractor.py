import json
import logging
from pathlib import Path
from typing import List, Optional, Tuple, Dict
from PIL import Image

from config import GEMINI_API_KEY, GEMINI_MODEL_NAME
from src.models import AccountingHead, AccountSet

logger = logging.getLogger(__name__)


PROMPT_HEAD_MASTER_EXTRACTION = """
You are an expert handwritten accounting document analyst.
Analyze this handwritten General Accounts Head Master photograph.
Extract all accounting head numbers and their corresponding head names into a JSON array.

CRITICAL RULES:
1. Each row typically contains a head number and a head name.
2. Preserve the exact handwritten head number.
3. Preserve the exact handwritten head name.
4. Provide a confidence score (0.0 to 1.0) for the extraction.
5. If unreadable or smudged, assign low confidence. Do not invent missing heads.
6. Return ALL heads you can see.

Return ONLY a valid JSON object matching this schema:
{
  "heads": [
    {
      "accounting_head_number": "78",
      "accounting_head_name": "Office Expenses",
      "extraction_confidence": 0.95,
      "source_row": 1
    }
  ]
}
"""

class HeadMasterExtractor:
    """Multimodal Vision Extractor for Head Master mapping (number -> name)."""

    def __init__(self, api_key: Optional[str] = None, model_name: str = GEMINI_MODEL_NAME):
        self.api_key = api_key or GEMINI_API_KEY
        self.model_name = model_name

    def extract(self, image_path: Path, account_set: AccountSet = AccountSet.GENERAL) -> Tuple[List[AccountingHead], Dict]:
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY is not set.")

        logger.info(f"Extracting Head Master rows with Gemini ({self.model_name}) from {image_path.name}")

        try:
            from google import genai
            from google.genai import types
        except ImportError:
            raise ImportError(
                "google-genai package is required for HeadMasterExtractor. "
                "Install it using: pip install google-genai"
            )

        client = genai.Client(api_key=self.api_key)
        pil_img = Image.open(image_path)

        import time
        response = None
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                response = client.models.generate_content(
                    model=self.model_name,
                    contents=[pil_img, PROMPT_HEAD_MASTER_EXTRACTION],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.1,
                    ),
                )
                break
            except Exception as e:
                err_msg = str(e)
                if ("503" in err_msg or "UNAVAILABLE" in err_msg or "429" in err_msg) and attempt < max_retries:
                    wait_time = 1  # Fast fail for testing candidate models
                    logger.warning(f"Gemini API transient error (attempt {attempt}/{max_retries}): {err_msg[:80]}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Gemini API failed after {attempt} attempts: {e}.")
                    raise e

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

        raw_heads = data.get("heads", [])
        if not isinstance(raw_heads, list):
            raw_heads = []

        results: List[AccountingHead] = []
        for idx, row in enumerate(raw_heads, start=1):
            results.append(
                AccountingHead(
                    account_set=account_set,
                    accounting_head_number=str(row.get("accounting_head_number", "")),
                    accounting_head_name=str(row.get("accounting_head_name", "")),
                    source_page=image_path.name,
                    source_row=row.get("source_row", idx),
                    extraction_confidence=float(row.get("extraction_confidence", 0.9)),
                )
            )

        logger.info(f"Gemini extracted {len(results)} head entries from {image_path.name}")
        return results, data

    def _mock_extract(self, image_path: Path, account_set: AccountSet) -> List[AccountingHead]:
        logger.warning(f"Using Mock Extraction for {image_path.name}")
        results = []
        # Simulate extracting a subset of the 101 heads (e.g., 1 to 50 for the first image, 51 to 101 for the second)
        start_idx = 1 if "28" in image_path.name else 51
        end_idx = 50 if start_idx == 1 else 101
        
        for i in range(start_idx, end_idx + 1):
            # Simulate a missing head for testing
            if i == 45:
                continue
            
            name = f"Mock Head {i}"
            if i == 78:
                name = "Office Expenses"
                
            results.append(
                AccountingHead(
                    account_set=account_set,
                    accounting_head_number=str(i),
                    accounting_head_name=name,
                    source_page=image_path.name,
                    source_row=i,
                    extraction_confidence=0.95
                )
            )
            
            # Simulate a duplicate conflict for testing
            if i == 80:
                results.append(
                    AccountingHead(
                        account_set=account_set,
                        accounting_head_number=str(i),
                        accounting_head_name="Conflicting Mock Head 80",
                        source_page=image_path.name,
                        source_row=i+1,
                        extraction_confidence=0.85
                    )
                )
        return results
