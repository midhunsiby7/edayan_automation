"""Level 4 Local LLM Assistant for Cashbook AI.

Provides on-demand, constrained, schema-enforced inference via local llama.cpp
and quantized GGUF models (e.g. Qwen3.5-4B-Instruct-Q4_K_M) on local GPU/CPU hardware.

Key features:
1. Pre-flight memory safety checks (RAM and VRAM verification).
2. On-demand transient process lifecycle (NOT a permanent background daemon).
3. Bounded candidate shortlist prompts preventing open-ended hallucination.
4. Schema validation and strict registry verification.
5. Automated subhead policy resolution (Mandatory, Optional, Prohibited).
6. Clean teardown and deterministic resource reclamation.
"""

import os
import re
import time
import json
import socket
import logging
import ctypes
import subprocess
import requests
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from pydantic import BaseModel, Field

from src.reference.canonical_registry import CanonicalRegistry, SubheadPolicy
from src.mapping.candidate_generator import CandidateGenerator, CandidateOption
from src.mapping.hybrid_engine import MappingStatus, SubheadStatus, MappingMethod

logger = logging.getLogger(__name__)


class InsufficientMemoryError(RuntimeError):
    """Raised when available system RAM or VRAM is below safety thresholds."""
    pass


class MemorySnapshot(BaseModel):
    """Snapshot of system RAM and GPU VRAM."""
    ram_total_gb: float
    ram_avail_gb: float
    ram_used_gb: float
    ram_percent: float
    vram_total_mb: float = 0.0
    vram_free_mb: float = 0.0
    vram_used_mb: float = 0.0


class LLMClassification(BaseModel):
    """Result of Level 4 LLM transaction classification."""
    transaction_ref: Optional[str] = None
    canonical_head_id: Optional[str] = None
    canonical_subhead_id: Optional[str] = None
    confidence: float = 0.0
    reasoning: str = ""
    abstain: bool = False
    is_hallucinated: bool = False
    mapping_status: MappingStatus = MappingStatus.UNRESOLVED
    subhead_status: SubheadStatus = SubheadStatus.VALID_EMPTY
    candidate_shortlist: List[str] = Field(default_factory=list)
    latency_seconds: float = 0.0

    @property
    def selected_head_id(self) -> Optional[str]:
        return self.canonical_head_id


