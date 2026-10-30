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
from .edayan_dry_run import SUBHEAD_ALIASES, resolve_subhead_option, run_dry_run

DEFAULT_RELAY_URL = "ws://localhost:11800"

READY = "READY"
PENDING = "pending"
SUBMITTED = "submitted"
CONFIRMED = "confirmed"
FAILED = "failed"
UNCERTAIN = "uncertain"
TERMINAL = {CONFIRMED, UNCERTAIN}
ACTIVE = {PENDING, SUBMITTED, FAILED}


class ReconciliationError(Exception):
    """Raised when an uncertain journal entry cannot be safely reconciled."""


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
                try:
                    message = json.loads(await asyncio.wait_for(
                        socket.recv(), timeout=max(0.1, deadline - time.monotonic())
                    ))
                except (asyncio.TimeoutError, TimeoutError):
                    raise TimeoutError(
                        f"Browser command {command!r} timed out after {self.timeout_seconds}s (deadline exceeded)"
                    )
                if message.get("type") == "error":
                    raise RuntimeError(f"Relay error: {message.get('error')}")
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

    def command(self, command: str, params: Mapping[str, Any] | None = None, retries: int = 2) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                return asyncio.run(self._command_async(command, params or {}))
            except (TimeoutError, asyncio.TimeoutError, Exception) as exc:
                last_exc = exc
                if attempt < retries:
                    time.sleep(1.0)
                    continue
                raise last_exc

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


def extract_table_text(text: str, html: str = "") -> str:
    """Extract table rows as pipe-delimited lines from HTML if available, or fallback to text."""
    if html and ("<tr" in html.lower() or "<td" in html.lower()):
        rows: list[str] = []
        for r in re.findall(r'<tr\b[^>]*>(.*?)</tr>', html, re.DOTALL | re.IGNORECASE):
            cells = [
                re.sub(r'<[^>]+>', ' ', c).strip()
                for c in re.findall(r'<t[dh]\b[^>]*>(.*?)</t[dh]>', r, re.DOTALL | re.IGNORECASE)
            ]
            if cells:
                rows.append(" | ".join(cells))
        if rows:
            return "\n".join(rows)
    return text


