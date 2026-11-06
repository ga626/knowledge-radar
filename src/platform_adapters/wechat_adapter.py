"""微信仅注册平台特有合同；公共执行器负责任务阶段与证据。"""

from dataclasses import replace
import uuid
from kr_core import DetailResponse
from kr_core.models import PlatformCapability
from collectors.platform import wechat


class WechatAdapter:
    platform = "微信"
    capabilities = PlatformCapability(
        platform=platform,
        search=True,
        detail=True,
        comments=False,
        media_extract=True,
        login_required=False,
        strategies=["public_http", "explicit_opencli", "shared_media"],
        notes="公开公众号文章、公共搜索/微信读书候选与公共媒体；验证停交人工，不承诺全历史；读者留言已暂停，显式请求返回暂停缺口。",
    )
    matches_url = staticmethod(wechat.matches_url)

    def search(self, request):
        # 注册表与MCP/CLI复用已注册用例，不再建立第二套搜索配置/缓存。
        from runtime.application import invoke
        from kr_core.models import SearchResponse
        from kr_core.errors import KnowledgeRadarError

        data = invoke(
            "search_wechat_articles",
            {
                "query": request.keyword,
                "limit": request.limit,
                "page": request.page,
                **{
                    k: v
                    for k, v in request.options.items()
                    if k in {"freshness", "account_hint", "source", "timeout_s", "research_task_id"}
                },
            },
        )
        response = SearchResponse.from_legacy_items(self.platform, data.get("items", []))
        error = data.get("error")
        return replace(
            response,
            metadata={k: v for k, v in data.items() if k != "items"},
            error=KnowledgeRadarError(str(error.get("message") or error.get("type") or "微信搜索失败"))
            if isinstance(error, dict)
            else None,
        )

    @staticmethod
    def runtime_identity():
        return {"contract": wechat.CONTRACT}

    @staticmethod
    def prepare_detail(request):
        return replace(request, options={"wechat_checkpoint_id": uuid.uuid4().hex, **request.options})

    @staticmethod
    def enhance_detail(request, collected):
        from detail_strategies.wechat import enhance_detail

        return DetailResponse.from_legacy(
            "微信", request.url, enhance_detail(request, collected), metadata={"strategy": "wechat_readonly"}
        )


def register(registry):
    from detail_strategies.wechat import WechatDetailStrategy

    registry.register(WechatAdapter(), detail_factory=WechatDetailStrategy)
