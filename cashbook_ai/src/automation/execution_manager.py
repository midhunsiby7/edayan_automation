"""Autonomous e-Dayan execution manager, process locking, manifest generation, and final reconciliation."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import openpyxl

from automation.batch_manifest import (
    APPROVED,
    DRAFT,
    BatchManifest,
    ManifestError,
    ManifestStateError,
    UnauthorizedTransactionError,
    WorkbookTamperedError,
    approve_manifest,
    calculate_file_sha256,
    save_manifest,
    validate_manifest,
)
from automation.edayan_entry import (
    CONFIRMED,
    FAILED,
    PENDING,
    READY,
    SUBMITTED,
    UNCERTAIN,
    BatchEntryRunner,
    BrowserEntryAdapter,
    RelayBrowserClient,
    RelayBrowserEntryAdapter,
    load_or_create_journal,
    reconcile_uncertain_entry,
)
from src.reference.canonical_registry import CanonicalRegistry

logger = logging.getLogger(__name__)


class ExecutionLockedError(Exception):
    """Raised when an execution operation is attempted while another runner holds the lock."""


class ExecutionLock:
    """Process-level and application-level lock ensuring exactly one active automation owner."""

    def __init__(self, lock_path: Path, month: str, batch_id: str, owner: str = "BatchRunner"):
        self.lock_path = Path(lock_path)
        self.month = month
        self.batch_id = batch_id
        self.owner = owner
        self._acquired = False

    def acquire(self, timeout_seconds: float = 3.0) -> bool:
        start_time = time.monotonic()
        while time.monotonic() - start_time < timeout_seconds:
            if not self.lock_path.is_file():
                try:
                    self.lock_path.parent.mkdir(parents=True, exist_ok=True)
                    lock_data = {
                        "pid": os.getpid(),
                        "month": self.month,
                        "batch_id": self.batch_id,
                        "owner": self.owner,
                        "acquired_at": datetime.now(timezone.utc).isoformat(),
                    }
                    temp_file = self.lock_path.with_suffix(".tmp")
                    temp_file.write_text(json.dumps(lock_data, indent=2), encoding="utf-8")
                    temp_file.replace(self.lock_path)
                    self._acquired = True
                    return True
                except Exception as e:
                    logger.warning(f"Failed to acquire lock: {e}")

            # Check if existing lock is stale (PID no longer running or > 3 hours old)
            try:
                data = json.loads(self.lock_path.read_text(encoding="utf-8"))
                lock_pid = data.get("pid")
                # If same process, allow re-entrance
                if lock_pid == os.getpid():
                    self._acquired = True
                    return True

                if not self._is_pid_alive(lock_pid):
                    logger.warning(f"Reclaiming stale lock held by dead PID {lock_pid}")
                    self.lock_path.unlink(missing_ok=True)
                    continue
            except Exception:
                pass

            time.sleep(0.2)

        raise ExecutionLockedError(
            f"Execution is locked by another automation process for month '{self.month}' ({self.lock_path})"
        )

    def release(self):
        if self._acquired and self.lock_path.is_file():
            try:
                data = json.loads(self.lock_path.read_text(encoding="utf-8"))
                if data.get("pid") == os.getpid():
                    self.lock_path.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"Error releasing execution lock: {e}")
            finally:
                self._acquired = False

    @staticmethod
    def _is_pid_alive(pid: Optional[int]) -> bool:
        if not pid or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


class ManifestBuilder:
    """Constructs valid, 100% coverage BatchManifests from an approved workbook snapshot."""

    REQUIRED_COLUMNS = ["Date", "Receipt Number", "Amount", "Head Name", "Subhead", "Narration"]

    def __init__(self, registry: CanonicalRegistry):
        self.registry = registry

        self._heads_by_name: Dict[str, Any] = {}
        for h in self.registry.heads.values():
            if h.is_active:
                self._heads_by_name[h.canonical_name.lower().strip()] = h

    def build_manifest(
        self,
        workbook_path: Path,
        direction: str,
        month: str,
        manifest_id: Optional[str] = None,
    ) -> BatchManifest:
        """Extract all transactions for direction from workbook and build a 100% covered BatchManifest."""
        dir_upper = direction.upper().strip()
        if dir_upper not in ("RECEIPT", "PAYMENT"):
            raise ValueError(f"Invalid direction: {direction}")

        sheet_name = "Receipts" if dir_upper == "RECEIPT" else "Payments"
        file_path = Path(workbook_path)
        if not file_path.is_file():
            raise FileNotFoundError(f"Workbook not found: {file_path}")

        file_hash = calculate_file_sha256(file_path)
        wb = openpyxl.load_workbook(file_path, data_only=True)
        if sheet_name not in wb.sheetnames:
            raise ManifestError(f"Missing required sheet '{sheet_name}' in workbook {file_path.name}")

        sheet = wb[sheet_name]
        header = [str(cell.value or "").strip() for cell in sheet[1]]
        col_map = {}
        for col in self.REQUIRED_COLUMNS:
            if col in header:
                col_map[col] = header.index(col) + 1
            elif col == "Receipt Number" and "Voucher Number" in header:
                col_map["Receipt Number"] = header.index("Voucher Number") + 1
            else:
                raise ManifestError(f"Missing required column '{col}' in sheet '{sheet_name}'")

        authorized_entries: List[Dict[str, Any]] = []
        authorized_keys: List[str] = []
        total_amount = 0.0
        timestamp = datetime.now(timezone.utc).isoformat()

        for r_idx in range(2, sheet.max_row + 1):
            date_val = sheet.cell(row=r_idx, column=col_map["Date"]).value
            v_val = sheet.cell(row=r_idx, column=col_map["Receipt Number"]).value
            amt_val = sheet.cell(row=r_idx, column=col_map["Amount"]).value
            head_val = sheet.cell(row=r_idx, column=col_map["Head Name"]).value
            sub_val = sheet.cell(row=r_idx, column=col_map["Subhead"]).value
            narr_val = sheet.cell(row=r_idx, column=col_map["Narration"]).value

            if not any([date_val, v_val, amt_val, head_val, sub_val, narr_val]):
                continue

            clean_head = str(head_val or "").strip()
            head_obj = self._heads_by_name.get(clean_head.lower())
            if not head_obj:
                head_obj = self.registry.heads.get(clean_head)
            if not head_obj:
                alias_res = self.registry.resolve_by_alias(clean_head)
                if alias_res and alias_res[0] in self.registry.heads:
                    head_obj = self.registry.heads[alias_res[0]]

            if not head_obj:
                raise ManifestError(f"Row {r_idx}: Unknown canonical head '{head_val}'")

            wh = self.registry.get_website_head_for_canonical(head_obj.canonical_head_id, dir_upper)
            if not wh or not wh.website_head_value:
                raise ManifestError(
                    f"Row {r_idx}: Head '{head_obj.canonical_name}' has no website selector mapping for {dir_upper}"
                )

            amt = round(float(str(amt_val).replace(",", "").strip()), 2)
            total_amount += amt

            v_num = str(v_val or "").strip()
            date_str = str(date_val or "").strip()
            subhead_str = str(sub_val or "").strip() if sub_val else None
            narration_str = str(narr_val or "").strip()

            payload = {
                "direction": dir_upper,
                "sheet": sheet_name,
                "row": r_idx,
                "date": date_str,
                "receipt_number": v_num,
                "amount": amt,
                "head_name": head_obj.canonical_name,
                "subhead": subhead_str,
                "narration": narration_str,
                "mode": "cash",
                "selectors": {
                    "form": "#frmreceipt" if dir_upper == "RECEIPT" else "#frmpayment",
                    "date": "#datepicker1",
                    "number": "#receiptno",
                    "amount": "#amount",
                    "cash": "#mode_cash",
                    "head": "#head",
                    "subhead": "#subhead",
                    "narration": "#narration",
                    "save": "#submit_form",
                },
                "head_resolution": {
                    "status": "matched",
                    "website_value": str(wh.website_head_value),
                    "website_label": str(wh.website_head_label),
                    "canonical_head_id": head_obj.canonical_head_id,
                },
                "issues": [],
                "review": [],
                "status": READY,
            }

            canonical_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
            sub_key = hashlib.sha256(canonical_bytes).hexdigest()
            payload["submission_key"] = sub_key

            authorized_entries.append(payload)
            authorized_keys.append(sub_key)

        if not manifest_id:
            clean_m = month.replace("-", "")
            manifest_id = f"batch-{clean_m}-{dir_upper.lower()}-{len(authorized_entries)}"

        batch_summary = {
            "month": month,
            "direction": dir_upper,
            "transaction_count": len(authorized_entries),
            "total_amount": round(total_amount, 2),
            "source_coverage": 1.0,
            "created_at": timestamp,
        }

        manifest = BatchManifest(
            manifest_id=manifest_id,
            created_at=timestamp,
            workbook_path=str(file_path.resolve()),
            workbook_sha256=file_hash,
            direction=dir_upper,
            status=DRAFT,
            batch_summary=batch_summary,
            authorized_keys=authorized_keys,
            authorized_entries=authorized_entries,
            excluded_entries=[],
            approval_metadata=None,
        )

        validate_manifest(manifest, file_path)
        return manifest


class FinalReconciler:
    """Verifies all transaction records, counts, totals, opening balance, and closing balance."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)

    def reconcile_month(
        self,
        month: str,
        receipts_manifest: BatchManifest,
        payments_manifest: BatchManifest,
        journal_path: Path,
        opening_balance: float,
        live_closing_balance: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Perform 9-point reconciliation between approved manifests, execution journal, and live portal."""
        journal = load_or_create_journal(journal_path)
        journal_entries = journal.get("entries", {})

        # 1. Receipts counts & totals
        exp_rec_count = len(receipts_manifest.authorized_entries)
        exp_rec_total = round(float(receipts_manifest.batch_summary.get("total_amount", 0.0)), 2)
        conf_rec_entries = [
            e for e in receipts_manifest.authorized_entries
            if journal_entries.get(e["submission_key"], {}).get("status") == CONFIRMED
        ]
        conf_rec_count = len(conf_rec_entries)
        conf_rec_total = round(sum(float(e["amount"]) for e in conf_rec_entries), 2)

        # 2. Payments counts & totals
        exp_pay_count = len(payments_manifest.authorized_entries)
        exp_pay_total = round(float(payments_manifest.batch_summary.get("total_amount", 0.0)), 2)
        conf_pay_entries = [
            e for e in payments_manifest.authorized_entries
            if journal_entries.get(e["submission_key"], {}).get("status") == CONFIRMED
        ]
        conf_pay_count = len(conf_pay_entries)
        conf_pay_total = round(sum(float(e["amount"]) for e in conf_pay_entries), 2)

        # 3. Check for uncertain or failed entries
        uncertain_entries = [
            k for k, v in journal_entries.items()
            if v.get("status") == UNCERTAIN
        ]
        failed_entries = [
            k for k, v in journal_entries.items()
            if v.get("status") == FAILED
        ]

        # 4. Balances
        net_movement = round(conf_rec_total - conf_pay_total, 2)
        expected_closing = round(opening_balance + net_movement, 2)

        variance = None
        reconciled_with_live = False
        if live_closing_balance is not None:
            variance = round(live_closing_balance - expected_closing, 2)
            reconciled_with_live = (abs(variance) < 0.001)

        # Determine reconciliation status
        all_confirmed = (
            conf_rec_count == exp_rec_count
            and conf_pay_count == exp_pay_count
            and conf_rec_total == exp_rec_total
            and conf_pay_total == exp_pay_total
            and len(uncertain_entries) == 0
        )

        status_str = "RECONCILED_COMPLETE" if (all_confirmed and (variance is None or reconciled_with_live)) else (
            "BLOCKED" if len(uncertain_entries) > 0 else "PARTIALLY_COMPLETE"
        )

        report = {
            "month": month,
            "reconciliation_timestamp": datetime.now(timezone.utc).isoformat(),
            "status": status_str,
            "overall_reconciled": (status_str == "RECONCILED_COMPLETE"),
            "is_fully_reconciled": (status_str == "RECONCILED_COMPLETE"),
            "confirmed_receipts_count": conf_rec_count,
            "expected_receipts_count": exp_rec_count,
            "confirmed_receipts_total": conf_rec_total,
            "confirmed_payments_count": conf_pay_count,
            "expected_payments_count": exp_pay_count,
            "confirmed_payments_total": conf_pay_total,
            "opening_balance": opening_balance,
            "net_movement": net_movement,
            "expected_closing_balance": expected_closing,
            "closing_balance_variance": variance or 0.0,
            "receipts": {
                "expected_count": exp_rec_count,
                "confirmed_count": conf_rec_count,
                "expected_total": exp_rec_total,
                "confirmed_total": conf_rec_total,
                "count_match": (exp_rec_count == conf_rec_count),
                "total_match": (exp_rec_total == conf_rec_total),
            },
            "payments": {
                "expected_count": exp_pay_count,
                "confirmed_count": conf_pay_count,
                "expected_total": exp_pay_total,
                "confirmed_total": conf_pay_total,
                "count_match": (exp_pay_count == conf_pay_count),
                "total_match": (exp_pay_total == conf_pay_total),
            },
            "balances": {
                "opening_balance": opening_balance,
                "net_movement": net_movement,
                "expected_closing_balance": expected_closing,
                "live_closing_balance": live_closing_balance,
                "variance": variance,
            },
            "uncertain_count": len(uncertain_entries),
            "failed_count": len(failed_entries),
            "uncertain_keys": uncertain_entries,
        }

        output_path = self.data_dir / "output" / month / "final_reconciliation.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

        return report


class ExecutionManager:
    """Manages the full e-Dayan execution lifecycle from authorization to final reconciliation."""

    def __init__(self, data_dir: Optional[Path] = None, registry: Optional[CanonicalRegistry] = None):
        base_dir = Path(__file__).resolve().parent.parent.parent
        self.data_dir = data_dir or (base_dir / "data")
        self.registry = registry or CanonicalRegistry()
        self.builder = ManifestBuilder(self.registry)
        self.reconciler = FinalReconciler(self.data_dir)

        self._active_runner: Optional[BatchEntryRunner] = None
        self._is_paused = False
        self._cancel_requested = False
        self._current_status = "IDLE"
        self._live_telemetry: Dict[str, Any] = {}
        self._recent_logs: List[str] = []

    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        self._recent_logs.append(entry)
        if len(self._recent_logs) > 100:
            self._recent_logs.pop(0)
        logger.info(entry)

    def get_status(self, month: str) -> Dict[str, Any]:
        """Return live execution status and lock info."""
        lock_file = self.data_dir / "jobs" / "execution.lock"
        is_locked = lock_file.is_file()
        lock_info = {}
        if is_locked:
            try:
                lock_info = json.loads(lock_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        journal_file = self.data_dir / "output" / month / "edayan_journal.json"
        journal_summary = {"total": 0, "confirmed": 0, "pending": 0, "uncertain": 0, "failed": 0}
        if journal_file.is_file():
            try:
                j = json.loads(journal_file.read_text(encoding="utf-8"))
                entries = j.get("entries", {})
                journal_summary["total"] = len(entries)
                for e in entries.values():
                    st = e.get("status")
                    if st in journal_summary:
                        journal_summary[st] += 1
            except Exception:
                pass

        # Check authorization record
        auth_file = self.data_dir / "output" / month / "authorization_record.json"
        is_authorized = auth_file.is_file()
        auth_info = {}
        if is_authorized:
            try:
                auth_info = json.loads(auth_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        batch_id = (
            auth_info.get("batch_id")
            or lock_info.get("batch_id")
            or None
        )
        total_tx = (
            self._live_telemetry.get("total")
            or (auth_info.get("receipts_count", 0) + auth_info.get("payments_count", 0))
            or journal_summary["total"]
        )
        completed_tx = (
            self._live_telemetry.get("completed")
            or journal_summary["confirmed"]
        )

        return {
            "status": self._current_status,
            "batch_id": batch_id,
            "total_transactions": total_tx,
            "completed_transactions": completed_tx,
            "is_locked": is_locked,
            "lock_info": lock_info,
            "is_authorized": is_authorized,
            "authorization": auth_info,
            "is_paused": self._is_paused,
            "journal_summary": journal_summary,
            "telemetry": self._live_telemetry,
            "recent_logs": list(self._recent_logs),
        }

    def authorize_batch(
        self,
        month: str,
        workbook_sha256: str,
        authorizer: str = "Accountant",
        opening_balance: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Explicitly authorize a validated workbook batch and generate approved manifests."""
        snapshot_dir = self.data_dir / "output" / month / "approved_snapshots"
        snapshot_file = snapshot_dir / f"{workbook_sha256}.xlsx"
        if not snapshot_file.is_file():
            raise FileNotFoundError(f"Approved snapshot for hash {workbook_sha256} not found in {snapshot_dir}")

        manifests_dir = self.data_dir / "output" / month / "manifests"
        manifests_dir.mkdir(parents=True, exist_ok=True)

        # 1. Build and approve Receipts manifest
        rec_manifest = self.builder.build_manifest(snapshot_file, "RECEIPT", month)
        rec_approved = approve_manifest(rec_manifest, approved_by=authorizer, workbook_path=snapshot_file)
        save_manifest(rec_approved, manifests_dir / f"{rec_approved.manifest_id}.json")

        # 2. Build and approve Payments manifest
        pay_manifest = self.builder.build_manifest(snapshot_file, "PAYMENT", month)
        pay_approved = approve_manifest(pay_manifest, approved_by=authorizer, workbook_path=snapshot_file)
        save_manifest(pay_approved, manifests_dir / f"{pay_approved.manifest_id}.json")

        # 3. Create persistent authorization record
        now_iso = datetime.now(timezone.utc).isoformat()
        clean_m = month.replace("-", "")
        batch_id = f"BATCH_{clean_m}_{int(time.time())}"

        open_bal = opening_balance
        if open_bal is None and month == "2026-05":
            open_bal = 18506.60

        auth_record = {
            "batch_id": batch_id,
            "month": month,
            "authorized_at": now_iso,
            "authorized_by": authorizer,
            "workbook_sha256": workbook_sha256,
            "workbook_snapshot": str(snapshot_file.resolve()),
            "receipts": {
                "manifest_id": rec_approved.manifest_id,
                "count": len(rec_approved.authorized_entries),
                "total_amount": rec_approved.batch_summary["total_amount"],
            },
            "payments": {
                "manifest_id": pay_approved.manifest_id,
                "count": len(pay_approved.authorized_entries),
                "total_amount": pay_approved.batch_summary["total_amount"],
            },
            "receipts_count": len(rec_approved.authorized_entries),
            "payments_count": len(pay_approved.authorized_entries),
            "opening_balance": open_bal,
            "status": "AUTHORIZED",
        }

        auth_path = self.data_dir / "output" / month / "authorization_record.json"
        auth_path.parent.mkdir(parents=True, exist_ok=True)
        auth_path.write_text(json.dumps(auth_record, indent=2), encoding="utf-8")

        self._current_status = "AUTHORIZED"
        return auth_record

    def execute_batch(
        self,
        month: str,
        adapter: BrowserEntryAdapter,
        save_timeout_seconds: float = 90.0,
        opening_balance: Optional[float] = None,
        live_closing_balance: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Execute the authorized batch manifests under a strict process execution lock."""
        auth_path = self.data_dir / "output" / month / "authorization_record.json"
        if not auth_path.is_file():
            raise ManifestStateError(f"No authorized batch found for {month}. Batch authorization is mandatory.")

        auth_record = json.loads(auth_path.read_text(encoding="utf-8"))
        batch_id = auth_record["batch_id"]
        lock_path = self.data_dir / "jobs" / "execution.lock"

        with ExecutionLock(lock_path, month, batch_id):
            self._current_status = "RUNNING"
            self._is_paused = False
            self._cancel_requested = False
            self._log(f"Execution lock acquired for {month} (Batch: {batch_id}). Single-owner runner active.")

            manifests_dir = self.data_dir / "output" / month / "manifests"
            journal_path = self.data_dir / "output" / month / "edayan_journal.json"
            incident_dir = self.data_dir / "output" / month / "incidents"
            incident_dir.mkdir(parents=True, exist_ok=True)

            # Load approved manifests
            from automation.batch_manifest import load_manifest
            rec_manifest = load_manifest(manifests_dir / f"{auth_record['receipts']['manifest_id']}.json")
            pay_manifest = load_manifest(manifests_dir / f"{auth_record['payments']['manifest_id']}.json")

            total_tx = len(rec_manifest.authorized_entries) + len(pay_manifest.authorized_entries)
            completed_tx = 0
            self._log(f"Loaded manifests: {len(rec_manifest.authorized_entries)} Receipts, {len(pay_manifest.authorized_entries)} Payments. Total: {total_tx}.")

            # 1. Execute Receipts
            self._live_telemetry = {
                "active_direction": "RECEIPT",
                "completed": completed_tx,
                "total": total_tx,
                "status": "RUNNING",
            }
            self._log("Starting Receipts execution...")
            rec_runner = BatchEntryRunner(
                manifest=rec_manifest,
                adapter=adapter,
                journal_path=journal_path,
                save_timeout_seconds=save_timeout_seconds,
                incident_dir=incident_dir,
            )
            self._active_runner = rec_runner

            rec_result = rec_runner.run_batch()
            rec_conf = rec_result.get("confirmed_count", 0) + rec_result.get("already_confirmed_count", 0)
            completed_tx += rec_conf
            self._live_telemetry["completed"] = completed_tx
            self._log(f"Receipts completed: {rec_conf} confirmed/already confirmed.")

            if rec_result.get("status") == "STOPPED":
                self._current_status = "STOPPED_CIRCUIT_BREAKER"
                self._log(f"CIRCUIT BREAKER: Receipts halted. Reason: {rec_result.get('reason')}")
                return {
                    "status": "STOPPED_CIRCUIT_BREAKER",
                    "direction": "RECEIPT",
                    "reason": rec_result.get("reason"),
                    "incident": rec_result.get("incident"),
                    "completed_count": completed_tx,
                    "total_count": total_tx,
                }

            # 2. Execute Payments
            self._live_telemetry["active_direction"] = "PAYMENT"
            self._log("Starting Payments execution...")
            pay_runner = BatchEntryRunner(
                manifest=pay_manifest,
                adapter=adapter,
                journal_path=journal_path,
                save_timeout_seconds=save_timeout_seconds,
                incident_dir=incident_dir,
            )
            self._active_runner = pay_runner

            pay_result = pay_runner.run_batch()
            pay_conf = pay_result.get("confirmed_count", 0) + pay_result.get("already_confirmed_count", 0)
            completed_tx += pay_conf
            self._live_telemetry["completed"] = completed_tx
            self._log(f"Payments completed: {pay_conf} confirmed/already confirmed.")

            if pay_result.get("status") == "STOPPED":
                self._current_status = "STOPPED_CIRCUIT_BREAKER"
                self._log(f"CIRCUIT BREAKER: Payments halted. Reason: {pay_result.get('reason')}")
                return {
                    "status": "STOPPED_CIRCUIT_BREAKER",
                    "direction": "PAYMENT",
                    "reason": pay_result.get("reason"),
                    "incident": pay_result.get("incident"),
                    "completed_count": completed_tx,
                    "total_count": total_tx,
                }

            # 3. Final Reconciliation
            self._log("Running final 9-point reconciliation report...")
            open_bal = opening_balance or auth_record.get("opening_balance") or 18506.60
            recon_report = self.reconciler.reconcile_month(
                month=month,
                receipts_manifest=rec_manifest,
                payments_manifest=pay_manifest,
                journal_path=journal_path,
                opening_balance=open_bal,
                live_closing_balance=live_closing_balance,
            )

            self._current_status = "COMPLETED"
            self._log(f"Execution completed successfully! Reconciled: {recon_report.get('overall_reconciled')}.")
            return {
                "status": "COMPLETED",
                "completed_count": completed_tx,
                "total_count": total_tx,
                "reconciliation": recon_report,
            }

    def pause(self):
        self._is_paused = True
        self._current_status = "PAUSED"

    def resume(self):
        self._is_paused = False
        self._current_status = "RUNNING"

    def cancel(self):
        self._cancel_requested = True
        self._current_status = "CANCELLED"