def verify_table_row_evidence(
    table_text: str, entry: Mapping[str, Any], table_html: str = ""
) -> tuple[bool, str | None, dict[str, Any]]:
    """Verify all 5 critical transaction fields (voucher, date, amount, head, narration) in live table text.

    Returns: (is_confirmed, mismatch_reason, details)
    """
    if table_html:
        table_text = extract_table_text(table_text, table_html)
    if not table_text:
        return False, "Table text is empty", {}

    exp_v = str(entry.get("receipt_number", "")).strip()
    if not exp_v:
        return False, "No voucher number in entry", {}

    exp_date = normalize_date_for_form(entry.get("date"))
    exp_amt = float(entry.get("amount", 0))
    amt_comma = f"{exp_amt:,.2f}"
    amt_raw = str(int(exp_amt) if exp_amt.is_integer() else exp_amt)
    amt_comma_no_dec = f"{int(exp_amt):,}"

    clean_head = re.sub(r'[\(\)\-\/\s]+', ' ', str(entry.get("head_name", ""))).strip().lower()
    res = entry.get("head_resolution", {})
    clean_web = re.sub(r'[\(\)\-\/\s]+', ' ', str(res.get("website_label", ""))).strip().lower()
    clean_web = clean_web.replace("&amp;", "&")
    exp_narr = str(entry.get("narration", "")).strip().lower()

    lines = [line.strip() for line in table_text.splitlines() if line.strip()]

    matching_line: str | None = None
    head_cell: str | None = None
    narr_cell: str | None = None

    for line in lines:
        tokens = [t.strip() for t in re.split(r'[|\t]', line)]
        if not tokens or tokens[0] in {"No", "-->"} or not any(tokens):
            continue
        for idx in range(1, len(tokens)):
            matched_v = (tokens[idx] == exp_v)
            if not matched_v and exp_v.isdigit() and tokens[idx].isdigit():
                try:
                    matched_v = (int(tokens[idx]) == int(exp_v))
                except (ValueError, TypeError):
                    pass
            if matched_v:
                matching_line = line
                if idx > 1:
                    head_cell = tokens[idx - 1]
                if idx + 2 < len(tokens):
                    narr_cell = tokens[idx + 2]
                elif idx + 1 < len(tokens):
                    narr_cell = tokens[idx + 1]
                break
        if matching_line:
            break

    if not matching_line:
        return False, f"Voucher {exp_v} not found in visible table", {}

    line_lower = matching_line.lower()

    # 1. Date check
    date_matches = exp_date in matching_line

    # 2. Amount check
    amt_matches = (
        amt_comma in matching_line or amt_raw in matching_line or amt_comma_no_dec in matching_line
    )

    # 3. Head check: compare against isolated head cell if available, else line_lower
    head_matches = True
    head_text = head_cell.lower() if head_cell is not None else line_lower
    all_head = f"{clean_head} {clean_web}".strip()
    if all_head:
        significant_words = [
            w for w in all_head.split()
            if len(w) > 3 and w not in {"credit", "debit", "account", "from", "the"}
        ]
        if significant_words:
            head_matches = any(w in head_text for w in significant_words)
        else:
            head_matches = clean_head in head_text or (clean_web and clean_web in head_text)

    # 4. Narration check: compare against isolated narration cell if available, else line_lower
    narr_matches = True
    narr_text = narr_cell.lower() if narr_cell is not None else line_lower
    if exp_narr:
        narr_words = [w for w in exp_narr.split() if len(w) > 3]
        if narr_words:
            narr_matches = any(w in narr_text for w in narr_words)
        else:
            narr_matches = exp_narr in narr_text

    details = {
        "voucher": exp_v,
        "date_match": date_matches,
        "amount_match": amt_matches,
        "head_match": head_matches,
        "narration_match": narr_matches,
        "matching_row": matching_line,
    }

    if date_matches and amt_matches and head_matches and narr_matches:
        return True, None, details

    mismatches = []
    if not date_matches:
        mismatches.append(f"date expected '{exp_date}'")
    if not amt_matches:
        mismatches.append(f"amount expected '{amt_comma}'")
    if not head_matches:
        expected_head_repr = f"'{clean_head}'" + (f" or '{clean_web}'" if clean_web else "")
        mismatches.append(f"head expected {expected_head_repr}")
    if not narr_matches:
        mismatches.append(f"narration expected '{exp_narr}'")

    return False, f"Table row mismatch for voucher {exp_v}: {'; '.join(mismatches)} in row: {matching_line!r}", details


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
        try:
            curr_date_res = self.client.command("type", {"selector": date_sel, "text": "", "clear": False})
            curr_date = str(curr_date_res.get("data", {}).get("value", "")).strip()
        except Exception:
            curr_date = ""
        if curr_date != date_value:
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

        # 5. Subhead: If the entry requires a subhead, wait until dependent control completes its update and stabilizes
        subhead_name = entry.get("subhead")
        if subhead_name:
            observed = wait_for_subhead(
                lambda: self.client.get_dom(subhead_sel),
                timeout_seconds=20,
                interval_seconds=0.5,
                stability_checks=2,
            )
            if observed.get("state") == "no_options":
                raise RuntimeError(
                    f"Entry requires Subhead {subhead_name!r}, but website settled with 'No Heads Exist' for head {entry.get('head_name')!r}"
                )
            html = str(observed.get("html", ""))
            options = {
                val: label.strip()
                for val, label in re.findall(r'<option\s+value="([^"]+)"[^>]*>(.*?)</option>', html, re.I | re.S)
                if val != "0"
            }
            resolved_sub = resolve_subhead_option(subhead_name, options)
            if not resolved_sub:
                raise RuntimeError(
                    f"Expected live Subhead option {subhead_name!r} was not observed in settled subhead options: {options}"
                )
            subhead_val, _ = resolved_sub
            self.client.select_option(subhead_sel, subhead_val)
        else:
            time.sleep(0.5)

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

        def _get_table_text() -> str:
            for sel in ("#myTable", "#showtbook"):
                try:
                    dom = self.client.get_dom(sel)
                    text = str(dom.get("text", "")).strip()
                    html = str(dom.get("html", "")).strip()
                    extracted = extract_table_text(text, html)
                    if extracted:
                        return extracted
                except Exception:
                    continue
            return ""

        table_text = _get_table_text()

        return {
            "date": _get_input(date_sel),
            "receipt_number": _get_input(no_sel),
            "amount": _get_input(amt_sel),
            "mode_cash": "checked" in self.client.get_dom(cash_sel).get("html", ""),
            "head": _get_text("#select2-head-container"),
            "subhead": _get_text("#select2-subhead-container"),
            "narration": _get_input(narration_sel),
            "table_text": table_text,
            "dom": {},
        }

    def submit(self) -> Mapping[str, Any]:
        if not self.armed:
            raise RuntimeError("Submitting accounting transactions is approval-gated")
        return self.client.command("click", {"selector": "#submit_form"})["data"]

    def observe_save(
        self, expected_entry: Mapping[str, Any] | None = None, timeout_seconds: float = 90.0
    ) -> Mapping[str, Any]:
        """Poll for post-submit observable confirmation evidence, resilient to navigation and disconnects."""
        deadline = time.monotonic() + timeout_seconds
        evidence: dict[str, Any] = {
            "confirmed": False,
            "record_id": None,
            "success_text": None,
            "url": None,
            "title": None,
            "table_evidence": False,
            "reconnections": 0,
            "transient_errors": [],
            "outcome_classification": "UNRESOLVED_UNCERTAIN",
        }

        entry = dict(expected_entry or {})
        exp_v = str(entry.get("receipt_number", "")).strip() or "514"

        while time.monotonic() < deadline:
            time.sleep(1.0)
            try:
                dom = self.client.get_dom()
                evidence["url"] = dom.get("url")
                evidence["title"] = dom.get("title")
                html = str(dom.get("html", ""))

                # 1. Check for explicit server rejection / error alerts
                danger_match = re.search(
                    r'class="[^"]*alert-(?:danger|error)[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL | re.IGNORECASE
                )
                if danger_match:
                    clean_err = re.sub(r'<[^>]+>', ' ', danger_match.group(1)).strip()
                    evidence["error_text"] = clean_err
                    evidence["outcome_classification"] = "CONFIRMED_SERVER_REJECTION"
                    return evidence

                # 2. Check for explicit success alert (informational only; never confirms on its own)
                alert_match = re.search(
                    r'class="[^"]*alert-success[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL | re.IGNORECASE
                )
                if alert_match:
                    clean_alert = re.sub(r'<[^>]+>', ' ', alert_match.group(1)).strip()
                    evidence["success_text"] = clean_alert

                # 3. Check #myTable for complete 5-point match
                try:
                    table_dom = self.client.get_dom("#myTable")
                    table_text = extract_table_text(
                        str(table_dom.get("text", "")),
                        str(table_dom.get("html", "")),
                    )
                    is_valid, mismatch_reason, details = verify_table_row_evidence(table_text, entry)
                    evidence["verification_details"] = details
                    if is_valid:
                        evidence["confirmed"] = True
                        evidence["record_id"] = exp_v
                        evidence["table_evidence"] = True
                        evidence["table_entry"] = details.get("matching_row")
                        evidence["outcome_classification"] = "CONFIRMED_PERSISTENCE"
                    elif mismatch_reason and f"Table row mismatch for voucher {exp_v}" in mismatch_reason:
                        evidence["confirmed"] = False
                        evidence["contradictory_evidence"] = mismatch_reason
                        evidence["outcome_classification"] = "CONTRADICTORY_EVIDENCE"
                        return evidence
                except Exception as table_err:
                    evidence["last_table_error"] = str(table_err)

                # 4. Check form reset / next receipt number
                try:
                    no_sel = entry.get("selectors", {}).get("number", "#receiptno")
                    rno_res = self.client.command("type", {"selector": no_sel, "text": "", "clear": False})
                    new_rno = str(rno_res.get("data", {}).get("value", ""))
                    if new_rno and new_rno != exp_v:
                        evidence["new_receipt_no"] = new_rno
                        if not evidence["record_id"]:
                            evidence["record_id"] = exp_v
                except Exception:
                    pass

                if evidence["confirmed"] and evidence.get("outcome_classification") == "CONFIRMED_PERSISTENCE":
                    return evidence

            except Exception as exc:
                # Transient navigation or relay disconnect error: keep polling until deadline
                evidence["reconnections"] += 1
                evidence["transient_errors"].append(str(exc))
                evidence["last_error"] = str(exc)

        # Deadline reached without confirmed save
        if not evidence["confirmed"]:
            if evidence.get("outcome_classification") not in {
                "CONFIRMED_SERVER_REJECTION", "CONTRADICTORY_EVIDENCE"
            }:
                evidence["outcome_classification"] = "UNRESOLVED_UNCERTAIN"

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
    v_match = (act_v == exp_v)
    if not v_match and exp_v.isdigit() and act_v.isdigit():
        try:
            v_match = (int(exp_v) == int(act_v))
        except (ValueError, TypeError):
            pass
    if not v_match:
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
        is_subhead_match = (
            exp_subhead in act_subhead
            or act_subhead in exp_subhead
            or SUBHEAD_ALIASES.get(exp_subhead) == act_subhead
            or SUBHEAD_ALIASES.get(act_subhead) == exp_subhead
        )
        if not is_subhead_match:
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
        if not tokens or tokens[0] in {"No", "-->"} or not any(tokens):
            continue
        voucher_tokens = tokens[1:] if len(tokens) > 1 else tokens
        v_matched = (v_str in voucher_tokens)
        if not v_matched and v_str.isdigit():
            for t in voucher_tokens:
                if t.isdigit():
                    try:
                        if int(t) == int(v_str):
                            v_matched = True
                            break
                    except (ValueError, TypeError):
                        pass
        if v_matched:
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
    if status == FAILED and previous not in {PENDING, SUBMITTED, FAILED, "dry_run_seen"}:
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
    interval_seconds: float = 0.5,
    stability_checks: int = 1,
) -> Mapping[str, Any]:
    """Wait for a settled dependent select, not merely an attached element."""
    deadline = time.monotonic() + timeout_seconds
    last: Mapping[str, Any] = {}
    stable_count = 0
    candidate_state: str | None = None

    while time.monotonic() < deadline:
        try:
            last = dict(read_options())
        except Exception:
            time.sleep(interval_seconds)
            continue
        state = last.get("state")
        html = str(last.get("html", "")).strip()
        text = str(last.get("text", "")).strip()

        detected_state: str | None = None
        if state in {"options", "no_options"}:
            detected_state = state
        elif 'value="0">No Heads Exist' in html or text == "No Heads Exist" or "No Heads Exist" in text:
            detected_state = "no_options"
        elif re.search(r'<option\s+value="(?!0")[^"]*"', html):
            detected_state = "options"

        if detected_state:
            if detected_state == candidate_state:
                stable_count += 1
            else:
                candidate_state = detected_state
                stable_count = 1

            if stable_count >= stability_checks:
                return dict(last, state=detected_state)
        else:
            candidate_state = None
            stable_count = 0

        time.sleep(interval_seconds)

    raise TimeoutError(
        f"Dependent subhead control did not settle within {timeout_seconds}s; last state={last!r}"
    )


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
        save_timeout_seconds: float = 90.0,
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

        # Ensure fresh clean start on target URL
        target_url = (
            "https://account.edayan.org/transactions"
            if self.manifest.direction == "RECEIPT"
            else "https://account.edayan.org/transactions/payment"
        )
        if hasattr(self.adapter, "client") and hasattr(self.adapter.client, "navigate"):
            try:
                self.adapter.client.navigate(target_url)
                time.sleep(1.0)
            except Exception:
                pass

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
                readback = self.adapter.enter(entry)
                if not readback:
                    readback = (
                        self.adapter.readback(entry)
                        if hasattr(self.adapter, "readback")
                        else self.adapter.readback()
                    )
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
                classification = evidence.get("outcome_classification", "UNRESOLVED_UNCERTAIN")
                if classification == "CONFIRMED_SERVER_REJECTION":
                    error_msg = f"Server rejected submission: {evidence.get('error_text', 'Unknown server rejection')}"
                    failure_stage = "SERVER_REJECTED"
                    target_status = FAILED
                elif classification == "CONTRADICTORY_EVIDENCE":
                    error_msg = f"Contradictory table evidence observed: {evidence.get('contradictory_evidence')}"
                    failure_stage = "CONTRADICTORY_TABLE_EVIDENCE"
                    target_status = UNCERTAIN
                else:
                    error_msg = (
                        f"Save outcome could not be conclusively verified ({classification}): "
                        f"{evidence.get('last_error', 'Timeout waiting for table evidence')}"
                    )
                    failure_stage = "SAVE_UNCONFIRMED"
                    target_status = UNCERTAIN

                incident = self._record_incident(
                    key, entry, failure_stage, error_msg, evidence=evidence
                )
                transition(self.journal_path, key, target_status, error=error_msg)
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


