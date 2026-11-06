"""猎聘薄适配器；不持有浏览器或MCP门面。"""
from kr_core.models import PlatformCapability
from .common import BorrowedSearchAdapter
from urllib.parse import urlsplit

class LiepinAdapter(BorrowedSearchAdapter):
    platform='猎聘'
    collector_module='liepin'
    capabilities=PlatformCapability(platform=platform,search=True,detail=True,comments=False,
        media_extract=False,login_required=False,strategies=['public_http_detail', 'borrowed_daily_browser', 'browser_link_native'],
        notes='公开HTTP详情优先；动态搜索和详情借用日常Chrome。')

    def __init__(self,search_func=None):
        self._search_func=search_func or search_from_request

    @classmethod
    def cancel_task(cls, task_id): return cls.collector()._site().cancel_task(task_id)

    @staticmethod
    def matches_url(url):
        p=urlsplit(url)
        return p.hostname in {'liepin.com', 'www.liepin.com'} and p.path.startswith(('/job/','/a/'))


def search_from_request(request):
    from collectors.platform.liepin import legacy_search_liepin
    return legacy_search_liepin(request.keyword,limit=request.limit,city=request.options.get("city", ""))


def detail_strategy():
    from understanding.evidence import build_detail_evidence
    from detail_strategies.recruitment import RecruitmentDetailStrategy
    from collectors.platform.liepin import detail
    return RecruitmentDetailStrategy(platform='猎聘',extractor=detail,evidence_builder=build_detail_evidence)


def register(target_registry,search_func=None):
    target_registry.register(LiepinAdapter(search_func),detail_factory=detail_strategy)
