"""Candidate shortlist generator for Cashbook AI Level 4 Assistant.

Produces bounded shortlists (typically 4-6 candidates) of canonical accounting heads
for genuinely ambiguous transactions that could not be resolved by Levels 1-3.
"""

import re
import logging
from typing import List, Dict, Set, Optional, Any
from pydantic import BaseModel, Field

from src.reference.canonical_registry import CanonicalRegistry, CanonicalHead, SubheadPolicy

logger = logging.getLogger(__name__)


class CandidateOption(BaseModel):
    """A single candidate accounting head option presented to LLM or human."""
    canonical_head_id: str
    canonical_name: str
    subhead_policy: str
    description: str
    score: float = 0.0
    matched_terms: List[str] = Field(default_factory=list)


class CandidateGenerator:
    """Bounded candidate shortlist generator for Level 4 LLM classifier."""

    ABSTAIN_ID = "ABSTAIN"

    def __init__(self, registry: CanonicalRegistry):
        self.registry = registry
        self._head_docs: Dict[str, Dict[str, Any]] = {}
        self._build_index()

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        if not text:
            return []
        text = text.lower()
        tokens = re.findall(r"[\w']+", text)
        return [t for t in tokens if len(t) > 1]

    def _build_index(self) -> None:
        """Index canonical heads, approved aliases, subheads, and domain keywords."""
        for hid, head in self.registry.heads.items():
            subheads = self.registry.get_subheads_for_head(hid)
            aliases = [a for a in self.registry.aliases if a.target_id == hid and a.is_active]

            terms: Set[str] = set(self._tokenize(head.canonical_name))
            terms.update(self._tokenize(head.description))

            for a in aliases:
                terms.update(self._tokenize(a.alias_text))

            for s in subheads:
                terms.update(self._tokenize(s.canonical_name))
                s_aliases = [sa for sa in self.registry.aliases if sa.target_id == s.canonical_subhead_id and sa.is_active]
                for sa in s_aliases:
                    terms.update(self._tokenize(sa.alias_text))

            # Domain associations for common parish accounting terminology
            domain_hints: Set[str] = set()
            if "OFFERINGS_SPECIAL_D" in hid or "DONATION_SPECIAL_C" in hid or "DIOCESE_CHARITY" in hid:
                domain_hints.update(["charity", "box", "special", "purpose", "collection", "ദാനപ്പെട്ടി", "പെട്ടി"])
            if "CANDLE" in hid:
                domain_hints.update(["candle", "thiri", "mezhukuthiri", "തിരി"])
            if "FUNERAL" in hid:
                domain_hints.update(["funeral", "burial", "oppees", "മരണം", "ശവസംസ്കാരം"])
            if "NERCHA" in hid or "AUCTION" in hid:
                domain_hints.update(["nercha", "auction", "lelam", "bhandaram", "karisam", "നേർച്ച", "ലേലം", "ഭണ്ഡാരം"])
            if "OFFERINGS_DAILY" in hid:
                domain_hints.update(["daily", "mass", "holy mass", "donation for holy mass", "കുർബാന", "നിത്യചിലവ്"])
            if "RENT" in hid:
                domain_hints.update(["rent", "shop", "building", "advance", "security", "tenant"])
            if "SCHOOL" in hid:
                domain_hints.update(["school", "catechism", "student", "teacher", "maintenance", "banvel"])
            if "ALLOWANCE" in hid:
                domain_hints.update(["allowance", "vicar", "accountant", "sacristan", "assistant", "sweeper", "salary"])
            if "ELECTRICITY" in hid:
                domain_hints.update(["electricity", "current", "bill", "kseb", "power", "light"])
            if "BANK" in hid or "INTEREST" in hid:
                domain_hints.update(["bank", "interest", "deposit", "south", "indian", "sib", "fd", "sb", "account"])
            if "ORDER" in hid:
                domain_hints.update(["order", "diocese", "fund", "holy", "seminarians", "land"])
            if "MISC" in hid:
                domain_hints.update(["cooly", "charges", "meeting", "petrol", "tea", "seva", "wages", "bolt", "handle", "miscellaneous"])
            if "WINE" in hid or "ARTICLES" in hid:
                domain_hints.update(["wine", "host", "oil", "pooja", "purchase", "items", "church", "articles", "globe"])
            if "PASSARAM" in hid or "MARRIAGE" in hid:
                domain_hints.update(["marriage", "passaram", "certificate", "insurance"])
            if "SUNDAY_SCHOOL" in hid:
                domain_hints.update(["banvel", "scholarship", "catechism", "vts", "teachers", "sunday school"])

            terms.update(domain_hints)

            self._head_docs[hid] = {
                "head": head,
                "terms": terms,
                "subheads": subheads,
                "aliases": aliases
            }

    def generate_shortlist(
        self,
        direction: str,
        narration: Optional[str] = None,
        max_candidates: int = 5,
        include_abstain: bool = True
    ) -> List[CandidateOption]:
        """Generate bounded shortlist of candidate heads.
        
        Args:
            direction: 'RECEIPT' or 'PAYMENT'
            narration: Transaction narration text
            max_candidates: Maximum number of concrete candidates (excluding abstain)
            include_abstain: Whether to append ABSTAIN as the final candidate
            
        Returns:
            List of CandidateOption
        """
        dir_upper = direction.upper().strip()
        narr = narration or ""
        narr_lower = narr.lower()
        tokens = set(self._tokenize(narr))

        scored: List[CandidateOption] = []

        for hid, doc in self._head_docs.items():
            head: CanonicalHead = doc["head"]
            if head.primary_direction not in (dir_upper, "BOTH"):
                continue

            score = 0.0
            matched: List[str] = []

            # 1. Matching tokens
            matching_tokens = tokens.intersection(doc["terms"])
            if matching_tokens:
                score += len(matching_tokens) * 3.0
                matched.extend(sorted(list(matching_tokens)))

            # 2. Substring match in head name
            if head.canonical_name.lower() in narr_lower:
                score += 5.0
                matched.append("head_name")

            # 3. Match in aliases
            for a in doc["aliases"]:
                if a.alias_text.lower() in narr_lower:
                    score += 6.0
                    matched.append(f"alias:{a.alias_text}")

            # 4. Match in subhead names
            for s in doc["subheads"]:
                if s.canonical_name.lower() in narr_lower:
                    score += 5.0
                    matched.append(f"subhead:{s.canonical_name}")

            # 5. Prior for frequent catch-all heads
            if score > 0:
                if hid in ("GEN_HEAD_MISC_EXPENSES", "GEN_HEAD_OFFERINGS_DAILY", "GEN_HEAD_SUNDAY_COLLECTION_INC"):
                    score += 0.5
                scored.append(CandidateOption(
                    canonical_head_id=hid,
                    canonical_name=head.canonical_name,
                    subhead_policy=head.subhead_policy.value,
                    description=head.description,
                    score=round(score, 2),
                    matched_terms=matched
                ))

        # Always guarantee catch-all exists in candidate list if score > 0 or fallback
        catch_all = "GEN_HEAD_OFFERINGS_DAILY" if dir_upper == "RECEIPT" else "GEN_HEAD_MISC_EXPENSES"
        if not any(c.canonical_head_id == catch_all for c in scored):
            if catch_all in self._head_docs:
                doc = self._head_docs[catch_all]
                scored.append(CandidateOption(
                    canonical_head_id=catch_all,
                    canonical_name=doc["head"].canonical_name,
                    subhead_policy=doc["head"].subhead_policy.value,
                    description=doc["head"].description,
                    score=0.5,
                    matched_terms=["catch_all_default"]
                ))

        # Sort descending by score
        scored.sort(key=lambda x: x.score, reverse=True)

        # Institution de-duplication: if narration does not specify a specific bank name,
        # cap generic bank contra heads to 1 to avoid crowding out operational expense heads
        specific_bank_mentioned = any(b in narr_lower for b in ["sib", "south indian", "kappad", "meenachil", "pala", "sbi", "state bank", "thidanad"])
        filtered_candidates: List[CandidateOption] = []
        bank_contra_count = 0

        for c in scored:
            is_bank_contra = c.canonical_head_id.startswith("GEN_HEAD_BANK_") and c.canonical_head_id not in ("GEN_HEAD_BANK_INTEREST", "GEN_HEAD_BANK_CHARGES")
            if is_bank_contra and not specific_bank_mentioned:
                if bank_contra_count >= 1:
                    continue
                bank_contra_count += 1
            filtered_candidates.append(c)

        # Fallback padding if fewer than max_candidates
        if len(filtered_candidates) < max_candidates:
            existing_ids = {c.canonical_head_id for c in filtered_candidates}
            defaults = (
                ["GEN_HEAD_OFFERINGS_DAILY", "GEN_HEAD_SUNDAY_COLLECTION_INC", "GEN_HEAD_ORDER_FROM_DIOCESE", "GEN_HEAD_OFFERINGS_NERCHA_AUCTION", "GEN_HEAD_RENT_BUILDINGS"]
                if dir_upper == "RECEIPT"
                else ["GEN_HEAD_MISC_EXPENSES", "GEN_HEAD_ALLOWANCES", "GEN_HEAD_ELECTRICITY", "GEN_HEAD_SCHOOL_INST_EXP", "GEN_HEAD_REPAIR_MAINTENANCE"]
            )
            for hid in defaults:
                if hid not in existing_ids and hid in self._head_docs:
                    doc = self._head_docs[hid]
                    filtered_candidates.append(CandidateOption(
                        canonical_head_id=hid,
                        canonical_name=doc["head"].canonical_name,
                        subhead_policy=doc["head"].subhead_policy.value,
                        description=doc["head"].description,
                        score=0.1,
                        matched_terms=["default_prior"]
                    ))
                if len(filtered_candidates) >= max_candidates:
                    break

        candidates = filtered_candidates[:max_candidates]

        if include_abstain:
            candidates.append(CandidateOption(
                canonical_head_id=self.ABSTAIN_ID,
                canonical_name="None of the above / Abstain",
                subhead_policy="PROHIBITED",
                description="Use when none of the candidate heads accurately represent the transaction.",
                score=0.0,
                matched_terms=[]
            ))

        return candidates
