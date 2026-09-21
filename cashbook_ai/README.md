# Cashbook AI - Handwritten Cashbook Digitization (Prototype 1)

An AI-assisted, accounting-aware digitization system designed to convert photographs of handwritten cashbooks (containing handwritten Malayalam and English, ruled notebook lines, and varied lighting conditions) into structured machine-readable data, exported directly into formatted Excel workbooks.

---

## Architecture & Data Flow

```
[Cashbook Photograph]
          │
          ▼
[Preprocessing] ──> EXIF auto-rotation, memory-safe scaling, CLAHE illumination correction
          │
          ▼
[Extraction Layer] ──> BaseExtractor (MockExtractor or GeminiVisionExtractor)
          │            - Captures verbatim Malayalam script & English translation
          │            - Captures physical column positions: Col 1 (Receipt) vs Col 2 (Payment)
          │            - Scopes ditto marks strictly to the date field
          │            - Extracts row bounding boxes & granular per-field confidence
          │
          ▼
[Normalization & Accounting Rules]
          │ ──> DateNormalizer: Stateful ditto inheritance across multi-page jobs
          │ ──> AmountNormalizer: Numerical sanitization without silently inventing missing digits
          │ ──> TextNormalizer: Preserves Malayalam script, distinct handling for voucher '-'
          │ ──> AccountingRuleEngine: Physical column classification, record type tagging
          │
          ▼
[Validation & Audit Flagging] ──> Flags review_required=True for ambiguous/low-confidence rows
          │
          ▼
[Excel Exporter] ──> 4-Sheet Workbook: Receipts, Payments, All_Transactions, Extraction_Audit
```

---

## Core Accounting Conventions Enforced

1. **Physical Amount-Column Evidence**:
   - The physical location of the amount dictates classification:
     * **Column 1** (nearer/first amount column) = **Receipt**
     * **Column 2** (second/farther amount column) = **Payment**
   - Narration semantics **never** override physical column position. Raw column strings (`raw_amount_col1`, `raw_amount_col2`) and `detected_amount_column` are preserved on every row for total audit transparency.

2. **Scoped Date Ditto / Continuation Handling**:
   - Ditto marks (`"`, `do`, `ditto`, `-`) are strictly scoped to the **date** field.
   - Hyphens (`-`) in other fields (e.g. voucher number or head number) represent **absence** of a number and are **never** treated as ditto marks.

3. **Multi-Page Date Persistence**:
   - Cashbook pages within a monthly batch are processed in chronological order.
   - The date inheritance state persists across page boundaries: if Page 2 starts with a ditto mark (`"`), it seamlessly inherits the last valid date from Page 1.

4. **Informational vs. Transaction Records**:
   - `Opening Balance` and `Closing Balance` rows are classified under `record_type = OPENING_BALANCE / CLOSING_BALANCE` with `transaction_type = INFORMATIONAL`.
   - Numbered vouchers (e.g., `1`, `2`, `3`...) designate verified transactions.

5. **Granular Per-Field Confidence**:
   - Every row tracks individual confidence scores:
     * `date_confidence`
     * `head_confidence`
     * `voucher_confidence`
     * `narration_confidence`
     * `amount_confidence`
     * `extraction_confidence` (overall)

6. **Auditability & Zero Silent Invention**:
   - Unclear, smudged, or missing values are left blank or flagged with `review_required = True`.
   - Every row retains `source_image`, `source_page`, `source_row`, and bounding box coordinates `row_bbox`.

---

## Excel Output Structure

The generated Excel file contains 4 dedicated sheets:
1. **`Receipts`**: All receipt transactions and opening balance entries.
2. **`Payments`**: All disbursement and expense transactions.
3. **`All_Transactions`**: Complete chronological list of all records with 15 standard fields.
4. **`Extraction_Audit`**: Detailed auditor's sheet showing raw text vs. resolved values, physical column index, bounding boxes, per-field confidence scores, and specific warning reasons. Rows requiring review are highlighted in soft yellow/amber.

---

## Directory Structure

```
cashbook_ai/
├── config.py                     # Configuration settings & environment variables
├── main.py                       # CLI entrypoint for running digitization
├── requirements.txt              # Dependency specifications
├── README.md                     # Documentation
├── src/
│   ├── models.py                 # Pydantic data schemas
│   ├── preprocessing/            # CLAHE, EXIF orientation, memory-safe resizing
│   ├── extraction/               # BaseExtractor, MockExtractor, GeminiVisionExtractor
│   ├── normalization/            # Scoped date normalizer, amount cleaner, text cleaner
│   ├── validation/               # Accounting rules, physical column mapping, audit engine
│   └── export/                   # 4-sheet formatted Excel exporter
├── data/
│   ├── input/                    # Put photographs of cashbook pages here
│   └── output/                   # Output Excel files and debug images
└── tests/                        # Comprehensive test suite
```

---

## Setup & Execution

### 1. Install Dependencies
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Run with Built-In Test Suite (Mock Mode)
No API keys or heavy GPU required. Runs the full pipeline on simulated cashbook pages demonstrating:
- Opening Balance with no voucher
- Head 78 / Voucher 1 / payment 700
- Head 78 / Voucher 2 / payment 1080
- Scoped ditto date continuation
- Cross-page date persistence
- Physical column classification
- Uncertain row flagging

```bash
python main.py --mock
```

### 3. Run with Gemini Multimodal Vision (Real Cashbook Photos)
Set your Google Gemini API key:
```bash
set GEMINI_API_KEY=your_api_key_here
python main.py --provider gemini --input data/input/
```

### 4. Run Automated Tests
```bash
pytest tests/ -v
```
