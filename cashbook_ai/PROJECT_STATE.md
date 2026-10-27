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
      - **Reconciled Master Status (Oct 16, 2026)**: The active General master was rebuilt from `data/reference/accounting_heads_general_human_review.xlsx`. It contains 61 unique GENERAL head numbers, each with a populated `human_final_name`, preserved `extracted_head_name`, review metadata, source provenance, and `source_type=MANUAL_VERIFIED`. The prior active CSV is retained in `data/reference/backups/`.
  - **Crosswalk Mapping**: The system maps the manually verified accounting heads to the actual website options (parsed from `website_heads.html`).
    - Website source contains 82 options.
    - 32 mappings currently established.
    - 29 mappings unresolved.
    - Unresolved mapping review report exists (`general_head_mapping_review.xlsx`).
    - The accounting-book head name is preserved as source evidence. The exact website head label and website option value are stored separately and are authoritative for future website automation.
    - Matches use spelling variation, semantic mapping, and strict direction constraints (Receipt->Credit, Payment->Debit).
    - The website crosswalk was not changed by the master reconciliation: 32 mappings remain established and 29 remain unresolved.
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
- [x] Active verified head master separated into `data/reference/active/` and versioned backups in `data/reference/backups/`.
- [x] Structured monthly photos directory `data/monthly/YYYY-MM/`.
- [x] Expanded Transaction Data Model (`ProcessedRow`) with subheads, granular confidences, and review reasons.
- [x] Subhead Rules Engine (embedded colons and narration-based extraction stubs).
- [x] Reconciled active General Head Master from the human-reviewed workbook; canonical monthly lookup uses `human_final_name` while embedded subheads use preserved extracted source names.
- [x] Added April 2026 manual-review export at `data/monthly/2026-04/2026-04_manual_review.xlsx` with exactly two sheets (`Receipts`, `Payments`) and six columns (`Date`, `Receipt Number`, `Amount`, `Head Name`, `Subhead`, `Narration`). Current local-rule results are 7 receipts and 45 payments; 1 opening balance is excluded.
- [x] Added physical-column verification workflow for April 2026:
  - Review workbook: `data/monthly/2026-04/2026-04_physical_column_review.xlsx`
  - Decision sidecar: `data/monthly/2026-04/physical_column_review.json`
  - Reconciliation report: `data/monthly/2026-04/physical_column_reconciliation.json` (written when finalization is attempted)
  - Final workbook target: `data/monthly/2026-04/2026-04_verified.xlsx`; it is not created until every eligible transaction has an explicit human-approved Receipt or Payment decision.
  - Source hash: the review workbook and sidecar record the SHA-256 of `data/monthly/raw_extractions.json`; stale hashes are rejected.
  - The original model classification, raw amount fields, and source identifiers remain unchanged. Human decisions are stored separately and may override the model only after explicit `HUMAN_VERIFIED` approval.
  - Full pytest validation after this workflow: 39 passed.
- [x] Added `correct_april.py` to recover explicit directions from the saved review workbook without requiring completion of unrelated review columns, preserve a timestamped workbook backup, and generate a new corrected April export plus reconciliation/correction/exception artifacts.
  - Current run used the saved workbook `data/monthly/2026-04/2026-04_physical_column_review.xlsx` and backup `data/monthly/2026-04/2026-04_physical_column_review_user_saved_backup_20261020_152704.xlsx`.
  - Corrected workbook: `data/monthly/2026-04/2026-04_corrected.xlsx`.
  - Result: 53 source records, 52 eligible transactions, 20 Receipts, 32 Payments, 1 informational opening balance excluded.
  - All 52 transaction directions were recovered from explicit saved user directions; 13 differ from the original model classification. No direction exceptions remain.
  - Head exceptions remain for page 2 rows 3 and 4 (raw/normalized head `80`); head 80 is not present in the active verified 61-row master, so no name was invented. Details are in `data/monthly/2026-04/april_corrected_exceptions.json`.
  - Per-record provenance: `data/monthly/2026-04/physical_column_corrections.json`. Reconciliation: `data/monthly/2026-04/april_corrected_reconciliation.json`.
  - The reproducible one-command correction is `python correct_april.py`; it reads the saved review workbook, preserves a timestamped backup, applies only explicit directions, and writes new artifacts without mutating raw or historical files.
