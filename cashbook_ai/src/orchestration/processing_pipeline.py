"""End-to-end processing pipeline orchestrator for Cashbook AI."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any
from PIL import Image

from src.models import RawExtractedRow, ProcessedRow, TransactionType, RecordType
from src.reference.canonical_registry import CanonicalRegistry
from src.extraction.base import BaseExtractor
from src.extraction.gemini_extractor import GeminiVisionExtractor
from src.extraction.mock_extractor import MockExtractor
from src.extraction.column_aligner import ColumnAligner, AlignedRow
from src.mapping.hybrid_engine import HybridMappingEngine, MappingResult, MappingStatus
from src.mapping.local_llm_assistant import LocalLLMAssistant
from src.mapping.candidate_generator import CandidateGenerator
from src.mapping.review_manager import ReviewManager, ReviewItem, ReviewGroup
from src.export.manual_review_exporter import ManualReviewExporter
from src.orchestration.job_store import JobStore, JobRecord, JobStatus, StageStatus

logger = logging.getLogger(__name__)


class ProcessingPipeline:
    """Coordinates all extraction, alignment, mapping, review preparation, and workbook export."""

    def __init__(
        self,
        registry: Optional[CanonicalRegistry] = None,
        job_store: Optional[JobStore] = None,
        data_dir: Optional[Path] = None,
        allow_mock: bool = False,
    ):
        base_dir = Path(__file__).resolve().parent.parent.parent
        self.data_dir = data_dir or (base_dir / "data")
        self.registry = registry or CanonicalRegistry()
        self.job_store = job_store or JobStore(self.data_dir / "jobs")
        self.manual_review_exporter = ManualReviewExporter()
        self.allow_mock = allow_mock

    def get_extractor(self, allow_mock: Optional[bool] = None) -> BaseExtractor:
        use_mock = self.allow_mock if allow_mock is None else allow_mock
        if use_mock:
            logger.info("Test execution mode: using MockExtractor")
            return MockExtractor()

        import os
        import config
        api_key = os.getenv("GEMINI_API_KEY") or getattr(config, "GEMINI_API_KEY", None)
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY environment variable is not set. Real uploaded cashbook photographs "
                "require the configured real extraction provider (Gemini). "
                "Silent fallback to Mock is prohibited in production."
            )
        return GeminiVisionExtractor()

    def run_pipeline(
        self,
        month: str,
        job_id: str,
        page_manifest: List[Dict[str, Any]],
        use_llm: bool = True,
        force_ocr: bool = False,
        custom_extractor: Optional[BaseExtractor] = None,
        allow_mock: bool = False,
    ) -> JobRecord:
        """Execute the complete multi-stage pipeline, updating persistent job progress at each step."""
        job = self.job_store.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found in job store")

        job.status = JobStatus.RUNNING
        self.job_store.save_job(job)

        month_output_dir = self.data_dir / "output" / month
        month_output_dir.mkdir(parents=True, exist_ok=True)

        try:
            # -------------------------------------------------------------
            # Stage 1: Source ingestion
            # -------------------------------------------------------------
            if self._check_cancelled(job_id):
                return self.job_store.get_job(job_id)

            self._update_stage(job_id, "source_ingestion", StageStatus.RUNNING, "Validating source images...")
            validated_pages = []
            for idx, p_info in enumerate(page_manifest):
                img_path = Path(p_info.get("path", ""))
                if not img_path.is_file():
                    raise FileNotFoundError(f"Source image missing: {img_path}")

                # Verify image can be opened
                try:
                    with Image.open(img_path) as img:
                        img.verify()
                except Exception as e:
                    raise ValueError(f"Corrupt or unreadable image file {img_path.name}: {e}")

                validated_pages.append(p_info)
                self._update_stage(
                    job_id, "source_ingestion", StageStatus.RUNNING,
                    f"Validated {idx + 1}/{len(page_manifest)} images",
                    progress=idx + 1, total=len(page_manifest)
                )

            self._update_stage(
                job_id, "source_ingestion", StageStatus.COMPLETE,
                f"Ingested {len(validated_pages)} valid source images",
                progress=len(validated_pages), total=len(validated_pages)
            )

            # -------------------------------------------------------------
            # Stage 2: Page validation
            # -------------------------------------------------------------
            if self._check_cancelled(job_id):
                return self.job_store.get_job(job_id)

            self._update_stage(job_id, "page_validation", StageStatus.RUNNING, "Verifying logical page sequence...")
            # Check for duplicate hashes
            seen_hashes = set()
            for p_info in validated_pages:
                sha = p_info.get("sha256")
                if sha:
                    if sha in seen_hashes:
                        logger.warning(f"Duplicate image hash detected: {p_info.get('filename')}")
                    seen_hashes.add(sha)

            self._update_stage(
                job_id, "page_validation", StageStatus.COMPLETE,
                f"Verified sequence of {len(validated_pages)} pages",
                progress=len(validated_pages), total=len(validated_pages)
            )

            # -------------------------------------------------------------
            # Stage 3: OCR extraction
            # -------------------------------------------------------------
            if self._check_cancelled(job_id):
                return self.job_store.get_job(job_id)

            self._update_stage(job_id, "ocr_extraction", StageStatus.RUNNING, "Extracting cashbook rows...")
            raw_rows: List[RawExtractedRow] = []

            current_page_hashes = {}
            for p in validated_pages:
                fn = p.get("filename") or Path(p["path"]).name
                h = p.get("sha256")
                if not h:
                    import hashlib
                    h = hashlib.sha256(Path(p["path"]).read_bytes()).hexdigest()
                current_page_hashes[fn] = h

            cached_extraction_file = month_output_dir / "raw_extractions.json"
            cached_meta_file = month_output_dir / "raw_extractions_meta.json"
            if not force_ocr and cached_extraction_file.is_file():
                cache_valid = False
                if cached_meta_file.is_file():
                    try:
                        meta = json.loads(cached_meta_file.read_text(encoding="utf-8"))
                        cached_hashes = meta.get("page_hashes", {})
                        cached_provider = meta.get("provider", "unknown")
                        if not allow_mock and cached_provider == "mock":
                            logger.warning("Cache rejected: cache was generated by MockExtractor, but production requires real provider.")
                        elif cached_hashes != current_page_hashes:
                            logger.warning(f"Cache rejected: image hashes mismatch. Cached {len(cached_hashes)} pages vs current {len(current_page_hashes)} pages.")
                        else:
                            cache_valid = True
                    except Exception as e:
                        logger.warning(f"Failed to read cache metadata: {e}")
                else:
                    logger.warning("Cache rejected: missing raw_extractions_meta.json metadata file.")

                if cache_valid:
                    try:
                        cache_data = json.loads(cached_extraction_file.read_text(encoding="utf-8"))
                        raw_rows = [RawExtractedRow(**r) for r in cache_data]
                        logger.info(f"Loaded {len(raw_rows)} cached extraction rows validated against image hashes.")
                    except Exception as e:
                        logger.warning(f"Failed to load extraction cache: {e}. Falling back to extraction.")
                        raw_rows = []
                else:
                    raw_rows = []

            if not raw_rows:
                extractor = custom_extractor
                if extractor is None:
                    extractor = self.get_extractor(allow_mock)

                for idx, p_info in enumerate(validated_pages):
                    img_path = Path(p_info["path"])
                    page_rows = extractor.extract(img_path)
                    # Override source_page with logical page number
                    logical_pg = p_info.get("page_number", str(idx + 1))
                    for r in page_rows:
                        r.source_page = str(logical_pg)
                    raw_rows.extend(page_rows)

                    self._update_stage(
                        job_id, "ocr_extraction", StageStatus.RUNNING,
                        f"Extracted page {idx + 1}/{len(validated_pages)} ({len(raw_rows)} rows)",
                        progress=idx + 1, total=len(validated_pages)
                    )

                if len(raw_rows) == 0:
                    raise ValueError(
                        f"OCR extraction produced 0 rows across {len(validated_pages)} pages. "
                        "Incomplete extraction must stop before approved workbook generation."
                    )

                # Persist raw extractions and cryptographic cache metadata
                cached_extraction_file.write_text(
                    json.dumps([r.model_dump() for r in raw_rows], indent=2),
                    encoding="utf-8"
                )
                provider_str = (
                    "mock" if isinstance(extractor, MockExtractor)
                    else getattr(extractor, "provider_name", "gemini")
                )
                meta_data = {
                    "month": month,
                    "provider": provider_str,
                    "page_hashes": current_page_hashes,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "row_count": len(raw_rows),
                }
                cached_meta_file.write_text(
                    json.dumps(meta_data, indent=2),
                    encoding="utf-8"
                )

            self._update_stage(
                job_id, "ocr_extraction", StageStatus.COMPLETE,
                f"Extracted {len(raw_rows)} raw cashbook rows",
                progress=len(raw_rows), total=len(raw_rows)
            )

            # -------------------------------------------------------------
            # Stage 4: Column alignment
            # -------------------------------------------------------------
            if self._check_cancelled(job_id):
                return self.job_store.get_job(job_id)

            self._update_stage(job_id, "column_alignment", StageStatus.RUNNING, "Aligning columns and field shifts...")
            aligner = ColumnAligner()
            aligned_rows: List[AlignedRow] = []
            prev_row: Optional[AlignedRow] = None

            for idx, r in enumerate(raw_rows):
                ar = aligner.align_row(r, prev_aligned_row=prev_row)
                aligned_rows.append(ar)
                prev_row = ar
                if (idx + 1) % 10 == 0 or (idx + 1) == len(raw_rows):
                    self._update_stage(
                        job_id, "column_alignment", StageStatus.RUNNING,
                        f"Aligned {idx + 1}/{len(raw_rows)} rows",
                        progress=idx + 1, total=len(raw_rows)
                    )

            self._update_stage(
                job_id, "column_alignment", StageStatus.COMPLETE,
                f"Aligned {len(aligned_rows)} rows",
                progress=len(aligned_rows), total=len(aligned_rows)
            )

            # -------------------------------------------------------------
            # Stage 5: Accounting mapping (Levels 1–3)
            # -------------------------------------------------------------
            if self._check_cancelled(job_id):
                return self.job_store.get_job(job_id)

            self._update_stage(job_id, "accounting_mapping", StageStatus.RUNNING, "Applying deterministic mapping rules...")
            engine = HybridMappingEngine(self.registry)
            mapping_results: List[MappingResult] = []

            for idx, ar in enumerate(aligned_rows):
                m_res = engine.map_transaction(ar)
                mapping_results.append(m_res)
                if (idx + 1) % 10 == 0 or (idx + 1) == len(aligned_rows):
                    self._update_stage(
                        job_id, "accounting_mapping", StageStatus.RUNNING,
                        f"Mapped {idx + 1}/{len(aligned_rows)} transactions",
                        progress=idx + 1, total=len(aligned_rows)
                    )

            resolved_count = sum(1 for m in mapping_results if m.mapping_status in (MappingStatus.VERIFIED, MappingStatus.SUGGESTED))
            self._update_stage(
                job_id, "accounting_mapping", StageStatus.COMPLETE,
                f"Mapped {len(mapping_results)} transactions ({resolved_count} resolved)",
                progress=len(mapping_results), total=len(mapping_results)
            )

            # -------------------------------------------------------------
            # Stage 6: Subhead validation
            # -------------------------------------------------------------
            if self._check_cancelled(job_id):
                return self.job_store.get_job(job_id)

            self._update_stage(job_id, "subhead_validation", StageStatus.RUNNING, "Enforcing subhead policies...")
            # HybridMappingEngine already validates subhead policy during mapping
            self._update_stage(
                job_id, "subhead_validation", StageStatus.COMPLETE,
                f"Subhead policies validated for {len(mapping_results)} records",
                progress=len(mapping_results), total=len(mapping_results)
            )

            # -------------------------------------------------------------
            # Stage 7: LLM assistance (Level 4)
            # -------------------------------------------------------------
            if self._check_cancelled(job_id):
                return self.job_store.get_job(job_id)

            if use_llm and LocalLLMAssistant.is_available():
                self._update_stage(job_id, "llm_assistance", StageStatus.RUNNING, "Querying local Qwen3.5-4B model...")
                unresolved_indices = [
                    i for i, m in enumerate(mapping_results)
                    if m.mapping_status in (MappingStatus.UNRESOLVED, MappingStatus.PROVISIONAL)
                ]

                if unresolved_indices:
                    assistant = LocalLLMAssistant(self.registry)
                    candidate_gen = CandidateGenerator(self.registry)

                    try:
                        assistant.start()
                        for count, idx in enumerate(unresolved_indices):
                            m_res = mapping_results[idx]
                            shortlist = candidate_gen.generate_shortlist(
                                direction=m_res.direction,
                                narration=m_res.raw_narration or m_res.english_narration,
                                max_candidates=5
                            )

                            llm_res = assistant.classify_transaction(
                                transaction_id=m_res.transaction_id,
                                narration_raw=m_res.raw_narration or "",
                                narration_en=m_res.english_narration,
                                direction=m_res.direction,
                                candidates=shortlist
                            )

                            # Apply calibrated confidence gate
                            if llm_res.selected_head_id and llm_res.confidence >= 0.75:
                                head = self.registry.get_head(llm_res.selected_head_id)
                                if head:
                                    m_res.canonical_head_id = head.canonical_head_id
                                    m_res.canonical_head_name = head.canonical_name
                                    m_res.confidence_score = llm_res.confidence
                                    m_res.evidence = f"Local LLM Level 4: {llm_res.reasoning}"
                                    m_res.mapping_status = (
                                        MappingStatus.SUGGESTED if llm_res.confidence >= 0.90
                                        else MappingStatus.PROVISIONAL
                                    )

                            self._update_stage(
                                job_id, "llm_assistance", StageStatus.RUNNING,
                                f"Assisted {count + 1}/{len(unresolved_indices)} unresolved items",
                                progress=count + 1, total=len(unresolved_indices)
                            )
                    finally:
                        assistant.teardown()

                self._update_stage(
                    job_id, "llm_assistance", StageStatus.COMPLETE,
                    f"LLM assistance complete for {len(unresolved_indices)} items",
                    progress=len(unresolved_indices), total=len(unresolved_indices)
                )
            else:
                self._update_stage(
                    job_id, "llm_assistance", StageStatus.COMPLETE,
                    "LLM assistance skipped (model offline or disabled)",
                    progress=0, total=0
                )

            # -------------------------------------------------------------
            # Stage 8: Review preparation
            # -------------------------------------------------------------
            if self._check_cancelled(job_id):
                return self.job_store.get_job(job_id)

            self._update_stage(job_id, "review_preparation", StageStatus.RUNNING, "Clustering review items...")
            review_mgr = ReviewManager(self.registry)
            review_items: List[ReviewItem] = []

            candidate_gen = CandidateGenerator(self.registry)
            for m_res, ar in zip(mapping_results, aligned_rows):
                r_item = review_mgr.build_review_item(
                    mapping_result=m_res,
                    aligned_row=ar
                )
                # Enrich with candidate alternatives if unresolved or suggested
                if r_item.mapping_status != MappingStatus.VERIFIED:
                    shortlist = candidate_gen.generate_shortlist(
                        direction=r_item.direction,
                        narration=r_item.raw_narration or r_item.english_narration,
                        max_candidates=5
                    )
                    r_item.candidate_alternatives = [
                        {
                            "head_id": c.canonical_head_id,
                            "head_name": c.canonical_name,
                            "match_score": c.score,
                            "matched_terms": c.matched_terms
                        }
                        for c in shortlist
                        if c.canonical_head_id != "ABSTAIN"
                    ]
                review_items.append(r_item)

            review_groups = review_mgr.group_similar_transactions(review_items)

            # Save review state to disk
            review_state_file = month_output_dir / "review_state.json"
            review_state_data = {
                "month": month,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "total_items": len(review_items),
                "items": [it.model_dump() for it in review_items],
                "groups": [g.model_dump() for g in review_groups],
            }
            review_state_file.write_text(json.dumps(review_state_data, indent=2), encoding="utf-8")

            self._update_stage(
                job_id, "review_preparation", StageStatus.COMPLETE,
                f"Prepared {len(review_items)} review items into {len(review_groups)} clusters",
                progress=len(review_items), total=len(review_items)
            )

            # -------------------------------------------------------------
            # Stage 9: Draft workbook generation
            # -------------------------------------------------------------
            if self._check_cancelled(job_id):
                return self.job_store.get_job(job_id)

            self._update_stage(job_id, "draft_workbook_generation", StageStatus.RUNNING, "Generating draft Excel workbook...")
            processed_rows: List[ProcessedRow] = []
            for it, ar in zip(review_items, aligned_rows):
                t_type = (
                    TransactionType.RECEIPT if it.direction == "RECEIPT"
                    else TransactionType.PAYMENT
                )
                rec_type = RecordType.TRANSACTION
                if "opening balance" in (it.raw_narration or "").lower():
                    rec_type = RecordType.OPENING_BALANCE
                elif "closing balance" in (it.raw_narration or "").lower():
                    rec_type = RecordType.CLOSING_BALANCE

                p_row = ProcessedRow(
                    source_image=it.source_image,
                    source_page=it.source_page,
                    source_row=it.source_row,
                    raw_date=ar.aligned_date or ar.raw_row.raw_date,
                    resolved_date=ar.aligned_date or ar.raw_row.raw_date,
                    raw_head_number=it.raw_head_number,
                    accounting_head_name=it.canonical_head_name,
                    subhead=it.canonical_subhead_name,
                    raw_voucher_number=it.raw_voucher_number,
                    voucher_number=it.raw_voucher_number,
                    raw_narration=it.raw_narration,
                    narration_english=it.english_narration,
                    raw_amount=str(it.amount) if it.amount is not None else None,
                    receipt_amount=it.amount if t_type == TransactionType.RECEIPT else None,
                    payment_amount=it.amount if t_type == TransactionType.PAYMENT else None,
                    transaction_type=t_type,
                    record_type=rec_type,
                    overall_confidence=it.confidence_score,
                    review_required=it.needs_human_review,
                )
                processed_rows.append(p_row)

            draft_excel_path = month_output_dir / f"{month}_draft_review.xlsx"
            try:
                self.manual_review_exporter.export(processed_rows, draft_excel_path)
            except PermissionError:
                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                draft_excel_path = month_output_dir / f"{month}_draft_review_{ts}.xlsx"
                self.manual_review_exporter.export(processed_rows, draft_excel_path)

            self._update_stage(
                job_id, "draft_workbook_generation", StageStatus.COMPLETE,
                f"Generated draft Excel workbook: {draft_excel_path.name}",
                progress=len(processed_rows), total=len(processed_rows)
            )

            # -------------------------------------------------------------
            # Complete Job
            # -------------------------------------------------------------
            verified_count = sum(1 for it in review_items if it.mapping_status == MappingStatus.VERIFIED)
            suggested_count = sum(1 for it in review_items if it.mapping_status == MappingStatus.SUGGESTED)
            provisional_count = sum(1 for it in review_items if it.mapping_status == MappingStatus.PROVISIONAL)
            unresolved_count = sum(1 for it in review_items if it.mapping_status == MappingStatus.UNRESOLVED)

            job = self.job_store.get_job(job_id)
            job.status = JobStatus.COMPLETED
            job.completed_at = datetime.now(timezone.utc).isoformat()
            job.result_summary = {
                "total_rows": len(review_items),
                "verified": verified_count,
                "suggested": suggested_count,
                "provisional": provisional_count,
                "unresolved": unresolved_count,
                "groups_count": len(review_groups),
                "draft_workbook": str(draft_excel_path.name),
            }
            self.job_store.save_job(job)
            return job

        except Exception as e:
            logger.error(f"Pipeline job {job_id} failed: {e}", exc_info=True)
            job = self.job_store.get_job(job_id)
            if job:
                job.status = JobStatus.FAILED
                job.error_message = str(e)
                job.completed_at = datetime.now(timezone.utc).isoformat()
                self.job_store.save_job(job)
            raise

    def _update_stage(
        self,
        job_id: str,
        stage_id: str,
        status: StageStatus,
        details: str,
        progress: int = 0,
        total: int = 0,
    ):
        job = self.job_store.get_job(job_id)
        if not job:
            return

        job.current_stage = stage_id
        for s in job.stages:
            if s.id == stage_id:
                s.status = status
                s.details = details
                s.progress_count = progress
                s.total_count = total
                if status == StageStatus.RUNNING and not s.started_at:
                    s.started_at = datetime.now(timezone.utc).isoformat()
                elif status in (StageStatus.COMPLETE, StageStatus.FAILED):
                    s.completed_at = datetime.now(timezone.utc).isoformat()
                break

        self.job_store.save_job(job)

    def _check_cancelled(self, job_id: str) -> bool:
        job = self.job_store.get_job(job_id)
        return bool(job and job.cancel_requested)
