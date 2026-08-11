"""ASR worker lifecycle policy and CTranslate2 capability probes.

The module is intentionally conservative: it can inspect local packages and
environment state without loading a Whisper model or allocating GPU memory.
Actual warm/cold benchmarks live in ``tools/asr_lifecycle_probe.py``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import importlib
import os
import shutil
from runtime.process import silent_subprocess_run
import time
from typing import Any, Dict, Optional

from runtime.task_scope import SERVER_RUN_ID


ASR_LIFECYCLE_SCHEMA = "knowledgeradar-asr-lifecycle/v1"
ASR_LIFECYCLE_STATES = ("cold", "context_warm", "cpu_resident", "gpu_hot", "terminated")


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except Exception:
        return default


@dataclass(frozen=True)
class AsrLifecyclePolicy:
    mode: str = "task_burst"
    prewarm_trigger: str = "subtitle_miss"
    gpu_hot_ttl_seconds: int = 120
    worker_keepalive_seconds: int = 300
    gpu_unload_policy: str = "to_cpu_then_terminate"
    enable_ctranslate2_unload: bool = True
    gpu_min_free_mb: int = 2048

    @classmethod
    def from_env(cls) -> "AsrLifecyclePolicy":
        return cls(
            mode=(os.environ.get("KR_ASR_LIFECYCLE") or "task_burst").strip() or "task_burst",
            prewarm_trigger=(os.environ.get("KR_ASR_PREWARM_TRIGGER") or "subtitle_miss").strip() or "subtitle_miss",
            gpu_hot_ttl_seconds=max(1, _env_int("KR_ASR_GPU_HOT_TTL_SECONDS", 120)),
            worker_keepalive_seconds=max(1, _env_int("KR_ASR_WORKER_KEEPALIVE_SECONDS", 300)),
            gpu_unload_policy=(os.environ.get("KR_ASR_GPU_UNLOAD_POLICY") or "to_cpu_then_terminate").strip() or "to_cpu_then_terminate",
            enable_ctranslate2_unload=_env_bool("KR_ASR_ENABLE_CTRANSLATE2_UNLOAD", True),
            gpu_min_free_mb=max(0, _env_int("KR_ASR_GPU_MIN_FREE_MB", 2048)),
        )

    def compact(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AsrWorkerKey:
    server_run_id: str
    worker_scope_id: str
    engine: str
    model: str
    device: str
    compute_type: str
    batch_size: int = 1

    def compact(self) -> Dict[str, Any]:
        return asdict(self)

    def stable_id(self) -> str:
        return "|".join(
            [
                self.server_run_id,
                self.worker_scope_id,
                self.engine,
                self.model,
                self.device,
                self.compute_type,
                str(self.batch_size),
            ]
        )


@dataclass
class AsrLifecycleRecord:
    key: AsrWorkerKey
    state: str = "cold"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    transitions: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def compact(self, *, now: Optional[float] = None, policy: Optional[AsrLifecyclePolicy] = None) -> Dict[str, Any]:
        reference_now = time.time() if now is None else now
        age = max(0.0, reference_now - self.created_at)
        idle = max(0.0, reference_now - self.last_used_at)
        data = {
            "schema": ASR_LIFECYCLE_SCHEMA,
            "key": self.key.compact(),
            "worker_key": self.key.stable_id(),
            "state": self.state,
            "age_s": round(age, 3),
            "idle_s": round(idle, 3),
            "transitions": self.transitions,
            "metadata": dict(self.metadata),
        }
        if policy is not None:
            data["should_release"] = idle >= policy.gpu_hot_ttl_seconds if self.state == "gpu_hot" else False
            data["should_terminate"] = idle >= policy.worker_keepalive_seconds if self.state != "terminated" else False
        return data


class AsrLifecycleManager:
    """Track ASR worker lifecycle without owning model objects.

    The manager deliberately stores only lifecycle metadata. Model loading,
    unloading, and process management stay in the future ASR worker adapter so
    this contract can be tested without allocating GPU memory.
    """

    def __init__(self, policy: Optional[AsrLifecyclePolicy] = None, *, server_run_id: str = SERVER_RUN_ID) -> None:
        self.policy = policy or AsrLifecyclePolicy.from_env()
        self.server_run_id = server_run_id
        self._records: Dict[str, AsrLifecycleRecord] = {}

    def make_key(
        self,
        *,
        worker_scope_id: str = "task_burst",
        engine: str = "faster-whisper",
        model: str = "base",
        device: str = "cpu",
        compute_type: str = "int8",
        batch_size: int = 1,
    ) -> AsrWorkerKey:
        return AsrWorkerKey(
            server_run_id=self.server_run_id,
            worker_scope_id=worker_scope_id or "task_burst",
            engine=engine,
            model=model,
            device=device,
            compute_type=compute_type,
            batch_size=max(1, int(batch_size or 1)),
        )

    def get_or_create(self, key: AsrWorkerKey, *, now: Optional[float] = None) -> AsrLifecycleRecord:
        worker_id = key.stable_id()
        if worker_id not in self._records:
            ts = time.time() if now is None else now
            self._records[worker_id] = AsrLifecycleRecord(key=key, created_at=ts, updated_at=ts, last_used_at=ts)
        return self._records[worker_id]

    def transition(
        self,
        key: AsrWorkerKey,
        state: str,
        *,
        now: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
        mark_used: bool = True,
    ) -> AsrLifecycleRecord:
        if state not in ASR_LIFECYCLE_STATES:
            raise ValueError(f"unsupported ASR lifecycle state: {state}")
        ts = time.time() if now is None else now
        record = self.get_or_create(key, now=ts)
        record.state = state
        record.updated_at = ts
        if mark_used:
            record.last_used_at = ts
        record.transitions += 1
        if metadata:
            record.metadata.update(metadata)
        return record

    def mark_context_warm(self, key: AsrWorkerKey, **metadata: Any) -> AsrLifecycleRecord:
        return self.transition(key, "context_warm", metadata=metadata)

    def mark_cpu_resident(self, key: AsrWorkerKey, **metadata: Any) -> AsrLifecycleRecord:
        return self.transition(key, "cpu_resident", metadata=metadata)

    def mark_gpu_hot(self, key: AsrWorkerKey, **metadata: Any) -> AsrLifecycleRecord:
        return self.transition(key, "gpu_hot", metadata=metadata)

    def mark_terminated(self, key: AsrWorkerKey, **metadata: Any) -> AsrLifecycleRecord:
        return self.transition(key, "terminated", metadata=metadata)

    def should_release_gpu(self, key: AsrWorkerKey, *, now: Optional[float] = None) -> bool:
        record = self.get_or_create(key, now=now)
        if record.state != "gpu_hot":
            return False
        ts = time.time() if now is None else now
        return (ts - record.last_used_at) >= self.policy.gpu_hot_ttl_seconds

    def should_terminate_worker(self, key: AsrWorkerKey, *, now: Optional[float] = None) -> bool:
        record = self.get_or_create(key, now=now)
        if record.state == "terminated":
            return False
        ts = time.time() if now is None else now
        return (ts - record.last_used_at) >= self.policy.worker_keepalive_seconds

    def compact(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        return {
            "schema": ASR_LIFECYCLE_SCHEMA,
            "server_run_id": self.server_run_id,
            "policy": self.policy.compact(),
            "records": [record.compact(now=now, policy=self.policy) for record in self._records.values()],
        }


def _gpu_memory_from_nvidia_smi() -> Dict[str, Any]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {"available": False, "reason": "nvidia-smi not found"}
    try:
        result = silent_subprocess_run(
            [
                exe,
                "--query-gpu=memory.total,memory.used,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as exc:
        return {"available": False, "reason": str(exc)[:160]}
    if result.returncode != 0:
        return {"available": False, "reason": (result.stderr or result.stdout or "")[:160]}
    line = (result.stdout or "").strip().splitlines()[0] if (result.stdout or "").strip() else ""
    parts = [part.strip() for part in line.split(",")]
    if len(parts) < 3:
        return {"available": False, "reason": "unexpected nvidia-smi output"}
    try:
        total, used, free = (int(float(part)) for part in parts[:3])
    except Exception:
        return {"available": False, "reason": "failed to parse nvidia-smi output"}
    return {"available": True, "total_mb": total, "used_mb": used, "free_mb": free}


def ctranslate2_lifecycle_probe_summary() -> Dict[str, Any]:
    started = time.time()
    data: Dict[str, Any] = {
        "schema": "knowledgeradar-ctranslate2-lifecycle-probe/v1",
        "mode": "import_probe_no_model_load",
        "status": "unknown",
        "elapsed_s": 0.0,
        "supports": {},
    }
    try:
        ctranslate2 = importlib.import_module("ctranslate2")
        whisper_cls = getattr(getattr(ctranslate2, "models", None), "Whisper", None)
        supported_cuda = []
        try:
            supported_cuda = list(ctranslate2.get_supported_compute_types("cuda"))
        except Exception:
            supported_cuda = []
        data.update(
            {
                "status": "ok" if whisper_cls is not None else "degraded",
                "version": getattr(ctranslate2, "__version__", ""),
                "supported_cuda_compute_types": supported_cuda,
                "supports": {
                    "whisper_class": whisper_cls is not None,
                    "unload_model": bool(whisper_cls and hasattr(whisper_cls, "unload_model")),
                    "load_model": bool(whisper_cls and hasattr(whisper_cls, "load_model")),
                    "model_is_loaded": bool(whisper_cls and hasattr(whisper_cls, "model_is_loaded")),
                    "cuda_available_by_compute_types": bool(supported_cuda),
                },
            }
        )
    except Exception as exc:
        data.update({"status": "degraded", "error": str(exc)[:200]})
    data["gpu_memory"] = _gpu_memory_from_nvidia_smi()
    data["elapsed_s"] = round(time.time() - started, 3)
    return data


def asr_lifecycle_summary() -> Dict[str, Any]:
    policy = AsrLifecyclePolicy.from_env()
    probe = ctranslate2_lifecycle_probe_summary()
    gpu_memory = probe.get("gpu_memory") if isinstance(probe.get("gpu_memory"), dict) else {}
    gpu_free_ok = bool(gpu_memory.get("available")) and int(gpu_memory.get("free_mb") or 0) >= policy.gpu_min_free_mb
    return {
        "schema": ASR_LIFECYCLE_SCHEMA,
        "status": "implemented_p2_2_policy_probe",
        "server_run_id": SERVER_RUN_ID,
        "states": list(ASR_LIFECYCLE_STATES),
        "worker_key": "(server_run_id, worker_scope_id, engine, model, device, compute_type, batch_size)",
        "default_binding": "server_run/task_burst scope; never research_session_id",
        "policy": policy.compact(),
        "ctranslate2_probe": probe,
        "gpu_opt_in_ready": bool(
            policy.mode in {"task_burst", "worker_pool"}
            and policy.enable_ctranslate2_unload
            and (probe.get("supports") or {}).get("cuda_available_by_compute_types")
            and gpu_free_ok
        ),
        "fallback": "CPU int8 faster-whisper/base then tiny when CUDA, VRAM, cache, or lifecycle probe is unavailable",
    }
