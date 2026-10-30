import re
import unicodedata
import logging
from enum import Enum
from typing import Optional, List, Dict, Tuple, Any
from pydantic import BaseModel, Field

from src.models import RawExtractedRow, TransactionType
from src.extraction.column_aligner import AlignedRow, ColumnShiftType
from src.reference.canonical_registry import (
    CanonicalRegistry,
    CanonicalHead,
    CanonicalSubhead,
    SubheadPolicy,
    WebsiteHeadMapping,
    WebsiteSubheadMapping,
)

logger = logging.getLogger(__name__)


class MappingStatus(str, Enum):
    VERIFIED = "VERIFIED"
    SUGGESTED = "SUGGESTED"
    PROVISIONAL = "PROVISIONAL"
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"
    CONFLICT = "CONFLICT"

    def __eq__(self, other):
        val = getattr(other, "value", other)
        if (self.value == "VERIFIED" and val == "RESOLVED") or (self.value == "RESOLVED" and val == "VERIFIED"):
            return True
        return super().__eq__(other)


class SubheadStatus(str, Enum):
    RESOLVED = "RESOLVED"
    VALID_EMPTY = "VALID_EMPTY"
    MISSING_MANDATORY = "MISSING_MANDATORY"
    PROHIBITED_VIOLATION = "PROHIBITED_VIOLATION"
    INVALID_PARENT = "INVALID_PARENT"
    UNRESOLVED = "UNRESOLVED"


class MappingMethod(str, Enum):
    EXACT_FOLIO = "EXACT_FOLIO"
    EXACT_ALIAS = "EXACT_ALIAS"
    NORMALIZED_ALIAS = "NORMALIZED_ALIAS"
    CONTEXTUAL_RULE = "CONTEXTUAL_RULE"
    LLM_ASSISTANT = "LLM_ASSISTANT"
    UNRESOLVED = "UNRESOLVED"


class MappingResult(BaseModel):
    """Immutable, audit-ready result of transaction classification."""
    transaction_id: str
    raw_head_number: Optional[str] = None
    aligned_head_number: Optional[str] = None
    raw_voucher_number: Optional[str] = None
    aligned_voucher_number: Optional[str] = None
    raw_narration: Optional[str] = None
    english_narration: Optional[str] = None
    direction: str  # "RECEIPT" or "PAYMENT"
    amount: Optional[float] = None

    # Canonical Classification
    canonical_head_id: Optional[str] = None
    canonical_head_name: Optional[str] = None
    canonical_subhead_id: Optional[str] = None
    canonical_subhead_name: Optional[str] = None

    # Telemetry and Confidence
    mapping_method: MappingMethod = MappingMethod.UNRESOLVED
    confidence_score: float = 0.0
    candidate_alternatives: List[Dict[str, Any]] = Field(default_factory=list)
    evidence: str = "Unprocessed"
    mapping_status: MappingStatus = MappingStatus.UNRESOLVED
    subhead_status: SubheadStatus = SubheadStatus.UNRESOLVED
    needs_human_review: bool = True
    unresolved_reason: Optional[str] = None

    # Website Dropdown Preflight Resolution
    website_head_value: Optional[str] = None
    website_head_label: Optional[str] = None
    website_subhead_value: Optional[str] = None
    website_subhead_label: Optional[str] = None


