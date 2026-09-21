import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Base paths
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
INPUT_DIR = DATA_DIR / "input"
OUTPUT_DIR = DATA_DIR / "output"
SAMPLES_DIR = DATA_DIR / "samples"

# Ensure runtime directories exist
for directory in [INPUT_DIR, OUTPUT_DIR, SAMPLES_DIR]:
    directory.mkdir(parents=True, exist_ok=True)

# Extraction Provider Settings
DEFAULT_PROVIDER = os.getenv("CASHBOOK_PROVIDER", "mock")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

# Validation & Confidence Thresholds
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.75"))

# Preprocessing Settings (Lightweight & memory conscious for 8GB RAM)
MAX_IMAGE_DIMENSION = int(os.getenv("MAX_IMAGE_DIMENSION", "2400"))
ENABLE_CLAHE = True
CLAHE_CLIP_LIMIT = 2.0
CLAHE_GRID_SIZE = (8, 8)

# Logging Settings
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FORMAT = "%(asctime)s - [%(levelname)s] - [%(name)s] - %(message)s"
