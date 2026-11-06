"""平台装配入口；新平台worker不反向导入MCP门面。"""
import time
from kr_core.registry import registry

def ensure_bindings():
    from platform_adapters.factory import register_default_adapters
    return register_default_adapters()


def worker_detail(request, collected=None):
    ensure_bindings()
    from runtime.detail_composition import resolve_strategy
    strategy = resolve_strategy(request.platform)
    if strategy is None:
        raise ValueError("unsupported_detail_platform")
    from use_cases.content_detail import execute_detail_request, resolve_detail_response
    from runtime.failure_cache import detail_failure_cache
    from kr_core.evidence_store import EvidenceStore
    from runtime.task_scope import attach_detail_task_scope
    def record_evidence(platform, url, data):
        started = time.monotonic()
        EvidenceStore().record_detail(platform=platform, url=url, response=data)
        data.setdefault('timings', {})['evidence_archive_s'] = round(time.monotonic()-started, 3)
    started = time.monotonic()
    result = execute_detail_request(request, failure_cache=detail_failure_cache,
        handle=lambda req: resolve_detail_response(req, strategy=strategy, collected=collected), bind_tasks=attach_detail_task_scope,
        record_evidence=record_evidence)
    result.setdefault('timings', {})['worker_detail_wall_s'] = round(time.monotonic()-started, 3)
    return result