- [x] Finalized the April user-facing workbook at `data/monthly/2026-04/2026-04_ready_for_entry.xlsx`. It contains only visible `Receipts` and `Payments` sheets, six editable columns, and a hidden canonical human-final head-name list used by native Excel dropdown validation.
- [x] Applied transaction-level OCR corrections without changing raw extraction: page 2 row 3/voucher 524 `80 -> 20`, and page 2 row 4/voucher 525 `80 -> 40`. Both resolve through the existing verified master. Final artifacts: `data/monthly/2026-04/april_final_corrections.json`, `april_ready_for_entry_reconciliation.json`, and `april_ready_for_entry_exceptions.json`.
- [x] Added reusable `process_month.py` and `.vscode/tasks.json` task `Cashbook: Process Month`. It consumes `data/monthly/YYYY-MM/raw_extractions.json` when present, otherwise extracts the month's photographs using the configured provider, then normalizes, classifies, resolves canonical head names, generates the six-column workbook, and writes a reconciliation report.
  - The ready-workbook exporter applies native Excel list validation to `Head Name` cells `D2:D200` on both visible sheets, using a hidden `__HeadNames` sheet and workbook-defined `CanonicalHeadNames` range. The list is deduplicated from active `human_final_name` values.
  - Full pytest validation after finalization and reusable-workflow changes: 44 passed.
- [ ] Resolve the 29 ambiguous unmapped accounting heads by manually reviewing `data/reference/general_head_mapping_review.xlsx`.
- [ ] First-month processing (Next Task) - wait for human to provide the organized photographs for the first month into `data/monthly/YYYY-MM/`.
- [ ] Complete April physical-column decisions against the original photographs; the generated review artifact currently has 52 `UNREVIEWED` transactions and 1 informational balance marked `NOT_REQUIRED`.
- [ ] Refining accounting-specific normalization and real-domain rules.
- Expanding monthly multi-page processing robustness.
- Improving structured dataset quality and validation logic for auditability.
*Note: Real private source files remain local. No ML or browser automation features are to be introduced in this phase.*

## 7A. e-Dayan Read-Only Automation Validation (Oct 24, 2026)
- The approved workbook remains unchanged at `data/monthly/2026-04/2026-04_ready_for_entry.xlsx` with 20 Receipts and 32 Payments.
- `automation/edayan_dry_run.py` now reconciles each workbook Head through the active verified master, the existing direction-specific crosswalk, and the captured website option HTML. It never contacts e-Dayan or submits a form.
- Existing crosswalk evidence resolves: Collections as per Order from the Diocese (143), Bank Interest/ Other Interest Received (C) (107), Marriage Offerings (Passaram)(C) (151), Offerings/Auction/Bhandaram (154), Offerings for Special Purpose(D) (146), Rent from Buildings(C) (156), Miscellaneous Expenses (126), Sunday School Expense (137), Electricity Charges (116), and Advance to Agriculture Account (109).
- Website reference evidence exists but the crosswalk is missing for: Offerings for Daily Expense(C) (128), Sunday Collection (Vedapracharam) Income(C) (164), Wine,Candle, Host, ,Incense Etc. (139), and Church Articles & Vestments (111). These remain review-only.
- The website parent option exists for Allowances (108), but the canonical master has no matching parent row; its populated subheads still require live dependent-option verification.
- The canonical master name is ambiguous for The South Indian Bank Kappad (four master numbers) and Rent Security Received & Refund (two master numbers). These remain NEEDS_REVIEW.
- Live read-only reconnaissance established that Subhead depends on Head. Selecting the observed `A D C P ( Debit )` option (`7075`) changed `#subhead` to `No Heads Exist`. This is the only recorded no-subhead observation and is not generalized to other Heads. Populated workbook Subheads are preserved and require live option verification; blank Subheads require a live check unless explicitly covered by observed evidence.
- Revised dry-run result: 52 entries, READY 0, BLOCKED 0, NEEDS_REVIEW 52. The prior all-BLOCKED result was caused by treating every blank Subhead as invalid; the validator now distinguishes missing mappings, no-subhead evidence, and review-only uncertainty.
- The next safe automation stage must wait for form readiness, Select2 results, and dependent Subhead updates, and stop on unresolved mappings or uncertain save confirmation. No live transaction has been submitted.

