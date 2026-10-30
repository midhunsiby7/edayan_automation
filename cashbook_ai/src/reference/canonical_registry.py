import json
import logging
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class SubheadPolicy(str, Enum):
    MANDATORY = "MANDATORY"
    OPTIONAL = "OPTIONAL"
    PROHIBITED = "PROHIBITED"


class CanonicalHead(BaseModel):
    canonical_head_id: str
    canonical_name: str
    account_set: str = "GENERAL"
    primary_direction: str  # "RECEIPT", "PAYMENT", "BOTH"
    subhead_policy: SubheadPolicy
    is_contra: bool = False
    description: str = ""
    is_active: bool = True


class CanonicalSubhead(BaseModel):
    canonical_subhead_id: str
    parent_head_id: str
    canonical_name: str
    website_subhead_value: Optional[str] = None
    is_active: bool = True


class HistoricalFolio(BaseModel):
    folio_number: int
    account_set: str = "GENERAL"
    year_context: str = "2026"
    financial_year: str = "2026-2027"
    direction: str  # "RECEIPT", "PAYMENT", "BOTH"
    canonical_head_id: str
    canonical_subhead_id: Optional[str] = None
    source_note: str = ""


class ApprovedAlias(BaseModel):
    alias_id: str
    target_id: str
    target_type: str  # "HEAD" or "SUBHEAD"
    alias_text: str
    language: str = "EN"  # "EN", "ML", "MIXED"
    direction_context: str = "ANY"  # "RECEIPT", "PAYMENT", "ANY"
    source: str = ""
    is_active: bool = True


class WebsiteHeadMapping(BaseModel):
    canonical_head_id: str
    website_head_value: str
    website_head_label: str
    direction: str  # "RECEIPT", "PAYMENT", "BOTH"
    account_set: str = "GENERAL"
    validity: str = "ACTIVE"
    notes: str = ""


class WebsiteSubheadMapping(BaseModel):
    canonical_subhead_id: str
    parent_website_head_value: str
    website_subhead_value: str
    website_subhead_label: str
    validity: str = "ACTIVE"


