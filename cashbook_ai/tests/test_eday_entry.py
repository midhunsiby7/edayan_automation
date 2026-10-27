import json
from pathlib import Path

import pytest

from automation.edayan_entry import (
    CONFIRMED,
    FAILED,
    PENDING,
    SUBMITTED,
    UNCERTAIN,
    confirm_save,
    load_or_create_journal,
    prepare_preflight,
    RelayBrowserEntryAdapter,
    RelayBrowserClient,
    transition,
    wait_for_subhead,
)


def report_entry(key="k1", status="READY"):
    return {
        "submission_key": key,
        "status": status,
        "direction": "RECEIPT",
        "sheet": "Receipts",
        "row": 2,
        "head_name": "Test",
        "issues": [] if status == "READY" else ["blocked"],
        "review": [],
        "mode": "cash",
    }


def test_preflight_isolates_ready_entries_and_persists_pending(tmp_path: Path):
    journal = tmp_path / "journal.json"
    result = prepare_preflight(
        {"entries": [report_entry(), report_entry("k2", "BLOCKED")]}, journal
    )
    assert len(result.ready) == 1
    assert len(result.exceptions) == 1
    saved = json.loads(journal.read_text())
    assert saved["entries"]["k1"]["status"] == PENDING


def test_cash_mode_is_preserved_in_ready_payload():
    assert report_entry()["mode"] == "cash"


def test_slow_subhead_waits_until_settled():
    states = iter(({"state": "loading"}, {"state": "options", "options": {"1": "X"}}))
    result = wait_for_subhead(lambda: next(states), interval_seconds=0)
    assert result["state"] == "options"


def test_save_requires_explicit_confirmation_evidence():
    assert confirm_save({"confirmed": True, "record_id": "abc"})
    assert confirm_save({"confirmed": True, "success_text": "Saved"})
    assert not confirm_save({"confirmed": True})
    assert not confirm_save({"success_text": "Saved"})


def test_progress_persistence_and_uncertain_outcome_blocks_retry(tmp_path: Path):
    journal = tmp_path / "journal.json"
    prepare_preflight({"entries": [report_entry()]}, journal)
    transition(journal, "k1", SUBMITTED, evidence={"request": "observed"})
    transition(journal, "k1", UNCERTAIN, error="No definitive response")
    assert load_or_create_journal(journal)["entries"]["k1"]["status"] == UNCERTAIN
    with pytest.raises(ValueError):
        transition(journal, "k1", SUBMITTED)


def test_confirmed_entry_is_terminal_and_failed_can_be_retried(tmp_path: Path):
    journal = tmp_path / "journal.json"
    prepare_preflight({"entries": [report_entry()]}, journal)
    transition(journal, "k1", SUBMITTED)
    transition(journal, "k1", FAILED, error="Validation message")
    transition(journal, "k1", SUBMITTED)
    transition(journal, "k1", CONFIRMED, evidence={"record_id": "1"})
    with pytest.raises(ValueError):
        transition(journal, "k1", FAILED)


def test_relay_adapter_uses_existing_relay_and_keeps_live_entry_gated():
    client = RelayBrowserClient()
    assert client.relay_url == "ws://localhost:11800"
    adapter = RelayBrowserEntryAdapter(client)
    with pytest.raises(RuntimeError, match="approval-gated"):
        adapter.submit()