## 7B. e-Dayan Mapping and Subhead Resolution (Oct 25, 2026)
- The manually verified accounting-head master remains authoritative and was not modified or rebuilt.
- Added evidence-backed crosswalk entries for website values `128` (Offerings for Daily Expense), `164` (Sunday Collection (Vedapracharam) Income), `111` (Church Articles & Vestments), and `139` (Wine/Candle/Host/Incense). Added parent mapping `108` for master heads 52-56 (Allowances) and canonical bank mapping `4949` for payment heads 58-59.
- Live read-only Subhead inspection covered every website Head used by the April workbook after Select2/dependent loading. Head `143` loads selectable options after approximately three seconds; the saved April values `Holy Land` (`454`) and `Seminarian's Aid Fund` (`467`) are present. Head `107` also has selectable options. Tested Heads with an empty option list or `No Heads Exist` have no selectable Subhead.
- The dry-run now loads `website_subheads_observed.json`. It blocks a blank Subhead when the observed Head has options, blocks a populated Subhead when the observed Head has no options, and accepts blank Subheads only for observed no-option Heads. It never treats unobserved behavior as ready.
- Revised April result: 52 entries, READY 44, BLOCKED 6, NEEDS_REVIEW 2. Blocked rows are one Bank Interest receipt missing a required Subhead and five Allowances payments whose saved Subhead values are not selectable under website Head `108`. Review rows are the receipt The South Indian Bank Kappad (no credit website Head mapping) and the payment Rent Security Received & Refund (canonical master ambiguity/no observed Subhead mapping).
- Remaining work is limited to those six blocked rows and two genuine review rows, plus confirmation of server-side save behavior on one approved test transaction. No live transaction has been submitted.

## 7C. e-Dayan Exception Recheck (Oct 25, 2026)
- Repeated delayed live inspection of Head `107` on both Receipt and Payment forms showed the dependent Subhead eventually settles to `No Heads Exist`. An earlier options capture was stale DOM from the preceding Head selection and was not retained as authoritative.
- The Bank Interest receipt (`Receipts!3`) has a blank Subhead and is now valid under the observed no-option state. Its narration is `In South Indian Bank (1-1-26/31-3-26)`; no invented bank-specific Subhead was added.
- Head `108` (Allowances) was rechecked with latency polling and remains an empty dependent select. The five saved role Subheads are preserved in the workbook but cannot be selected on the website, so those rows remain BLOCKED.
- The Receipt-side South Indian Bank Kappad row remains unresolved: the website has no exact credit-side Kappad option; payment-side `4949` must not be substituted. The Payment-side Rent Security row remains unresolved because the master has two candidate numbers and no unambiguous payment website option was found.
- Revised dry-run result: 52 entries, READY 45, BLOCKED 5, NEEDS_REVIEW 2. Remaining exceptions are five Allowances rows with unavailable website Subheads, one receipt-side bank mapping ambiguity, and one payment-side Rent Security mapping ambiguity.

## 7D. Guarded Live-Entry Workflow (Oct 25, 2026)
- Added `automation/edayan_entry.py`, a read-only preflight and persistent live-entry state machine. It registers READY rows as `pending`, isolates BLOCKED/NEEDS_REVIEW rows, uses atomic journal writes, and supports `submitted`, `confirmed`, `failed`, and terminal `uncertain` states.
- The workflow does not infer save success. A browser adapter must return explicit observed confirmation evidence (`confirmed` plus a record identifier or success text); uncertain outcomes cannot be retried automatically.
- Dependent Subhead loading uses polling for a settled `options` or `no_options` state rather than a fixed sleep. Cash mode remains the only supported mode because the dry-run payloads are cash-only.
- Preflight command, run from `cashbook_ai`:
  `python -m automation.edayan_entry --workbook data/monthly/2026-04/2026-04_ready_for_entry.xlsx --crosswalk data/reference/general_head_crosswalk.csv --journal data/output/eday_entry_progress.json --master data/reference/active/accounting_heads_general_verified.csv --website-html data/reference/website_heads.html --subheads data/reference/website_subheads_observed.json`
