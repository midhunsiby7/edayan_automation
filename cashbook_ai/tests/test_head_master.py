import json
import pytest
from pathlib import Path
from src.models import AccountSet, AccountingHead
from src.validation.head_master import HeadMasterRepository


def test_head_master_lookup(tmp_path: Path):
    repo_file = tmp_path / "head_master.json"
    repo = HeadMasterRepository(repo_file)

    record1 = AccountingHead(
        account_set=AccountSet.GENERAL,
        accounting_head_number="78",
        accounting_head_name="Office Expenses"
    )
    repo.add_record(record1)
    
    assert repo.get_head_name(AccountSet.GENERAL, "78") == "Office Expenses"
    assert repo.get_head_name(AccountSet.GENERAL, "99") is None
    assert repo.get_head_name(AccountSet.AGRICULTURE, "78") is None


def test_general_vs_agriculture_separation(tmp_path: Path):
    repo_file = tmp_path / "head_master.json"
    repo = HeadMasterRepository(repo_file)

    gen_record = AccountingHead(
        account_set=AccountSet.GENERAL,
        accounting_head_number="10",
        accounting_head_name="General Head 10"
    )
    agr_record = AccountingHead(
        account_set=AccountSet.AGRICULTURE,
        accounting_head_number="10",
        accounting_head_name="Agri Head 10"
    )
    repo.add_record(gen_record)
    repo.add_record(agr_record)
    
    assert repo.get_head_name(AccountSet.GENERAL, "10") == "General Head 10"
    assert repo.get_head_name(AccountSet.AGRICULTURE, "10") == "Agri Head 10"


def test_duplicate_identical_head_definition(tmp_path: Path):
    repo_file = tmp_path / "head_master.json"
    repo = HeadMasterRepository(repo_file)

    r1 = AccountingHead(account_set=AccountSet.GENERAL, accounting_head_number="5", accounting_head_name="Travel")
    r2 = AccountingHead(account_set=AccountSet.GENERAL, accounting_head_number="5", accounting_head_name=" travel ")
    
    repo.add_record(r1)
    repo.add_record(r2)
    
    # Should not be marked for review
    assert repo._records[AccountSet.GENERAL]["5"].review_required is False


def test_conflicting_head_definitions(tmp_path: Path):
    repo_file = tmp_path / "head_master.json"
    repo = HeadMasterRepository(repo_file)

    r1 = AccountingHead(account_set=AccountSet.GENERAL, accounting_head_number="6", accounting_head_name="Travel")
    r2 = AccountingHead(account_set=AccountSet.GENERAL, accounting_head_number="6", accounting_head_name="Food")
    
    repo.add_record(r1)
    repo.add_record(r2)
    
    # Name should remain 'Travel' (first one wins as authoritative)
    assert repo.get_head_name(AccountSet.GENERAL, "6") == "Travel"
    # But it must be flagged for review due to conflict
    assert repo._records[AccountSet.GENERAL]["6"].review_required is True


def test_save_and_load(tmp_path: Path):
    repo_file = tmp_path / "head_master.json"
    repo = HeadMasterRepository(repo_file)
    repo.add_record(AccountingHead(account_set=AccountSet.GENERAL, accounting_head_number="1", accounting_head_name="Test"))
    repo.save()

    repo2 = HeadMasterRepository(repo_file)
    assert repo2.get_head_name(AccountSet.GENERAL, "1") == "Test"
