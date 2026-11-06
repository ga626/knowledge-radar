"""知乎借用通道适配；采集与增强复用公共执行和证据层。"""
from kr_core.models import PlatformCapability, SearchRequest, DetailResponse
from .common import BorrowedSearchAdapter

class ZhihuAdapter(BorrowedSearchAdapter):
    platform = '知乎'
    collector_module = 'zhihu'
    capabilities = PlatformCapability(platform=platform, search=True, detail=True, comments=True,
        media_extract=True, login_required=False, strategies=['browser_link_native', 'borrowed_daily_browser', 'staged_public_ocr'],
        notes='读取获准可见内容；回答按ID，问题返回有界回答目录；评论保留可见回复关系；视频仅页面说明，不承诺转写。')

    def __init__(self, search_func=None): self._search_func = search_func or search_from_request

    def search(self, request: SearchRequest): return self._call_legacy(request)

    @staticmethod
    def matches_url(url):
        from collectors.platform.zhihu import parse_target
        try: parse_target(url); return True
        except (ValueError, TypeError): return False

    @classmethod
    def cancel_task(cls, task_id): return cls.collector()._site().cancel_task(task_id)

    @classmethod
    def resource_ready(cls, resource):
        if resource != 'browser_link:zhihu': return None
        from runtime.leases import get_runtime_lease_coordinator
        if get_runtime_lease_coordinator().resource_leases('browser_site', 'daily-chrome:zhihu'): return False
        from runtime.browser_link import BrowserLink
        health = BrowserLink().health()
        return not health.get('connected') or (not health.get('draining') and int(health.get('pending', 0)) < 2)

    @staticmethod
    def enhance_detail(request, collected):
        from detail_strategies.zhihu import enhance_detail
        from understanding.evidence import build_detail_evidence
        data = enhance_detail(request, collected)
        return DetailResponse.from_legacy('知乎', request.url, data, evidence=build_detail_evidence(request.url, '知乎', data))


def search_from_request(request):
    from collectors.platform.zhihu import legacy_search_zhihu
    from detail_strategies.zhihu import _with_browser
    from runtime.borrowed_observations import observe_borrowed_result
    return observe_borrowed_result('zhihu', _with_browser('zhihu_search', lambda: legacy_search_zhihu(request.keyword, limit=request.limit)))


def register(target_registry):
    from detail_strategies.zhihu import ZhihuDetailStrategy
    target_registry.register(ZhihuAdapter(), detail_factory=ZhihuDetailStrategy)
