"""Review manager and data structures for Cashbook AI review intelligence.

Prepares transactions for human review, groups identical or similar unresolved items,
and manages the confirmation/rejection lifecycle.

Critical rule:
Only explicitly user-confirmed corrections may become permanent aliases.
Never automatically promote model predictions into canonical registry or approved aliases.
"""

import json
import logging
import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any, Set
from pydantic import BaseModel, Field

from src.models import RawExtractedRow
from src.extraction.column_aligner import AlignedRow
from src.reference.canonical_registry import CanonicalRegistry, ApprovedAlias, SubheadPolicy
from src.mapping.hybrid_engine import MappingResult, MappingStatus, MappingMethod

logger = logging.getLogger(__name__)


class ReviewAction(str, Enum):
    """User review decision on a transaction mapping."""
    CONFIRM = "CONFIRM"          # User approved the suggested/provisional mapping
    REJECT = "REJECT"            # User rejected the suggestion; marks back to UNRESOLVED
    OVERRIDE = "OVERRIDE"        # User explicitly chose a different head/subhead
    DEFER = "DEFER"              # User deferred decision for later investigation
    EDIT = "EDIT"                # User edited one or more transaction fields


class ReviewItem(BaseModel):
    """Rich presentation model for a transaction requiring or having undergone review."""
    transaction_id: str
    source_image: str
    source_page: str
    source_row: int
    raw_head_number: Optional[str] = None
    raw_voucher_number: Optional[str] = None
    voucher_number: Optional[str] = None
    raw_date: Optional[str] = None
    date: Optional[str] = None
    raw_narration: str = ""
    english_narration: Optional[str] = None
    direction: str  # "RECEIPT" or "PAYMENT"
    amount: Optional[float] = None

    # Suggested classification
    mapping_status: MappingStatus = MappingStatus.UNRESOLVED
    mapping_method: MappingMethod = MappingMethod.UNRESOLVED
    canonical_head_id: Optional[str] = None
    canonical_head_name: Optional[str] = None
    canonical_subhead_id: Optional[str] = None
    canonical_subhead_name: Optional[str] = None
    confidence_score: float = 0.0
    evidence: str = ""
    candidate_alternatives: List[Dict[str, Any]] = Field(default_factory=list)
    needs_human_review: bool = True

    # User review decision
    review_action: Optional[ReviewAction] = None
    confirmed_head_id: Optional[str] = None
    confirmed_subhead_id: Optional[str] = None
    reviewed_by: Optional[str] = None
    review_timestamp: Optional[str] = None
    review_notes: str = ""
    alias_promoted: bool = False

    # Source evidence preservation & audit trail
    original_extracted_data: Optional[Dict[str, Any]] = None
    audit_history: List[Dict[str, Any]] = Field(default_factory=list)


class ReviewGroup(BaseModel):
    """A cluster of similar or identical unresolved/suggested transactions."""
    group_key: str
    group_type: str  # "IDENTICAL_NARRATION", "COMMON_CATEGORY", "SIMILAR_HEAD"
    transaction_ids: List[str] = Field(default_factory=list)
    transaction_count: int = 0
    suggested_head_id: Optional[str] = None
    suggested_head_name: Optional[str] = None
    sample_narrations: List[str] = Field(default_factory=list)
    total_amount: float = 0.0


