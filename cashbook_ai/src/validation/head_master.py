import json
import logging
from pathlib import Path
from typing import Dict, Optional, List, Any

from src.models import AccountSet, AccountingHead, MasterSourceType

logger = logging.getLogger(__name__)


def _parse_source_row(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    return int(float(value))


class HeadMasterRepository:
    """Provides a lookup service for authoritative Head Master records (JSON)."""

    def __init__(self, storage_path: Path):
        self.storage_path = storage_path
        self._records: Dict[AccountSet, Dict[str, AccountingHead]] = {
            AccountSet.GENERAL: {},
            AccountSet.AGRICULTURE: {},
        }
        self.subhead_rules = {}
        self.load()
        self._load_subhead_rules()

    def _load_subhead_rules(self):
        rules_path = Path("data/reference/subhead_rules_general.json")
        if rules_path.exists():
            try:
                with open(rules_path, "r", encoding="utf-8") as f:
                    self.subhead_rules = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load subhead rules: {e}")

    def get_subhead_rule(self, head_number: str) -> Optional[str]:
        if not self.subhead_rules:
            return None
            
        for rule in self.subhead_rules.get("embedded_subheads", []):
            if rule["accounting_head_number"] == head_number:
                return rule["rule_type"]
                
        for rule in self.subhead_rules.get("narration_subheads", []):
            if rule["accounting_head_number"] == head_number:
                return rule["rule_type"]
                
        return None

    def load(self):
        """Load records from the storage file."""
        if not self.storage_path.exists():
            return
        
        try:
            if self.storage_path.suffix == ".csv":
                import csv
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        extracted_name = row.get("extracted_head_name") or row.get("accounting_head_name") or ""
                        human_final_name = row.get("human_final_name") or None
                        record = AccountingHead(
                            account_set=AccountSet(row["account_set"]),
                            accounting_head_number=row["accounting_head_number"],
                            extracted_head_name=extracted_name,
                            human_final_name=human_final_name,
                            review_status=row.get("review_status") or None,
                            human_comment=row.get("human_comment") or None,
                            accounting_head_name=human_final_name or row.get("accounting_head_name") or extracted_name,
                            normalized_accounting_head_name=row.get("normalized_accounting_head_name") or None,
                            source_image=row.get("source_image") or None,
                            source_page=row.get("source_page") or None,
                            source_row=_parse_source_row(row.get("source_row")),
                            extraction_confidence=float(row.get("extraction_confidence") or 1.0),
                            review_required=(row.get("review_required") == "True"),
                            source_type=MasterSourceType(row.get("source_type", MasterSourceType.REAL_AI_EXTRACTED.value))
                        )
                        self.add_record(record, is_load=True)
            else:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for item in data:
                        record = AccountingHead(**item)
                        self.add_record(record, is_load=True)
        except Exception as e:
            logger.error(f"Failed to load Head Master records from {self.storage_path}: {e}")

    def save(self):
        """Persist records to the storage file."""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            if self.storage_path.suffix == ".csv":
                import csv
                with open(self.storage_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        "account_set", "accounting_head_number", "extracted_head_name",
                        "human_final_name", "review_status", "human_comment", "accounting_head_name",
                        "normalized_accounting_head_name", "source_image", "source_page", "source_row",
                        "extraction_confidence", "review_required", "source_type"
                    ])
                    for account_set in self._records:
                        for record in self._records[account_set].values():
                            writer.writerow([
                                record.account_set.value,
                                record.accounting_head_number,
                                record.extracted_head_name or record.accounting_head_name,
                                record.human_final_name or "",
                                record.review_status or "",
                                record.human_comment or "",
                                record.accounting_head_name,
                                record.normalized_accounting_head_name or "",
                                record.source_image or "",
                                record.source_page or "",
                                record.source_row or "",
                                record.extraction_confidence,
                                record.review_required,
                                record.source_type.value
                            ])
            else:
                all_records = []
                for account_set in self._records:
                    for record in self._records[account_set].values():
                        all_records.append(record.model_dump(mode="json"))
                        
                with open(self.storage_path, "w", encoding="utf-8") as f:
                    json.dump(all_records, f, indent=2, ensure_ascii=False)
        except PermissionError:
            logger.warning(f"Could not save to {self.storage_path}, file might be open in another program.")

    def add_record(self, new_record: AccountingHead, is_load: bool = False):
        """
        Add a new record to the master list. 
        If it conflicts with an existing number with a different name, it flags it for review 
        but does NOT overwrite the existing authoritative name.
        """
        acct_set = new_record.account_set
        head_num = new_record.accounting_head_number
        
        existing = self._records[acct_set].get(head_num)
        if existing:
            # Check for conflict
            if existing.accounting_head_name.lower().strip() != new_record.accounting_head_name.lower().strip():
                existing.review_required = True
                if not is_load:
                    logger.warning(
                        f"Conflict in Head Master for {acct_set.value} - Head {head_num}: "
                        f"Existing='{existing.accounting_head_name}' vs New='{new_record.accounting_head_name}'. "
                        "Marked for review. Not overwriting."
                    )
        else:
            self._records[acct_set][head_num] = new_record

    def get_head_name(self, account_set: AccountSet, head_number: str) -> Optional[str]:
        """Lookup the authoritative human-final name for a given accounting head."""
        if not head_number:
            return None
        record = self._records.get(account_set, {}).get(head_number)
        if not record:
            return None
        return record.human_final_name or record.accounting_head_name

    def get_extracted_head_name(self, account_set: AccountSet, head_number: str) -> Optional[str]:
        """Return the preserved extracted/master name for audit and embedded subhead rules."""
        if not head_number:
            return None
        record = self._records.get(account_set, {}).get(head_number)
        if not record:
            return None
        return record.extracted_head_name or record.accounting_head_name

    def import_verified_csv(self, csv_path: Path, expected_count: Optional[int] = None) -> Dict[str, Any]:
        """
        Import a human-verified CSV file directly into the repository.
        Validates the CSV and returns a detailed report.
        """
        if not csv_path.exists():
            return {"status": "ERROR", "message": f"File not found: {csv_path}"}
            
        import csv
        
        loaded_records = []
        conflicts = []
        missing = []
        
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    record = AccountingHead(
                        account_set=AccountSet(row["account_set"]),
                        accounting_head_number=row["accounting_head_number"],
                        extracted_head_name=row.get("extracted_head_name") or row.get("accounting_head_name"),
                        human_final_name=row.get("human_final_name") or None,
                        review_status=row.get("review_status") or None,
                        human_comment=row.get("human_comment") or None,
                        accounting_head_name=row.get("human_final_name") or row.get("accounting_head_name") or row.get("extracted_head_name") or "",
                        normalized_accounting_head_name=row.get("normalized_accounting_head_name") or None,
                        source_image=row.get("source_image") or None,
                        source_page=row.get("source_page") or None,
                        source_row=_parse_source_row(row.get("source_row")),
                        extraction_confidence=float(row.get("extraction_confidence") or 1.0),
                        review_required=(row.get("review_required", "").lower() == "true"),
                        source_type=MasterSourceType.MANUAL_VERIFIED
                    )
                    loaded_records.append(record)
                except Exception as e:
                    conflicts.append(f"Row error ({row.get('accounting_head_number', 'unknown')}): {e}")

        # Check for duplicates within the imported file
        seen_numbers = {}
        for r in loaded_records:
            if r.accounting_head_number in seen_numbers:
                conflicts.append(f"Duplicate head number in import: {r.accounting_head_number}")
                r.review_required = True
            seen_numbers[r.accounting_head_number] = r
            
        # Add to repository
        for r in loaded_records:
            # We explicitly allow MANUAL_VERIFIED to replace MOCK_TEST or REAL_AI_EXTRACTED
            self._records[r.account_set][r.accounting_head_number] = r
            
        unique_numbers = list(seen_numbers.keys())
        
        # Check completeness based on declared expectation (not hardcoded 101 unless specified)
        is_complete = False
        if expected_count is not None:
            is_complete = len(unique_numbers) >= expected_count
            for i in range(1, expected_count + 1):
                if str(i) not in unique_numbers:
                    missing.append(str(i))
        
        status = "COMPLETE" if is_complete else "PARTIAL"
        
        report = {
            "Master status": status,
            "Source": MasterSourceType.MANUAL_VERIFIED.value,
            "Total entries loaded": len(loaded_records),
            "Unique head numbers": len(unique_numbers),
            "Conflicts": len(conflicts),
            "Conflict Details": conflicts,
            "Review required": sum(1 for r in loaded_records if r.review_required),
            "Missing/unknown entries": missing
        }
        
        self.save()
        return report