class CanonicalRegistry:
    """Authoritative service for canonical accounting reference data.
    
    Distinguishes:
      1. Canonical accounting head identity
      2. Canonical subhead identity
      3. Historical folio numbers with account-set/year context
      4. Approved aliases and alternative spellings
      5. e-Dayan website selector identities
    """

    DEFAULT_PATH = Path("data/reference/canonical_accounting_reference.json")

    def __init__(self, storage_path: Optional[Path | str] = None):
        self.storage_path = Path(storage_path) if storage_path else self.DEFAULT_PATH
        self.heads: Dict[str, CanonicalHead] = {}
        self.subheads: Dict[str, CanonicalSubhead] = {}
        self.subheads_by_parent: Dict[str, List[CanonicalSubhead]] = {}
        self.folios: List[HistoricalFolio] = []
        self.aliases: List[ApprovedAlias] = []
        self.website_heads: List[WebsiteHeadMapping] = []
        self.website_subheads: List[WebsiteSubheadMapping] = []
        
        # Indexes for fast resolution
        self._folio_index: Dict[Tuple[int, str, str, str], HistoricalFolio] = {}
        self._alias_index: Dict[Tuple[str, str], ApprovedAlias] = {}
        self._website_head_index: Dict[Tuple[str, str], WebsiteHeadMapping] = {}
        self._website_subhead_index: Dict[str, WebsiteSubheadMapping] = {}

        self.load()

    def load(self) -> None:
        """Load and index all reference records from JSON storage."""
        if not self.storage_path.is_file():
            raise FileNotFoundError(f"Canonical reference file not found: {self.storage_path}")

        with open(self.storage_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Clear existing
        self.heads.clear()
        self.subheads.clear()
        self.subheads_by_parent.clear()
        self.folios.clear()
        self.aliases.clear()
        self.website_heads.clear()
        self.website_subheads.clear()
        self._folio_index.clear()
        self._alias_index.clear()
        self._website_head_index.clear()
        self._website_subhead_index.clear()

        # 1. Heads
        for hid, hdata in data.get("canonical_heads", {}).items():
            head = CanonicalHead(**hdata)
            self.heads[hid] = head

        # 2. Subheads
        for sid, sdata in data.get("canonical_subheads", {}).items():
            subhead = CanonicalSubhead(**sdata)
            self.subheads[sid] = subhead
            self.subheads_by_parent.setdefault(subhead.parent_head_id, []).append(subhead)

        # 3. Historical Folios
        for frow in data.get("historical_folios", []):
            folio = HistoricalFolio(**frow)
            self.folios.append(folio)
            # Index by financial year (e.g. 2026-2027)
            key_fy = (folio.folio_number, folio.direction, folio.account_set, folio.financial_year)
            self._folio_index[key_fy] = folio
            # Also index by calendar year context (e.g. 2026) for backwards compatibility
            key_cy = (folio.folio_number, folio.direction, folio.account_set, folio.year_context)
            self._folio_index[key_cy] = folio

            # Also index with BOTH if direction is BOTH
            if folio.direction in ("RECEIPT", "PAYMENT"):
                key_both_fy = (folio.folio_number, "BOTH", folio.account_set, folio.financial_year)
                self._folio_index.setdefault(key_both_fy, folio)
                key_both_cy = (folio.folio_number, "BOTH", folio.account_set, folio.year_context)
                self._folio_index.setdefault(key_both_cy, folio)

        # 4. Approved Aliases
        for arow in data.get("approved_aliases", []):
            alias = ApprovedAlias(**arow)
            self.aliases.append(alias)
            if alias.is_active:
                norm_text = self._normalize_text(alias.alias_text)
                self._alias_index[(norm_text, alias.direction_context)] = alias
                if alias.direction_context != "ANY":
                    self._alias_index.setdefault((norm_text, "ANY"), alias)

        # 5. Website Heads
        for wrow in data.get("website_heads", []):
            wh = WebsiteHeadMapping(**wrow)
            self.website_heads.append(wh)
            self._website_head_index[(wh.canonical_head_id, wh.direction)] = wh
            if wh.direction == "BOTH":
                self._website_head_index.setdefault((wh.canonical_head_id, "RECEIPT"), wh)
                self._website_head_index.setdefault((wh.canonical_head_id, "PAYMENT"), wh)

        # 6. Website Subheads
        for wsrow in data.get("website_subheads", []):
            wsub = WebsiteSubheadMapping(**wsrow)
            self.website_subheads.append(wsub)
            self._website_subhead_index[wsub.canonical_subhead_id] = wsub

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Strip punctuation, collapse whitespace, lowercase for matching."""
        if not text:
            return ""
        norm = text.lower().strip()
        for ch in ".,/():;-_'\"":
            norm = norm.replace(ch, " ")
        return " ".join(norm.split())

    def get_head(self, head_id: str) -> Optional[CanonicalHead]:
        """Retrieve canonical head by ID."""
        return self.heads.get(head_id)

    def get_subhead(self, subhead_id: str) -> Optional[CanonicalSubhead]:
        """Retrieve canonical subhead by ID."""
        return self.subheads.get(subhead_id)

    def get_subheads_for_head(self, head_id: str) -> List[CanonicalSubhead]:
        """Retrieve all active subheads belonging to a canonical head."""
        return [s for s in self.subheads_by_parent.get(head_id, []) if s.is_active]

    def get_subhead_policy(self, head_id: str) -> SubheadPolicy:
        """Get subhead policy for a head (MANDATORY, OPTIONAL, PROHIBITED)."""
        head = self.heads.get(head_id)
        if not head:
            return SubheadPolicy.PROHIBITED
        return head.subhead_policy

    def resolve_by_folio(
        self,
        folio_num: int | str,
        direction: str,
        account_set: str = "GENERAL",
        financial_year: str = "2026-2027",
        year: Optional[str] = None
    ) -> Optional[Tuple[CanonicalHead, Optional[CanonicalSubhead]]]:
        """Resolve a historical folio number to (CanonicalHead, Optional[CanonicalSubhead]).
        
        Checks financial_year (e.g. '2026-2027'), calendar year fallback (e.g. '2026'), and 'ALL'.
        """
        try:
            f_int = int(str(folio_num).strip())
        except (ValueError, TypeError):
            return None

        dir_upper = direction.upper().strip()
        target_year = year or financial_year
        
        # 1. Try exact financial_year match
        folio = self._folio_index.get((f_int, dir_upper, account_set, target_year))
        if not folio:
            folio = self._folio_index.get((f_int, "BOTH", account_set, target_year))
        # 2. Try calendar year fallback if target_year has hyphen (e.g. "2026-2027" -> "2026")
        if not folio and "-" in target_year:
            cal_year = target_year.split("-")[0]
            folio = self._folio_index.get((f_int, dir_upper, account_set, cal_year))
            if not folio:
                folio = self._folio_index.get((f_int, "BOTH", account_set, cal_year))
        # 3. Try "ALL"
        if not folio:
            folio = self._folio_index.get((f_int, dir_upper, account_set, "ALL"))
            if not folio:
                folio = self._folio_index.get((f_int, "BOTH", account_set, "ALL"))

        if not folio:
            return None

        head = self.get_head(folio.canonical_head_id)
        if not head or not head.is_active:
            return None

        subhead = self.get_subhead(folio.canonical_subhead_id) if folio.canonical_subhead_id else None
        return (head, subhead)

    def resolve_by_alias(
        self,
        text: str,
        direction: str = "ANY"
    ) -> Optional[Tuple[str, str, float]]:
        """Resolve text against approved human aliases.
        
        Returns:
            Tuple[target_id, target_type ("HEAD"|"SUBHEAD"), confidence] or None
        """
        norm = self._normalize_text(text)
        if not norm:
            return None

        dir_upper = direction.upper().strip() if direction else "ANY"

        # 1. Exact normalized match with direction
        alias = self._alias_index.get((norm, dir_upper))
        if not alias and dir_upper != "ANY":
            # 2. Exact normalized match with ANY direction
            alias = self._alias_index.get((norm, "ANY"))

        if alias:
            return (alias.target_id, alias.target_type, 1.0)

        return None

    def get_website_head_for_canonical(
        self,
        head_id: str,
        direction: str
    ) -> Optional[WebsiteHeadMapping]:
        """Retrieve website dropdown mapping for a canonical head and direction."""
        dir_upper = direction.upper().strip()
        wh = self._website_head_index.get((head_id, dir_upper))
        if not wh:
            wh = self._website_head_index.get((head_id, "BOTH"))
        return wh

    def get_website_subhead_for_canonical(
        self,
        subhead_id: str
    ) -> Optional[WebsiteSubheadMapping]:
        """Retrieve website dropdown mapping for a canonical subhead."""
        return self._website_subhead_index.get(subhead_id)

    def validate_transaction(
        self,
        head_id: str,
        subhead_id: Optional[str],
        direction: str
    ) -> Tuple[bool, List[str]]:
        """Validate transaction against canonical rules and subhead policies.
        
        Returns:
            Tuple[is_valid: bool, issues: List[str]]
        """
        issues = []
        head = self.get_head(head_id)
        if not head:
            return False, [f"CANONICAL_HEAD_NOT_FOUND: '{head_id}'"]
        if not head.is_active:
            issues.append(f"INACTIVE_HEAD: '{head_id}' is marked inactive")

        dir_upper = direction.upper().strip()
        if head.primary_direction != "BOTH" and head.primary_direction != dir_upper:
            # Contra bank transactions are allowed both directions
            if not head.is_contra:
                issues.append(f"DIRECTION_MISMATCH: Head '{head.canonical_name}' is {head.primary_direction}, transaction is {dir_upper}")

        # Subhead Policy Validation
        policy = head.subhead_policy
        if policy == SubheadPolicy.MANDATORY:
            if not subhead_id:
                issues.append(f"SUBHEAD_REQUIRED: Head '{head.canonical_name}' strictly requires a subhead")
            else:
                sub = self.get_subhead(subhead_id)
                if not sub:
                    issues.append(f"SUBHEAD_NOT_FOUND: Subhead '{subhead_id}' does not exist")
                elif sub.parent_head_id != head_id:
                    issues.append(f"INVALID_SUBHEAD_PARENT: Subhead '{sub.canonical_name}' belongs to '{sub.parent_head_id}', not '{head_id}'")
        elif policy == SubheadPolicy.PROHIBITED:
            if subhead_id:
                issues.append(f"SUBHEAD_PROHIBITED: Head '{head.canonical_name}' does not allow subheads (received '{subhead_id}')")
        elif policy == SubheadPolicy.OPTIONAL:
            if subhead_id:
                sub = self.get_subhead(subhead_id)
                if not sub:
                    issues.append(f"SUBHEAD_NOT_FOUND: Subhead '{subhead_id}' does not exist")
                elif sub.parent_head_id != head_id:
                    issues.append(f"INVALID_SUBHEAD_PARENT: Subhead '{sub.canonical_name}' belongs to '{sub.parent_head_id}', not '{head_id}'")

        # Website Mapping Validation
        web_head = self.get_website_head_for_canonical(head_id, dir_upper)
        if not web_head and head.canonical_head_id != "GEN_HEAD_OPENING_BAL":
            issues.append(f"NO_WEBSITE_MAPPING: No website selector mapped for head '{head_id}' ({dir_upper})")
        elif web_head and web_head.validity != "ACTIVE":
            issues.append(f"INACTIVE_WEBSITE_MAPPING: Website mapping for '{head_id}' is {web_head.validity}")

        if subhead_id and not issues:
            web_sub = self.get_website_subhead_for_canonical(subhead_id)
            if not web_sub:
                issues.append(f"NO_WEBSITE_SUBHEAD_MAPPING: No website subhead mapped for '{subhead_id}'")

        return (len(issues) == 0, issues)

    def validate_registry_integrity(self) -> List[str]:
        """Exhaustive self-consistency check on the entire reference database."""
        integrity_errors = []

        # 1. Check all subheads link to valid heads
        for sid, sub in self.subheads.items():
            if sub.parent_head_id not in self.heads:
                integrity_errors.append(f"ORPHAN_SUBHEAD: Subhead '{sid}' references non-existent parent '{sub.parent_head_id}'")
            else:
                parent = self.heads[sub.parent_head_id]
                if parent.subhead_policy == SubheadPolicy.PROHIBITED:
                    integrity_errors.append(f"POLICY_CONFLICT: Parent head '{parent.canonical_head_id}' prohibits subheads, but child '{sid}' exists")

        # 2. Check all folios link to valid heads and subheads
        for folio in self.folios:
            if folio.canonical_head_id not in self.heads:
                integrity_errors.append(f"INVALID_FOLIO_HEAD: Folio {folio.folio_number} references non-existent head '{folio.canonical_head_id}'")
            if folio.canonical_subhead_id:
                if folio.canonical_subhead_id not in self.subheads:
                    integrity_errors.append(f"INVALID_FOLIO_SUBHEAD: Folio {folio.folio_number} references non-existent subhead '{folio.canonical_subhead_id}'")
                elif folio.canonical_head_id != self.subheads[folio.canonical_subhead_id].parent_head_id:
                    integrity_errors.append(f"FOLIO_SUBHEAD_MISMATCH: Folio {folio.folio_number} head '{folio.canonical_head_id}' != subhead parent '{self.subheads[folio.canonical_subhead_id].parent_head_id}'")

        # 3. Check all aliases link to valid targets
        for alias in self.aliases:
            if alias.target_type == "HEAD":
                if alias.target_id not in self.heads:
                    integrity_errors.append(f"ORPHAN_ALIAS: Alias '{alias.alias_text}' references non-existent head '{alias.target_id}'")
            elif alias.target_type == "SUBHEAD":
                if alias.target_id not in self.subheads:
                    integrity_errors.append(f"ORPHAN_ALIAS: Alias '{alias.alias_text}' references non-existent subhead '{alias.target_id}'")
            else:
                integrity_errors.append(f"INVALID_ALIAS_TARGET_TYPE: Alias '{alias.alias_text}' has invalid type '{alias.target_type}'")

        # 4. Check all website heads link to valid canonical heads
        for wh in self.website_heads:
            if wh.canonical_head_id not in self.heads:
                integrity_errors.append(f"ORPHAN_WEBSITE_HEAD: Website head '{wh.website_head_value}' references non-existent canonical head '{wh.canonical_head_id}'")

        # 5. Check all website subheads link to valid canonical subheads
        for wsub in self.website_subheads:
            if wsub.canonical_subhead_id not in self.subheads:
                integrity_errors.append(f"ORPHAN_WEBSITE_SUBHEAD: Website subhead '{wsub.website_subhead_value}' references non-existent canonical subhead '{wsub.canonical_subhead_id}'")

        # 6. Bank Separation Integrity
        # Ensure separate bank accounts are distinct canonical identities
        bank_heads = [hid for hid in self.heads if "BANK" in hid]
        if "GEN_HEAD_BANK_SIB_KAPPAD" not in bank_heads or "GEN_HEAD_BANK_MEENACHIL_EAST" not in bank_heads:
            integrity_errors.append("BANK_SEPARATION_FAILED: South Indian Bank Kappad and Meenachil East Bank must be distinct canonical heads")

        return integrity_errors
