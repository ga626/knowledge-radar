"""Resource-aware concurrency policy for long-running media tasks.

The policy is deliberately local and lightweight. It gives the current
``LocalTaskAdapter`` enough back-pressure for CPU/GPU/provider-heavy work
without introducing Celery, Prefect, or another worker runtime.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import os
import threading
import time
from typing import Iterator

from .leases import default_owner, get_runtime_lease_coordinator


RESOURCE_SUBTITLE_PROBE = "subtitle_probe"
RESOURCE_MEDIA_DOWNLOAD = "media_download"
RESOURCE_ASR_CPU = "asr_cpu"
RESOURCE_ASR_GPU = "asr_gpu"
RESOURCE_PROVIDER_BAILIAN = "provider_bailian"
RESOURCE_FRAME_VISION = "frame_vision"
RESOURCE_LOCAL_HEAVY = "local_heavy"
RESOURCE_UNBOUNDED = "unbounded"


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return max(1, int(value))
    except ValueError:
        return default


@dataclass(frozen=True)
class ResourceConcurrencyPolicy:
    subtitle_probe: int = 6
    media_download: int = 2
    asr_cpu: int = 1
    asr_gpu: int = 1
    provider_bailian: int = 1
    frame_vision: int = 2

    @classmethod
    def from_env(cls) -> "ResourceConcurrencyPolicy":
        return cls(
            subtitle_probe=_env_int("KR_SUBTITLE_PROBE_CONCURRENCY", 6),
            media_download=_env_int("KR_MEDIA_DOWNLOAD_CONCURRENCY", 2),
            asr_cpu=_env_int("KR_ASR_CPU_CONCURRENCY", 1),
            asr_gpu=_env_int("KR_ASR_GPU_CONCURRENCY", 1),
            provider_bailian=_env_int("KR_PROVIDER_CONCURRENCY_BAILIAN", 1),
            frame_vision=_env_int("KR_FRAME_VISION_CONCURRENCY", 2),
        )

    def limit_for(self, resource: str) -> int | None:
        normalized = normalize_resource(resource)
        if normalized == RESOURCE_LOCAL_HEAVY:
            return 1
        if normalized == RESOURCE_SUBTITLE_PROBE:
            return self.subtitle_probe
        if normalized == RESOURCE_MEDIA_DOWNLOAD:
            return self.media_download
        if normalized == RESOURCE_ASR_CPU:
            return self.asr_cpu
        if normalized == RESOURCE_ASR_GPU:
            return self.asr_gpu
        if normalized == RESOURCE_PROVIDER_BAILIAN:
            return self.provider_bailian
        if normalized == RESOURCE_FRAME_VISION:
            return self.frame_vision
        return None

    def compact(self) -> dict[str, object]:
        return {
            "schema": "knowledgeradar-resource-concurrency/v1",
            "resources": {
                RESOURCE_LOCAL_HEAVY: {"limit": 1, "role": "ASR 与 OCR/文档模型跨进程共享；完成后退出 worker"},
                RESOURCE_SUBTITLE_PROBE: {
                    "limit": self.subtitle_probe,
                    "env": "KR_SUBTITLE_PROBE_CONCURRENCY",
                    "role": "cache/subtitle probes; cheap and safe to fan out",
                },
                RESOURCE_MEDIA_DOWNLOAD: {
                    "limit": self.media_download,
                    "env": "KR_MEDIA_DOWNLOAD_CONCURRENCY",
                    "role": "yt-dlp/ffmpeg/download slots",
                },
                RESOURCE_ASR_CPU: {
                    "limit": self.asr_cpu,
                    "env": "KR_ASR_CPU_CONCURRENCY",
                    "role": "本地转写；实际执行同时遵守 local_heavy=1",
                },
                RESOURCE_ASR_GPU: {
                    "limit": self.asr_gpu,
                    "env": "KR_ASR_GPU_CONCURRENCY",
                    "role": "single-GPU local ASR; default protects VRAM",
                },
                RESOURCE_PROVIDER_BAILIAN: {
                    "limit": self.provider_bailian,
                    "env": "KR_PROVIDER_CONCURRENCY_BAILIAN",
                    "role": "DashScope/Bailian API calls with 429/cost guard at caller layer",
                },
                RESOURCE_FRAME_VISION: {
                    "limit": self.frame_vision,
                    "env": "KR_FRAME_VISION_CONCURRENCY",
                    "role": "local frame extraction/OCR/VLM preparation",
                },
            },
        }


def normalize_resource(resource: str | None) -> str:
    value = (resource or "").strip().lower()
    aliases = {
        "subtitle": RESOURCE_SUBTITLE_PROBE,
        "caption": RESOURCE_SUBTITLE_PROBE,
        "download": RESOURCE_MEDIA_DOWNLOAD,
        "ffmpeg": RESOURCE_MEDIA_DOWNLOAD,
        "cpu_asr": RESOURCE_ASR_CPU,
        "gpu_asr": RESOURCE_ASR_GPU,
        "bailian": RESOURCE_PROVIDER_BAILIAN,
        "provider": RESOURCE_PROVIDER_BAILIAN,
        "vision": RESOURCE_FRAME_VISION,
        "frame": RESOURCE_FRAME_VISION,
    }
    return aliases.get(value, value or RESOURCE_UNBOUNDED)


def infer_task_resource(task_type: str, metadata: dict | None = None) -> str:
    metadata = metadata or {}
    explicit = normalize_resource(str(metadata.get("resource_kind") or ""))
    if explicit != RESOURCE_UNBOUNDED:
        return explicit
    value = (task_type or "").lower()
    if "transcribe" in value or "asr" in value:
        device = str(metadata.get("device") or os.environ.get("KR_ASR_DEVICE") or "cpu").strip().lower()
        return RESOURCE_ASR_GPU if device in {"cuda", "gpu"} else RESOURCE_ASR_CPU
    if "qwen_video" in value or "bailian" in value or "native_media" in value:
        return RESOURCE_PROVIDER_BAILIAN
    if "frame" in value or "ocr" in value:
        return RESOURCE_FRAME_VISION
    if "download" in value:
        return RESOURCE_MEDIA_DOWNLOAD
    return RESOURCE_UNBOUNDED


_SEMAPHORES: dict[str, threading.BoundedSemaphore] = {}
_SEMAPHORE_LIMITS: dict[str, int] = {}
_RESOURCE_STATS: dict[str, dict[str, float]] = {}
_LOCK = threading.RLock()
_HELD_RESOURCES = ContextVar("kr_held_resources", default={})


def _stats_for(resource: str) -> dict[str, float]:
    with _LOCK:
        row = _RESOURCE_STATS.setdefault(
            resource,
            {
                "acquired": 0,
                "released": 0,
                "active": 0,
                "wait_total_s": 0.0,
                "wait_max_s": 0.0,
            },
        )
        return row


def _semaphore_for(resource: str, policy: ResourceConcurrencyPolicy) -> threading.BoundedSemaphore | None:
    normalized = normalize_resource(resource)
    if normalized in {RESOURCE_ASR_CPU, RESOURCE_ASR_GPU}:
        normalized = RESOURCE_LOCAL_HEAVY
    limit = policy.limit_for(normalized)
    if limit is None:
        return None
    with _LOCK:
        current = _SEMAPHORES.get(normalized)
        if current is not None and _SEMAPHORE_LIMITS.get(normalized) == limit:
            return current
        sem = threading.BoundedSemaphore(limit)
        _SEMAPHORES[normalized] = sem
        _SEMAPHORE_LIMITS[normalized] = limit
        return sem


@contextmanager
def acquire_resource(resource: str, policy: ResourceConcurrencyPolicy | None = None) -> Iterator[dict[str, object]]:
    selected_policy = policy or ResourceConcurrencyPolicy.from_env()
    normalized = normalize_resource(resource)
    if normalized in {RESOURCE_ASR_CPU, RESOURCE_ASR_GPU}:
        normalized = RESOURCE_LOCAL_HEAVY
    held = _HELD_RESOURCES.get()
    if normalized in held:
        # 同一调用链复用外层租约，避免单 GPU 路径再次申请自己的槽位。
        yield {**held[normalized], "reused_outer_lease": True}
        return
    sem = _semaphore_for(normalized, selected_policy)
    if sem is None:
        yield {"resource_kind": normalized, "limit": None, "limited": False, "wait_s": 0.0}
        return
    limit = selected_policy.limit_for(normalized)
    lease_started = time.time()
    coordinator = get_runtime_lease_coordinator()
    from runtime.process_identity import process_identity
    ttl = max(30, int(os.environ.get("KR_RESOURCE_LEASE_TTL_S", "60")))
    wait_deadline = time.monotonic() + (
        max(0, float(os.environ.get("KR_LOCAL_HEAVY_QUEUE_TIMEOUT_S", "600")))
        if normalized == RESOURCE_LOCAL_HEAVY
        else 0
    )
    while True:
        # 取消/崩溃后仅核对有确切进程身份的本地资源，不影响共享浏览器租约。
        for active in coordinator.summary(limit=100).get("active", []):
            metadata = active.get("metadata") or {}
            if active.get("resource_kind") != "runtime_resource" or not metadata.get("owner_process_identity"):
                continue
            actual = process_identity(metadata.get("owner_pid"))
            if not actual or actual != metadata["owner_process_identity"]:
                coordinator.release(active["lease_id"])
        lease = coordinator.acquire_bounded(
            "runtime_resource",
            normalized,
            limit=int(limit or 1),
            owner=default_owner("acquire_resource"),
            ttl_s=ttl,
            metadata={"resource_kind": normalized, "limit": limit,
                      "owner_pid": os.getpid(), "owner_process_identity": process_identity()},
        )
        if lease.acquired or time.monotonic() >= wait_deadline:
            break
        time.sleep(0.2)
    lease_wait_s = round(time.time() - lease_started, 3)
    if not lease.acquired:
        raise RuntimeError(f"runtime resource busy: {normalized}; retry_after_s={lease.retry_after_s}")
    started = time.time()
    sem.acquire()
    wait_s = round(time.time() - started, 3)
    stats = _stats_for(normalized)
    with _LOCK:
        stats["acquired"] += 1
        stats["active"] += 1
        stats["wait_total_s"] += wait_s
        stats["wait_max_s"] = max(float(stats.get("wait_max_s") or 0.0), wait_s)
    token = None
    stop_heartbeat = threading.Event()

    def heartbeat():
        while not stop_heartbeat.wait(ttl / 3):
            coordinator.heartbeat(lease.lease_id, ttl_s=ttl)

    heart = threading.Thread(target=heartbeat, daemon=True)
    heart.start()
    try:
        resource_meta = {
            "resource_kind": normalized,
            "limit": limit,
            "limited": True,
            "wait_s": wait_s,
            "lease_id": lease.lease_id,
            "lease_wait_s": lease_wait_s,
            "lease_backed": True,
        }
        token = _HELD_RESOURCES.set({**held, normalized: resource_meta})
        yield resource_meta
    finally:
        stop_heartbeat.set()
        heart.join(timeout=2)
        if token is not None:
            _HELD_RESOURCES.reset(token)
        try:
            sem.release()
        finally:
            get_runtime_lease_coordinator().release(lease.lease_id)
            with _LOCK:
                stats["released"] += 1
                stats["active"] = max(0, float(stats.get("active") or 0) - 1)


def resource_concurrency_summary() -> dict[str, object]:
    policy = ResourceConcurrencyPolicy.from_env()
    data = policy.compact()
    data["status"] = "implemented_p2_2"
    data["lease_backed"] = True
    data["adapter_scope"] = (
        "runtime.task_adapter.LocalTaskAdapter acquires a cross-process runtime lease and a process-local semaphore before running task callables"
    )
    data["default_principle"] = "probe fan-out is allowed; GPU ASR and downloads are intentionally conservative"
    with _LOCK:
        data["runtime_stats"] = {
            name: {
                "acquired": int(row.get("acquired") or 0),
                "released": int(row.get("released") or 0),
                "active": int(row.get("active") or 0),
                "wait_total_s": round(float(row.get("wait_total_s") or 0), 3),
                "wait_max_s": round(float(row.get("wait_max_s") or 0), 3),
            }
            for name, row in sorted(_RESOURCE_STATS.items())
        }
    return data