- The preflight produced 45 READY rows and 7 isolated exceptions. The first proposed entry is Receipt row 2, voucher `514`, dated `2026-04-03`, amount `13245`, website Head `143` (`Collections as per Order from the Diocese ( Credit )`), Subhead `Seminarian's Aid Fund`, narration `As per order (Seminarians aid fund)`.
- Live submission remains gated: the Browser Agent was not discoverable in this session (`list_browser` returned no instances; diagnostics reported no runtime and no matching extension), so no form was filled or submitted and no save-confirmation behavior was claimed.

## 7E. Relay Runtime Adapter (Oct 25, 2026)
- `list_browser` is a Browser Use MCP discovery API, not the project's Copilot Browser relay. It returned no MCP-managed instances even though the existing relay at `ws://localhost:11800` had one registered `vscode` client and one registered `browser` client.
- The installed extension protocol was verified in `extension.js` and `relay-server/index.js`: clients register with `clientType`, commands are sent as `{type:"command", id, command, params}`, and browser responses return as `{type:"response", id, success, data, error}`.
- Added `RelayBrowserClient` and `RelayBrowserEntryAdapter` to `automation/edayan_entry.py`. The adapter uses the existing relay only, performs a read-only `get_dom` probe, and keeps form entry and submission explicitly approval-gated.
- Verified through the actual entry module:
  `python -m automation.edayan_entry --probe --workbook x --crosswalk x --journal x --master x --website-html x --subheads x`
  returned the live title `EDAYAN Syro-Malabar Eparchy of Palai` from the browser.
- No browser fields were filled and no accounting transaction was submitted. Targeted tests: 11 passed; full suite: 55 passed.

## 7F. First Transaction Live Form Verification (Oct 28, 2026)
- The Copilot Browser relay was restarted on port 11800 and the live Chrome browser reconnected.
- Probe verified live page title `EDAYAN Syro-Malabar Eparchy of Palai` and navigated to `https://account.edayan.org/transactions`.
- Live form inspection confirmed all 9 critical selectors: `#frmreceipt`, `#datepicker1`, `#receiptno`, `#amount`, `#mode_cash`, `#head`, `#subhead`, `#narration`, `#submit_form`.
- Read-only table reconnaissance on `#showtbook` / `#myTable` verified that the last recorded transaction was on 31-03-2026 (voucher 50). No transactions exist yet for April 2026, confirming voucher 514 is not a duplicate.
- Identified the cause of previous agent timeouts: typing long text into `#narration` triggers Chrome datalist filtering against thousands of options (`datalist#n`), which took ~35 seconds character-by-character, exceeding the previous 30s client timeout. Increased default timeout in `RelayBrowserClient` to 60s.
- Updated `RelayBrowserEntryAdapter.readback()` to accurately read live input values via probe commands and Select2 container text (`#select2-head-container`, `#select2-subhead-container`).
- First transaction (Receipt row 2, voucher 514, amount 13245, date 03-04-2026, Head 143 / "Collections as per Order from the Diocese", Subhead 467 / "Seminarian's Aid Fund", narration "As per order (Seminarians aid fund)") was successfully populated and verified via live readback.
- All 55 tests pass cleanly. Live submission remains explicitly approval-gated behind user confirmation.

