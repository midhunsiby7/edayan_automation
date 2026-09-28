# PROJECT_STATE.md

## 1. Project Purpose
The project is an AI-assisted handwritten cashbook digitization system. It automates the conversion of physical cashbook photographs (containing handwritten Malayalam and English, ruled pages) into structured machine-readable datasets for accounting software, relieving the user from repetitive manual typing. This project also serves as a college Machine Learning project, where a genuine ML component will be incorporated in a later phase.

## 2. Current Architecture
The system employs a modular Python pipeline that decouples extraction, normalization, and export.
- **Extraction Layer**: Uses `BaseExtractor` abstraction, currently implemented via `GeminiVisionExtractor` (for real data) and `MockExtractor` (for testing). Extracts raw Malayalam/English text, handles bounding boxes, physical column positions, and field-level confidence scores.
- **Preprocessing**: Handles EXIF auto-rotation, memory-safe image scaling, and CLAHE illumination correction via OpenCV/Pillow.
- **Normalization Layer**: Includes `DateNormalizer` (for stateful ditto cross-page date inheritance), `AmountNormalizer`, and `TextNormalizer`.
- **Validation & Accounting Rules**: `AccountingRuleEngine` applies domain rules (e.g., column-based receipt/payment mapping).
- **Export**: Exports to a formatted Excel workbook with 4 sheets (Receipts, Payments, All_Transactions, Extraction_Audit).
- **Data Models**: Employs Pydantic `BaseModel` schemas (`RawExtractedRow`, `ProcessedRow`) for rigid data typing.

## 3. Accounting Conventions Already Learned
- **Opening/Closing Balance**: Informational records only. They do not represent typical receipt/payment transactions but are preserved for audit and validation.
- **Date Inheritance**: Ditto marks (`"`, `do`, `ditto`, `-`) apply strictly to the date field and carry over chronologically across page boundaries.
- **Voucher Numbers vs. Hyphens**: Numeric voucher numbers identify valid transactions. Hyphens in other fields denote absence, not dittos.
- **Physical Position for Transaction Type**: 
  - Amount in **Column 1** (nearer column) = **Receipt**
  - Amount in **Column 2** (farther column) = **Payment**
  - Narration semantics never override physical column position.
- **Narration**: Requires preserving the raw Malayalam, the interpreted English translation, and eventually the user's final normalized accounting narration.

## 4. Current Prototype Status
Prototype 1 is effectively complete and successful.
Prototype 2 (Phase 2) mapping architecture is functionally complete.
- **Real-Data Capability**: A real-world test utilizing Gemini Vision demonstrated approximately 99.5% accuracy as manually audited by the user.
- **Current Capability**: It successfully processes full-page cashbook photos, extracts rows, handles stateful ditto dates, correctly identifies amounts based on physical columns, and generates the Excel audit outputs.

