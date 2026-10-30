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
ARCHIVE_DIR = DATA_DIR / "archive"
COMPLETED_DIR = DATA_DIR / "completed"
MONTHLY_DIR = DATA_DIR / "monthly"

# Ensure runtime directories exist
for directory in [INPUT_DIR, OUTPUT_DIR, SAMPLES_DIR, ARCHIVE_DIR, COMPLETED_DIR, MONTHLY_DIR]:
    directory.mkdir(parents=True, exist_ok=True)


def get_month_archive_dir(month: str, base_dir: Path = DATA_DIR) -> Path:
    """Return standard data/archive/{month} root directory."""
    return base_dir / "archive" / month


def get_month_archive_subdirs(month: str, base_dir: Path = DATA_DIR) -> dict[str, Path]:
    """Return categorized archive directory paths for a month."""
    root = get_month_archive_dir(month, base_dir)
    return {
        "incidents": root / "incidents",
        "manifests": root / "manifests",
        "execution_logs": root / "execution_logs",
        "reconciliation": root / "reconciliation",
        "intermediate": root / "intermediate",
        "extraction": root / "extraction",
    }


def get_month_output_dir(month: str | None = None, base_dir: Path = DATA_DIR) -> Path:
    """Return active output directory, partitioned by month if specified."""
    if month:
        target = base_dir / "output" / month
        target.mkdir(parents=True, exist_ok=True)
        return target
    return base_dir / "output"

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
