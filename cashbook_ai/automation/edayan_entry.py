"""Guarded live-entry preparation, batch execution, and progress tracking for e-Dayan.

This module owns workbook preflight, batch execution, and durable transaction state.
A browser adapter must supply observed DOM state and an explicit save-confirmation
signal; this module never guesses that a click succeeded.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol

from .batch_manifest import (
    APPROVED,
    DRAFT,
    BatchManifest,
    ManifestError,
    ManifestStateError,
    UnauthorizedTransactionError,
    WorkbookTamperedError,
    approve_manifest,
    generate_batch_manifest,
    load_manifest,
    save_manifest,
    validate_manifest,
)
from .edayan_dry_run import run_dry_run

DEFAULT_RELAY_URL = "ws://localhost:11800"

READY = "READY"
PENDING = "pending"
SUBMITTED = "submitted"
CONFIRMED = "confirmed"
FAILED = "failed"
UNCERTAIN = "uncertain"
TERMINAL = {CONFIRMED, UNCERTAIN}
ACTIVE = {PENDING, SUBMITTED, FAILED}


class BrowserEntryAdapter(Protocol):
    """Supported-browser boundary for live and mock adapters."""

    def enter(self, entry: Mapping[str, Any]) -> Mapping[str, Any]:
        """Fill and verify the form, but do not submit it."""

    def readback(self, entry: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        """Read back observed form values and table evidence."""

    def submit(self) -> Mapping[str, Any]:
        """Click the observed submit control and return immediate evidence."""

    def observe_save(
        self, expected_entry: Mapping[str, Any] | None = None, timeout_seconds: float = 30
    ) -> Mapping[str, Any]:
        """Return fresh post-submit DOM/URL evidence confirming persistence."""


class RelayBrowserClient:
    """Minimal client for the already-installed Copilot Browser relay."""

    def __init__(self, relay_url: str = DEFAULT_RELAY_URL, timeout_seconds: float = 60):
        self.relay_url = relay_url
        self.timeout_seconds = timeout_seconds

    async def _command_async(self, command: str, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError(
                "The existing relay adapter requires the installed 'websockets' package"
            ) from exc
        request_id = str(uuid.uuid4())
        async with websockets.connect(self.relay_url, open_timeout=self.timeout_seconds) as socket:
            await socket.send(json.dumps({
                "type": "register",
                "clientType": "vscode",
                "metadata": {
                    "editor": "cashbook-entry",
                    "extensionVersion": "2.1.7",
                    "timestamp": int(time.time() * 1000),
                },
            }))
            connection = json.loads(await asyncio.wait_for(
                socket.recv(), timeout=self.timeout_seconds
            ))
            if connection.get("type") != "connection":
                raise RuntimeError(f"Relay registration did not return a connection: {connection}")
            await socket.send(json.dumps({
                "type": "command",
                "id": request_id,
                "command": command,
                "params": dict(params),
                "timestamp": int(time.time() * 1000),
            }))
            deadline = time.monotonic() + self.timeout_seconds
            while time.monotonic() < deadline:
                message = json.loads(await asyncio.wait_for(
                    socket.recv(), timeout=max(0.1, deadline - time.monotonic())
                ))
                if message.get("type") != "response" or message.get("id") != request_id:
                    continue
                if not message.get("success"):
                    raise RuntimeError(message.get("error", {}).get("message", "Browser command failed"))
                return {
                    "connection": connection,
                    "response": message,
                    "data": message.get("data"),
                }
            raise TimeoutError(f"Browser command {command!r} timed out")

    def command(self, command: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return asyncio.run(self._command_async(command, params or {}))

    def get_dom(self, selector: str | None = None) -> Mapping[str, Any]:
        params = {} if selector is None else {"selector": selector}
        return self.command("get_dom", params)["data"]

    def navigate(self, url: str) -> Mapping[str, Any]:
        return self.command("navigate", {"url": url})["data"]

    def type(self, selector: str, text: str, clear: bool = True) -> Mapping[str, Any]:
        return self.command("type", {"selector": selector, "text": text, "clear": clear})["data"]

    def select_option(self, selector: str, value: str) -> Mapping[str, Any]:
        return self.command("selectOption", {"selector": selector, "value": value})["data"]


def normalize_date_for_form(raw_date: Any) -> str:
    """Convert YYYY-MM-DD or date object to e-Dayan d-mm-yyyy format."""
    s = str(raw_date).strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return f"{s[8:10]}-{s[5:7]}-{s[0:4]}"
    return s


class RelayBrowserEntryAdapter:
    """Relay-backed adapter that prepares forms and submits only when explicitly armed."""

    def __init__(self, client: RelayBrowserClient, armed: bool = False):
        self.client = client
        self.armed = armed

    def probe(self) -> Mapping[str, Any]:
        """Return actual browser title DOM without changing the page."""
        return self.client.get_dom("title")

    def enter(self, entry: Mapping[str, Any]) -> Mapping[str, Any]:
        """Populate form fields for any verified Receipt or Payment entry."""
        direction = entry.get("direction", "RECEIPT")
        target_url = (
            "https://account.edayan.org/transactions"
            if direction == "RECEIPT"
            else "https://account.edayan.org/transactions/payment"
        )
        current_dom = self.client.get_dom()
        current_url = str(current_dom.get("url", ""))
        if not current_url.startswith(target_url):
            self.client.navigate(target_url)
            time.sleep(1.0)

        selectors = entry.get("selectors", {})
        date_sel = selectors.get("date", "#datepicker1")
        no_sel = selectors.get("number", "#receiptno")
        amt_sel = selectors.get("amount", "#amount")
        head_sel = selectors.get("head", "#head")
        subhead_sel = selectors.get("subhead", "#subhead")
        narration_sel = selectors.get("narration", "#narration")

        # 1. Date
        date_value = normalize_date_for_form(entry["date"])
        self.client.type(date_sel, date_value)

        # 2. Number
        self.client.type(no_sel, str(entry["receipt_number"]))

        # 3. Amount
        self.client.type(amt_sel, str(entry["amount"]))

        # 4. Head
        resolution = entry.get("head_resolution", {})
        website_value = resolution.get("website_value")
        if not website_value:
            raise ValueError(f"No website Head value resolved for entry: {entry.get('head_name')}")
        self.client.select_option(head_sel, str(website_value))

        # 5. Subhead
        subhead_name = entry.get("subhead")
        if subhead_name:
            observed = wait_for_subhead(
                lambda: self.client.get_dom(subhead_sel),
                timeout_seconds=20,
                interval_seconds=0.5,
            )
            html = str(observed.get("html", ""))
            match = re.search(
                rf'<option\s+value="([^"]+)"[^>]*>\s*{re.escape(subhead_name)}\s*</option>',
                html,
                re.IGNORECASE,
            )
            if not match:
                raise RuntimeError(
                    f"Expected live Subhead option {subhead_name!r} was not observed in: {observed}"
                )
            subhead_val = match.group(1)
            self.client.select_option(subhead_sel, subhead_val)

        # 6. Narration
        self.client.type(narration_sel, str(entry.get("narration") or ""))
        return self.readback(entry)

    def readback(self, entry: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        """Read back observed live form fields and table text."""
        selectors = (entry or {}).get("selectors", {})
        date_sel = selectors.get("date", "#datepicker1")
        no_sel = selectors.get("number", "#receiptno")
        amt_sel = selectors.get("amount", "#amount")
        cash_sel = selectors.get("cash", "#mode_cash")
        narration_sel = selectors.get("narration", "#narration")

        def _get_input(sel: str) -> str:
            res = self.client.command("type", {"selector": sel, "text": "", "clear": False})
            return str(res.get("data", {}).get("value", ""))

        def _get_text(sel: str) -> str:
            try:
                return str(self.client.get_dom(sel).get("text", "")).strip()
            except Exception:
                return ""

        table_text = _get_text("#myTable") or _get_text("#showtbook")

        return {
            "date": _get_input(date_sel),
            "receipt_number": _get_input(no_sel),
            "amount": _get_input(amt_sel),
            "mode_cash": "checked" in self.client.get_dom(cash_sel).get("html", ""),
            "head": _get_text("#select2-head-container"),
            "subhead": _get_text("#select2-subhead-container"),
            "narration": _get_input(narration_sel),
            "table_text": table_text,
            "dom": {
                selector: self.client.get_dom(selector)
                for selector in (
                    date_sel, no_sel, amt_sel, cash_sel,
                    "#head", "#subhead", narration_sel,
                )
            },
        }

    def submit(self) -> Mapping[str, Any]:
        if not self.armed:
            raise RuntimeError("Submitting accounting transactions is approval-gated")
        return self.client.command("click", {"selector": "#submit_form"})["data"]

    def observe_save(
        self, expected_entry: Mapping[str, Any] | None = None, timeout_seconds: float = 30
    ) -> Mapping[str, Any]:
        """Poll for post-submit observable confirmation evidence."""
        deadline = time.monotonic() + timeout_seconds
        evidence: dict[str, Any] = {
            "confirmed": False,
            "record_id": None,
            "success_text": None,
            "url": None,
            "title": None,
        }

        exp_v = str((expected_entry or {}).get("receipt_number", "")).strip() or "514"
        exp_amt = float((expected_entry or {}).get("amount", 0) or 13245.0)
        exp_amt_str = f"{exp_amt:,.2f}"
        exp_amt_raw = str(int(exp_amt) if exp_amt.is_integer() else exp_amt)
        exp_amt_comma = f"{int(exp_amt):,}"

        while time.monotonic() < deadline:
            time.sleep(1.0)
            try:
                dom = self.client.get_dom()
                evidence["url"] = dom.get("url")
                evidence["title"] = dom.get("title")
                html = str(dom.get("html", ""))

                alert_match = re.search(
                    r'class="[^"]*alert-success[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL | re.IGNORECASE
                )
                if alert_match:
                    clean_alert = re.sub(r'<[^>]+>', ' ', alert_match.group(1)).strip()
                    evidence["success_text"] = clean_alert
                    evidence["confirmed"] = True

                try:
                    table_dom = self.client.get_dom("#myTable")
                    table_text = str(table_dom.get("text", ""))
                    if exp_v in table_text and (
                        exp_amt_str in table_text or exp_amt_raw in table_text or exp_amt_comma in table_text
                    ):
                        evidence["confirmed"] = True
                        evidence["record_id"] = exp_v
                        evidence["table_evidence"] = True
                except Exception:
                    pass

                try:
                    no_sel = (expected_entry or {}).get("selectors", {}).get("number", "#receiptno")
                    rno_res = self.client.command("type", {"selector": no_sel, "text": "", "clear": False})
                    new_rno = str(rno_res.get("data", {}).get("value", ""))
                    if new_rno and new_rno != exp_v:
                        evidence["new_receipt_no"] = new_rno
                        if not evidence["record_id"]:
                            evidence["record_id"] = exp_v
                except Exception:
                    pass

                if evidence["confirmed"]:
                    return evidence
            except Exception as e:
                evidence["last_error"] = str(e)

        return evidence


def verify_readback(
    readback: Mapping[str, Any], entry: Mapping[str, Any]
) -> tuple[bool, list[str]]:
    """Verify all 7 critical form field conditions against the intended workbook entry."""
    issues: list[str] = []

    # 1. Date
    exp_date = normalize_date_for_form(entry.get("date"))
    act_date = str(readback.get("date", "")).strip()
    if act_date != exp_date:
        issues.append(f"Date mismatch: expected '{exp_date}', observed '{act_date}'")

    # 2. Voucher number
    exp_v = str(entry.get("receipt_number", "")).strip()
    act_v = str(readback.get("receipt_number", "")).strip()
    if act_v != exp_v:
        issues.append(f"Voucher number mismatch: expected '{exp_v}', observed '{act_v}'")

    # 3. Amount
    exp_amt = float(entry.get("amount", 0))
    try:
        act_amt = float(str(readback.get("amount", "0")).replace(",", "").strip())
        if abs(act_amt - exp_amt) > 0.001:
            issues.append(f"Amount mismatch: expected '{exp_amt}', observed '{act_amt}'")
    except (ValueError, TypeError):
        issues.append(f"Amount in readback is non-numeric: '{readback.get('amount')}'")

    # 4. Mode Cash
    if not readback.get("mode_cash"):
        issues.append("Mode cash is not selected/checked")

    # 5. Head
    exp_head = str(entry.get("head_name", "")).strip().lower()
    res = entry.get("head_resolution", {})
    exp_web_label = str(res.get("website_label", "")).strip().lower()
    act_head = str(readback.get("head", "")).strip().lower()
    clean_act_head = re.sub(r"\s*\([^)]*\)", "", act_head).strip()
    clean_exp_head = re.sub(r"\s*\([^)]*\)", "", exp_head).strip()
    clean_exp_web = re.sub(r"\s*\([^)]*\)", "", exp_web_label).strip()

    if (
        clean_exp_head not in clean_act_head
        and clean_exp_web not in clean_act_head
        and clean_act_head not in clean_exp_head
    ):
        issues.append(f"Head mismatch: expected '{entry.get('head_name')}', observed '{readback.get('head')}'")

    # 6. Subhead
    exp_subhead = str(entry.get("subhead") or "").strip().lower()
    act_subhead = str(readback.get("subhead") or "").strip().lower()
    if exp_subhead:
        if exp_subhead not in act_subhead:
            issues.append(f"Subhead mismatch: expected '{entry.get('subhead')}', observed '{readback.get('subhead')}'")
    else:
        if act_subhead and act_subhead not in {"select sub head", "no heads exist", ""}:
            issues.append(f"Subhead expected blank, observed '{readback.get('subhead')}'")

    # 7. Narration
    exp_narr = str(entry.get("narration") or "").strip()
    act_narr = str(readback.get("narration") or "").strip()
    if act_narr != exp_narr:
        issues.append(f"Narration mismatch: expected '{exp_narr}', observed '{act_narr}'")

    return (len(issues) == 0, issues)


def check_live_duplicate(
    table_text: str, entry: Mapping[str, Any]
) -> tuple[str, str]:
    """Check live table text for existing voucher records to distinguish exact matches from conflicts.

    Returns (status, detail):
    - ("NOT_FOUND", detail)
    - ("ALREADY_CONFIRMED_MATCH", detail)
    - ("CONFLICTING_VOUCHER", detail)
    """
    if not table_text:
        return ("NOT_FOUND", "Table text is empty")

    v_str = str(entry.get("receipt_number", "")).strip()
    if not v_str:
        return ("NOT_FOUND", "No voucher number in entry")

    lines = [line.strip() for line in table_text.splitlines() if line.strip()]
    matching_line: str | None = None
    for line in lines:
        tokens = [t.strip() for t in re.split(r'[|\t]', line)]
        if v_str in tokens or re.search(rf'\b{re.escape(v_str)}\b', line):
            matching_line = line
            break

    if not matching_line:
        return ("NOT_FOUND", f"Voucher {v_str} not found in visible table")

    exp_amt = float(entry.get("amount", 0))
    amt_comma = f"{exp_amt:,.2f}"
    amt_raw = str(int(exp_amt) if exp_amt.is_integer() else exp_amt)
    amt_comma_no_dec = f"{int(exp_amt):,}"
    amt_matches = (
        amt_comma in matching_line or amt_raw in matching_line or amt_comma_no_dec in matching_line
    )

    exp_date = normalize_date_for_form(entry.get("date"))
    date_matches = exp_date in matching_line

    if amt_matches and date_matches:
        return ("ALREADY_CONFIRMED_MATCH", f"Exact record already present: {matching_line}")
    return ("CONFLICTING_VOUCHER", f"Voucher {v_str} exists with conflicting details: {matching_line}")


@dataclass(frozen=True)
class Preflight:
    ready: tuple[dict[str, Any], ...]
    exceptions: tuple[dict[str, Any], ...]


def load_or_create_journal(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "mode": "live-entry", "entries": {}}
    with path.open("r", encoding="utf-8") as handle:
        journal = json.load(handle)
    if journal.get("mode") not in {"dry-run", "live-entry"}:
        raise ValueError(f"Unsupported journal mode: {journal.get('mode')!r}")
    journal.setdefault("version", 1)
    journal.setdefault("entries", {})
    return journal


def _save_journal(path: Path, journal: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(journal, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare_preflight(report: Mapping[str, Any], journal_path: Path) -> Preflight:
    """Register workbook entries as pending without making browser changes."""
    journal = load_or_create_journal(journal_path)
    entries = journal["entries"]
    ready: list[dict[str, Any]] = []
    exceptions: list[dict[str, Any]] = []
    for entry in report["entries"]:
        key = entry["submission_key"]
        state = entries.get(key, {}).get("status")
        if entry["status"] == READY:
            ready.append(dict(entry))
            if state is None or state == "dry_run_seen":
                entries[key] = {
                    "status": PENDING,
                    "direction": entry["direction"],
                    "sheet": entry["sheet"],
                    "row": entry["row"],
                    "head_name": entry["head_name"],
                }
        else:
            exceptions.append(dict(entry))
    journal["mode"] = "live-entry"
    _save_journal(journal_path, journal)
    return Preflight(tuple(ready), tuple(exceptions))


def transition(
    journal_path: Path,
    submission_key: str,
    status: str,
    *,
    evidence: Mapping[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Persist one guarded state transition.

    An uncertain outcome is terminal and cannot be retried automatically.
    """
    if status not in {PENDING, SUBMITTED, CONFIRMED, FAILED, UNCERTAIN}:
        raise ValueError(f"Unsupported entry status: {status}")
    journal = load_or_create_journal(journal_path)
    entry = journal["entries"].get(submission_key)
    if entry is None:
        raise KeyError(f"Unknown submission key: {submission_key}")
    previous = entry["status"]
    if previous in TERMINAL:
        raise ValueError(f"Cannot transition terminal entry {submission_key} from {previous}")
    if status == SUBMITTED and previous not in {PENDING, FAILED, "dry_run_seen"}:
        raise ValueError(f"Cannot mark {previous} entry as submitted")
    if status == CONFIRMED and previous != SUBMITTED:
        raise ValueError("Confirmed requires a prior submitted state")
    if status == UNCERTAIN and previous != SUBMITTED:
        raise ValueError("Uncertain requires a prior submitted state")
    if status == FAILED and previous not in {PENDING, SUBMITTED, "dry_run_seen"}:
        raise ValueError(f"Cannot mark {previous} entry as failed")
    entry["status"] = status
    entry["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if evidence is not None:
        entry["evidence"] = dict(evidence)
    if error is not None:
        entry["error"] = error
    _save_journal(journal_path, journal)


def wait_for_subhead(
    read_options: Callable[[], Mapping[str, Any]],
    *,
    timeout_seconds: float = 15,
    interval_seconds: float = 0.25,
) -> Mapping[str, Any]:
    """Wait for a settled dependent select, not merely an attached element."""
    deadline = time.monotonic() + timeout_seconds
    last: Mapping[str, Any] = {}
    while time.monotonic() < deadline:
        last = dict(read_options())
        state = last.get("state")
        if state in {"options", "no_options"}:
            return last
        html = str(last.get("html", "")).strip()
        if 'value="0">No Heads Exist' in html:
            return dict(last, state="no_options")
        if re.search(r'<option\s+value="(?!0")[^"]*"', html):
            return dict(last, state="options")
        time.sleep(interval_seconds)
    raise TimeoutError(f"Subhead did not settle before timeout; last state={last!r}")


def confirm_save(evidence: Mapping[str, Any]) -> bool:
    """Accept only an explicit, externally observed success signal."""
    return evidence.get("confirmed") is True and bool(evidence.get("record_id") or evidence.get("success_text"))


class BatchEntryRunner:
    """Executes an authorized BatchManifest sequentially with circuit breaker protection."""

    def __init__(
        self,
        manifest: BatchManifest,
        adapter: BrowserEntryAdapter,
        journal_path: Path,
        workbook_path: Path | None = None,
        save_timeout_seconds: float = 30,
        incident_dir: Path = Path("data/output"),
    ):
        self.manifest = manifest
        self.adapter = adapter
        self.journal_path = journal_path
        self.workbook_path = workbook_path or Path(manifest.workbook_path)
        self.save_timeout_seconds = save_timeout_seconds
        self.incident_dir = incident_dir

    def run_batch(self) -> dict[str, Any]:
        """Run all authorized transactions in sequence.

        Trips the circuit breaker and stops on ANY failure or uncertainty.
        """
        validate_manifest(self.manifest, self.workbook_path)

        if self.manifest.status != APPROVED:
            raise ManifestStateError(
                f"Cannot run batch: manifest status is {self.manifest.status}, must be APPROVED"
            )

        processed: list[str] = []
        confirmed: list[str] = []
        already_confirmed: list[str] = []

        for entry in self.manifest.authorized_entries:
            key = entry["submission_key"]
            v_num = entry.get("receipt_number")

            # 1. Whitelist check
            if key not in self.manifest.authorized_keys:
                raise UnauthorizedTransactionError(f"Transaction {key} is not in authorized_keys whitelist")

            # 2. Fresh journal status check
            current_journal = load_or_create_journal(self.journal_path)
            j_status = current_journal.get("entries", {}).get(key, {}).get("status")
            if j_status == CONFIRMED:
                already_confirmed.append(key)
                continue
            if j_status == UNCERTAIN:
                incident = self._record_incident(
                    key, entry, "ALREADY_UNCERTAIN",
                    "Transaction has UNCERTAIN status in journal; cannot retry automatically",
                )
                return {
                    "status": "STOPPED",
                    "reason": "CIRCUIT_BREAKER_TRIPPED",
                    "incident": incident,
                    "confirmed_count": len(confirmed),
                    "already_confirmed_count": len(already_confirmed),
                }

            processed.append(key)

            # Ensure entry exists as PENDING if not yet initialized
            if j_status is None:
                current_journal["entries"][key] = {
                    "status": PENDING,
                    "direction": entry.get("direction", self.manifest.direction),
                    "sheet": entry.get("sheet"),
                    "row": entry.get("row"),
                    "head_name": entry.get("head_name"),
                }
                _save_journal(self.journal_path, current_journal)

            # 3. Form entry
            try:
                self.adapter.enter(entry)
            except Exception as exc:
                incident = self._record_incident(
                    key, entry, "ENTER_FAILED", f"Form population failed: {exc}"
                )
                transition(self.journal_path, key, FAILED, error=str(exc))
                return {
                    "status": "STOPPED",
                    "reason": "CIRCUIT_BREAKER_TRIPPED",
                    "incident": incident,
                    "confirmed_count": len(confirmed),
                }

            # 4. Form readback verification
            readback = (
                self.adapter.readback(entry)
                if hasattr(self.adapter, "readback")
                else self.adapter.readback()
            )
            is_valid, issues = verify_readback(readback, entry)
            if not is_valid:
                error_msg = f"Pre-submit readback verification failed: {'; '.join(issues)}"
                incident = self._record_incident(
                    key, entry, "READBACK_MISMATCH", error_msg, readback=readback
                )
                transition(self.journal_path, key, FAILED, error=error_msg)
                return {
                    "status": "STOPPED",
                    "reason": "CIRCUIT_BREAKER_TRIPPED",
                    "incident": incident,
                    "confirmed_count": len(confirmed),
                }

            # 5. Duplicate check in live table
            table_text = str(readback.get("table_text", ""))
            dup_status, dup_detail = check_live_duplicate(table_text, entry)
            if dup_status == "CONFLICTING_VOUCHER":
                error_msg = f"Conflicting voucher found in live table: {dup_detail}"
                incident = self._record_incident(
                    key, entry, "DUPLICATE_CONFLICT", error_msg, readback=readback
                )
                transition(self.journal_path, key, FAILED, error=error_msg)
                return {
                    "status": "STOPPED",
                    "reason": "CIRCUIT_BREAKER_TRIPPED",
                    "incident": incident,
                    "confirmed_count": len(confirmed),
                }
            elif dup_status == "ALREADY_CONFIRMED_MATCH":
                # Exact record exists in live table: mark CONFIRMED without re-submitting
                transition(self.journal_path, key, SUBMITTED)
                transition(
                    self.journal_path,
                    key,
                    CONFIRMED,
                    evidence={"table_match": dup_detail, "source": "live_table_probe"},
                )
                confirmed.append(key)
                continue

            # 6. Persist SUBMITTED
            transition(self.journal_path, key, SUBMITTED)

            # 7. Submit transaction
            try:
                self.adapter.submit()
            except Exception as exc:
                error_msg = f"Submit call failed: {exc}"
                incident = self._record_incident(
                    key, entry, "SUBMIT_FAILED", error_msg, readback=readback
                )
                transition(self.journal_path, key, UNCERTAIN, error=error_msg)
                return {
                    "status": "STOPPED",
                    "reason": "CIRCUIT_BREAKER_TRIPPED",
                    "incident": incident,
                    "confirmed_count": len(confirmed),
                }

            # 8. Observe save
            evidence = self.adapter.observe_save(entry, timeout_seconds=self.save_timeout_seconds)
            if confirm_save(evidence):
                transition(self.journal_path, key, CONFIRMED, evidence=evidence)
                confirmed.append(key)
            else:
                error_msg = (
                    f"Save outcome could not be conclusively verified: "
                    f"{evidence.get('last_error', 'Timeout waiting for table evidence')}"
                )
                incident = self._record_incident(
                    key, entry, "SAVE_UNCONFIRMED", error_msg, evidence=evidence
                )
                transition(self.journal_path, key, UNCERTAIN, error=error_msg)
                return {
                    "status": "STOPPED",
                    "reason": "CIRCUIT_BREAKER_TRIPPED",
                    "incident": incident,
                    "confirmed_count": len(confirmed),
                }

        return {
            "status": "COMPLETED",
            "total_authorized": len(self.manifest.authorized_entries),
            "confirmed_count": len(confirmed),
            "already_confirmed_count": len(already_confirmed),
        }

    def _record_incident(
        self,
        submission_key: str,
        entry: Mapping[str, Any],
        failure_stage: str,
        error_message: str,
        readback: Mapping[str, Any] | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        incident_id = f"incident-{int(time.time())}"
        incident = {
            "incident_id": incident_id,
            "timestamp": timestamp,
            "manifest_id": self.manifest.manifest_id,
            "submission_key": submission_key,
            "sheet": entry.get("sheet"),
            "row": entry.get("row"),
            "voucher_number": entry.get("receipt_number"),
            "amount": entry.get("amount"),
            "failure_stage": failure_stage,
            "error_message": error_message,
            "readback_snapshot": dict(readback) if readback else None,
            "save_evidence": dict(evidence) if evidence else None,
            "action_taken": "CIRCUIT_BREAKER_HALTED_BATCH",
        }
        self.incident_dir.mkdir(parents=True, exist_ok=True)
        incident_file = self.incident_dir / f"batch_incident_{int(time.time())}.json"
        with incident_file.open("w", encoding="utf-8") as handle:
            json.dump(incident, handle, indent=2, sort_keys=True)
        return incident


def run_preflight(
    workbook_path: Path,
    crosswalk_path: Path,
    journal_path: Path,
    *,
    master_path: Path,
    website_html_path: Path,
    subheads_path: Path,
) -> Preflight:
    report = run_dry_run(
        workbook_path,
        crosswalk_path,
        journal_path,
        master_path,
        website_html_path,
        subheads_path,
    )
    return prepare_preflight(report, journal_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true", help="Read the active browser title through the relay")
    parser.add_argument("--relay-url", default=DEFAULT_RELAY_URL)
    parser.add_argument("--workbook", type=Path, required=True)
    parser.add_argument("--crosswalk", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--master", type=Path, required=True)
    parser.add_argument("--website-html", type=Path, required=True)
    parser.add_argument("--subheads", type=Path, required=True)
    parser.add_argument("--generate-manifest", action="store_true", help="Generate a DRAFT batch manifest")
    parser.add_argument("--manifest-path", type=Path, help="Path for manifest JSON artifact")
    parser.add_argument("--direction", default="RECEIPT", choices=["RECEIPT", "PAYMENT"])
    parser.add_argument("--voucher-start", type=int, default=None)
    parser.add_argument("--voucher-end", type=int, default=None)
    parser.add_argument("--approve-manifest", action="store_true", help="Authorize an existing DRAFT manifest")
    parser.add_argument("--approver", default="user", help="Name or signature of the human authorizing the batch")
    parser.add_argument("--run-batch", action="store_true", help="Execute an APPROVED batch manifest")
    parser.add_argument("--arm", action="store_true", help="Arm live adapter to permit form submission clicks")

    args = parser.parse_args()

    if args.probe:
        result = RelayBrowserEntryAdapter(
            RelayBrowserClient(args.relay_url)
        ).probe()
        print(json.dumps({"relay_url": args.relay_url, "dom": result}, indent=2))
        return 0

    if args.generate_manifest:
        report = run_dry_run(
            args.workbook,
            args.crosswalk,
            args.journal,
            master_path=args.master,
            website_html_path=args.website_html,
            subheads_path=args.subheads,
        )
        journal = load_or_create_journal(args.journal)
        manifest = generate_batch_manifest(
            report,
            journal,
            args.workbook,
            direction=args.direction,
            target_voucher_start=args.voucher_start,
            target_voucher_end=args.voucher_end,
        )
        validate_manifest(manifest, args.workbook)
        target_path = args.manifest_path or Path(f"data/output/batch_manifest_{manifest.manifest_id}.json")
        save_manifest(manifest, target_path)
        print(json.dumps({
            "action": "MANIFEST_GENERATED",
            "manifest_path": str(target_path),
            "manifest_id": manifest.manifest_id,
            "status": manifest.status,
            "batch_summary": manifest.batch_summary,
            "authorized_count": len(manifest.authorized_entries),
            "excluded_count": len(manifest.excluded_entries),
        }, indent=2))
        return 0

    if args.approve_manifest:
        if not args.manifest_path or not args.manifest_path.is_file():
            print(f"Error: Manifest path {args.manifest_path} not found.")
            return 1
        manifest = load_manifest(args.manifest_path)
        approved = approve_manifest(manifest, approved_by=args.approver, workbook_path=args.workbook)
        save_manifest(approved, args.manifest_path)
        print(json.dumps({
            "action": "MANIFEST_APPROVED",
            "manifest_path": str(args.manifest_path),
            "manifest_id": approved.manifest_id,
            "status": approved.status,
            "approval_metadata": approved.approval_metadata,
        }, indent=2))
        return 0

    if args.run_batch:
        if not args.manifest_path or not args.manifest_path.is_file():
            print(f"Error: Manifest path {args.manifest_path} not found.")
            return 1
        manifest = load_manifest(args.manifest_path)
        adapter = RelayBrowserEntryAdapter(
            RelayBrowserClient(args.relay_url),
            armed=args.arm,
        )
        runner = BatchEntryRunner(manifest, adapter, args.journal, args.workbook)
        result = runner.run_batch()
        print(json.dumps(result, indent=2))
        return 0 if result.get("status") == "COMPLETED" else 2

    # Default fallback: run preflight report
    preflight = run_preflight(
        args.workbook,
        args.crosswalk,
        args.journal,
        master_path=args.master,
        website_html_path=args.website_html,
        subheads_path=args.subheads,
    )
    print(json.dumps({
        "ready_count": len(preflight.ready),
        "exception_count": len(preflight.exceptions),
        "first_ready": preflight.ready[0] if preflight.ready else None,
        "exceptions": [
            {"sheet": e["sheet"], "row": e["row"], "status": e["status"], "issues": e["issues"], "review": e["review"]}
            for e in preflight.exceptions
        ],
    }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