## 5. Important Implementation Decisions
- The use of Gemini Multimodal Vision API is retained as the primary real-data extraction engine based on successful empirical tests.
- Pydantic models are used for robust row-level validation.
- Confidence scores and row bounding boxes are captured for human review transparency, leaving ambiguous data blank and flagged rather than silently inventing it.
- **Phase 2A (Dual Head Master)**: The system recognizes two distinct identifier schemes:
  - **Accounting Head**: Reused across cashbook months (e.g., Head `78`). Managed by `HeadMasterRepository` (JSON) based on an `AccountSet` (GENERAL vs AGRICULTURE strictly separated).
  - **Website Head**: Unique options on the target website HTML (e.g., `<option value="106">Bank Charges ( Debit )</option>`). The `WebsiteHeadParser` extracts the exact `website_head_value`, `website_head_label`, and determines the accounting direction (RECEIPT/CREDIT vs PAYMENT/DEBIT) based on label hints.
  - **Mapping Rule**: The deterministic mapping resolves `(accounting_head_number + transaction_type) -> website_head_value`.
  - ML is explicitly excluded from these basic number-to-name and number-to-website lookups. Unknown heads or missing mappings trigger explicit review flags (`HEAD_NOT_FOUND` and `HEAD_MAPPING_NOT_FOUND`).
  - **Accounting Head Master Ingestion**: Two real photographs of the General Accounts Head Master were provided.
    - Expected count: 101 heads
    - Current extracted total: 61 verified entries (via manual fallback)
  - **Manual Verified Fallback**: A `MANUAL_VERIFIED` import path is implemented for bypassing live Gemini API failures via `HeadMasterRepository.import_verified_csv()`. It consumes `data/reference/accounting_heads_general_verified.csv` (human verified).
    - **Draft Extraction Status**: Two real pages of the General Accounts Head Master were provided (partial subset of 101-head master). 61 general accounting heads are currently verified.
  - **Crosswalk Mapping**: The system maps the manually verified accounting heads to the actual website options (parsed from `website_heads.html`).
    - Website source contains 82 options.
    - 32 mappings currently established.
    - 29 mappings unresolved.
    - Unresolved mapping review report exists (`general_head_mapping_review.xlsx`).
    - The accounting-book head name is preserved as source evidence. The exact website head label and website option value are stored separately and are authoritative for future website automation.
    - Matches use spelling variation, semantic mapping, and strict direction constraints (Receipt->Credit, Payment->Debit).
- **Fixed Bug**: As of Phase 2 (Sept 28, 2026), Opening Balance is correctly excluded from Receipts and Payments sheets, confined strictly to All_Transactions/Extraction_Audit.

## 6. Test Status
- A successful mock test suite exists (`test_accounting_rules.py`, `test_date_normalizer.py`, `test_pipeline_e2e.py`).
- E2E tests have been expanded to verify that Opening Balance records are properly filtered out of Receipts/Payments and included in All_Transactions/Extraction_Audit.
- Testing infrastructure is built on `pytest`.
- Mock tests validate stateful ditto continuation, cross-page persistence, column classification, and uncertain row flagging without requiring real GPU/API calls.

## 7. Next Planned Phase (Phase 2)
The immediate next phase focuses strictly on **improving the data quality and rule engines**, specifically:
- [x] Fixing the Opening Balance export bug (Fixed Sept 28, 2026).
- [x] General Head Master concept & data model (Phase 2A).
- [x] Two distinct head master systems (Accounting vs Website) (Phase 2A).
- [x] Website option value vs accounting head number distinction (Phase 2A).
- [x] Website HTML parsing and direction-aware mapping logic (Phase 2A).
- [x] General vs Agriculture separation (Phase 2A).
- [x] head_number -> head_name lookup design (Phase 2A).
- [x] Ingest real Accounting Head Master photographs (Phase 2A).
- [x] Ingest real Website HTML source (Phase 2A).
- [ ] First-month processing (Next Task).
- [ ] Refining accounting-specific normalization and real-domain rules.
- Expanding monthly multi-page processing robustness.
- Improving structured dataset quality and validation logic for auditability.
*Note: Real private source files remain local. No ML or browser automation features are to be introduced in this phase.*

## 8. Security Requirements
- The `GEMINI_API_KEY` must **never** be exposed, committed, logged, or added to Git.
- The `.env` file is safely ignored in `.gitignore` and is not tracked.
- Real cashbook photographs and generated Excel outputs containing sensitive financial data must remain local and uncommitted.

## 9. Hardware Constraints
The development machine has strict constraints:
- AMD Ryzen 5 4600H, 8 GB RAM.
- NVIDIA GeForce GTX 1650 Ti (4 GB dedicated VRAM).
System design must remain resource-conscious; large local LLMs should not be integrated without concrete justification.

## 10. Future Roadmap
1. Stabilize the raw transaction extraction and normalization pipeline (Current Phase).
2. Develop a genuine Machine Learning component (e.g., Decision Tree / ID3) for feature engineering, potentially for smart head classification or narration clustering.
3. Develop browser automation (Playwright/e-Dayan integration) to automatically inject the human-approved dataset into the final accounting software.