## 7G. First Live Transaction Confirmed (Oct 28, 2026)
- Financial year was corrected to 2026-2027 in the live browser session.
- Fixed datalist lag in extension `content.js`: replaced synchronous character-by-character `InputEvent` loop with direct value assignment and single dispatch, eliminating typing stalls on `#narration`.
- Successfully re-populated Row 2 Receipt: Date `03-04-2026`, Receipt No `514`, Amount `13245`, Head `143` (`Collections as per Order from the Diocese`), Subhead `467` (`Seminarian's Aid Fund`), Narration `As per order (Seminarians aid fund)`.
- Upon explicit user approval ("go"), `#submit_form` was clicked. Pre-submission checks (`chkreceiptno` and `getsubheadname`) completed with HTTP 200, and the user confirmed the browser modal dialog.
- Live database persistence was verified via `#myTable`:
  - Entry 1: Date `03-04-2026` | Head `Collections as per Order from the DioceseSeminarian's Aid Fund` | Re/Vo.No `514` | Mode Cash | Narration `As per order (Seminarians aid fund)` | Receipt `₹13,245.00`.
  - Form automatically reset for the next receipt with pre-filled Date `03-04-2026` and incremented Receipt No `515`.
- Journal `data/output/eday_entry_progress.json` transitioned `06cc8f50...` to `CONFIRMED` with full server-side table evidence.
- Full pytest validation: 55 passed.

## 7H. Guarded Batch Execution Mode Implementation (Oct 28, 2026)
- Implemented bounded batch authorization manifests in `automation/batch_manifest.py` and sequential batch execution in `automation/edayan_entry.py`.
- **First Confirmed Transaction Preserved**: Voucher `514` (Amount `₹13,245.00`) is permanently excluded from future batches based on the journal's cryptographic transaction identity and live website evidence. It is never resubmitted.
- **Bounded Batch Manifests**:
  - `BatchManifest` records manifest ID, timestamp, approved workbook path, workbook SHA-256 cryptographic seal, transaction direction, explicit authorized keys, detailed authorized entry records, workbook-derived count and sum, and explicit exclusions with reasons.
  - Generates in `DRAFT` state by default. Never automatically approved.
  - Requires explicit human authorization (`approve_manifest()`) binding approval metadata, signature, and manifest content SHA-256 to the exact approved workbook hash.
- **Voucher Sequence Validation**:
  - Validates contiguous voucher numbering separately for Receipts and Payments.
  - Automatically isolates discontinuous voucher numbers (Receipts Row 3 Voucher `1` and Row 21 Voucher `2`) for separate review, preventing sequence corruption on the website.
- **Transaction-Level Verification & Duplicate Detection**:
  - Prior to every submission, `verify_readback()` enforces 7 field conditions: Date (`DD-MM-YYYY`), Voucher number, Amount, Mode cash, Head container text, Subhead container text, and Narration.
  - `check_live_duplicate()` inspects live `#myTable` evidence using direction and voucher number as primary key, distinguishing exact confirmed matches from conflicting existing vouchers.
  - `observe_save()` dynamically confirms persistence in `#myTable` and form field reset before transitioning journal status to `CONFIRMED`.
- **Circuit Breaker Protection**:
  - Any pre-submit readback discrepancy, conflicting voucher, submit error, or save confirmation timeout immediately trips the circuit breaker, stopping the batch without touching subsequent transactions.
  - Saves an incident diagnostic report artifact (`data/output/batch_incident_<timestamp>.json`). Unresolved save outcomes transition to terminal `UNCERTAIN` and cannot be retried automatically.
- **Generated DRAFT Manifest**:
  - Path: `data/output/batch_manifest_receipt_515_531.json`.
  - Direction: `RECEIPT`. Range: Vouchers `515` to `531`.
  - Authorized Count: 17 transactions. Total Amount: `₹52,965.00`.
  - Excluded Count: 35 transactions (1 already CONFIRMED, 1 voucher discontinuity, 1 NEEDS_REVIEW, 32 Payments).
- **Testing**: Added `tests/test_batch_entry.py` (12 tests) covering manifest generation, hash verification, approval lifecycle, scope expansion prevention, readback mismatch, live duplicate conflict, timeout to UNCERTAIN, and successful sequential mock execution. Full pytest suite: 67 passed with zero live submissions.

