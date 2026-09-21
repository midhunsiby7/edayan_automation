import logging
from pathlib import Path
from typing import Optional, Tuple
import cv2
import numpy as np
from PIL import Image, ImageOps

from config import (
    MAX_IMAGE_DIMENSION,
    ENABLE_CLAHE,
    CLAHE_CLIP_LIMIT,
    CLAHE_GRID_SIZE,
    OUTPUT_DIR,
)

logger = logging.getLogger(__name__)


class ImagePreprocessor:
    """Lightweight and memory-conscious preprocessor for cashbook photographs."""

    def __init__(
        self,
        max_dimension: int = MAX_IMAGE_DIMENSION,
        enable_clahe: bool = ENABLE_CLAHE,
    ):
        self.max_dimension = max_dimension
        self.enable_clahe = enable_clahe
        self.debug_dir = OUTPUT_DIR / "preprocessed"
        self.debug_dir.mkdir(parents=True, exist_ok=True)

    def preprocess(self, image_path: Path, save_debug: bool = False) -> Tuple[np.ndarray, Path]:
        """Preprocess cashbook image for OCR/vision:
        1. Correct EXIF orientation (e.g. mobile photo capture).
        2. Downscale memory-safely if image exceeds max dimension.
        3. Enhance contrast & normalize illumination using CLAHE.
        """
        logger.info(f"Preprocessing image: {image_path.name}")
        
        # Step 1: Open with PIL to safely handle EXIF rotation without loading into GPU
        with Image.open(image_path) as pil_img:
            pil_img = ImageOps.exif_transpose(pil_img)
            
            # Step 2: Memory-safe resize maintaining aspect ratio
            width, height = pil_img.size
            max_dim = max(width, height)
            if max_dim > self.max_dimension:
                scale = self.max_dimension / float(max_dim)
                new_width = int(width * scale)
                new_height = int(height * scale)
                logger.info(f"Resizing from {width}x{height} to {new_width}x{new_height} to conserve RAM")
                pil_img = pil_img.resize((new_width, new_height), Image.Resampling.LANCZOS)

            # Convert to RGB numpy array
            rgb_array = np.array(pil_img.convert("RGB"))

        # Convert RGB to BGR for OpenCV processing
        bgr_image = cv2.cvtColor(rgb_array, cv2.COLOR_RGB2BGR)

        # Step 3: Contrast Limited Adaptive Histogram Equalization (CLAHE) on Luminance
        if self.enable_clahe:
            lab = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2LAB)
            l_channel, a_channel, b_channel = cv2.split(lab)
            clahe = cv2.createCLAHE(clipLimit=CLAHE_CLIP_LIMIT, tileGridSize=CLAHE_GRID_SIZE)
            cl = clahe.apply(l_channel)
            enhanced_lab = cv2.merge((cl, a_channel, b_channel))
            processed_bgr = cv2.cvtColor(enhanced_lab, cv2.COLOR_LAB2BGR)
        else:
            processed_bgr = bgr_image

        output_path = self.debug_dir / f"preprocessed_{image_path.stem}.jpg"
        if save_debug:
            cv2.imwrite(str(output_path), processed_bgr)
            logger.info(f"Saved debug preprocessed image to {output_path}")

        return processed_bgr, output_path
