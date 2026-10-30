# Cashbook AI — Technical Project Summary & State

## 1. Project Purpose

**Cashbook AI** is an AI-assisted cashbook digitization and accounting automation application. It streamlines the conversion of physical, handwritten cashbook photographs (containing Malayalam and English ledger rows on ruled pages) into structured, cryptographically verified accounting datasets for autonomous entry into the e-Dayan parish accounting web application.

The application eliminates error-prone manual data entry while enforcing strict accounting integrity, human review oversight, and mathematical reconciliation before and during live browser automation.

---

## 2. Hardware Environment (Verified Specifications)

* **Processor (CPU)**: AMD Ryzen 5 4600H (6 cores, 12 threads)
* **System Memory (RAM)**: 16 GB DDR4 (3200 MT/s)
* **Dedicated Graphics (GPU)**: NVIDIA GeForce GTX 1650 Ti (4 GB dedicated GDDR6 VRAM)
* **Storage**: 512 GB NVMe SSD
* **Operating System**: Microsoft Windows 11 (64-bit)
* **Resource Policy**: Memory-aware on-demand inference; local LLM processes unload automatically after transaction batches to ensure system stability.

---

## 3. High-Level Architecture

The system is structured into cleanly decoupled, testable subsystems:

```
[ Physical Ledger Photos ]
           │
           ▼
[ Source Ingestion & Hash Audit ] ──► (SHA-256 Image Fingerprinting)
           │
           ▼
[ OCR Extraction Layer ] ──────────► (Gemini Multimodal Vision API / Mock Provider)
           │
           ▼
[ Column Alignment Engine ] ───────► (Non-destructive transposition correction)
           │
           ▼
[ Canonical Registry & Mapping ] ──► (Levels 1–3: Exact Folios, Aliases, Rules)
           │
           ▼
[ Local LLM Assistant (Level 4) ] ─► (Constrained Qwen3.5-4B GPU Inference)
           │
           ▼
[ Review Intelligence UI ] ────────► (Web Dashboard, Card Review, Editing Dialog)
           │
           ▼
[ Draft Review Workbook (.xlsx) ] ─► (2-sheet Excel review draft with dropdowns)
           │
           ▼
[ Human Sign-off & Preflight ] ────► (Cryptographic Snapshot & Mathematical Balance Check)
           │
           ▼
[ Autonomous Execution Engine ] ──► (Guarded Browser Relay, 5-point DOM Verification)
           │
           ▼
[ Final Reconciliation Report ] ──► (Closing Balance & Transaction Audit Journal)
```

### Core Components
1. **Extraction Layer** (`src/extraction/`):
   * Abstracted extraction interface (`BaseExtractor`, `GeminiVisionExtractor`, `MockExtractor`).
   * Image preprocessing with EXIF auto-rotation and illumination normalization.
2. **Column Alignment** (`src/extraction/column_aligner.py`):
   * Detects and rectifies column transpositions (e.g. folio numbers placed in date or head columns) without mutating raw OCR evidence.
3. **Canonical Accounting Registry** (`src/reference/canonical_registry.py`):
   * In-memory indexed registry defining 60 canonical heads, 25 subheads, historical folios, approved aliases, and e-Dayan website selector identities.
4. **Hybrid Mapping Engine** (`src/mapping/hybrid_engine.py`):
   * Four-level tiered resolution:
     * **Level 1**: Exact Folio Lookup (highest priority deterministic match).
     * **Level 2**: Approved Alias Resolution (normalized exact match against dictionary).
     * **Level 3**: Contextual Accounting Rules (direction-aware narration pattern rules).
     * **Level 4**: Local LLM Assistant (on-demand constrained candidate selection).
5. **Review Intelligence & Exception Manager** (`src/mapping/review_manager.py`):
   * Transaction review queue, multi-item narration clustering, and draft workbook generation.
   * Full-featured transaction editing modal with canonical combobox search and subhead policy validation.
6. **Execution Engine & Preflight Validator** (`src/automation/`, `src/validation/`):
   * Validates signed-off review workbooks against schema, math, and website mapping constraints.
   * Bounded batch runner with single-owner process lock, pre-submit readback, and 5-point DOM table verification.

---

## 4. Implemented Technical Milestones

* **Milestone 1 — Canonical Accounting Registry & Ground Truth Infrastructure**:
  * Unified single-source-of-truth reference architecture separating canonical accounting identity from physical ledger notations and website selectors.
* **Milestone 2 — Column Alignment & Verified Ground Truth**:
  * Non-destructive column alignment engine resolving voucher and folio transpositions.
* **Milestone 3 — Deterministic Hybrid Mapping (Levels 1–3) & Subhead Intelligence**:
  * Multi-stage deterministic mapper with strict enforcement of `MANDATORY`, `OPTIONAL`, and `PROHIBITED` subhead policies.
* **Milestone 4 — Level 4 Local LLM Assistant**:
  * Integration of local `Qwen3.5-4B-Instruct-Q4_K_M` via `llama.cpp` using CUDA GPU offload and automatic lifecycle teardown.
* **Milestone 5 — Mapping Reliability, Confidence Calibration & Review Intelligence**:
  * Multi-criteria candidate generation, confidence score calibration, and narration similarity clustering.
* **Milestone 6 — Unified Local Windows Application**:
  * Self-contained FastAPI application and responsive web UI accessible locally without external orchestration.
* **Milestone 7 — Approved Workbook Intake to Autonomous Execution Engine**:
  * Preflight workbook intake, SHA-256 snapshotting, batch manifest authorization, and guarded browser entry via local relay.
* **Milestone 8 — Actual Accounting Period End-to-End Validation**:
  * Production image validation, complete ledger extraction, preflight reconciliation, and zero-variance cash balance verification.
* **Review Interface Improvements**:
  * Transaction editing dialog with live canonical head search combobox, dynamic subhead policy enforcement, audit trail tracking, segmented Receipts/Payments filtering, and interactive cluster filtering.

---

## 5. Development & Setup Instructions

### Prerequisites
* Windows 10/11 (64-bit).
* Python 3.12 (64-bit).
* NVIDIA GPU with CUDA driver (recommended for local LLM acceleration; CPU fallback supported).
* Google Chrome browser with Developer Mode enabled (for e-Dayan extension relay).

### Environment Setup
1. Clone the repository:
   ```cmd
   git clone https://github.com/midhunsiby7/edayan_automation.git
   cd edayan_automation
   ```
2. Create and activate a Python virtual environment:
   ```cmd
   python -m venv .venv
   .venv\Scripts\activate
   ```
3. Install dependencies:
   ```cmd
   pip install -r cashbook_ai/requirements.txt
   ```
4. Configure environment variables:
   * Copy `cashbook_ai/.env.example` to `cashbook_ai/.env`.
   * Add your Gemini API key (required only for live OCR extraction; testing runs fully on mock data).

---

## 6. Test Execution Instructions

Run the full regression test suite from the `cashbook_ai` directory:

```cmd
cd cashbook_ai
pytest tests
```

To run with verbose output:
```cmd
pytest tests -v
```

All tests execute entirely against isolated synthetic fixtures and mock extractors. No active network connection, API key, or live credentials are required to pass tests.

---

## 7. Upcoming Development Priorities

1. **Previous Period Handoff Automation**: Automated archival and balance forward propagation across successive accounting periods.
2. **Pluggable OCR Adapters**: Optional local OCR engines (e.g., Tesseract / Surya) for fully offline air-gapped image extraction.
3. **Enhanced Telemetry Reporting**: Exportable session reconciliation certificates and PDF audit summaries.
