"""Read-only validation for the approved e-Dayan workbook."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import openpyxl

REQUIRED_COLUMNS = ("Date", "Receipt Number", "Amount", "Head Name", "Subhead", "Narration")
SHEETS = {"Receipts": "RECEIPT", "Payments": "PAYMENT"}
DEFAULT_MASTER = Path("data/reference/active/accounting_heads_general_verified.csv")
DEFAULT_WEBSITE_HTML = Path("data/reference/website_heads.html")
DEFAULT_SUBHEADS = Path("data/reference/website_subheads_observed.json")

# This is deliberately narrow: it records the one live observation made during
# reconnaissance, rather than guessing that every debit head has no subheads.
def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _key(value: Any) -> str:
    value = re.sub(r"\(\s*(?:credit|debit|c|d)\s*\)", "", _text(value), flags=re.I)
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _json_value(value: Any) -> Any:
    return value.isoformat() if isinstance(value, (datetime, date)) else value


def _load_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _load_website_options(path: Path) -> list[dict[str, str]]:
    source = html.unescape(path.read_text(encoding="utf-8"))
    options = []
    for value, label in re.findall(
        r'<option\s+value="([^"]+)"[^>]*>(.*?)</option>', source, re.I | re.S
    ):
        clean_label = re.sub(r"<.*?>", "", label).strip()
        direction = (
            "RECEIPT" if re.search(r"\(\s*credit\s*\)", clean_label, re.I) else
            "PAYMENT" if re.search(r"\(\s*debit\s*\)", clean_label, re.I) else ""
        )
        options.append({"value": value, "label": clean_label, "direction": direction})
    return options


def _master_candidates(name: str, direction: str, master: list[dict[str, str]]) -> list[dict[str, str]]:
    # The master has no transaction direction column. Direction is supplied by
    # the workbook sheet and is checked against the website/crosswalk below.
    del direction
    exact = [row for row in master if _key(row.get("human_final_name")) == _key(name)]
    if exact:
        return exact
    if _key(name) == "allowances":
        return [row for row in master if _key(row.get("human_final_name", "")).startswith("allowances")]
    return []


def _resolve_head(
    name: str,
    direction: str,
    master: list[dict[str, str]],
    crosswalk: list[dict[str, str]],
    website_options: list[dict[str, str]],
) -> dict[str, Any]:
    master_matches = _master_candidates(name, direction, master)
    master_numbers = {row.get("accounting_head_number") for row in master_matches}

    crosswalk_matches = [
        row for row in crosswalk
        if row.get("transaction_type", "").upper() == direction
        and row.get("website_head_value")
        and row.get("accounting_head_number") in master_numbers
    ]
    crosswalk_targets = {
        (row["website_head_value"], row["website_head_label_exact"])
        for row in crosswalk_matches
    }
    if len(crosswalk_targets) == 1:
        row = crosswalk_matches[0]
        if len(crosswalk_matches) > 1 and _key(name) != "allowances":
            source = "verified_crosswalk_common_target"
        else:
            source = "verified_crosswalk"
        row = crosswalk_matches[0]
        return {
            "status": "resolved",
            "source": source,
            "master_matches": master_matches,
            "website_value": row["website_head_value"],
            "website_label": row["website_head_label_exact"],
            "confidence": row.get("mapping_confidence"),
        }
    if len(crosswalk_matches) > 1:
        return {
            "status": "needs_review",
            "reason": "Multiple direction-specific crosswalk options",
            "master_matches": master_matches,
        }
    direct_crosswalk = [
        row for row in crosswalk
        if row.get("transaction_type", "").upper() == direction
        and row.get("website_head_value")
        and (
            _key(row.get("accounting_head_name_original")) == _key(name)
            or _key(row.get("website_head_label_exact")) == _key(name)
        )
    ]
    if len(direct_crosswalk) == 1:
        row = direct_crosswalk[0]
        return {
            "status": "resolved",
            "source": "verified_crosswalk_name",
            "master_matches": master_matches,
            "website_value": row["website_head_value"],
            "website_label": row["website_head_label_exact"],
            "confidence": row.get("mapping_confidence"),
        }

    direct = [
        option for option in website_options
        if option["direction"] == direction and _key(option["label"]) == _key(name)
    ]
    if len(direct) == 1 and len(master_matches) == 1:
        return {
            "status": "needs_review",
            "source": "website_reference_without_crosswalk",
            "reason": "Website option is authoritative but crosswalk entry is missing",
            "master_matches": master_matches,
            "website_value": direct[0]["value"],
            "website_label": direct[0]["label"],
        }
    if len(direct) == 1 and not master_matches:
        return {
            "status": "needs_review",
            "source": "website_reference_master_missing",
            "reason": "Website option exists, but the workbook name is absent from the verified master",
            "master_matches": [],
            "website_value": direct[0]["value"],
            "website_label": direct[0]["label"],
        }
    if not master_matches:
        return {"status": "blocked", "reason": "Workbook Head Name is absent from verified master"}
    if len(master_matches) > 1:
        return {
            "status": "needs_review",
            "reason": "Canonical master name maps to multiple accounting head numbers",
            "master_matches": master_matches,
        }
    return {
        "status": "blocked",
        "reason": "No direction-specific website option or crosswalk mapping",
        "master_matches": master_matches,
    }


def _validate_row(
    direction: str,
    row_number: int,
    row: dict[str, Any],
    master: list[dict[str, str]],
    crosswalk: list[dict[str, str]],
    website_options: list[dict[str, str]],
    observed_subheads: dict[str, Any],
) -> dict[str, Any]:
    issues: list[str] = []
    review: list[str] = []
    for column in REQUIRED_COLUMNS:
        if column == "Subhead":
            continue
        if not _text(row.get(column)):
            issues.append(f"Missing required field: {column}")
    if row.get("Amount") is not None:
        try:
            if float(row["Amount"]) <= 0:
                issues.append("Amount must be greater than zero")
        except (TypeError, ValueError):
            issues.append("Amount is not numeric")

    resolution = _resolve_head(_text(row.get("Head Name")), direction, master, crosswalk, website_options)
    if resolution["status"] == "blocked":
        issues.append(f"Head mapping blocked: {resolution['reason']}")
    elif resolution["status"] == "needs_review":
        review.append(f"Head mapping needs review: {resolution['reason']}")

    subhead = _text(row.get("Subhead"))
    website_value = resolution.get("website_value")
    subhead_state = observed_subheads.get(str(website_value), {}).get("state")
    subhead_options = observed_subheads.get(str(website_value), {}).get("options", {})
    if subhead_state == "options":
        if not subhead:
            issues.append("Subhead is required: selected Head has selectable options")
        elif subhead not in subhead_options.values():
            issues.append("Workbook Subhead is not an available website option")
    elif subhead_state == "no_options":
        if subhead:
            issues.append("Workbook Subhead is populated but selected Head has no website Subhead options")
    else:
        review.append("Subhead behavior is unobserved for the selected Head")

    payload = {
        "direction": direction,
        "sheet": "Receipts" if direction == "RECEIPT" else "Payments",
        "row": row_number,
        "date": _json_value(row.get("Date")),
        "receipt_number": _text(row.get("Receipt Number")),
        "amount": row.get("Amount"),
        "head_name": _text(row.get("Head Name")),
        "subhead": subhead,
        "narration": _text(row.get("Narration")),
        "mode": "cash",
        "selectors": {
            "form": "#frmreceipt" if direction == "RECEIPT" else "#frmpayment",
            "date": "#datepicker1", "number": "#receiptno", "amount": "#amount",
            "cash": "#mode_cash", "head": "#head", "subhead": "#subhead",
            "narration": "#narration", "save": "#submit_form",
        },
        "head_resolution": resolution,
        "issues": issues,
        "review": review,
    }
    canonical = json.dumps(payload, sort_keys=True, default=_json_value).encode("utf-8")
    payload["submission_key"] = hashlib.sha256(canonical).hexdigest()
    payload["status"] = "BLOCKED" if issues else "NEEDS_REVIEW" if review else "READY"
    return payload


def run_dry_run(
    workbook_path: Path,
    crosswalk_path: Path,
    journal_path: Path,
    master_path: Path = DEFAULT_MASTER,
    website_html_path: Path = DEFAULT_WEBSITE_HTML,
    subheads_path: Path = DEFAULT_SUBHEADS,
) -> dict[str, Any]:
    """Read source artifacts and update a journal; never contacts or submits to e-Dayan."""
    for path in (workbook_path, crosswalk_path, master_path, website_html_path, subheads_path):
        if not path.is_file():
            raise FileNotFoundError(f"Required artifact not found: {path}")
    workbook = openpyxl.load_workbook(workbook_path, read_only=True, data_only=True)
    missing = sorted(set(SHEETS) - set(workbook.sheetnames))
    if missing:
        raise ValueError(f"Workbook is missing required sheets: {missing}")

    crosswalk = _load_csv(crosswalk_path)
    master = _load_csv(master_path)
    website_options = _load_website_options(website_html_path)
    with subheads_path.open("r", encoding="utf-8") as handle:
        observed_subheads = json.load(handle).get("heads", {})
    entries = []
    for sheet_name, direction in SHEETS.items():
        sheet = workbook[sheet_name]
        headers = [_text(cell.value) for cell in sheet[1]]
        if headers != list(REQUIRED_COLUMNS):
            raise ValueError(f"{sheet_name} columns do not match {list(REQUIRED_COLUMNS)}")
        for row_number, values in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
            if not any(_text(value) for value in values):
                continue
            entries.append(_validate_row(
                direction, row_number, dict(zip(REQUIRED_COLUMNS, values)),
                master, crosswalk, website_options, observed_subheads,
            ))

    prior_entries: dict[str, Any] = {}
    if journal_path.exists():
        with journal_path.open("r", encoding="utf-8") as handle:
            prior_entries = json.load(handle).get("entries", {})
    journal_entries = dict(prior_entries)
    for entry in entries:
        journal_entries.setdefault(entry["submission_key"], {
            "status": "dry_run_seen", "direction": entry["direction"],
            "sheet": entry["sheet"], "row": entry["row"],
        })
    exception_groups: dict[str, dict[str, Any]] = {}
    for entry in entries:
        for reason in entry["issues"] + entry["review"]:
            group = exception_groups.setdefault(reason, {"count": 0, "rows": []})
            group["count"] += 1
            group["rows"].append({
                "sheet": entry["sheet"],
                "row": entry["row"],
                "head_name": entry["head_name"],
            })
    report = {
        "mode": "dry-run",
        "workbook": str(workbook_path),
        "entry_count": len(entries),
        "ready_count": sum(e["status"] == "READY" for e in entries),
        "blocked_count": sum(e["status"] == "BLOCKED" for e in entries),
        "needs_review_count": sum(e["status"] == "NEEDS_REVIEW" for e in entries),
        "duplicate_keys_seen": sum(e["submission_key"] in prior_entries for e in entries),
        "exception_groups": exception_groups,
        "entries": entries,
    }
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with journal_path.open("w", encoding="utf-8") as handle:
        json.dump({"version": 2, "mode": "dry-run", "entries": journal_entries},
                  handle, indent=2, sort_keys=True)
        handle.write("\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", type=Path, required=True)
    parser.add_argument("--crosswalk", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--master", type=Path, default=DEFAULT_MASTER)
    parser.add_argument("--website-html", type=Path, default=DEFAULT_WEBSITE_HTML)
    parser.add_argument("--subheads", type=Path, default=DEFAULT_SUBHEADS)
    args = parser.parse_args()
    report = run_dry_run(
        args.workbook, args.crosswalk, args.journal, args.master,
        args.website_html, args.subheads,
    )
    print(json.dumps(report, indent=2, default=_json_value))
    return 0 if report["blocked_count"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