class ReviewManager:
    """Manages the review queue, grouping, and human confirmation lifecycle."""

    def __init__(self, registry: CanonicalRegistry):
        self.registry = registry

    def build_review_item(
        self,
        mapping_result: MappingResult,
        raw_row: Optional[RawExtractedRow] = None,
        aligned_row: Optional[AlignedRow] = None
    ) -> ReviewItem:
        """Construct a complete ReviewItem from mapping and extraction results."""
        src_img = ""
        src_pg = "1"
        src_row = 1
        r_date = None
        r_vouch = mapping_result.raw_voucher_number

        if aligned_row and aligned_row.raw_row:
            src_img = aligned_row.raw_row.source_image
            src_pg = aligned_row.raw_row.source_page
            src_row = aligned_row.raw_row.source_row
            r_date = aligned_row.aligned_date or aligned_row.raw_row.raw_date
            r_vouch = aligned_row.aligned_voucher_number or mapping_result.raw_voucher_number
        elif raw_row:
            src_img = raw_row.source_image
            src_pg = raw_row.source_page
            src_row = raw_row.source_row
            r_date = raw_row.raw_date
            r_vouch = raw_row.raw_voucher_number

        # Resolve formatted date if possible
        resolved_date = r_date
        if r_date and re.match(r"^\d{1,2}[\.\/\-]\d{1,2}([\.\/\-]\d{2,4})?$", str(r_date)):
            parts = re.split(r"[\.\/\-]", str(r_date))
            if len(parts) >= 3:
                day = int(parts[0])
                month = int(parts[1])
                yr = int(parts[2])
                if yr < 100:
                    yr += 2000
                resolved_date = f"{yr:04d}-{month:02d}-{day:02d}"

        orig_data = {
            "date": resolved_date or r_date,
            "raw_date": r_date,
            "voucher_number": r_vouch,
            "raw_voucher_number": mapping_result.raw_voucher_number,
            "amount": mapping_result.amount,
            "raw_head_number": mapping_result.raw_head_number,
            "canonical_head_id": mapping_result.canonical_head_id,
            "canonical_head_name": mapping_result.canonical_head_name,
            "canonical_subhead_id": mapping_result.canonical_subhead_id,
            "canonical_subhead_name": mapping_result.canonical_subhead_name,
            "narration": mapping_result.raw_narration or "",
            "english_narration": mapping_result.english_narration or "",
            "direction": mapping_result.direction,
        }

        return ReviewItem(
            transaction_id=mapping_result.transaction_id,
            source_image=src_img,
            source_page=src_pg,
            source_row=src_row,
            raw_head_number=mapping_result.raw_head_number,
            raw_voucher_number=mapping_result.raw_voucher_number,
            voucher_number=r_vouch,
            raw_date=r_date,
            date=resolved_date,
            raw_narration=mapping_result.raw_narration or "",
            english_narration=mapping_result.english_narration,
            direction=mapping_result.direction,
            amount=mapping_result.amount,
            mapping_status=mapping_result.mapping_status,
            mapping_method=mapping_result.mapping_method,
            canonical_head_id=mapping_result.canonical_head_id,
            canonical_head_name=mapping_result.canonical_head_name,
            canonical_subhead_id=mapping_result.canonical_subhead_id,
            canonical_subhead_name=mapping_result.canonical_subhead_name,
            confidence_score=mapping_result.confidence_score,
            evidence=mapping_result.evidence,
            candidate_alternatives=mapping_result.candidate_alternatives,
            needs_human_review=mapping_result.needs_human_review,
            original_extracted_data=orig_data,
            audit_history=[]
        )

    def group_similar_transactions(
        self,
        items: List[ReviewItem],
        only_needing_review: bool = True
    ) -> List[ReviewGroup]:
        """Group similar transactions by normalized narration to avoid repetitive review.
        
        For example: multiple rows with 'Donation for Holy Mass' or 'Catechism'
        are clustered into a single ReviewGroup.
        """
        target_items = [it for it in items if it.needs_human_review] if only_needing_review else items
        groups_dict: Dict[str, List[ReviewItem]] = {}

        for it in target_items:
            # Group key based on normalized narration
            norm_key = " ".join(it.raw_narration.lower().strip().split())
            if not norm_key:
                norm_key = f"DIRECTION_{it.direction}_UNSPECIFIED"
            groups_dict.setdefault(norm_key, []).append(it)

        groups: List[ReviewGroup] = []
        for key, g_items in sorted(groups_dict.items(), key=lambda x: len(x[1]), reverse=True):
            tot_amt = sum(it.amount or 0.0 for it in g_items)
            suggested_hid = g_items[0].canonical_head_id
            suggested_hname = g_items[0].canonical_head_name
            sample_narrs = list({it.raw_narration for it in g_items})[:3]

            groups.append(ReviewGroup(
                group_key=key,
                group_type="IDENTICAL_NARRATION" if len(g_items) > 1 else "SINGLETON",
                transaction_ids=[it.transaction_id for it in g_items],
                transaction_count=len(g_items),
                suggested_head_id=suggested_hid,
                suggested_head_name=suggested_hname,
                sample_narrations=sample_narrs,
                total_amount=round(tot_amt, 2)
            ))

        return groups

    # Convenient alias for group_similar_transactions
    group_review_items = group_similar_transactions

    def apply_user_decision(
        self,
        item: ReviewItem,
        action: ReviewAction,
        reviewed_by: str,
        timestamp: str,
        confirmed_head_id: Optional[str] = None,
        confirmed_subhead_id: Optional[str] = None,
        notes: str = "",
        promote_to_alias: bool = False
    ) -> ReviewItem:
        """Apply human decision, transitioning mapping status to VERIFIED or UNRESOLVED."""
        item.review_action = action
        item.reviewed_by = reviewed_by
        item.review_timestamp = timestamp
        item.review_notes = notes

        if action == ReviewAction.CONFIRM:
            # User confirmed the suggested/provisional mapping
            item.confirmed_head_id = item.canonical_head_id
            item.confirmed_subhead_id = item.canonical_subhead_id
            item.mapping_status = MappingStatus.VERIFIED
            item.needs_human_review = False
            item.evidence = f"Human verified by {reviewed_by} at {timestamp}: {notes}"

        elif action == ReviewAction.OVERRIDE:
            # User explicitly assigned a head
            if not confirmed_head_id or not self.registry.get_head(confirmed_head_id):
                raise ValueError(f"Invalid head ID '{confirmed_head_id}' for override")

            head_obj = self.registry.get_head(confirmed_head_id)
            item.canonical_head_id = head_obj.canonical_head_id
            item.canonical_head_name = head_obj.canonical_name
            item.confirmed_head_id = head_obj.canonical_head_id

            if confirmed_subhead_id:
                sub_obj = self.registry.get_subhead(confirmed_subhead_id)
                item.canonical_subhead_id = sub_obj.canonical_subhead_id if sub_obj else None
                item.canonical_subhead_name = sub_obj.canonical_name if sub_obj else None
                item.confirmed_subhead_id = item.canonical_subhead_id

            item.mapping_status = MappingStatus.VERIFIED
            item.needs_human_review = False
            item.evidence = f"Human override by {reviewed_by} to {confirmed_head_id} at {timestamp}: {notes}"

        elif action == ReviewAction.REJECT:
            # User rejected the suggestion; reset to UNRESOLVED
            item.canonical_head_id = None
            item.canonical_head_name = None
            item.canonical_subhead_id = None
            item.canonical_subhead_name = None
            item.mapping_status = MappingStatus.UNRESOLVED
            item.needs_human_review = True
            item.evidence = f"Suggestion rejected by {reviewed_by} at {timestamp}: {notes}"

        elif action == ReviewAction.DEFER:
            item.needs_human_review = True
            item.evidence = f"Deferred by {reviewed_by} at {timestamp}: {notes}"

        # Promote to permanent alias ONLY if explicitly requested by human
        if promote_to_alias and action in (ReviewAction.CONFIRM, ReviewAction.OVERRIDE) and item.confirmed_head_id:
            alias_text = item.raw_narration.strip()
            if alias_text:
                self.promote_confirmed_alias(
                    alias_text=alias_text,
                    target_id=item.confirmed_head_id,
                    target_type="HEAD",
                    source=f"Human review confirmation by {reviewed_by} ({item.transaction_id})"
                )
                item.alias_promoted = True

        return item

    def apply_edit(
        self,
        item: ReviewItem,
        date: str,
        voucher_number: str,
        amount: float,
        canonical_head_id: str,
        canonical_subhead_id: Optional[str] = None,
        narration: str = "",
        direction: str = "RECEIPT",
        editor: str = "Accountant",
        timestamp: Optional[str] = None
    ) -> ReviewItem:
        """Apply human edits to transaction fields with strict accounting validation.

        Preserves original extraction, records field-level audit trail,
        validates canonical head and subhead policy, and keeps transaction
        in awaiting-confirmation state (needs_human_review = True).
        """
        now_iso = timestamp or datetime.now(timezone.utc).isoformat()

        # 1. Validate Direction
        dir_clean = direction.upper().strip()
        if dir_clean not in ("RECEIPT", "PAYMENT"):
            raise ValueError(f"Invalid transaction direction '{direction}'. Must be RECEIPT or PAYMENT.")

        # 2. Validate Amount
        try:
            amt_val = float(amount)
            if amt_val <= 0.0:
                raise ValueError("Amount must be a positive number greater than 0")
        except (TypeError, ValueError) as e:
            raise ValueError(f"Invalid amount '{amount}': {e}")

        # 3. Validate Date
        date_clean = (date or "").strip()
        if not date_clean or not (re.match(r"^\d{4}-\d{2}-\d{2}$", date_clean) or re.match(r"^\d{1,2}[\.\/\-]\d{1,2}([\.\/\-]\d{2,4})?$", date_clean)):
            raise ValueError(f"Invalid date format '{date}'. Expected YYYY-MM-DD or DD.MM.YYYY.")

        # 4. Validate Canonical Head
        head = self.registry.get_head(canonical_head_id)
        if not head:
            raise ValueError(f"Canonical head ID '{canonical_head_id}' does not exist in registry.")
        if not head.is_active:
            raise ValueError(f"Canonical head '{head.canonical_name}' is inactive and cannot be assigned.")

        # Direction check
        if head.primary_direction not in ("BOTH", dir_clean):
            raise ValueError(
                f"Head '{head.canonical_name}' has primary direction '{head.primary_direction}', "
                f"which is incompatible with transaction direction '{dir_clean}'."
            )

        # 5. Validate Subhead Policy
        subhead_obj = None
        if head.subhead_policy == SubheadPolicy.MANDATORY:
            if not canonical_subhead_id:
                raise ValueError(f"Head '{head.canonical_name}' requires a mandatory subhead selection.")
            subhead_obj = self.registry.get_subhead(canonical_subhead_id)
            if not subhead_obj:
                raise ValueError(f"Subhead ID '{canonical_subhead_id}' not found in registry.")
            if subhead_obj.parent_head_id != head.canonical_head_id:
                raise ValueError(
                    f"Subhead '{subhead_obj.canonical_name}' belongs to '{subhead_obj.parent_head_id}', "
                    f"not '{head.canonical_head_id}'."
                )
        elif head.subhead_policy == SubheadPolicy.PROHIBITED:
            if canonical_subhead_id:
                raise ValueError(f"Head '{head.canonical_name}' strictly prohibits subheads, but got '{canonical_subhead_id}'.")
            canonical_subhead_id = None
        elif head.subhead_policy == SubheadPolicy.OPTIONAL:
            if canonical_subhead_id:
                subhead_obj = self.registry.get_subhead(canonical_subhead_id)
                if not subhead_obj:
                    raise ValueError(f"Optional subhead ID '{canonical_subhead_id}' not found in registry.")
                if subhead_obj.parent_head_id != head.canonical_head_id:
                    raise ValueError(f"Subhead belongs to '{subhead_obj.parent_head_id}', not '{head.canonical_head_id}'.")

        # 6. Ensure original extracted data is preserved
        if not item.original_extracted_data:
            item.original_extracted_data = {
                "date": item.date or item.raw_date,
                "raw_date": item.raw_date,
                "voucher_number": item.voucher_number or item.raw_voucher_number,
                "raw_voucher_number": item.raw_voucher_number,
                "amount": item.amount,
                "raw_head_number": item.raw_head_number,
                "canonical_head_id": item.canonical_head_id,
                "canonical_head_name": item.canonical_head_name,
                "canonical_subhead_id": item.canonical_subhead_id,
                "canonical_subhead_name": item.canonical_subhead_name,
                "narration": item.raw_narration,
                "english_narration": item.english_narration,
                "direction": item.direction,
            }

        # 7. Record field-level audit trail
        field_comparisons = [
            ("date", item.date or item.raw_date, date_clean),
            ("voucher_number", item.voucher_number or item.raw_voucher_number, voucher_number.strip()),
            ("amount", str(item.amount) if item.amount is not None else None, str(amt_val)),
            ("canonical_head_id", item.canonical_head_id, head.canonical_head_id),
            ("canonical_subhead_id", item.canonical_subhead_id, subhead_obj.canonical_subhead_id if subhead_obj else None),
            ("narration", item.raw_narration, narration.strip()),
            ("direction", item.direction, dir_clean),
        ]

        for field_name, old_val, new_val in field_comparisons:
            if str(old_val or "") != str(new_val or ""):
                item.audit_history.append({
                    "transaction_id": item.transaction_id,
                    "field_changed": field_name,
                    "previous_value": old_val,
                    "new_value": new_val,
                    "timestamp": now_iso,
                    "correction_source": editor,
                })

        # 8. Mutate draft fields
        item.date = date_clean
        item.raw_date = date_clean
        item.voucher_number = voucher_number.strip()
        item.raw_voucher_number = voucher_number.strip()
        item.amount = amt_val
        item.canonical_head_id = head.canonical_head_id
        item.canonical_head_name = head.canonical_name
        item.canonical_subhead_id = subhead_obj.canonical_subhead_id if subhead_obj else None
        item.canonical_subhead_name = subhead_obj.canonical_name if subhead_obj else None
        item.raw_narration = narration.strip()
        item.direction = dir_clean

        # User requirement 1.5:
        # "saving an edit is NOT equivalent to approving the transaction.
        # The edited transaction must remain awaiting explicit confirmation."
        item.needs_human_review = True
        item.review_action = ReviewAction.EDIT
        item.confirmed_head_id = None
        item.confirmed_subhead_id = None
        item.mapping_status = MappingStatus.SUGGESTED
        item.confidence_score = 1.0
        item.evidence = f"Draft edited by {editor} at {now_iso}"

        return item

    def promote_confirmed_alias(
        self,
        alias_text: str,
        target_id: str,
        target_type: str = "HEAD",
        direction_context: str = "ANY",
        language: str = "EN",
        source: str = "Human Review"
    ) -> ApprovedAlias:
        """Persist a human-confirmed alias into CanonicalRegistry.
        
        Strict rule: This method is only callable upon explicit human review action.
        """
        alias_id = f"ALIAS_CONFIRMED_{len(self.registry.aliases) + 1:04d}"
        new_alias = ApprovedAlias(
            alias_id=alias_id,
            target_id=target_id,
            target_type=target_type,
            alias_text=alias_text,
            language=language,
            direction_context=direction_context,
            source=source,
            is_active=True
        )
        self.registry.aliases.append(new_alias)
        norm = self.registry._normalize_text(alias_text)
        self.registry._alias_index[(norm, direction_context)] = new_alias
        if direction_context != "ANY":
            self.registry._alias_index.setdefault((norm, "ANY"), new_alias)

        logger.info(f"Promoted confirmed alias: '{alias_text}' -> {target_id} ({alias_id})")
        return new_alias