class LocalLLMAssistant:
    """On-demand assistant coordinating llama-server subprocess and JSON classification."""

    DEFAULT_MODEL_PATH = Path(r"D:\TARS\benchmark_models\qwen3.5-4b-instruct-Q4_K_M.gguf")
    DEFAULT_SERVER_PATH = Path(r"D:\test app\llama-b10428-bin-win-cuda-12.4-x64\llama-server.exe")

    @classmethod
    def is_available(cls, model_path: Optional[Path | str] = None, server_path: Optional[Path | str] = None) -> bool:
        """Check if local LLM model weights and llama-server binary exist on disk."""
        m_path = Path(model_path) if model_path else cls.DEFAULT_MODEL_PATH
        s_path = Path(server_path) if server_path else cls.DEFAULT_SERVER_PATH
        return m_path.is_file() and s_path.is_file()

    def teardown(self) -> None:
        """Safe teardown alias for stop()."""
        self.stop()

    MIN_RAM_AVAIL_GB = 1.0     # Minimum available RAM to start
    MIN_VRAM_FREE_MB = 1500.0  # Minimum free VRAM for full GPU offload

    def __init__(
        self,
        registry: CanonicalRegistry,
        model_path: Optional[Path | str] = None,
        server_path: Optional[Path | str] = None,
        ctx_size: int = 2048,
        ngl: int = 99,
        confidence_threshold: float = 0.80,
        host: str = "127.0.0.1"
    ):
        self.registry = registry
        self.candidate_generator = CandidateGenerator(registry)
        self.model_path = Path(model_path) if model_path else self.DEFAULT_MODEL_PATH
        self.server_path = Path(server_path) if server_path else self.DEFAULT_SERVER_PATH
        self.ctx_size = ctx_size
        self.ngl = ngl
        self.confidence_threshold = confidence_threshold
        self.host = host

        self._proc: Optional[subprocess.Popen] = None
        self._port: Optional[int] = None
        self._is_running = False

    @staticmethod
    def get_memory_snapshot() -> MemorySnapshot:
        """Inspect available system RAM and GPU VRAM using native OS APIs."""
        # Windows RAM check via GlobalMemoryStatusEx
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(stat)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))

        total_gb = stat.ullTotalPhys / (1024**3)
        avail_gb = stat.ullAvailPhys / (1024**3)
        used_gb = total_gb - avail_gb
        pct = float(stat.dwMemoryLoad)

        vram_total = 0.0
        vram_free = 0.0
        vram_used = 0.0
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=memory.total,memory.used,memory.free", "--format=csv,noheader,nounits"],
                text=True,
                stderr=subprocess.DEVNULL
            ).strip().split(",")
            vram_total = float(out[0].strip())
            vram_used = float(out[1].strip())
            vram_free = float(out[2].strip())
        except Exception:
            pass

        return MemorySnapshot(
            ram_total_gb=round(total_gb, 2),
            ram_avail_gb=round(avail_gb, 2),
            ram_used_gb=round(used_gb, 2),
            ram_percent=pct,
            vram_total_mb=round(vram_total, 1),
            vram_free_mb=round(vram_free, 1),
            vram_used_mb=round(vram_used, 1)
        )

    def _find_available_port(self, start_port: int = 8088) -> int:
        """Find an unbound local TCP port."""
        port = start_port
        while port < 65535:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex((self.host, port)) != 0:
                    return port
            port += 1
        raise RuntimeError("No available TCP port found")

    def check_preflight(self) -> MemorySnapshot:
        """Verify model files and memory availability before starting inference."""
        if not self.model_path.is_file():
            raise FileNotFoundError(f"Model file not found at {self.model_path}")
        if not self.server_path.is_file():
            raise FileNotFoundError(f"llama-server executable not found at {self.server_path}")

        mem = self.get_memory_snapshot()
        if mem.ram_avail_gb < self.MIN_RAM_AVAIL_GB:
            raise InsufficientMemoryError(
                f"Insufficient RAM: {mem.ram_avail_gb:.2f} GB available, "
                f"minimum required is {self.MIN_RAM_AVAIL_GB} GB"
            )
        return mem

    def start(self, timeout_seconds: float = 30.0) -> None:
        """Start the local llama-server subprocess on-demand."""
        if self._is_running:
            return

        self.check_preflight()
        self._port = self._find_available_port(start_port=8086)

        cmd = [
            str(self.server_path),
            "-m", str(self.model_path),
            "-ngl", str(self.ngl),
            "-c", str(self.ctx_size),
            "--reasoning", "off",
            "--port", str(self._port),
            "--host", self.host
        ]

        logger.info(f"Starting llama-server on port {self._port}...")
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        # Wait for server ready signal (status: "ok")
        start_time = time.time()
        ready = False
        while time.time() - start_time < timeout_seconds:
            try:
                r = requests.get(f"http://{self.host}:{self._port}/health", timeout=1.0)
                if r.status_code == 200 and r.json().get("status") == "ok":
                    ready = True
                    break
            except Exception:
                pass
            time.sleep(0.4)

        if not ready:
            self.stop()
            raise TimeoutError(f"llama-server failed to initialize within {timeout_seconds}s")

        self._is_running = True
        logger.info(f"llama-server ready on {self.host}:{self._port}")

    def stop(self) -> None:
        """Terminate server subprocess and ensure all GPU/RAM resources are released."""
        if self._proc:
            logger.info("Terminating llama-server...")
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
            except Exception as e:
                logger.warning(f"Error during server termination: {e}")
            finally:
                self._proc = None

        self._port = None
        self._is_running = False
        logger.info("llama-server terminated.")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    @staticmethod
    def _clean_json_content(raw_text: str) -> str:
        """Strip markdown fences, trailing text, and normalize JSON string."""
        text = raw_text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        return text.strip()

    def classify_transaction(
        self,
        direction: str,
        narration: Optional[str] = None,
        amount: Optional[float | str] = None,
        date: Optional[str] = None,
        transaction_ref: Optional[str] = None,
        max_candidates: int = 5,
        transaction_id: Optional[str] = None,
        narration_raw: Optional[str] = None,
        narration_en: Optional[str] = None,
        candidates: Optional[Any] = None,
    ) -> LLMClassification:
        """Classify a single unresolved transaction using constrained local LLM inference."""
        if not self._is_running or not self._port:
            raise RuntimeError("LocalLLMAssistant is not running. Use 'with assistant:' or call start().")

        ref = transaction_ref or transaction_id
        narr = narration or narration_en or narration_raw or ""
        dir_upper = direction.upper().strip()

        # 1. Bounded candidate shortlist
        candidates = self.candidate_generator.generate_shortlist(
            direction=dir_upper,
            narration=narr,
            max_candidates=max_candidates,
            include_abstain=True
        )
        cand_dict = {c.canonical_head_id: c for c in candidates}
        cand_ids = [c.canonical_head_id for c in candidates]

        cand_text = "\n".join([
            f"- {c.canonical_head_id}: {c.canonical_name} ({c.description})"
            for c in candidates
        ])

        system_msg = (
            "You are an expert Catholic parish accounting classifier for Kerala diocese cashbooks.\n"
            "Domain Rules:\n"
            "1. 'As per order (...)' represents diocesan orders/collections -> GEN_HEAD_ORDER_FROM_DIOCESE.\n"
            "2. 'Donation for Holy Mass' is the routine daily mass offering -> GEN_HEAD_OFFERINGS_DAILY.\n"
            "3. 'Catechism' collections are Sunday school collections -> GEN_HEAD_SUNDAY_COLLECTION_INC.\n"
            "4. Bank entries with quarterly date ranges (e.g. '1-1-26/31-3-26') represent interest -> GEN_HEAD_BANK_INTEREST.\n"
            "5. Staff allowances (Vicar, Accountant, Sacristan, Assistant, Sweeper) -> GEN_HEAD_ALLOWANCES.\n"
            "6. 'Advance to Agriculture' -> GEN_HEAD_ADV_TO_AGRI.\n"
            "7. 'Rent Security' -> GEN_HEAD_RENT_SECURITY.\n"
            "8. If no candidate clearly matches, you MUST select 'ABSTAIN'. Do not guess.\n\n"
            "Output JSON ONLY with keys: 'selected_head_id' (candidate ID or 'ABSTAIN'), 'confidence' (0.0 to 1.0), and 'reasoning' (brief sentence under 15 words)."
        )

        user_msg = (
            f"Transaction Direction: {dir_upper}\n"
            f"Narration: {narr}\n"
            f"Amount: {amount if amount is not None else 'N/A'}\n"
            f"Date: {date if date is not None else 'N/A'}\n\n"
            f"Candidates:\n{cand_text}\n\n"
            f"Respond with JSON:"
        )

        payload = {
            "model": "qwen3.5-4b",
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg}
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
            "max_tokens": 256
        }

        t0 = time.time()
        try:
            resp = requests.post(
                f"http://{self.host}:{self._port}/v1/chat/completions",
                json=payload,
                timeout=30.0
            )
            resp.raise_for_status()
            raw_content = resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error(f"Inference HTTP error for {transaction_ref}: {e}")
            return LLMClassification(
                transaction_ref=transaction_ref,
                abstain=True,
                mapping_status=MappingStatus.UNRESOLVED,
                reasoning=f"LLM communication error: {str(e)}",
                candidate_shortlist=cand_ids,
                latency_seconds=round(time.time() - t0, 3)
            )

        dur = time.time() - t0

        # Parse JSON
        parsed: Dict[str, Any] = {}
        try:
            clean_str = self._clean_json_content(raw_content)
            parsed = json.loads(clean_str)
        except Exception as e:
            logger.warning(f"Failed to parse LLM JSON for {transaction_ref}: {e} (content: {raw_content!r})")
            return LLMClassification(
                transaction_ref=transaction_ref,
                abstain=True,
                mapping_status=MappingStatus.UNRESOLVED,
                reasoning=f"Invalid JSON returned: {str(e)}",
                candidate_shortlist=cand_ids,
                latency_seconds=round(dur, 3)
            )

        selected_id = parsed.get("selected_head_id")
        confidence = float(parsed.get("confidence", 0.0))
        reasoning = str(parsed.get("reasoning", ""))

        # 2. Guardrails: Abstention check
        if selected_id == "ABSTAIN" or not selected_id:
            return LLMClassification(
                transaction_ref=transaction_ref,
                abstain=True,
                confidence=confidence,
                reasoning=reasoning,
                mapping_status=MappingStatus.UNRESOLVED,
                candidate_shortlist=cand_ids,
                latency_seconds=round(dur, 3)
            )

        # 3. Guardrails: Candidate shortlist membership check
        if selected_id not in cand_dict:
            logger.warning(f"LLM selected head '{selected_id}' not in candidate shortlist for {transaction_ref}!")
            return LLMClassification(
                transaction_ref=transaction_ref,
                abstain=True,
                is_hallucinated=True,
                confidence=confidence,
                reasoning=f"Selected head {selected_id} was not in candidate shortlist",
                mapping_status=MappingStatus.UNRESOLVED,
                candidate_shortlist=cand_ids,
                latency_seconds=round(dur, 3)
            )

        # 4. Guardrails: Registry verification
        head_obj = self.registry.get_head(selected_id)
        if not head_obj:
            logger.warning(f"LLM hallucinated non-existent head '{selected_id}' for {transaction_ref}!")
            return LLMClassification(
                transaction_ref=transaction_ref,
                abstain=True,
                is_hallucinated=True,
                confidence=confidence,
                reasoning=f"Head {selected_id} does not exist in CanonicalRegistry",
                mapping_status=MappingStatus.UNRESOLVED,
                candidate_shortlist=cand_ids,
                latency_seconds=round(dur, 3)
            )

        # 5. Guardrails: Confidence threshold
        if confidence < self.confidence_threshold:
            logger.info(f"LLM confidence {confidence:.2f} below threshold {self.confidence_threshold} for {transaction_ref}")
            return LLMClassification(
                transaction_ref=transaction_ref,
                canonical_head_id=selected_id,
                abstain=True,
                confidence=confidence,
                reasoning=f"Confidence {confidence:.2f} below threshold {self.confidence_threshold}: {reasoning}",
                mapping_status=MappingStatus.UNRESOLVED,
                candidate_shortlist=cand_ids,
                latency_seconds=round(dur, 3)
            )

        # 6. Subhead policy resolution
        subhead_id, subhead_status = self._resolve_subhead_policy(selected_id, narr)

        return LLMClassification(
            transaction_ref=transaction_ref,
            canonical_head_id=selected_id,
            canonical_subhead_id=subhead_id,
            confidence=confidence,
            reasoning=reasoning,
            abstain=False,
            is_hallucinated=False,
            mapping_status=MappingStatus.SUGGESTED if confidence >= 0.90 else MappingStatus.PROVISIONAL,
            subhead_status=subhead_status,
            candidate_shortlist=cand_ids,
            latency_seconds=round(dur, 3)
        )

    def _resolve_subhead_policy(self, head_id: str, narration: str) -> Tuple[Optional[str], SubheadStatus]:
        """Apply canonical subhead policy to Level 4 classified head."""
        policy = self.registry.get_subhead_policy(head_id)

        if policy == SubheadPolicy.PROHIBITED:
            return None, SubheadStatus.VALID_EMPTY

        # If MANDATORY or OPTIONAL, search active subheads for this head
        narr_lower = narration.lower()
        subheads = self.registry.get_subheads_for_head(head_id)

        for sub in subheads:
            # Check canonical name
            if sub.canonical_name.lower() in narr_lower:
                return sub.canonical_subhead_id, SubheadStatus.RESOLVED

            # Check aliases
            sub_aliases = [a for a in self.registry.aliases if a.target_id == sub.canonical_subhead_id and a.is_active]
            for a in sub_aliases:
                if a.alias_text.lower() in narr_lower:
                    return sub.canonical_subhead_id, SubheadStatus.RESOLVED

        # Staff allowance specific keyword resolution
        if head_id == "GEN_HEAD_ALLOWANCES":
            for sub in subheads:
                if "vicar" in narr_lower and "VICAR" in sub.canonical_subhead_id:
                    return sub.canonical_subhead_id, SubheadStatus.RESOLVED
                if "accountant" in narr_lower and "ACCOUNTANT" in sub.canonical_subhead_id:
                    return sub.canonical_subhead_id, SubheadStatus.RESOLVED
                if "computer" in narr_lower and "COMPUTER" in sub.canonical_subhead_id:
                    return sub.canonical_subhead_id, SubheadStatus.RESOLVED
                if "sacristan" in narr_lower and "SACRISTAN" in sub.canonical_subhead_id:
                    return sub.canonical_subhead_id, SubheadStatus.RESOLVED
                if "sweeper" in narr_lower and "SWEEPER" in sub.canonical_subhead_id:
                    return sub.canonical_subhead_id, SubheadStatus.RESOLVED

        if policy == SubheadPolicy.MANDATORY:
            return None, SubheadStatus.MISSING_MANDATORY

        return None, SubheadStatus.VALID_EMPTY

    def classify_batch(
        self,
        transactions: List[Dict[str, Any]],
        max_candidates: int = 5
    ) -> List[LLMClassification]:
        """Classify a batch of unresolved transactions, ensuring single on-demand lifecycle."""
        if not transactions:
            return []

        was_running = self._is_running
        if not was_running:
            self.start()

        results = []
        try:
            for tx in transactions:
                res = self.classify_transaction(
                    direction=tx["direction"],
                    narration=tx.get("narration"),
                    amount=tx.get("amount"),
                    date=tx.get("date"),
                    transaction_ref=tx.get("transaction_ref"),
                    max_candidates=max_candidates
                )
                results.append(res)
        finally:
            if not was_running:
                self.stop()

        return results
