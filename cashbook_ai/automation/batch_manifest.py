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
    authorized_vouchers: list[int] | None = None,
    manifest_id: str | None = None,
) -> BatchManifest:
    """Generate a DRAFT batch manifest from dry-run report and journal data.

    Transactions are authorized when they satisfy ALL of:
    - direction matches the batch direction
    - dry-run status == READY
    - not previously CONFIRMED or UNCERTAIN in the journal
    - if authorized_vouchers is given: voucher number is in that explicit set
    - if target_voucher_start/end are given: voucher number is within that range
    - if neither filter is given: ALL eligible READY transactions are authorized
      (non-contiguous voucher numbers are valid and included by default)

    All excluded transactions are listed explicitly with reasons.
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

    # Explicit authorized_vouchers whitelist takes priority over range filtering
    # When neither is specified, ALL eligible transactions are included (no range filter)
    use_voucher_whitelist = authorized_vouchers is not None
    use_range_filter = (target_voucher_start is not None or target_voucher_end is not None) and not use_voucher_whitelist

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

        # Voucher filter — applied only when an explicit constraint is given.
        # Rule: non-contiguous voucher numbers are valid. A voucher gap does NOT
        # automatically exclude a legitimate transaction. Filtering is only applied
        # when the caller explicitly passes authorized_vouchers or a range.
        if use_voucher_whitelist:
            assert authorized_vouchers is not None
            if v_int not in authorized_vouchers:
                excluded_entries.append({
                    "submission_key": key,
                    "sheet": sheet,
                    "row": row_num,
                    "voucher_number": v_num,
                    "amount": amt,
                    "head_name": e.get("head_name"),
                    "reason": f"NOT_IN_WHITELIST: Voucher '{v_num}' is not in the explicit authorized voucher list",
                })
                continue
        elif use_range_filter:
            if v_int is None or (target_voucher_start is not None and v_int < target_voucher_start) or (target_voucher_end is not None and v_int > target_voucher_end):
                excluded_entries.append({
                    "submission_key": key,
                    "sheet": sheet,
                    "row": row_num,
                    "voucher_number": v_num,
                    "amount": amt,
                    "head_name": e.get("head_name"),
                    "reason": f"VOUCHER_RANGE_EXCLUDED: Voucher '{v_num}' is outside requested batch range {target_voucher_start}..{target_voucher_end}",
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

    # Sort authorized entries sequentially: standard vouchers 1..N first, then 0/00 contra entries, breaking ties by row
    authorized_entries.sort(
        key=lambda item: (
            1 if (_parse_voucher_int(item["receipt_number"]) or 0) == 0 else 0,
            _parse_voucher_int(item["receipt_number"]) or 0,
            item.get("row", 0),
        )
    )
    authorized_keys = [item["submission_key"] for item in authorized_entries]

    total_amount = round(sum(float(item["amount"]) for item in authorized_entries), 2)

    # Compute a readable voucher range or list for the batch summary
    auth_v_ints = sorted(
        v for v in (_parse_voucher_int(item["receipt_number"]) for item in authorized_entries)
        if v is not None
    )
    if use_voucher_whitelist and authorized_vouchers is not None:
        voucher_range = ",".join(str(v) for v in sorted(authorized_vouchers))
    elif auth_v_ints:
        voucher_range = f"{auth_v_ints[0]}-{auth_v_ints[-1]}"
    else:
        voucher_range = "none"

    batch_summary = {
        "direction": direction,
        "transaction_count": len(authorized_entries),
        "total_amount": total_amount,
        "currency": "INR",
        "voucher_range": voucher_range,
    }

    if not manifest_id:
        if use_voucher_whitelist and authorized_vouchers is not None:
            v_label = "-".join(str(v) for v in sorted(authorized_vouchers))
        elif auth_v_ints:
            v_label = f"{auth_v_ints[0]}-{auth_v_ints[-1]}"
        else:
            v_label = "0-0"
        manifest_id = f"batch-{direction.lower()}-{v_label}"

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


def check_source_completeness(
    report: Mapping[str, Any],
    manifest: "BatchManifest",
    journal: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify that every eligible source transaction is accounted for before approval.

    Rule 2: Complete source-to-manifest coverage.

    Eligible source transactions are those with direction matching the manifest and
    dry-run status == READY (not BLOCKED, not NEEDS_REVIEW). Confirmed transactions
    are considered already complete. Uncertain transactions require reconciliation first.

    Returns a completeness report dict with 'complete' (bool) and detailed counts.
    A batch approval should be blocked when 'complete' is False.
    """
    direction = manifest.direction.upper()
    journal_entries = journal.get("entries", {})
    authorized_keys = set(manifest.authorized_keys)
    excl_reasons = {e["submission_key"]: e.get("reason", "") for e in manifest.excluded_entries}

    source_count = 0
    ready_count = 0
    blocked_count = 0
    needs_review_count = 0
    confirmed_count = 0
    uncertain_count = 0
    authorized_count = 0
    unaccounted: list[dict[str, Any]] = []

    for e in report.get("entries", []):
        if e.get("direction") != direction:
            continue
        source_count += 1
        key = e.get("submission_key", "")
        status = e.get("status", "")
        j_status = journal_entries.get(key, {}).get("status", "")

        if j_status == "confirmed":
            confirmed_count += 1
        elif j_status == "uncertain":
            uncertain_count += 1
        elif status == "BLOCKED":
            blocked_count += 1
        elif status == "NEEDS_REVIEW":
            needs_review_count += 1
        else:
            ready_count += 1

        if key in authorized_keys:
            authorized_count += 1
        elif j_status not in ("confirmed",) and key not in excl_reasons:
            # Source transaction not in authorized list and no exclusion reason recorded
            unaccounted.append({
                "submission_key": key,
                "row": e.get("row"),
                "voucher_number": e.get("receipt_number"),
                "amount": e.get("amount"),
                "head_name": e.get("head_name"),
                "dry_run_status": status,
                "journal_status": j_status,
            })

    receipt_total = round(sum(
        float(e.get("amount", 0)) for e in report.get("entries", [])
        if e.get("direction") == direction
    ), 2)
    authorized_total = manifest.batch_summary.get("total_amount", 0.0)
    confirmed_total = round(sum(
        float(e.get("amount", 0)) for e in report.get("entries", [])
        if e.get("direction") == direction
        and journal_entries.get(e.get("submission_key", ""), {}).get("status") == "confirmed"
    ), 2)

    complete = (
        len(unaccounted) == 0
        and uncertain_count == 0
        and needs_review_count == 0
        and blocked_count == 0
    )

    return {
        "complete": complete,
        "direction": direction,
        "source_count": source_count,
        "ready_count": ready_count,
        "blocked_count": blocked_count,
        "needs_review_count": needs_review_count,
        "confirmed_count": confirmed_count,
        "uncertain_count": uncertain_count,
        "authorized_count": authorized_count,
        "source_total": receipt_total,
        "authorized_total": authorized_total,
        "confirmed_total": confirmed_total,
        "unaccounted_entries": unaccounted,
        "completeness_failures": (
            ([f"UNCERTAIN_ENTRIES: {uncertain_count} entries require reconciliation before execution"] if uncertain_count else [])
            + ([f"NEEDS_REVIEW_ENTRIES: {needs_review_count} entries still need review"] if needs_review_count else [])
            + ([f"BLOCKED_ENTRIES: {blocked_count} entries have blocking validation failures"] if blocked_count else [])
            + ([f"UNACCOUNTED_ENTRIES: {len(unaccounted)} source transactions are not authorized or excluded"] if unaccounted else [])
        ),
    }


def validate_manifest(manifest: "BatchManifest", workbook_path: Path | None = None) -> None:
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