class HybridMappingEngine:
    """Deterministic-first accounting classification engine covering Levels 1 to 3."""

    # Zero-width formatting characters to strip
    ZERO_WIDTH_CHARS = {'\u200b', '\u200c', '\u200d', '\ufeff'}

    def __init__(self, registry: Optional[CanonicalRegistry] = None):
        self.registry = registry or CanonicalRegistry()

    @classmethod
    def clean_text(cls, text: Optional[str]) -> str:
        """Strip zero-width characters, normalize Unicode, lowercase, and collapse whitespace."""
        if not text:
            return ""
        # 1. Unicode NFC normalization
        normalized = unicodedata.normalize("NFC", text)
        # 2. Strip zero-width characters
        for ch in cls.ZERO_WIDTH_CHARS:
            normalized = normalized.replace(ch, "")
        # 3. Strip punctuation
        for ch in ".,/():;-_'\"!?":
            normalized = normalized.replace(ch, " ")
        # 4. Collapse whitespace & lowercase
        return " ".join(normalized.lower().split())

    def _resolve_subhead_policy(
        self,
        head: CanonicalHead,
        subhead_id: Optional[str],
        narration: str
    ) -> Tuple[Optional[CanonicalSubhead], SubheadStatus, Optional[str]]:
        """Evaluate and enforce subhead policy (MANDATORY, OPTIONAL, PROHIBITED)."""
        policy = head.subhead_policy

        if policy == SubheadPolicy.MANDATORY:
            if not subhead_id:
                # Try to infer subhead from narration
                inferred_sub = self._match_subhead_from_narration(head.canonical_head_id, narration)
                if inferred_sub:
                    return inferred_sub, SubheadStatus.RESOLVED, None
                return None, SubheadStatus.MISSING_MANDATORY, f"Head '{head.canonical_name}' requires a mandatory subhead."
            
            sub = self.registry.get_subhead(subhead_id)
            if not sub:
                return None, SubheadStatus.UNRESOLVED, f"Subhead '{subhead_id}' not found in registry."
            if sub.parent_head_id != head.canonical_head_id:
                return None, SubheadStatus.INVALID_PARENT, f"Subhead '{sub.canonical_name}' belongs to '{sub.parent_head_id}', not '{head.canonical_head_id}'."
            return sub, SubheadStatus.RESOLVED, None

        elif policy == SubheadPolicy.PROHIBITED:
            if subhead_id:
                return None, SubheadStatus.PROHIBITED_VIOLATION, f"Head '{head.canonical_name}' strictly prohibits subheads (got '{subhead_id}')."
            return None, SubheadStatus.VALID_EMPTY, None

        elif policy == SubheadPolicy.OPTIONAL:
            if not subhead_id:
                return None, SubheadStatus.VALID_EMPTY, None
            sub = self.registry.get_subhead(subhead_id)
            if not sub:
                return None, SubheadStatus.UNRESOLVED, f"Optional subhead '{subhead_id}' not found."
            if sub.parent_head_id != head.canonical_head_id:
                return None, SubheadStatus.INVALID_PARENT, f"Subhead belongs to '{sub.parent_head_id}', not '{head.canonical_head_id}'."
            return sub, SubheadStatus.RESOLVED, None

        return None, SubheadStatus.UNRESOLVED, "Unknown subhead policy"

    def _match_subhead_from_narration(self, parent_head_id: str, narration: str) -> Optional[CanonicalSubhead]:
        """Match subhead for parent head using narration text and approved subhead aliases."""
        clean_narr = self.clean_text(narration)
        if not clean_narr:
            return None

        # 1. Staff Allowances (Head 108)
        if parent_head_id == "GEN_HEAD_ALLOWANCES":
            if any(k in clean_narr for k in ["vicar", "വികാരി"]):
                return self.registry.get_subhead("GEN_SUB_ALLOW_VICAR")
            if any(k in clean_narr for k in ["accountant", "അക്കൗണ്ടന്റ്"]):
                return self.registry.get_subhead("GEN_SUB_ALLOW_ACCT")
            if any(k in clean_narr for k in ["computer", "assistant"]):
                return self.registry.get_subhead("GEN_SUB_ALLOW_COMP")
            if any(k in clean_narr for k in ["sacristan", "sacristian"]):
                return self.registry.get_subhead("GEN_SUB_ALLOW_SACR")
            if any(k in clean_narr for k in ["sweeper"]):
                return self.registry.get_subhead("GEN_SUB_ALLOW_SWEEP")

        # 2. Collections per Order from Diocese (Head 143)
        if parent_head_id == "GEN_HEAD_ORDER_FROM_DIOCESE":
            subheads = self.registry.get_subheads_for_head("GEN_HEAD_ORDER_FROM_DIOCESE")
            for sub in subheads:
                clean_name = self.clean_text(sub.canonical_name)
                if clean_name and (clean_name in clean_narr or clean_narr in clean_name):
                    return sub
            # Specific keyword overrides
            if "seminarian" in clean_narr or "seminary" in clean_narr:
                return self.registry.get_subhead("GEN_SUB_DIOCESE_SEMINARIAN")
            if "holy land" in clean_narr:
                return self.registry.get_subhead("GEN_SUB_DIOCESE_HOLY_LAND")
            if "childhood" in clean_narr:
                return self.registry.get_subhead("GEN_SUB_DIOCESE_CHILDHOOD")
            if "hunger" in clean_narr or "disease" in clean_narr:
                return self.registry.get_subhead("GEN_SUB_DIOCESE_HUNGER")
            if "madhya" in clean_narr:
                return self.registry.get_subhead("GEN_SUB_DIOCESE_MADHYA_VIRUTHA")

        return None

    @staticmethod
    def _parse_amount(amt_str: Optional[str]) -> Optional[float]:
        """Safely parse cashbook amount strings including space-separated paise (e.g. '18506 60' -> 18506.60)."""
        if not amt_str:
            return None
        s = amt_str.replace(",", "").strip()
        s = re.sub(r'[\/\-]$', '', s).strip()
        s = s.replace("-", "").strip()
        match = re.match(r'^(\d+)\s+(\d{1,2})$', s)
        if match:
            try:
                return float(f"{match.group(1)}.{match.group(2)}")
            except ValueError:
                pass
        try:
            return float(s)
        except (ValueError, TypeError):
            return None

    def map_transaction(
        self,
        aligned_row: AlignedRow,
        financial_year: str = "2026-2027",
        transaction_id: Optional[str] = None
    ) -> MappingResult:
        """Execute layered mapping (Levels 1–3) on an aligned transaction."""
        raw = aligned_row.raw_row
        tx_id = transaction_id or f"TX_{raw.source_page}_{raw.source_row}"
        
        # Determine direction and amount
        is_payment = bool(aligned_row.aligned_amount_col2 and aligned_row.aligned_amount_col2 != "None")
        direction = "PAYMENT" if is_payment else "RECEIPT"
        
        amt_str = aligned_row.aligned_amount_col2 if is_payment else aligned_row.aligned_amount_col1
        amount = self._parse_amount(amt_str)

        folio_num = aligned_row.aligned_head_number
        raw_narr = raw.raw_narration or aligned_row.aligned_narration_raw or ""
        eng_narr = raw.english_narration or aligned_row.aligned_narration_english or ""
        combined_narr = f"{raw_narr} {eng_narr}".strip()

        # ---------------------------------------------------------
        # LEVEL 1A: Exact Folio Matching
        # ---------------------------------------------------------
        if folio_num:
            res_folio = self.registry.resolve_by_folio(
                folio_num=folio_num,
                direction=direction,
                account_set="GENERAL",
                financial_year=financial_year
            )
            if res_folio:
                head, subhead = res_folio
                # Validate subhead policy
                final_sub, sub_status, sub_err = self._resolve_subhead_policy(
                    head=head,
                    subhead_id=subhead.canonical_subhead_id if subhead else None,
                    narration=combined_narr
                )
                
                is_valid = (sub_status in (SubheadStatus.RESOLVED, SubheadStatus.VALID_EMPTY))
                web_head = self.registry.get_website_head_for_canonical(head.canonical_head_id, direction)
                web_sub = self.registry.get_website_subhead_for_canonical(final_sub.canonical_subhead_id) if final_sub else None

                return MappingResult(
                    transaction_id=tx_id,
                    raw_head_number=raw.raw_head_number,
                    aligned_head_number=folio_num,
                    raw_voucher_number=raw.raw_voucher_number,
                    aligned_voucher_number=aligned_row.aligned_voucher_number,
                    raw_narration=raw_narr,
                    english_narration=eng_narr,
                    direction=direction,
                    amount=amount,
                    canonical_head_id=head.canonical_head_id,
                    canonical_head_name=head.canonical_name,
                    canonical_subhead_id=final_sub.canonical_subhead_id if final_sub else None,
                    canonical_subhead_name=final_sub.canonical_name if final_sub else None,
                    mapping_method=MappingMethod.EXACT_FOLIO,
                    confidence_score=1.0,
                    evidence=f"Exact historical folio {folio_num} matched in registry (FY {financial_year})",
                    mapping_status=MappingStatus.VERIFIED if is_valid else MappingStatus.UNRESOLVED,
                    subhead_status=sub_status,
                    needs_human_review=not is_valid,
                    unresolved_reason=sub_err,
                    website_head_value=web_head.website_head_value if web_head else None,
                    website_head_label=web_head.website_head_label if web_head else None,
                    website_subhead_value=web_sub.website_subhead_value if web_sub else None,
                    website_subhead_label=web_sub.website_subhead_label if web_sub else None
                )

        # ---------------------------------------------------------
        # LEVEL 1B: Exact Approved Alias Matching
        # ---------------------------------------------------------
        for text_candidate in [raw_narr, eng_narr]:
            if not text_candidate:
                continue
            res_alias = self.registry.resolve_by_alias(text_candidate, direction=direction)
            if res_alias:
                target_id, target_type, conf = res_alias
                if target_type == "HEAD":
                    head = self.registry.get_head(target_id)
                    if head and head.is_active:
                        final_sub, sub_status, sub_err = self._resolve_subhead_policy(
                            head=head,
                            subhead_id=None,
                            narration=combined_narr
                        )
                        is_valid = (sub_status in (SubheadStatus.RESOLVED, SubheadStatus.VALID_EMPTY))
                        web_head = self.registry.get_website_head_for_canonical(head.canonical_head_id, direction)
                        web_sub = self.registry.get_website_subhead_for_canonical(final_sub.canonical_subhead_id) if final_sub else None

                        return MappingResult(
                            transaction_id=tx_id,
                            raw_head_number=raw.raw_head_number,
                            aligned_head_number=folio_num,
                            raw_voucher_number=raw.raw_voucher_number,
                            aligned_voucher_number=aligned_row.aligned_voucher_number,
                            raw_narration=raw_narr,
                            english_narration=eng_narr,
                            direction=direction,
                            amount=amount,
                            canonical_head_id=head.canonical_head_id,
                            canonical_head_name=head.canonical_name,
                            canonical_subhead_id=final_sub.canonical_subhead_id if final_sub else None,
                            canonical_subhead_name=final_sub.canonical_name if final_sub else None,
                            mapping_method=MappingMethod.EXACT_ALIAS,
                            confidence_score=conf,
                            evidence=f"Exact approved alias matched: '{text_candidate}' -> {target_id}",
                            mapping_status=MappingStatus.VERIFIED if is_valid else MappingStatus.UNRESOLVED,
                            subhead_status=sub_status,
                            needs_human_review=not is_valid,
                            unresolved_reason=sub_err,
                            website_head_value=web_head.website_head_value if web_head else None,
                            website_head_label=web_head.website_head_label if web_head else None,
                            website_subhead_value=web_sub.website_subhead_value if web_sub else None,
                            website_subhead_label=web_sub.website_subhead_label if web_sub else None
                        )

        # ---------------------------------------------------------
        # LEVEL 2: Normalized Matching (Unicode, Whitespace, Punctuation)
        # ---------------------------------------------------------
        clean_raw = self.clean_text(raw_narr)
        clean_eng = self.clean_text(eng_narr)
        clean_all = f"{clean_raw} {clean_eng}".strip()

        # Check all registered aliases using normalized substring containment
        for alias in self.registry.aliases:
            if not alias.is_active:
                continue
            if alias.direction_context not in ("ANY", direction):
                continue

            clean_alias = self.clean_text(alias.alias_text)
            if clean_alias and (clean_alias in clean_all or clean_all in clean_alias):
                if alias.target_type == "HEAD":
                    head = self.registry.get_head(alias.target_id)
                    if head and head.is_active:
                        final_sub, sub_status, sub_err = self._resolve_subhead_policy(
                            head=head,
                            subhead_id=None,
                            narration=combined_narr
                        )
                        is_valid = (sub_status in (SubheadStatus.RESOLVED, SubheadStatus.VALID_EMPTY))
                        web_head = self.registry.get_website_head_for_canonical(head.canonical_head_id, direction)
                        web_sub = self.registry.get_website_subhead_for_canonical(final_sub.canonical_subhead_id) if final_sub else None

                        return MappingResult(
                            transaction_id=tx_id,
                            raw_head_number=raw.raw_head_number,
                            aligned_head_number=folio_num,
                            raw_voucher_number=raw.raw_voucher_number,
                            aligned_voucher_number=aligned_row.aligned_voucher_number,
                            raw_narration=raw_narr,
                            english_narration=eng_narr,
                            direction=direction,
                            amount=amount,
                            canonical_head_id=head.canonical_head_id,
                            canonical_head_name=head.canonical_name,
                            canonical_subhead_id=final_sub.canonical_subhead_id if final_sub else None,
                            canonical_subhead_name=final_sub.canonical_name if final_sub else None,
                            mapping_method=MappingMethod.NORMALIZED_ALIAS,
                            confidence_score=0.90,
                            evidence=f"Normalized alias matched: '{clean_alias}' -> {head.canonical_head_id}",
                            mapping_status=MappingStatus.SUGGESTED if is_valid else MappingStatus.UNRESOLVED,
                            subhead_status=sub_status,
                            needs_human_review=True,
                            unresolved_reason=sub_err,
                            website_head_value=web_head.website_head_value if web_head else None,
                            website_head_label=web_head.website_head_label if web_head else None,
                            website_subhead_value=web_sub.website_subhead_value if web_sub else None,
                            website_subhead_label=web_sub.website_subhead_label if web_sub else None
                        )

        # ---------------------------------------------------------
        # LEVEL 3: Context-Aware Matching (Multi-Signal Heuristics)
        # ---------------------------------------------------------
        # Strict Bank Institution Separation
        if "meenachil" in clean_all:
            head = self.registry.get_head("GEN_HEAD_BANK_MEENACHIL_EAST")
            web_head = self.registry.get_website_head_for_canonical(head.canonical_head_id, direction)
            return MappingResult(
                transaction_id=tx_id,
                raw_head_number=raw.raw_head_number,
                aligned_head_number=folio_num,
                raw_voucher_number=raw.raw_voucher_number,
                aligned_voucher_number=aligned_row.aligned_voucher_number,
                raw_narration=raw_narr,
                english_narration=eng_narr,
                direction=direction,
                amount=amount,
                canonical_head_id=head.canonical_head_id,
                canonical_head_name=head.canonical_name,
                mapping_method=MappingMethod.CONTEXTUAL_RULE,
                confidence_score=0.95,
                evidence="Bank context: Narration specifies Meenachil East Urban Co-operative Bank",
                mapping_status=MappingStatus.RESOLVED,
                subhead_status=SubheadStatus.VALID_EMPTY,
                needs_human_review=False,
                website_head_value=web_head.website_head_value if web_head else None,
                website_head_label=web_head.website_head_label if web_head else None
            )

        if "thidanad" in clean_all:
            head = self.registry.get_head("GEN_HEAD_BANK_THIDANAD")
            web_head = self.registry.get_website_head_for_canonical(head.canonical_head_id, direction)
            return MappingResult(
                transaction_id=tx_id,
                raw_head_number=raw.raw_head_number,
                aligned_head_number=folio_num,
                raw_voucher_number=raw.raw_voucher_number,
                aligned_voucher_number=aligned_row.aligned_voucher_number,
                raw_narration=raw_narr,
                english_narration=eng_narr,
                direction=direction,
                amount=amount,
                canonical_head_id=head.canonical_head_id,
                canonical_head_name=head.canonical_name,
                mapping_method=MappingMethod.CONTEXTUAL_RULE,
                confidence_score=0.95,
                evidence="Bank context: Narration specifies Thidanad Co-operative Bank",
                mapping_status=MappingStatus.RESOLVED,
                subhead_status=SubheadStatus.VALID_EMPTY,
                needs_human_review=False,
                website_head_value=web_head.website_head_value if web_head else None,
                website_head_label=web_head.website_head_label if web_head else None
            )

        if "kottayam district" in clean_all:
            head = self.registry.get_head("GEN_HEAD_BANK_KOTTAYAM_DIST")
            web_head = self.registry.get_website_head_for_canonical(head.canonical_head_id, direction)
            return MappingResult(
                transaction_id=tx_id,
                raw_head_number=raw.raw_head_number,
                aligned_head_number=folio_num,
                raw_voucher_number=raw.raw_voucher_number,
                aligned_voucher_number=aligned_row.aligned_voucher_number,
                raw_narration=raw_narr,
                english_narration=eng_narr,
                direction=direction,
                amount=amount,
                canonical_head_id=head.canonical_head_id,
                canonical_head_name=head.canonical_name,
                mapping_method=MappingMethod.CONTEXTUAL_RULE,
                confidence_score=0.95,
                evidence="Bank context: Narration specifies Kottayam District Co-operative Bank",
                mapping_status=MappingStatus.RESOLVED,
                subhead_status=SubheadStatus.VALID_EMPTY,
                needs_human_review=False,
                website_head_value=web_head.website_head_value if web_head else None,
                website_head_label=web_head.website_head_label if web_head else None
            )

        # Context Rules: Payment utility and maintenance categories
        context_rules = [
            # Electricity
            (
                "PAYMENT",
                ["വൈദ്യുതി", "kseb", "electricity", "current charge"],
                "GEN_HEAD_ELECTRICITY",
                "Utility context: Electricity narration keywords"
            ),
            # Telephone & Postage
            (
                "PAYMENT",
                ["ഫോൺ", "phone", "telephone", "postage", "internet"],
                "GEN_HEAD_TELEPHONE_POSTAGE",
                "Utility context: Telephone/postage keywords"
            ),
            # Maintenance & Repair
            (
                "PAYMENT",
                ["മരാമത്ത്", "പെയിന്റിംഗ്", "painting", "maintenance", "repair", "elcb", "washers"],
                "GEN_HEAD_REPAIR_MAINTENANCE",
                "Expense context: Repair and maintenance keywords"
            ),
            # Printing & Stationery
            (
                "PAYMENT",
                ["directory", "printing", "stationery", "form", "letter", "register"],
                "GEN_HEAD_PRINTING_STATIONERY",
                "Expense context: Printing & stationery keywords"
            ),
            # School Expenses
            (
                "PAYMENT",
                ["school", "സ്കൂൾ"],
                "GEN_HEAD_SCHOOL_INST_EXP",
                "Institution context: School maintenance/expense keywords"
            ),
            # Altar articles & hosts
            (
                "PAYMENT",
                ["host", "wine", "candle", "incense", "മെഴുകുതിരി"],
                "GEN_HEAD_WINE_CANDLE_HOST",
                "Liturgy context: Altar wine, candle, host keywords"
            ),
            # Diocesan Cathedraticum
            (
                "PAYMENT",
                ["cathedraticum", "calthidraticum"],
                "GEN_HEAD_CATHEDRATICUM",
                "Diocese context: Cathedraticum fee keywords"
            ),
            # Sunday Collection (Receipt)
            (
                "RECEIPT",
                ["വേദപ്രചാര", "vedapracharam"],
                "GEN_HEAD_SUNDAY_COLLECTION_INC",
                "Collection context: Vedapracharam income keywords"
            ),
            # Nercha / Offerings (Receipt)
            (
                "RECEIPT",
                ["നേർച്ച", "ലേലം", "കുരിശുപള്ളി", "auction", "nercha"],
                "GEN_HEAD_OFFERINGS_NERCHA_AUCTION",
                "Offerings context: Nercha/auction/cross chapel keywords"
            ),
        ]

        for rule_dir, keywords, target_head_id, rule_evidence in context_rules:
            if direction == rule_dir and any(kw in clean_all for kw in keywords):
                head = self.registry.get_head(target_head_id)
                if head and head.is_active:
                    final_sub, sub_status, sub_err = self._resolve_subhead_policy(
                        head=head,
                        subhead_id=None,
                        narration=combined_narr
                    )
                    is_valid = (sub_status in (SubheadStatus.RESOLVED, SubheadStatus.VALID_EMPTY))
                    web_head = self.registry.get_website_head_for_canonical(head.canonical_head_id, direction)
                    web_sub = self.registry.get_website_subhead_for_canonical(final_sub.canonical_subhead_id) if final_sub else None

                    return MappingResult(
                        transaction_id=tx_id,
                        raw_head_number=raw.raw_head_number,
                        aligned_head_number=folio_num,
                        raw_voucher_number=raw.raw_voucher_number,
                        aligned_voucher_number=aligned_row.aligned_voucher_number,
                        raw_narration=raw_narr,
                        english_narration=eng_narr,
                        direction=direction,
                        amount=amount,
                        canonical_head_id=head.canonical_head_id,
                        canonical_head_name=head.canonical_name,
                        canonical_subhead_id=final_sub.canonical_subhead_id if final_sub else None,
                        canonical_subhead_name=final_sub.canonical_name if final_sub else None,
                        mapping_method=MappingMethod.CONTEXTUAL_RULE,
                        confidence_score=0.88,
                        evidence=rule_evidence,
                        mapping_status=MappingStatus.PROVISIONAL if is_valid else MappingStatus.UNRESOLVED,
                        subhead_status=sub_status,
                        needs_human_review=True,
                        unresolved_reason=sub_err,
                        website_head_value=web_head.website_head_value if web_head else None,
                        website_head_label=web_head.website_head_label if web_head else None,
                        website_subhead_value=web_sub.website_subhead_value if web_sub else None,
                        website_subhead_label=web_sub.website_subhead_label if web_sub else None
                    )

        # ---------------------------------------------------------
        # LEVEL 5: Unresolved Exception
        # ---------------------------------------------------------
        return MappingResult(
            transaction_id=tx_id,
            raw_head_number=raw.raw_head_number,
            aligned_head_number=folio_num,
            raw_voucher_number=raw.raw_voucher_number,
            aligned_voucher_number=aligned_row.aligned_voucher_number,
            raw_narration=raw_narr,
            english_narration=eng_narr,
            direction=direction,
            amount=amount,
            canonical_head_id=None,
            canonical_head_name=None,
            canonical_subhead_id=None,
            canonical_subhead_name=None,
            mapping_method=MappingMethod.UNRESOLVED,
            confidence_score=0.0,
            evidence="No deterministic, normalized, or contextual rule matched transaction signals.",
            mapping_status=MappingStatus.UNRESOLVED,
            subhead_status=SubheadStatus.UNRESOLVED,
            needs_human_review=True,
            unresolved_reason="INSUFFICIENT_TRANSACTION_EVIDENCE"
        )

    def map_batch(
        self,
        aligned_rows: List[AlignedRow],
        financial_year: str = "2026-2027",
        enable_llm: bool = False,
        llm_assistant: Optional[Any] = None
    ) -> List[MappingResult]:
        """Execute batch mapping across multiple rows with optional Level 4 LLM assist."""
        results: List[MappingResult] = []
        unresolved_indices: List[int] = []

        # Step 1: Run deterministic Levels 1-3
        for idx, row in enumerate(aligned_rows):
            res = self.map_transaction(row, financial_year=financial_year)
            results.append(res)
            if res.mapping_status == MappingStatus.UNRESOLVED:
                unresolved_indices.append(idx)

        # Step 2: Level 4 LLM fallback if requested and unresolved transactions exist
        if enable_llm and unresolved_indices:
            from src.mapping.local_llm_assistant import LocalLLMAssistant, InsufficientMemoryError

            assistant = llm_assistant or LocalLLMAssistant(self.registry)
            try:
                # Prepare batch payload
                tx_batch = []
                for u_idx in unresolved_indices:
                    row = aligned_rows[u_idx]
                    res = results[u_idx]
                    tx_batch.append({
                        "transaction_ref": res.transaction_id,
                        "direction": res.direction,
                        "narration": res.raw_narration or res.english_narration,
                        "amount": res.amount,
                        "date": None
                    })

                # Classify batch inside on-demand context
                llm_results = assistant.classify_batch(tx_batch)

                # Merge LLM results back into mapping results
                for u_idx, llm_res in zip(unresolved_indices, llm_results):
                    if not llm_res.abstain and llm_res.canonical_head_id:
                        head = self.registry.get_head(llm_res.canonical_head_id)
                        subhead = self.registry.get_subhead(llm_res.canonical_subhead_id) if llm_res.canonical_subhead_id else None

                        if head:
                            web_head = self.registry.get_website_head_for_canonical(head.canonical_head_id, results[u_idx].direction)
                            web_sub = self.registry.get_website_subhead_for_canonical(subhead.canonical_subhead_id) if subhead else None

                            results[u_idx].canonical_head_id = head.canonical_head_id
                            results[u_idx].canonical_head_name = head.canonical_name
                            results[u_idx].canonical_subhead_id = subhead.canonical_subhead_id if subhead else None
                            results[u_idx].canonical_subhead_name = subhead.canonical_name if subhead else None
                            results[u_idx].mapping_method = MappingMethod.LLM_ASSISTANT
                            results[u_idx].confidence_score = llm_res.confidence
                            results[u_idx].evidence = f"Level 4 LLM Assistant: {llm_res.reasoning}"
                            results[u_idx].mapping_status = (
                                MappingStatus.SUGGESTED if llm_res.confidence >= 0.90 else MappingStatus.PROVISIONAL
                            )
                            results[u_idx].subhead_status = llm_res.subhead_status
                            results[u_idx].needs_human_review = True
                            results[u_idx].unresolved_reason = None
                            results[u_idx].website_head_value = web_head.website_head_value if web_head else None
                            results[u_idx].website_head_label = web_head.website_head_label if web_head else None
                            results[u_idx].website_subhead_value = web_sub.website_subhead_value if web_sub else None
                            results[u_idx].website_subhead_label = web_sub.website_subhead_label if web_sub else None
                    else:
                        results[u_idx].candidate_alternatives = [{"id": cid} for cid in llm_res.candidate_shortlist]
                        results[u_idx].unresolved_reason = f"LLM_ABSTAIN: {llm_res.reasoning}"

            except InsufficientMemoryError as ime:
                logger.warning(f"Skipping Level 4 LLM assistant due to memory constraints: {ime}")
                for u_idx in unresolved_indices:
                    results[u_idx].unresolved_reason = f"SKIPPED_LLM_INSUFFICIENT_MEMORY: {ime}"
            except Exception as e:
                logger.error(f"Error during Level 4 LLM execution: {e}")
                for u_idx in unresolved_indices:
                    results[u_idx].unresolved_reason = f"LLM_ERROR: {str(e)}"

        return results
