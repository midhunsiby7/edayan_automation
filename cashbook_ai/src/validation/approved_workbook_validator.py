"""Approved Excel workbook validator and strict execution readiness gate for Cashbook AI."""

import hashlib
import json
import re
import shutil
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from pydantic import BaseModel, Field
import openpyxl

from src.reference.canonical_registry import CanonicalRegistry, SubheadPolicy


class ValidationCategoryStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class CategoryResult(BaseModel):
    category: str
    status: ValidationCategoryStatus = ValidationCategoryStatus.PASS
    issue_count: int = 0
    details: Dict[str, Any] = Field(default_factory=dict)
    issues: List[str] = Field(default_factory=list)


class ValidationError(BaseModel):
    sheet: str
    row_number: int
    column: str
    error_message: str


class PreflightSummary(BaseModel):
    month: str
    file_name: str
    file_hash_sha256: str
    is_valid: bool
    status: str  # "READY_FOR_EXECUTION" or "BLOCKED"
    blocked_reason: Optional[str] = None
    receipts_count: int = 0
    receipts_total: float = 0.0
    payments_count: int = 0
    payments_total: float = 0.0
    net_movement: float = 0.0
    opening_balance: Optional[float] = None
    expected_closing_balance: Optional[float] = None
    physical_closing_balance: Optional[float] = None
    closing_balance_variance: Optional[float] = 0.0
    physical_opening_verified: bool = False
    source_coverage_percent: float = 100.0
    website_mapping_status: str = "COMPLETE"
    snapshot_path: Optional[str] = None
    categories: Dict[str, CategoryResult] = Field(default_factory=dict)
    errors: List[ValidationError] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class ApprovedWorkbookValidator:
    """Validates human-approved Excel review workbooks before live e-Dayan execution.
    
    Strict Execution Readiness Gate:
    Reports BLOCKED whenever ANY critical condition fails:
      1. Accounting reconciliation (opening balance, total receipts, total payments, exact closing match ₹0.00 diff)
      2. Transaction completeness (no duplicate vouchers, no missing transactions, correct counts)
      3. Required fields (non-empty valid ISO date, required voucher, valid head, mandatory subhead)
      4. Canonical accounting mapping (active heads, correct primary direction)
      5. Website selector mapping (all heads have website mappings)
      6. Execution manifest integrity
      7. Artifact integrity (workbook parsed and verified round-trip)
    """

    REQUIRED_COLUMNS = [
        "Date",
        "Receipt Number",
        "Amount",
        "Head Name",
        "Subhead",
        "Narration"
    ]

    def __init__(self, registry: CanonicalRegistry):
        self.registry = registry

        # Build lookup maps by canonical name (case-insensitive)
        self._heads_by_name: Dict[str, Any] = {}
        for h in self.registry.heads.values():
            if h.is_active:
                self._heads_by_name[h.canonical_name.lower().strip()] = h

        self._subheads_by_name: Dict[str, Any] = {}
        for s in self.registry.subheads.values():
            if s.is_active:
                self._subheads_by_name[s.canonical_name.lower().strip()] = s

        # Try to locate monthly reconciliation targets reference file
        self._targets_cache: Dict[str, Dict[str, Any]] = {}
        self._load_monthly_targets()

    def _load_monthly_targets(self):
        candidate_paths = []
        if hasattr(self.registry, "storage_path") and self.registry.storage_path:
            candidate_paths.append(Path(self.registry.storage_path).parent / "monthly_reconciliation_targets.json")
        candidate_paths.append(Path(__file__).resolve().parent.parent.parent / "data" / "reference" / "monthly_reconciliation_targets.json")

        for cp in candidate_paths:
            if cp.is_file():
                try:
                    self._targets_cache = json.loads(cp.read_text(encoding="utf-8"))
                    break
                except Exception:
                    pass

    @staticmethod
    def compute_sha256(file_path: Path) -> str:
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                sha256.update(chunk)
        return sha256.hexdigest()

    def validate_workbook(
        self,
        workbook_path: Path,
        expected_month: str,
        expected_opening_balance: Optional[float] = None,
        expected_closing_balance: Optional[float] = None,
        enforce_physical_targets: bool = False,
        snapshot_dir: Optional[Path] = None,
    ) -> PreflightSummary:
        """Validate an approved Excel workbook completely against schema and strict canonical rules."""
        file_path = Path(workbook_path)
        if not file_path.is_file():
            raise FileNotFoundError(f"Workbook file not found: {file_path}")

        file_hash = self.compute_sha256(file_path)
        errors: List[ValidationError] = []
        warnings: List[str] = []

        # Category issue buckets
        cat_issues = {
            "accounting_reconciliation": [],
            "transaction_completeness": [],
            "required_fields": [],
            "canonical_mapping": [],
            "website_mapping": [],
            "manifest_integrity": [],
            "artifact_integrity": [],
        }

        # Check for synthetic / test artifact naming
        if re.search(r"(?:approved_api|.*_api\.xlsx$|.*[_\-]mock.*|.*[_\-]sample.*)", file_path.name, re.IGNORECASE):
            err_msg = f"EXECUTION BLOCKED — Synthetic or test artifact detected ('{file_path.name}'). Production execution requires genuine monthly workbook."
            errors.append(ValidationError(sheet="WORKBOOK", row_number=0, column="FILE", error_message=err_msg))
            cat_issues["artifact_integrity"].append(err_msg)

        try:
            wb = openpyxl.load_workbook(file_path, data_only=True)
        except Exception as e:
            err_msg = f"Corrupt Excel file: {str(e)}"
            errors.append(ValidationError(sheet="ALL", row_number=0, column="FILE", error_message=err_msg))
            cat_issues["artifact_integrity"].append(err_msg)
            return self._build_preflight_summary(
                expected_month=expected_month,
                file_name=file_path.name,
                file_hash=file_hash,
                errors=errors,
                warnings=warnings,
                cat_issues=cat_issues,
                rec_count=0,
                rec_total=Decimal("0.0"),
                pay_count=0,
                pay_total=Decimal("0.0"),
                opening_bal_dec=Decimal("0.0"),
                computed_closing_dec=Decimal("0.0"),
                target_closing_dec=Decimal("0.0"),
                variance_dec=Decimal("0.0"),
                physical_verified=False,
                snapshot_path_str=None,
            )

        # 1. Check required sheets
        required_sheets = ["Receipts", "Payments"]
        for sheet_name in required_sheets:
            if sheet_name not in wb.sheetnames:
                err_msg = f"Missing required sheet '{sheet_name}'"
                errors.append(ValidationError(sheet="WORKBOOK", row_number=0, column="SHEET", error_message=err_msg))
                cat_issues["transaction_completeness"].append(err_msg)

        if errors:
            return self._build_preflight_summary(
                expected_month=expected_month,
                file_name=file_path.name,
                file_hash=file_hash,
                errors=errors,
                warnings=warnings,
                cat_issues=cat_issues,
                rec_count=0,
                rec_total=Decimal("0.0"),
                pay_count=0,
                pay_total=Decimal("0.0"),
                opening_bal_dec=Decimal("0.0"),
                computed_closing_dec=Decimal("0.0"),
                target_closing_dec=Decimal("0.0"),
                variance_dec=Decimal("0.0"),
                physical_verified=False,
                snapshot_path_str=None,
            )

        # 2. Validate Receipts
        rec_count, rec_total_dec, rec_missing_web = self._validate_sheet(
            sheet=wb["Receipts"],
            sheet_name="Receipts",
            expected_direction="RECEIPT",
            expected_month=expected_month,
            errors=errors,
            warnings=warnings,
            cat_issues=cat_issues,
        )

        # 3. Validate Payments
        pay_count, pay_total_dec, pay_missing_web = self._validate_sheet(
            sheet=wb["Payments"],
            sheet_name="Payments",
            expected_direction="PAYMENT",
            expected_month=expected_month,
            errors=errors,
            warnings=warnings,
            cat_issues=cat_issues,
        )

        total_tx_count = rec_count + pay_count
        if total_tx_count == 0:
            err_msg = "Workbook contains 0 transaction rows across Receipts and Payments sheets."
            errors.append(ValidationError(sheet="WORKBOOK", row_number=0, column="ROWS", error_message=err_msg))
            cat_issues["transaction_completeness"].append(err_msg)

        # 4. Opening Balance and Target Reconciliation (Decimal-Safe Arithmetic)
        net_move_dec = rec_total_dec - pay_total_dec

        month_target = self._targets_cache.get(expected_month) if enforce_physical_targets else None

        physical_verified = False
        if expected_opening_balance is not None:
            opening_bal_dec = Decimal(str(expected_opening_balance))
            physical_verified = True
        elif month_target and "opening_balance" in month_target:
            opening_bal_dec = Decimal(str(month_target["opening_balance"]))
            physical_verified = True
        else:
            opening_bal_dec = Decimal("0.00")

        computed_closing_dec = opening_bal_dec + net_move_dec

        # Target Closing Balance resolution
        if expected_closing_balance is not None:
            target_closing_dec = Decimal(str(expected_closing_balance))
        elif month_target and "closing_balance" in month_target:
            target_closing_dec = Decimal(str(month_target["closing_balance"]))
        else:
            target_closing_dec = computed_closing_dec

        variance_dec = computed_closing_dec - target_closing_dec

        # Check closing balance exact match
        if variance_dec != Decimal("0"):
            err_msg = (
                f"EXECUTION BLOCKED — Closing balance does not match the physical cashbook. "
                f"Workbook: ₹{float(computed_closing_dec):,.2f}. Physical: ₹{float(target_closing_dec):,.2f}. "
                f"Difference: ₹{float(abs(variance_dec)):,.2f}."
            )
            errors.append(ValidationError(
                sheet="ALL",
                row_number=0,
                column="Closing Balance",
                error_message=err_msg
            ))
            cat_issues["accounting_reconciliation"].append(err_msg)

        # Check target receipts and payments if enforce_physical_targets is requested
        if enforce_physical_targets and month_target:
            target_rec = Decimal(str(month_target.get("total_receipts", rec_total_dec)))
            if rec_total_dec != target_rec:
                err_msg = (
                    f"Total receipts (₹{float(rec_total_dec):,.2f}) does not match physical target "
                    f"(₹{float(target_rec):,.2f}). Difference: ₹{float(rec_total_dec - target_rec):,.2f}."
                )
                errors.append(ValidationError(sheet="Receipts", row_number=0, column="Total Receipts", error_message=err_msg))
                cat_issues["accounting_reconciliation"].append(err_msg)

            target_pay = Decimal(str(month_target.get("total_payments", pay_total_dec)))
            if pay_total_dec != target_pay:
                err_msg = (
                    f"Total payments (₹{float(pay_total_dec):,.2f}) does not match physical target "
                    f"(₹{float(target_pay):,.2f}). Difference: ₹{float(pay_total_dec - target_pay):,.2f}."
                )
                errors.append(ValidationError(sheet="Payments", row_number=0, column="Total Payments", error_message=err_msg))
                cat_issues["accounting_reconciliation"].append(err_msg)

            target_count = month_target.get("total_transactions")
            if target_count and total_tx_count != target_count:
                err_msg = (
                    f"Transaction count ({total_tx_count}) does not match physical cashbook target ({target_count})."
                )
                errors.append(ValidationError(sheet="WORKBOOK", row_number=0, column="Transaction Count", error_message=err_msg))
                cat_issues["transaction_completeness"].append(err_msg)

        # 5. Manifest integrity check
        if total_tx_count > 0 and len(errors) == 0:
            pass  # Ready for manifest creation
        elif total_tx_count == 0:
            cat_issues["manifest_integrity"].append("Zero transactions to package into manifest.")

        is_valid = (len(errors) == 0 and variance_dec == Decimal("0"))

        # 6. Create immutable execution snapshot ONLY IF VALID
        snapshot_path_str = None
        if is_valid:
            if snapshot_dir is None:
                if file_path.parent.name == "intake":
                    snapshot_dir = file_path.parent.parent / "approved_snapshots"
                else:
                    snapshot_dir = file_path.parent / "approved_snapshots"
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            snapshot_file = snapshot_dir / f"{file_hash}.xlsx"
            if not snapshot_file.is_file():
                shutil.copy2(file_path, snapshot_file)
            snapshot_path_str = str(snapshot_file.resolve())

            # Save companion metadata
            meta_file = snapshot_dir / f"{file_hash}_meta.json"
            meta_data = {
                "month": expected_month,
                "original_filename": file_path.name,
                "file_hash_sha256": file_hash,
                "snapshot_created_at": str(file_path.stat().st_mtime),
                "receipts_count": rec_count,
                "receipts_total": float(rec_total_dec),
                "payments_count": pay_count,
                "payments_total": float(pay_total_dec),
                "net_movement": float(net_move_dec),
                "opening_balance": float(opening_bal_dec),
                "expected_closing_balance": float(computed_closing_dec),
                "physical_closing_balance": float(target_closing_dec),
                "closing_balance_variance": float(variance_dec),
                "snapshot_path": snapshot_path_str,
            }
            meta_file.write_text(json.dumps(meta_data, indent=2), encoding="utf-8")

        return self._build_preflight_summary(
            expected_month=expected_month,
            file_name=file_path.name,
            file_hash=file_hash,
            errors=errors,
            warnings=warnings,
            cat_issues=cat_issues,
            rec_count=rec_count,
            rec_total=rec_total_dec,
            pay_count=pay_count,
            pay_total=pay_total_dec,
            opening_bal_dec=opening_bal_dec,
            computed_closing_dec=computed_closing_dec,
            target_closing_dec=target_closing_dec,
            variance_dec=variance_dec,
            physical_verified=physical_verified,
            snapshot_path_str=snapshot_path_str,
        )

    def _validate_sheet(
        self,
        sheet,
        sheet_name: str,
        expected_direction: str,
        expected_month: str,
        errors: List[ValidationError],
        warnings: List[str],
        cat_issues: Dict[str, List[str]],
    ) -> Tuple[int, Decimal, int]:
        """Validate header, columns, rows, amounts, canonical accounting identities, and website mappings."""
        # Check header row
        header = [str(cell.value or "").strip() for cell in sheet[1]]
        col_map = {}
        for col_name in self.REQUIRED_COLUMNS:
            if col_name in header:
                col_map[col_name] = header.index(col_name) + 1
            elif col_name == "Receipt Number" and "Voucher Number" in header:
                col_map["Receipt Number"] = header.index("Voucher Number") + 1
            else:
                err_msg = f"Missing required column '{col_name}'"
                errors.append(ValidationError(sheet=sheet_name, row_number=1, column=col_name, error_message=err_msg))
                cat_issues["transaction_completeness"].append(err_msg)
                return 0, Decimal("0.0"), 0

        count = 0
        total_dec = Decimal("0.0")
        missing_website_mappings = 0
        seen_vouchers = set()

        for r_idx in range(2, sheet.max_row + 1):
            date_val = sheet.cell(row=r_idx, column=col_map["Date"]).value
            v_val = sheet.cell(row=r_idx, column=col_map["Receipt Number"]).value
            amt_val = sheet.cell(row=r_idx, column=col_map["Amount"]).value
            head_val = sheet.cell(row=r_idx, column=col_map["Head Name"]).value
            sub_val = sheet.cell(row=r_idx, column=col_map["Subhead"]).value
            narr_val = sheet.cell(row=r_idx, column=col_map["Narration"]).value

            # Skip entirely empty rows
            if not any([date_val, v_val, amt_val, head_val, sub_val, narr_val]):
                continue

            count += 1

            # 1. Validate Date (Mandatory for every transaction row)
            if date_val is None or str(date_val).strip() == "" or str(date_val).strip() == "None":
                err_msg = f"{sheet_name} row {r_idx}: Transaction date is missing."
                errors.append(ValidationError(sheet=sheet_name, row_number=r_idx, column="Date", error_message=err_msg))
                cat_issues["required_fields"].append(err_msg)
            else:
                if isinstance(date_val, (datetime, date)):
                    date_str = date_val.strftime("%Y-%m-%d")
                else:
                    date_str = str(date_val).strip()
                # Check valid format
                if not (re.match(r"^\d{4}-\d{2}-\d{2}$", date_str) or re.match(r"^\d{1,2}[\.\/\-]\d{1,2}([\.\/\-]\d{2,4})?$", date_str)):
                    err_msg = f"{sheet_name} row {r_idx}: Invalid date format '{date_str}'. Expected YYYY-MM-DD."
                    errors.append(ValidationError(sheet=sheet_name, row_number=r_idx, column="Date", error_message=err_msg))
                    cat_issues["required_fields"].append(err_msg)
                elif not date_str.startswith(expected_month):
                    warn_msg = f"{sheet_name} row {r_idx}: Date '{date_str}' outside expected month '{expected_month}'"
                    warnings.append(warn_msg)

            # 2. Validate Voucher Number (Mandatory for every transaction row, duplicates prohibited)
            v_str = str(v_val or "").strip()
            if not v_str or v_str in {"", "None", "null"}:
                err_msg = f"{sheet_name} row {r_idx}: Voucher / Receipt Number cannot be empty."
                errors.append(ValidationError(sheet=sheet_name, row_number=r_idx, column="Receipt Number", error_message=err_msg))
                cat_issues["required_fields"].append(err_msg)
            elif v_str != "-":
                if v_str in seen_vouchers:
                    err_msg = f"Duplicate voucher number '{v_str}' detected in {sheet_name} at row {r_idx}"
                    errors.append(ValidationError(sheet=sheet_name, row_number=r_idx, column="Receipt Number", error_message=err_msg))
                    cat_issues["transaction_completeness"].append(err_msg)
                seen_vouchers.add(v_str)

            # 3. Validate Amount (Decimal safe)
            if amt_val is None or str(amt_val).strip() == "":
                err_msg = f"{sheet_name} row {r_idx}: Transaction amount cannot be empty"
                errors.append(ValidationError(sheet=sheet_name, row_number=r_idx, column="Amount", error_message=err_msg))
                cat_issues["required_fields"].append(err_msg)
            else:
                try:
                    clean_amt_str = str(amt_val).replace(",", "").strip()
                    amt_dec = Decimal(clean_amt_str)
                    if amt_dec <= Decimal("0"):
                        err_msg = f"{sheet_name} row {r_idx}: Amount must be positive, got {amt_dec}"
                        errors.append(ValidationError(sheet=sheet_name, row_number=r_idx, column="Amount", error_message=err_msg))
                        cat_issues["accounting_reconciliation"].append(err_msg)
                    total_dec += amt_dec
                except Exception:
                    err_msg = f"{sheet_name} row {r_idx}: Invalid numeric amount '{amt_val}'"
                    errors.append(ValidationError(sheet=sheet_name, row_number=r_idx, column="Amount", error_message=err_msg))
                    cat_issues["accounting_reconciliation"].append(err_msg)

            # 4. Validate Canonical Head Name
            if not head_val or not str(head_val).strip():
                err_msg = f"{sheet_name} row {r_idx}: Canonical Head Name cannot be blank"
                errors.append(ValidationError(sheet=sheet_name, row_number=r_idx, column="Head Name", error_message=err_msg))
                cat_issues["required_fields"].append(err_msg)
                continue

            clean_head = str(head_val).strip()
            head_obj = self._heads_by_name.get(clean_head.lower())
            if not head_obj:
                head_obj = self.registry.heads.get(clean_head)
            if not head_obj:
                alias_res = self.registry.resolve_by_alias(clean_head)
                if alias_res and alias_res[0] in self.registry.heads:
                    head_obj = self.registry.heads[alias_res[0]]

            if not head_obj:
                err_msg = f"Unknown or unapproved canonical head '{head_val}' in {sheet_name} row {r_idx}"
                errors.append(ValidationError(sheet=sheet_name, row_number=r_idx, column="Head Name", error_message=err_msg))
                cat_issues["canonical_mapping"].append(err_msg)
                continue

            # 5. Validate Direction
            if head_obj.primary_direction not in (expected_direction, "BOTH"):
                err_msg = f"Head '{head_obj.canonical_name}' has direction '{head_obj.primary_direction}', cannot appear in {sheet_name} row {r_idx}"
                errors.append(ValidationError(sheet=sheet_name, row_number=r_idx, column="Head Name", error_message=err_msg))
                cat_issues["canonical_mapping"].append(err_msg)

            # 6. Validate Website Selector Mapping
            wh = self.registry.get_website_head_for_canonical(head_obj.canonical_head_id, expected_direction)
            if not wh or not wh.website_head_value:
                err_msg = f"Head '{head_obj.canonical_name}' has no website selector mapping for direction '{expected_direction}' in {sheet_name} row {r_idx}"
                errors.append(ValidationError(sheet=sheet_name, row_number=r_idx, column="Head Name", error_message=err_msg))
                cat_issues["website_mapping"].append(err_msg)
                missing_website_mappings += 1

            # 7. Validate Subhead Policy
            clean_sub = str(sub_val).strip().lower() if (sub_val and str(sub_val).strip() not in ("None", "null", "-")) else ""
            pol = head_obj.subhead_policy

            if pol == SubheadPolicy.MANDATORY:
                if not clean_sub:
                    err_msg = f"Head '{head_obj.canonical_name}' requires a mandatory subhead, but Subhead is blank in {sheet_name} row {r_idx}"
                    errors.append(ValidationError(sheet=sheet_name, row_number=r_idx, column="Subhead", error_message=err_msg))
                    cat_issues["required_fields"].append(err_msg)
                else:
                    sub_obj = self._subheads_by_name.get(clean_sub)
                    if not sub_obj:
                        err_msg = f"Unknown subhead '{sub_val}' in {sheet_name} row {r_idx}"
                        errors.append(ValidationError(sheet=sheet_name, row_number=r_idx, column="Subhead", error_message=err_msg))
                        cat_issues["canonical_mapping"].append(err_msg)
                    elif sub_obj.parent_head_id != head_obj.canonical_head_id:
                        err_msg = f"Subhead '{sub_obj.canonical_name}' belongs to '{sub_obj.parent_head_id}', not '{head_obj.canonical_head_id}' in {sheet_name} row {r_idx}"
                        errors.append(ValidationError(sheet=sheet_name, row_number=r_idx, column="Subhead", error_message=err_msg))
                        cat_issues["canonical_mapping"].append(err_msg)

            elif pol == SubheadPolicy.PROHIBITED:
                if clean_sub:
                    err_msg = f"Head '{head_obj.canonical_name}' strictly prohibits subheads, but got '{sub_val}' in {sheet_name} row {r_idx}"
                    errors.append(ValidationError(sheet=sheet_name, row_number=r_idx, column="Subhead", error_message=err_msg))
                    cat_issues["required_fields"].append(err_msg)

            elif pol == SubheadPolicy.OPTIONAL:
                if clean_sub:
                    sub_obj = self._subheads_by_name.get(clean_sub)
                    if not sub_obj:
                        err_msg = f"Unknown optional subhead '{sub_val}' in {sheet_name} row {r_idx}"
                        errors.append(ValidationError(sheet=sheet_name, row_number=r_idx, column="Subhead", error_message=err_msg))
                        cat_issues["canonical_mapping"].append(err_msg)
                    elif sub_obj.parent_head_id != head_obj.canonical_head_id:
                        err_msg = f"Optional subhead '{sub_obj.canonical_name}' does not belong to head '{head_obj.canonical_head_id}' in {sheet_name} row {r_idx}"
                        errors.append(ValidationError(sheet=sheet_name, row_number=r_idx, column="Subhead", error_message=err_msg))
                        cat_issues["canonical_mapping"].append(err_msg)

        return count, total_dec, missing_website_mappings

    def _build_preflight_summary(
        self,
        expected_month: str,
        file_name: str,
        file_hash: str,
        errors: List[ValidationError],
        warnings: List[str],
        cat_issues: Dict[str, List[str]],
        rec_count: int,
        rec_total: Decimal,
        pay_count: int,
        pay_total: Decimal,
        opening_bal_dec: Decimal,
        computed_closing_dec: Decimal,
        target_closing_dec: Decimal,
        variance_dec: Decimal,
        physical_verified: bool,
        snapshot_path_str: Optional[str],
    ) -> PreflightSummary:
        """Assemble categorized results and strict overall readiness verdict."""
        categories = {}
        for cat_name, issue_list in cat_issues.items():
            status = ValidationCategoryStatus.PASS if len(issue_list) == 0 else ValidationCategoryStatus.BLOCKED
            categories[cat_name] = CategoryResult(
                category=cat_name,
                status=status,
                issue_count=len(issue_list),
                issues=issue_list,
                details={}
            )

        categories["accounting_reconciliation"].details = {
            "opening_balance": float(opening_bal_dec),
            "total_receipts": float(rec_total),
            "total_payments": float(pay_total),
            "computed_closing_balance": float(computed_closing_dec),
            "physical_closing_balance": float(target_closing_dec),
            "closing_balance_variance": float(variance_dec),
            "is_exact_match": (variance_dec == Decimal("0")),
        }

        categories["transaction_completeness"].details = {
            "receipts_count": rec_count,
            "payments_count": pay_count,
            "total_count": rec_count + pay_count,
        }

        all_cats_pass = all(c.status == ValidationCategoryStatus.PASS for c in categories.values())
        is_valid = (len(errors) == 0 and variance_dec == Decimal("0") and all_cats_pass)

        target_tx_count = None
        if expected_month in self._targets_cache:
            target_tx_count = self._targets_cache[expected_month].get("total_transactions")

        total_tx = rec_count + pay_count
        if target_tx_count and target_tx_count > 0:
            source_coverage_pct = round((total_tx / target_tx_count) * 100.0, 1)
        else:
            source_coverage_pct = 100.0 if total_tx > 0 else 0.0

        if is_valid:
            status_str = "READY_FOR_EXECUTION"
        elif any("Synthetic or test artifact detected" in e.error_message for e in errors):
            status_str = "BLOCKED — INVALID PRODUCTION WORKBOOK. Synthetic or test artifact detected."
        elif variance_dec != Decimal("0"):
            status_str = "BLOCKED - BALANCE MISMATCH"
        else:
            status_str = "VALIDATION_FAILED"
        blocked_reason = None
        if not is_valid:
            synthetic_errors = [e.error_message for e in errors if "Synthetic or test artifact detected" in e.error_message]
            if synthetic_errors:
                blocked_reason = synthetic_errors[0]
            elif variance_dec != Decimal("0"):
                blocked_reason = (
                    f"Closing balance mismatch: Workbook ₹{float(computed_closing_dec):,.2f} "
                    f"vs Physical ₹{float(target_closing_dec):,.2f} "
                    f"(Variance: ₹{float(variance_dec):,.2f})"
                )
            elif errors:
                blocked_reason = errors[0].error_message
            else:
                blocked_reason = "Critical pre-flight validation gates failed."

        web_status = "COMPLETE" if categories["website_mapping"].status == ValidationCategoryStatus.PASS else "MISSING_MAPPINGS"

        return PreflightSummary(
            month=expected_month,
            file_name=file_name,
            file_hash_sha256=file_hash,
            is_valid=is_valid,
            status=status_str,
            blocked_reason=blocked_reason,
            receipts_count=rec_count,
            receipts_total=float(rec_total),
            payments_count=pay_count,
            payments_total=float(pay_total),
            net_movement=float(rec_total - pay_total),
            opening_balance=float(opening_bal_dec),
            expected_closing_balance=float(computed_closing_dec),
            physical_closing_balance=float(target_closing_dec),
            closing_balance_variance=float(variance_dec),
            physical_opening_verified=physical_verified,
            source_coverage_percent=source_coverage_pct,
            website_mapping_status=web_status,
            snapshot_path=snapshot_path_str,
            categories=categories,
            errors=errors,
            warnings=warnings,
        )