def _find_incident_file(incident_dir: Path, incident_id: str) -> tuple[Path | None, dict[str, Any] | None]:
    """Locate an incident file by exact filename or by scanning incident JSON files."""
    candidates = [
        incident_dir / f"batch_{incident_id}.json",
        incident_dir / f"{incident_id}.json",
    ]
    for c in candidates:
        if c.is_file():
            try:
                with c.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if data.get("incident_id") == incident_id:
                    return c, data
            except Exception:
                pass

    if incident_dir.is_dir():
        for f in incident_dir.glob("batch_incident_*.json"):
            try:
                with f.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if data.get("incident_id") == incident_id:
                    return f, data
            except Exception:
                pass

    return None, None


def reconcile_uncertain_entry(
    journal_path: Path,
    submission_key: str,
    *,
    incident_id: str,
    authorizer: str,
    decision: str,
    evidence: Mapping[str, Any],
    incident_dir: Path = Path("data/output"),
    reconciliation_dir: Path = Path("data/output"),
    notes: str = "",
) -> dict[str, Any]:
    """Audit-reconcile an UNCERTAIN journal entry with explicit human authorization and evidence.

    Transitions the entry only if all validation passes:
    - Incident exists on disk and matches submission_key and voucher.
    - Journal entry exists and is currently in UNCERTAIN state.
    - Explicit human authorizer and valid decision ('PERMIT_RETRY', 'MARK_CONFIRMED', 'DO_NOT_RETRY').
    - Non-empty evidence mapping is provided.
    - Writes journal update atomically and saves companion reconciliation artifact.
    """
    if not authorizer or not str(authorizer).strip():
        raise ReconciliationError("Explicit human authorizer name is required for reconciliation")

    valid_decisions = {"PERMIT_RETRY", "MARK_CONFIRMED", "DO_NOT_RETRY"}
    if decision not in valid_decisions:
        raise ReconciliationError(f"Unsupported decision '{decision}'; must be one of {valid_decisions}")

    if not evidence or not isinstance(evidence, Mapping) or len(evidence) == 0:
        raise ReconciliationError("Non-empty evidence mapping is required for reconciliation")

    # 1. Verify incident exists on disk and matches target key
    incident_path, incident_data = _find_incident_file(incident_dir, incident_id)
    if not incident_path or not incident_data:
        raise ReconciliationError(f"Incident '{incident_id}' not found in {incident_dir}")

    inc_key = incident_data.get("submission_key")
    if inc_key != submission_key:
        raise ReconciliationError(
            f"Incident '{incident_id}' belongs to key '{inc_key}', which does not match requested '{submission_key}'"
        )

    inc_voucher = str(incident_data.get("voucher_number", "")).strip()

    # 2. Verify journal entry exists and is currently in UNCERTAIN status
    journal = load_or_create_journal(journal_path)
    entry = journal.get("entries", {}).get(submission_key)
    if not entry:
        raise ReconciliationError(f"Submission key '{submission_key}' not found in journal")

    current_status = entry.get("status")
    if current_status != UNCERTAIN:
        raise ReconciliationError(
            f"Cannot reconcile entry '{submission_key}' with status '{current_status}'; status must be '{UNCERTAIN}'"
        )

    entry_voucher = str(entry.get("voucher_number") or entry.get("receipt_number") or inc_voucher).strip()
    if inc_voucher and entry_voucher and inc_voucher != entry_voucher:
        raise ReconciliationError(
            f"Voucher number mismatch: incident has '{inc_voucher}', journal has '{entry_voucher}'"
        )

    # 3. Build audit reconciliation record preserving prior history
    now_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    reconciliation_id = f"reconciliation-{incident_id}-{int(time.time())}"

    prior_uncertainty = {
        "status": UNCERTAIN,
        "incident_id": incident_id,
        "error": entry.get("error"),
        "timestamp": entry.get("updated_at") or incident_data.get("timestamp"),
    }
    history = list(entry.get("uncertainty_history", []))
    history.append(prior_uncertainty)

    reconciliation_record = {
        "reconciliation_id": reconciliation_id,
        "incident_id": incident_id,
        "submission_key": submission_key,
        "sheet": entry.get("sheet", incident_data.get("sheet")),
        "row": entry.get("row", incident_data.get("row")),
        "voucher_number": entry_voucher,
        "authorizer": str(authorizer).strip(),
        "decision": decision,
        "prior_status": UNCERTAIN,
        "decision_timestamp": now_utc,
        "evidence": dict(evidence),
        "notes": notes,
    }

    # 4. Determine target status
    if decision == "PERMIT_RETRY":
        target_status = PENDING
        entry.pop("error", None)
    elif decision == "MARK_CONFIRMED":
        target_status = CONFIRMED
        entry["evidence"] = dict(evidence)
    else:  # DO_NOT_RETRY
        target_status = FAILED
        entry["error"] = f"Reconciled as DO_NOT_RETRY by {authorizer}: {notes}"

    entry["status"] = target_status
    entry["prior_status"] = UNCERTAIN
    entry["updated_at"] = now_utc
    entry["uncertainty_history"] = history
    entry["reconciliation"] = reconciliation_record

    # 5. Persist atomically to journal
    _save_journal(journal_path, journal)

    # 6. Save companion artifact
    reconciliation_dir.mkdir(parents=True, exist_ok=True)
    companion_path = reconciliation_dir / f"{reconciliation_id}.json"
    with companion_path.open("w", encoding="utf-8") as handle:
        json.dump(reconciliation_record, handle, indent=2, sort_keys=True)

    return {
        "status": "RECONCILED",
        "reconciliation_id": reconciliation_id,
        "submission_key": submission_key,
        "decision": decision,
        "target_status": target_status,
        "artifact_path": str(companion_path),
        "reconciliation_record": reconciliation_record,
    }


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
    parser.add_argument("--workbook", type=Path, default=None)
    parser.add_argument("--crosswalk", type=Path, default=None)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--master", type=Path, default=None)
    parser.add_argument("--website-html", type=Path, default=None)
    parser.add_argument("--subheads", type=Path, default=None)
    parser.add_argument("--generate-manifest", action="store_true", help="Generate a DRAFT batch manifest")
    parser.add_argument("--manifest-path", type=Path, help="Path for manifest JSON artifact")
    parser.add_argument("--direction", default="RECEIPT", choices=["RECEIPT", "PAYMENT"])
    parser.add_argument("--voucher-start", type=int, default=None)
    parser.add_argument("--voucher-end", type=int, default=None)
    parser.add_argument("--approve-manifest", action="store_true", help="Authorize an existing DRAFT manifest")
    parser.add_argument("--approver", default="user", help="Name or signature of the human authorizing the batch")
    parser.add_argument("--run-batch", action="store_true", help="Execute an APPROVED batch manifest")
    parser.add_argument("--arm", action="store_true", help="Arm live adapter to permit form submission clicks")
    parser.add_argument("--save-timeout", type=float, default=90.0, help="Overall timeout in seconds for save observation")
    parser.add_argument("--reconcile-uncertain", action="store_true", help="Audit-reconcile an UNCERTAIN journal entry")
    parser.add_argument("--submission-key", type=str, default=None, help="Submission key of uncertain entry to reconcile")
    parser.add_argument("--incident-id", type=str, default=None, help="Incident ID associated with the uncertain entry")
    parser.add_argument("--authorizer", type=str, default=None, help="Explicit human authorizer name for reconciliation")
    parser.add_argument(
        "--reconciliation-decision",
        type=str,
        choices=["PERMIT_RETRY", "MARK_CONFIRMED", "DO_NOT_RETRY"],
        default="PERMIT_RETRY",
        help="Reconciliation decision",
    )
    parser.add_argument("--evidence-json", type=str, default=None, help="JSON string or file path containing verified evidence")
    parser.add_argument("--reconciliation-notes", type=str, default="", help="Audit notes for reconciliation")

    args = parser.parse_args()

    if args.reconcile_uncertain:
        if not args.submission_key or not args.incident_id:
            print("Error: --submission-key and --incident-id are required for reconciliation.")
            return 1
        if not args.authorizer or not str(args.authorizer).strip():
            print("Error: Explicit human authorizer name (--authorizer) is required for reconciliation.")
            return 1
        evidence_dict = {}
        if args.evidence_json:
            ev_path = Path(args.evidence_json)
            if ev_path.is_file():
                with ev_path.open("r", encoding="utf-8") as handle:
                    evidence_dict = json.load(handle)
            else:
                try:
                    evidence_dict = json.loads(args.evidence_json)
                except Exception as exc:
                    print(f"Error parsing --evidence-json: {exc}")
                    return 1
        result = reconcile_uncertain_entry(
            journal_path=args.journal,
            submission_key=args.submission_key,
            incident_id=args.incident_id,
            authorizer=str(args.authorizer).strip(),
            decision=args.reconciliation_decision,
            evidence=evidence_dict,
            notes=args.reconciliation_notes,
        )
        print(json.dumps(result, indent=2))
        return 0

    if args.probe:
        result = RelayBrowserEntryAdapter(
            RelayBrowserClient(args.relay_url)
        ).probe()
        print(json.dumps({"relay_url": args.relay_url, "dom": result}, indent=2))
        return 0

    if args.generate_manifest:
        if not args.workbook or not args.crosswalk or not args.master or not args.website_html or not args.subheads:
            print("Error: --workbook, --crosswalk, --master, --website-html, and --subheads are required for manifest generation.")
            return 1
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
        client_timeout = 45.0
        adapter = RelayBrowserEntryAdapter(
            RelayBrowserClient(args.relay_url, timeout_seconds=client_timeout),
            armed=args.arm,
        )
        runner = BatchEntryRunner(
            manifest,
            adapter,
            args.journal,
            args.workbook,
            save_timeout_seconds=args.save_timeout,
        )
        result = runner.run_batch()
        print(json.dumps(result, indent=2))
        return 0 if result.get("status") == "COMPLETED" else 2

    # Default fallback: run preflight report
    if not args.workbook or not args.crosswalk or not args.master or not args.website_html or not args.subheads:
        print("Error: --workbook, --crosswalk, --master, --website-html, and --subheads are required for preflight.")
        return 1
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
