"""隔离转写 worker 的生命周期合同；不在服务进程加载模型。"""

from __future__ import annotations
import os
import shutil
from typing import Any, Dict
from runtime.process import silent_subprocess_run


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


def asr_lifecycle_summary():
    from runtime.model_roles import ASR_COMPONENT

    return {
        "schema": "knowledgeradar-asr-lifecycle/v2",
        "status": "implemented_isolated_worker",
        "component": ASR_COMPONENT,
        "mode": "task_burst",
        "resident_model": False,
        "concurrency_resource": "local_heavy",
        "release_policy": "退出本次隔离子进程后释放模型内存；取消由任务服务终止其拥有的进程树",
        "default_device": os.environ.get("KR_ASR_DEVICE", "cpu"),
        "batch_size": 1,
        "gpu": _gpu_memory_from_nvidia_smi(),
    }
