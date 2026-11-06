"""BOSS 薄适配器：日常Chrome借用与站点采集分开。"""

from __future__ import annotations

from typing import Optional

from kr_core.models import PlatformCapability, SearchRequest, SearchResponse

from .common import BorrowedSearchAdapter, LegacySearchCallable


class BossAdapter(BorrowedSearchAdapter):
    collector_module = "boss"
    platform = "BOSS直聘"
    capabilities = PlatformCapability(
        platform=platform,
        search=True,
        detail=True,
        comments=False,
        media_extract=False,
        login_required=True,
        strategies=["browser_link_native", "borrowed_daily_browser", "stop_on_first_challenge"],
        notes="KR浏览器连接器借用日常Chrome；仅回收KR工作标签，登录等待不占执行槽。",
    )

    def __init__(self, search_func: Optional[LegacySearchCallable] = None) -> None:
        self._search_func = search_func or search_from_request

    @staticmethod
    def matches_url(url):
        from urllib.parse import urlsplit
        p = urlsplit(url)
        return p.hostname in {'www.zhipin.com', 'zhipin.com'} and p.path.startswith('/job_detail/')

    def search(self, request: SearchRequest) -> SearchResponse:
        return self._call_legacy(request)

    @classmethod
    def cancel_task(cls, task_id):
        return cls.collector().cancel_task(task_id)

    @classmethod
    def prepare_detail(cls, request):
        return request



def search_from_request(request):
    from collectors.platform.boss import search
    return search(request.keyword, city=request.options.get("city", ""), limit=request.limit)


def detail_strategy():
    from collectors.platform.boss import detail
    from detail_strategies.recruitment import RecruitmentDetailStrategy
    from understanding.evidence import build_detail_evidence
    return RecruitmentDetailStrategy(platform="BOSS直聘", extractor=detail,
                                    evidence_builder=build_detail_evidence)


def register(target_registry, search_func=None):
    target_registry.register(BossAdapter(search_func), detail_factory=detail_strategy)
