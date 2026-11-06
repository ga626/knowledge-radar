"""小红书业务适配器；底层身份不进入公开工具合同。"""
from runtime.execution_context import execution_value
from kr_core.models import PlatformCapability, SearchRequest, SearchResponse
from .common import response_from_legacy


class XiaohongshuAdapter:
    platform = "小红书"
    capabilities = PlatformCapability(platform=platform, search=True, detail=True,
        comments=True, media_extract=True, login_required=True,
        strategies=["opencli_browser_bridge", "borrowed_daily_browser", "stop_on_first_challenge", "budgeted_paid_fallback"],
        notes="固定日常浏览器单账号；评论有上限；KR管理自有会话，用户浏览器不被关闭。")

    def __init__(self, search_func=None):
        pass

    @staticmethod
    def matches_url(url):
        from urllib.parse import urlsplit
        return (urlsplit(url).hostname or "").lower() in {"xiaohongshu.com", "www.xiaohongshu.com", "xhslink.com"}

    @staticmethod
    def prepare_detail(request):
        return prepare_detail(request)

    @staticmethod
    def interaction_status():
        from runtime.xhs_opencli import interaction_state
        return interaction_state()

    @staticmethod
    def runtime_identity():
        from runtime.xhs_opencli import runtime_identity
        return runtime_identity()

    @staticmethod
    def enhance_detail(request, collected):
        from runtime.xhs_opencli import enhance_detail
        from kr_core import DetailResponse
        from understanding.evidence import build_detail_evidence
        data = enhance_detail(request, collected)
        return DetailResponse.from_legacy("小红书", request.url, data,
            evidence=build_detail_evidence(request.url, "小红书", data))

    def search(self, request: SearchRequest) -> SearchResponse:
        from runtime.xhs_opencli import search
        return response_from_legacy(self.platform, search(request.keyword, request.limit,
            request.search_type, options=request.options))


def prepare_detail(request):
    import re
    from dataclasses import replace
    from runtime.xhs_opencli import setting, _run
    import os
    if setting("KR_XHS_OPENCLI_CONTEXT_ID") and execution_value("KR_DURABLE_WORKER") != "1":
        readiness = _run("inventory", start_service=True)
        if not readiness.get("ok"):
            return replace(request, options={**request.options,"preparation_failure":readiness.get("failure_type") or "opencli_dependency_not_ready"})
    from urllib.parse import urlsplit
    if (urlsplit(request.url).hostname or "").lower() == "xhslink.com":
        from generic_web.url_policy import guarded_fetch, validate_detail_target
        try:
            response = guarded_fetch(request.url, timeout=8, max_bytes=512*1024)
            response.raise_for_status()
            target = str(response.url)
            validate_detail_target(target, "小红书")
            if (urlsplit(target).hostname or "").lower() not in {"xiaohongshu.com", "www.xiaohongshu.com"}:
                raise ValueError("short_link_not_resolved")
            request = replace(request, url=target)
        except Exception as exc:
            return replace(request, options={**request.options,"preparation_failure":"short_link_resolution_failed",
                "preparation_error_type":type(exc).__name__})
    from collectors.platform.xiaohongshu import _cached_xhs_record, protected_detail_url
    match = re.search(r"/([a-f0-9]{24})(?:[?/#]|$)", request.url)
    if not match:
        return request
    return replace(request, url=protected_detail_url(request.url), options={**request.options, "note_type": request.options.get("note_type") or _cached_xhs_record(match[1])[2]})


def register(target_registry):
    from detail_strategies.xiaohongshu import XiaohongshuDetailStrategy
    target_registry.register(XiaohongshuAdapter(), detail_factory=XiaohongshuDetailStrategy)
