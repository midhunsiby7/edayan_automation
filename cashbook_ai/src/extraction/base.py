from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional
from src.models import RawExtractedRow


class BaseExtractor(ABC):
    """Abstract interface for cashbook vision/OCR extraction providers."""

    @abstractmethod
    def extract(self, image_path: Path, preprocessed_image_path: Optional[Path] = None) -> List[RawExtractedRow]:
        """Extract raw structured rows with column evidence and bounding boxes from a cashbook page image."""
        pass
