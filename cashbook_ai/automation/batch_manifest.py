"""Bounded batch authorization manifests for e-Dayan transaction entry.

This module provides data structures and functions to generate, serialize,
validate, and authorize batches of transactions.
Batch authorization grants permission for one strictly bounded batch of eligible
transactions, while preserving full transaction-level verification at runtime.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

DRAFT = "DRAFT"
APPROVED = "APPROVED"
MANIFEST_STATES = {DRAFT, APPROVED}


class ManifestError(Exception):
    """Base error for manifest validation and processing."""


class WorkbookTamperedError(ManifestError):
    """Raised when the workbook SHA-256 hash does not match the manifest."""


class UnauthorizedTransactionError(ManifestError):
    """Raised when an entry is not present in the authorized manifest whitelist."""


class ManifestStateError(ManifestError):
    """Raised when an operation is invalid for the current manifest state."""


@dataclass
class BatchManifest:
    manifest_id: str
    created_at: str
    workbook_path: str
    workbook_sha256: str
    direction: str  # "RECEIPT" or "PAYMENT"
    status: str     # "DRAFT" or "APPROVED"
    batch_summary: dict[str, Any]
    authorized_keys: list[str]
    authorized_entries: list[dict[str, Any]]
    excluded_entries: list[dict[str, Any]]
    approval_metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def calculate_file_sha256(path: Path) -> str:
    """Calculate the SHA-256 hash of a file on disk."""
    if not path.is_file():
        raise FileNotFoundError(f"File not found for hash calculation: {path}")
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _parse_voucher_int(v: Any) -> int | None:
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        return None


def generate_batch_manifest(
    report: Mapping[str, Any],
    journal: Mapping[str, Any],
    workbook_path: Path,
    *,
    direction: str = "RECEIPT",
    target_voucher_start: int | None = None,
    target_voucher_end: int | None = None,
    manifest_id: str | None = None,
) -> BatchManifest:
    """Generate a DRAFT batch manifest from dry-run report and journal data.

    Only transactions with status == READY, matching the requested direction,
    not previously CONFIRMED or UNCERTAIN in the journal, and falling into the
    contiguous voucher range are authorized.
    All others are explicitly preserved in excluded_entries with detailed reasons.
    """
    direction = direction.upper()
    if direction not in {"RECEIPT", "PAYMENT"}:
        raise ValueError(f"Unsupported direction: {direction}")

    workbook_sha256 = calculate_file_sha256(workbook_path)
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    journal_entries = journal.get("entries", {})

    all_entries = list(report.get("entries", []))

    # Identify confirmed vouchers for context
    confirmed_vouchers = set()
    for e in all_entries:
        key = e.get("submission_key")
        j_entry = journal_entries.get(key, {})
        if j_entry.get("status") == "confirmed":
            v_int = _parse_voucher_int(e.get("receipt_number"))
            if v_int is not None:
                confirmed_vouchers.add(v_int)

    # Determine contiguous sequence boundaries if not explicitly specified
    if target_voucher_start is None:
        # Find candidates: direction match, READY, not confirmed
        candidate_vouchers: list[int] = []
        for e in all_entries:
            if e.get("direction") == direction and e.get("status") == "READY":
                key = e.get("submission_key")
                j_status = journal_entries.get(key, {}).get("status")
                if j_status not in {"confirmed", "uncertain"}:
                    v_int = _parse_voucher_int(e.get("receipt_number"))
                    if v_int is not None:
                        candidate_vouchers.append(v_int)

        # Detect the longest contiguous run of consecutive voucher numbers
        candidate_vouchers.sort()
        best_run: list[int] = []
        current_run: list[int] = []
        for v in candidate_vouchers:
            if not current_run:
                current_run.append(v)
            elif v == current_run[-1] + 1:
                current_run.append(v)
            elif v == current_run[-1]:
                # Duplicate number in candidates
                pass
            else:
                if len(current_run) > len(best_run):
                    best_run = list(current_run)
                current_run = [v]
        if len(current_run) > len(best_run):
            best_run = list(current_run)

        if best_run:
            target_voucher_start = best_run[0]
            target_voucher_end = best_run[-1]

    authorized_entries: list[dict[str, Any]] = []
    excluded_entries: list[dict[str, Any]] = []

    for e in all_entries:
        key = e["submission_key"]
        v_num = str(e.get("receipt_number", "")).strip()
        v_int = _parse_voucher_int(v_num)
        amt = e.get("amount")
        sheet = e.get("sheet")
        row_num = e.get("row")
        entry_dir = e.get("direction")
        status = e.get("status")
        j_entry = journal_entries.get(key, {})
        j_status = j_entry.get("status")

        # Direction filter
        if entry_dir != direction:
            excluded_entries.append({
                "submission_key": key,
                "sheet": sheet,
                "row": row_num,
                "voucher_number": v_num,
                "amount": amt,
                "head_name": e.get("head_name"),
                "reason": f"DIRECTION_EXCLUDED: Sheet '{sheet}' direction '{entry_dir}' does not match batch direction '{direction}'",
            })
            continue

        # Journal terminal state check: CONFIRMED
        if j_status == "confirmed":
            excluded_entries.append({
                "submission_key": key,
                "sheet": sheet,
                "row": row_num,
                "voucher_number": v_num,
                "amount": amt,
                "head_name": e.get("head_name"),
                "reason": "ALREADY_CONFIRMED: Transaction already confirmed with website evidence in journal",
                "evidence": j_entry.get("evidence"),
            })
            continue

        # Journal terminal state check: UNCERTAIN
        if j_status == "uncertain":
            excluded_entries.append({
                "submission_key": key,
                "sheet": sheet,
                "row": row_num,
                "voucher_number": v_num,
                "amount": amt,
                "head_name": e.get("head_name"),
                "reason": "ALREADY_UNCERTAIN: Prior submission outcome is uncertain and requires independent human reconciliation",
                "error": j_entry.get("error"),
            })
            continue

        # Dry-run validation check: BLOCKED
        if status == "BLOCKED":
            issues_str = "; ".join(e.get("issues", [])) or "Validation blocked"
            excluded_entries.append({
                "submission_key": key,
                "sheet": sheet,
                "row": row_num,
                "voucher_number": v_num,
                "amount": amt,
                "head_name": e.get("head_name"),
                "reason": f"BLOCKED: {issues_str}",
            })
            continue

        # Dry-run validation check: NEEDS_REVIEW
        if status == "NEEDS_REVIEW":
            review_str = "; ".join(e.get("review", [])) or "Review required"
            excluded_entries.append({
                "submission_key": key,
                "sheet": sheet,
                "row": row_num,
                "voucher_number": v_num,
                "amount": amt,
                "head_name": e.get("head_name"),
                "reason": f"NEEDS_REVIEW: {review_str}",
            })
            continue

        # Voucher sequence check
        if target_voucher_start is not None and target_voucher_end is not None:
            if v_int is None or v_int < target_voucher_start or v_int > target_voucher_end:
                excluded_entries.append({
                    "submission_key": key,
                    "sheet": sheet,
                    "row": row_num,
                    "voucher_number": v_num,
                    "amount": amt,
                    "head_name": e.get("head_name"),
                    "reason": f"VOUCHER_DISCONTINUITY: Voucher '{v_num}' is outside contiguous batch sequence {target_voucher_start}..{target_voucher_end}",
                })
                continue

        # If eligible, add to authorized entries
        authorized_entries.append({
            "submission_key": key,
            "sheet": sheet,
            "row": row_num,
            "date": e.get("date"),
            "receipt_number": v_num,
            "amount": amt,
            "head_name": e.get("head_name"),
            "subhead": e.get("subhead"),
            "narration": e.get("narration"),
            "mode": e.get("mode", "cash"),
            "head_resolution": e.get("head_resolution", {}),
            "selectors": e.get("selectors", {}),
        })

    # Sort authorized entries by voucher integer for strict sequential execution
    authorized_entries.sort(key=lambda item: _parse_voucher_int(item["receipt_number"]) or 0)
    authorized_keys = [item["submission_key"] for item in authorized_entries]

    total_amount = round(sum(float(item["amount"]) for item in authorized_entries), 2)
    voucher_range = (
        f"{target_voucher_start}-{target_voucher_end}"
        if target_voucher_start and target_voucher_end
        else "none"
    )

    batch_summary = {
        "direction": direction,
        "transaction_count": len(authorized_entries),
        "total_amount": total_amount,
        "currency": "INR",
        "voucher_range": voucher_range,
    }

    if not manifest_id:
        v_start_label = str(target_voucher_start or "0")
        v_end_label = str(target_voucher_end or "0")
        manifest_id = f"batch-{direction.lower()}-{v_start_label}-{v_end_label}"

    return BatchManifest(
        manifest_id=manifest_id,
        created_at=timestamp,
        workbook_path=str(workbook_path),
        workbook_sha256=workbook_sha256,
        direction=direction,
        status=DRAFT,
        batch_summary=batch_summary,
        authorized_keys=authorized_keys,
        authorized_entries=authorized_entries,
        excluded_entries=excluded_entries,
        approval_metadata=None,
    )


def validate_manifest(manifest: BatchManifest, workbook_path: Path | None = None) -> None:
    """Validate manifest integrity, hash, whitelist, and state invariants."""
    wb_path = workbook_path or Path(manifest.workbook_path)
    if not wb_path.is_file():
        raise FileNotFoundError(f"Workbook not found at {wb_path}")

    current_hash = calculate_file_sha256(wb_path)
    if current_hash != manifest.workbook_sha256:
        raise WorkbookTamperedError(
            f"Workbook hash mismatch! Manifest hash: {manifest.workbook_sha256}, disk hash: {current_hash}"
        )

    # Validate authorized keys match entries exactly
    entry_keys = [e["submission_key"] for e in manifest.authorized_entries]
    if entry_keys != manifest.authorized_keys:
        raise UnauthorizedTransactionError("authorized_keys does not match authorized_entries submission keys")

    # Validate count and total amount
    if len(manifest.authorized_entries) != manifest.batch_summary.get("transaction_count"):
        raise ManifestError("batch_summary transaction_count does not match authorized_entries length")

    actual_total = round(sum(float(e["amount"]) for e in manifest.authorized_entries), 2)
    if actual_total != round(float(manifest.batch_summary.get("total_amount", 0)), 2):
        raise ManifestError(
            f"batch_summary total_amount mismatch: declared {manifest.batch_summary.get('total_amount')}, actual {actual_total}"
        )

    if manifest.status not in MANIFEST_STATES:
        raise ManifestStateError(f"Invalid manifest status: {manifest.status}")

    if manifest.status == APPROVED:
        if not manifest.approval_metadata:
            raise ManifestStateError("Approved manifest is missing approval_metadata")
        if not manifest.approval_metadata.get("approved_by"):
            raise ManifestStateError("Approved manifest missing approved_by")


def approve_manifest(
    manifest: BatchManifest,
    approved_by: str,
    workbook_path: Path | None = None,
) -> BatchManifest:
    """Explicitly authorize a batch manifest, binding approval to content & hash."""
    validate_manifest(manifest, workbook_path)
    if manifest.status == APPROVED:
        raise ManifestStateError("Manifest is already approved")

    approved = copy.deepcopy(manifest)
    manifest_bytes = json.dumps(approved.to_dict(), sort_keys=True).encode("utf-8")
    content_hash = hashlib.sha256(manifest_bytes).hexdigest()

    approved.status = APPROVED
    approved.approval_metadata = {
        "approved_by": approved_by,
        "approved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "manifest_content_sha256": content_hash,
    }
    return approved


def save_manifest(manifest: BatchManifest, path: Path) -> None:
    """Persist manifest to disk atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_manifest(path: Path) -> BatchManifest:
    """Load a BatchManifest from disk."""
    if not path.is_file():
        raise FileNotFoundError(f"Manifest not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return BatchManifest(
        manifest_id=data["manifest_id"],
        created_at=data["created_at"],
        workbook_path=data["workbook_path"],
        workbook_sha256=data["workbook_sha256"],
        direction=data["direction"],
        status=data["status"],
        batch_summary=data["batch_summary"],
        authorized_keys=data["authorized_keys"],
        authorized_entries=data["authorized_entries"],
        excluded_entries=data["excluded_entries"],
        approval_metadata=data.get("approval_metadata"),
    )
