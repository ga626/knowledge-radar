"""V2EX讨论适配器；HTTP不申请浏览器。"""
import re
from urllib.parse import urlsplit
from kr_core.models import PlatformCapability
from .common import LegacySearchAdapterMixin


class V2exAdapter(LegacySearchAdapterMixin):
    platform = 'V2EX'
    capabilities = PlatformCapability(platform=platform, search=True, detail=True, comments=True,
        media_extract=False, login_required=False, strategies=['public_http', 'optional_official_v2'],
        notes='jobs节点分页和帖子回复；官方没有全站关键词API，历史发现由开放索引补充。')

    def __init__(self): self._search_func = search_from_request
    def search(self, request): return self._call_legacy(request)

    @staticmethod
    def matches_url(url):
        p = urlsplit(url)
        return p.hostname in {'www.v2ex.com', 'v2ex.com', 'hk.v2ex.com'} and bool(re.fullmatch(r'/t/\d+/?', p.path))


def search_from_request(request):
    from collectors.platform.v2ex import legacy_search_v2ex
    return legacy_search_v2ex(request.keyword, request.limit, city=request.options.get('city', ''),
        page=request.options.get('page',1),pages=request.options.get('pages',2),offset=request.options.get('offset',0))


def detail_strategy():
    from detail_strategies.legacy import LegacyDetailStrategy
    from collectors.platform.v2ex import detail
    from understanding.evidence import build_detail_evidence
    def extract(request):
        comments = request.options.get('comments') or {}
        # V2EX公开回复是平面流；不根据@昵称推测回复关系。
        budget = min(int(comments.get('root_limit', 100)), int(comments.get('total_limit', request.options.get('comment_limit', 50))))
        return detail(request.url,
            include_comments=bool(request.options.get('include_comments', request.enable_comment_filtering)),
            comment_limit=max(1, budget) if comments else max(1, min(100, budget)), comment_max_pages=max(1, min(20, int(comments.get('max_pages', 2)))),
            comment_page=request.options.get('comment_page',1), comment_offset=request.options.get('comment_offset',0))
    return LegacyDetailStrategy(platform='V2EX', extractor=extract, evidence_builder=build_detail_evidence)


def register(target_registry):
    target_registry.register(V2exAdapter(), detail_factory=detail_strategy)