## 7I. Second Live Transaction Confirmed & Circuit Breaker In Action (Oct 28, 2026)
- **Voucher 515 Confirmed Live**:
  - Row 4 (Receipts): Date `04-04-2026`, Receipt No `515`, Amount `₹6,405.00`, Head `143` (*Collections as per Order from the Diocese*), Subhead `454` (*Holy Land*), Narration *As per order (Holy land)*.
  - Successfully populated, 7-point pre-submit verified, and submitted via live batch execution.
  - Verified persistence in `#myTable`:
    `1 | 04-04-2026 | Collections as per Order from the DioceseHoly Land | 515 | 💵 | As per order (Holy land) | ₹6,405.00`.
  - Form reset verified and Receipt No auto-incremented to `516`. Journal status updated to `CONFIRMED`.
- **Voucher 516 Circuit Breaker Safety Halt**:
  - Row 5 (Receipts): Date `05-04-2026`, Receipt No `516`, Amount `₹8,000.00`, Head `128` (*Offerings for Daily Expense*), Narration *Donation for Holy Mass*.
  - After `#submit_form` click, `observe_save()` polled for 30s. Because Voucher 516 was not observed in `#myTable` within the timeout, the Circuit Breaker tripped immediately.
  - Status transitioned to terminal `UNCERTAIN` (`incident-1793134120`). Batch execution safely halted without touching subsequent transactions (517–531).
  - All code, tests, and documentation committed and pushed to GitHub main.

## 8. Security Requirements
- The `GEMINI_API_KEY` must **never** be exposed, committed, logged, or added to Git.
- The `.env` file is safely ignored in `.gitignore` and is not tracked.
- Real cashbook photographs and generated Excel outputs containing sensitive financial data must remain local and uncommitted.

## 9. Hardware Constraints
The development machine has strict constraints:
- AMD Ryzen 5 4600H, 8 GB RAM.
- NVIDIA GeForce GTX 1650 Ti (4 GB dedicated VRAM).
System design must remain resource-conscious; large local LLMs should not be integrated without concrete justification.

## 10. Physical-Column Review Workflow

The model-generated `raw_amount_col1`, `raw_amount_col2`, and `detected_amount_column` fields are proposals from the same extraction response, not independent verification. The review workbook is the decision interface and includes all source records, including informational balances. The row bounding box is only a row locator; it is not an amount-cell coordinate or proof of physical placement.

Commands, run from `cashbook_ai`:

```powershell
python generate_physical_column_review.py
python import_physical_column_review.py --reviewer "optional-local-label"
python generate_verified_april_workbook.py
```

`generate_physical_column_review.py` rebuilds the new review workbook and an initial sidecar from the immutable raw JSON. A reviewer opens the original page photographs and fills `Human-Verified Direction` with `Receipt`, `Payment`, or `Ambiguous`, then sets `Review Status` to the corresponding explicit status. Transactions must be `HUMAN_VERIFIED` to permit finalization; `AMBIGUOUS` and `UNREVIEWED` decisions block it. Informational opening/closing balances are `NOT_REQUIRED` and are excluded from final transaction sheets.

`import_physical_column_review.py` validates that every source identifier appears exactly once, no unexpected rows exist, the workbook model fields were not altered, and the recorded extraction SHA-256 matches the current raw JSON. It then writes the sidecar without changing the raw extraction or historical workbooks. The optional reviewer label is not inferred.

`generate_verified_april_workbook.py` validates the sidecar again and writes `physical_column_reconciliation.json`. If any eligible transaction is unresolved, it stops without creating `2026-04_verified.xlsx`. When finalization is allowed, the new workbook has exactly `Receipts` and `Payments` sheets with the existing six-column manual-review format. The original model classification remains available in the review workbook and sidecar, and source identifiers remain the traceability key.

## 11. Future Roadmap
1. Stabilize the raw transaction extraction and normalization pipeline (Current Phase).
2. Develop a genuine Machine Learning component (e.g., Decision Tree / ID3) for feature engineering, potentially for smart head classification or narration clustering.
3. Develop browser automation (Playwright/e-Dayan integration) to automatically inject the human-approved dataset into the final accounting software.
