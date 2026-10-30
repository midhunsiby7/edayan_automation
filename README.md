# Cashbook AI

**AI-Assisted Cashbook Digitization & Autonomous Accounting Automation for e-Dayan**

Cashbook AI is a local Windows application designed to streamline the digitization and automation of church and parish financial records. It converts physical, handwritten ledger photographs (containing mixed Malayalam and English entries on ruled cashbook pages) into verified accounting datasets, provides human review intelligence, and executes guarded autonomous batch entries into the e-Dayan accounting portal.

---

## Table of Contents

- [Project Overview](#project-overview)
- [Architecture](#architecture)
- [Hardware Requirements](#hardware-requirements)
- [Installation & Setup](#installation--setup)
- [Launching the Application](#launching-the-application)
- [Testing](#testing)
- [Privacy & Security](#privacy--security)
- [Project Status](#project-status)
- [License](#license)

---

## Project Overview

The traditional cashbook data-entry process involves manually transcribing handwritten ledger rows, interpreting Malayalam narrations, looking up historical accounting head numbers, resolving dynamic subheads, and typing each transaction into a web portal.

Cashbook AI transforms this into an auditable, human-in-the-loop automated workflow:

1. **Physical Image Ingestion**: Uploads multi-page photographs of ruled cashbook ledgers with automatic orientation correction and SHA-256 fingerprinting.
2. **Malayalam & English OCR**: Extracts raw ledger lines using multimodal vision APIs with field-level confidence scoring.
3. **Column Alignment**: Non-destructively detects and corrects transposed columns (e.g. folio numbers recorded in date or voucher fields).
4. **Canonical Accounting Mapping**: Tiered mapping engine resolving canonical accounting heads and subhead policies through exact historical folios, aliases, and contextual rules.
5. **Local LLM Assistant**: On-demand local GPU inference (`Qwen3.5-4B`) to suggest canonical classifications for genuinely ambiguous entries without cloud exposure.
6. **Review Intelligence & Editing**: Interactive web interface featuring single-transaction editing dialogs, searchable canonical head comboboxes, dynamic subhead policy enforcement, and narration similarity clustering.
7. **Draft Workbook Export**: Generates standardized 2-sheet Excel workbooks (`Receipts` & `Payments`) equipped with native Excel validation dropdowns.
8. **Approved Workbook Preflight**: Cryptographically snapshots the signed-off review workbook, verifies mathematical reconciliation (Opening Balance + Receipts - Payments = Expected Closing), and validates website selectors.
9. **Guarded Browser Automation**: Drives e-Dayan entry via a local browser relay under single-owner process locking.
10. **Transaction Verification & Reconciliation**: Verifies entries against the live website DOM across 5 validation checkpoints and issues audit reconciliation reports.

---

## Architecture

```
                    ┌───────────────────────────────┐
                    │  Physical Cashbook Ledger     │
                    │  (Malayalam / English Photos) │
                    └───────────────┬───────────────┘
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │  Source Ingestion & SHA-256   │
                    │  EXIF Auto-Rotation & Audit   │
                    └───────────────┬───────────────┘
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │   Multimodal OCR Extraction   │
                    │   (Gemini Vision / Mock)      │
                    └───────────────┬───────────────┘
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │   Column Alignment Engine     │
                    │   (Transposition Detection)   │
                    └───────────────┬───────────────┘
                                    │
                                    ▼
        ┌────────────────────────────────────────────────────────┐
        │              Hybrid Mapping Hierarchy                  │
        │  Level 1: Exact Historical Folio Lookup                │
        │  Level 2: Approved Multi-Language Aliases              │
        │  Level 3: Direction-Aware Contextual Rules             │
        │  Level 4: Local Qwen3.5-4B GPU Assistant               │
        └───────────────────────────┬────────────────────────────┘
                                    │
                                    ▼
        ┌────────────────────────────────────────────────────────┐
        │            Review Intelligence Interface               │
        │  • Card-by-Card Audit (Confirm, Override, Reject)      │
        │  • Full Transaction Editing Dialog with Combobox       │
        │  • Subhead Policy Enforcement (Mandatory/Prohibited)   │
        │  • Segmented Receipts / Payments Filter                │
        │  • Clustered Narration Groups & Bulk Actions           │
        └───────────────────────────┬────────────────────────────┘
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │    Draft Excel Export (.xlsx) │
                    │    Native Dropdowns & Review  │
                    └───────────────┬───────────────┘
                                    │ (Human Sign-off)
                                    ▼
                    ┌───────────────────────────────┐
                    │    Preflight Intake Validator │
                    │    SHA-256 Snapshot & Math    │
                    └───────────────┬───────────────┘
                                    │ (Explicit Batch Authorization)
                                    ▼
                    ┌───────────────────────────────┐
                    │   Autonomous Execution Runner │
                    │   Single Lock & DOM Verify    │
                    └───────────────┬───────────────┘
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │   Reconciliation Report &     │
                    │   Audited Transaction Journal │
                    └───────────────────────────────┘
```

---

## Hardware Requirements

### Minimum Requirements
* **Operating System**: Microsoft Windows 10 or 11 (64-bit).
* **Processor (CPU)**: AMD Ryzen 5 4600H / Intel Core i5 (6 cores or higher recommended).
* **System RAM**: 16 GB DDR4 (3200 MT/s).
* **Free Disk Space**: 10 GB SSD storage.

### Optional GPU Acceleration (for Level 4 Local LLM)
* **Graphics Card (GPU)**: NVIDIA GeForce GTX 1650 Ti (4 GB VRAM) or higher.
* **CUDA Driver**: NVIDIA CUDA 12.4+ supported driver.
* **Model Footprint**: `Qwen3.5-4B-Instruct-Q4_K_M` (approx. 2.78 GB VRAM footprint; unloaded automatically when idle).

---

## Installation & Setup

### 1. Clone the Repository
```cmd
git clone https://github.com/midhunsiby7/edayan_automation.git
cd edayan_automation
```

### 2. Set Up Python Virtual Environment
Cashbook AI requires **Python 3.12 (64-bit)**:
```cmd
python -m venv .venv
.venv\Scripts\activate
```

### 3. Install Python Dependencies
```cmd
pip install --upgrade pip
pip install -r cashbook_ai/requirements.txt
```

### 4. Configure Environment Variables
Copy the template configuration file:
```cmd
copy cashbook_ai\.env.example cashbook_ai\.env
```
Open `cashbook_ai/.env` and supply your Gemini API key (only necessary for live image extraction; mock testing runs entirely offline):
```ini
GEMINI_API_KEY=your_gemini_api_key_here
OCR_PROVIDER=gemini
APP_HOST=127.0.0.1
APP_PORT=8000
```

### 5. (Optional) Local LLM Setup
If running the Level 4 Assistant locally with GPU acceleration:
1. Download `Qwen3.5-4B-Instruct-Q4_K_M.gguf` from Hugging Face into `cashbook_ai/models/`.
2. Ensure `llama.cpp` Windows CUDA binaries are in your system PATH or local runtime directory.
*(Note: If no GPU or local model is present, the pipeline operates seamlessly in Levels 1–3 deterministic mode).*

### 6. Browser Extension Setup (for e-Dayan Entry)
1. Open Google Chrome and navigate to `chrome://extensions/`.
2. Enable **Developer mode**.
3. Load the browser extension unpacked from the extension directory.
4. Verify the local relay server starts on port `11800` when running batch operations.

---

## Launching the Application

Cashbook AI provides a single, convenient Windows launcher:

```cmd
cd cashbook_ai
run_cashbook_ai.bat
```

This launches the local application server and opens the web dashboard in your default browser at `http://127.0.0.1:8000`.

### Standard 10-Step Workflow

1. **Launch**: Double-click `run_cashbook_ai.bat`.
2. **Select Period**: Select or create the target accounting month (e.g. `2026-05`).
3. **Upload Photographs**: Drag and drop ledger page photographs in the **Upload & Arrange** view; verify chronological order.
4. **Run Extraction**: Click **Confirm Pages & Start Processing** to initiate background OCR, column alignment, and canonical mapping.
5. **Review Transactions**: Open the **Review Intelligence** dashboard. Use the `ALL | RECEIPTS | PAYMENTS` segmented filter to review entries.
6. **Edit or Resolve**: Use the `✎ Edit` button on transaction cards to modify any field, select canonical heads via searchable combobox, or assign required subheads.
7. **Export & Sign-Off**: Download the draft Excel review workbook (`.xlsx`), verify with church trustees or accountants, and save the approved file.
8. **Preflight Validation**: Upload the signed-off workbook to the **Approved Workbook & Execution** intake. The validator verifies schema, sums, SHA-256 fingerprint, and website mappings.
9. **Authorize Execution**: Authorize the bounded batch and initiate single-owner browser automation.
10. **Reconciliation**: Review real-time progress in the telemetry audit log, verify 5-point DOM assertions, and inspect the final reconciliation report.

---

## Testing

Cashbook AI includes an extensive, fully offline test suite covering accounting domain rules, column alignment, hybrid mapping, candidate generation, workbook validation, and browser automation.

Run the test suite using `pytest`:

```cmd
cd cashbook_ai
pytest tests
```

To run with full test names and execution details:
```cmd
pytest tests -v
```

*Note: All tests utilize synthetic benchmark fixtures and mock drivers. Testing requires no active internet connection, external API keys, or live credentials.*

---

## Privacy & Security

Financial accounting records and church data are strictly confidential. The following privacy principles are enforced throughout the project:

* **Zero Cloud Data Leakage**: All transaction records, journals, approved workbooks, and reconciliation files remain strictly on the local machine.
* **Public Repository Protection**: Real cashbook photographs, financial workbooks (`*.xlsx`), monthly manifests, and secret `.env` files are excluded by `.gitignore` and must never be committed to Git.
* **Deterministic Audit Trails**: All human edits are captured in an append-only `audit_history` log with timestamp and reviewer attribution.
* **Explicit Authorization**: Autonomous entry is strictly gated by explicit human authorization; no transactions are submitted to live software without human confirmation.
* **Safe Synthetic Fixtures**: Automated tests use synthetic, de-identified datasets that preserve validation behavior without exposing private accounting figures.

---

## Project Status

* **Milestone 1 — Reference Registry & Ground Truth**: Completed.
* **Milestone 2 — Non-Destructive Column Alignment**: Completed.
* **Milestone 3 — Deterministic Hybrid Mapping (Levels 1–3)**: Completed.
* **Milestone 4 — Level 4 Local LLM Assistant (Qwen3.5-4B)**: Completed.
* **Milestone 5 — Mapping Reliability & Review Intelligence**: Completed.
* **Milestone 6 — Unified Local Windows Application**: Completed.
* **Milestone 7 — Approved Workbook Intake & Execution Engine**: Completed.
* **Milestone 8 — End-to-End Accounting Period Validation**: Completed.
* **Review UI Enhancements — Transaction Editing, Filters & Clusters**: Completed.

---

## License

This project is licensed under the MIT License - see the LICENSE file for details.
