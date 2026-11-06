"""脉脉薄适配器；不持有浏览器或MCP门面。"""
from kr_core.models import PlatformCapability
from .common import BorrowedSearchAdapter
from urllib.parse import urlsplit

class MaimaiAdapter(BorrowedSearchAdapter):
    platform='脉脉'
    collector_module='maimai'
    capabilities=PlatformCapability(platform=platform,search=True,detail=True,comments=True,
        media_extract=False,login_required=True,strategies=['public_http_detail', 'borrowed_daily_browser', 'browser_link_native'],
        notes='本人可见社区讨论和可见评论，公开分享HTTP优先；不承诺全量。')

    def __init__(self,search_func=None):
        self._search_func=search_func or search_from_request

    @classmethod
    def cancel_task(cls, task_id): return cls.collector()._site().cancel_task(task_id)

    @staticmethod
    def matches_url(url):
        p=urlsplit(url)
        return p.hostname in {'maimai.cn'} and (p.path.startswith(('/community/feed-detail/','/community/gossip-detail/','/web/gossip_detail','/content/')) or p.path=='/article/detail')


def search_from_request(request):
    from collectors.platform.maimai import legacy_search_maimai
    return legacy_search_maimai(request.keyword,limit=request.limit)


def detail_strategy():
    from understanding.evidence import build_detail_evidence
    from detail_strategies.legacy import LegacyDetailStrategy
    from collectors.platform.maimai import discussion_detail
    def extract(request):
        return discussion_detail(request.url,include_comments=bool(request.options.get('include_comments', request.enable_comment_filtering)),
            comment_limit=int(request.options.get('comment_limit',20)))
    return LegacyDetailStrategy(platform='脉脉',extractor=extract,evidence_builder=build_detail_evidence)


def register(target_registry,search_func=None):
    target_registry.register(MaimaiAdapter(search_func),detail_factory=detail_strategy)
