"""Shared helpers for wrapping legacy platform search functions."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from kr_core.errors import ErrorCode, KnowledgeRadarError
from kr_core.models import SearchRequest, SearchResponse

LegacySearchCallable = Callable[[SearchRequest], Dict[str, Any]]


def classify_legacy_error(platform: str, error_data: Dict[str, Any] | str) -> KnowledgeRadarError:
    if not isinstance(error_data, dict):
        error_data = {"error": str(error_data), "type": str(error_data)}
    message = str(error_data.get("error") or error_data.get("message") or "平台搜索失败")
    raw_type = str(error_data.get("type") or "").lower()
    retryable = bool(error_data.get("retryable"))

    platform_state = str(error_data.get("platform_state") or "").lower()
    if raw_type in {"access_forbidden", "legal_restriction", "not_found", "content_restricted",
                    "page_identity_changed", "page_structure_changed", "zhihu_adapter_incompatible",
                    "native_host_unavailable", "native_host_unreachable", "browser_channel_not_connected"}:
        code = ErrorCode.REQUEST_FAILED
        retryable = False
    elif raw_type == "recruitment_gate_unavailable":
        code = ErrorCode.REQUEST_FAILED
        retryable = False
    elif platform_state == "empty_results" or raw_type == "empty_results":
        code = ErrorCode.EMPTY_RESULTS
        retryable = False
    elif platform_state in {"login_required", "first_login_required"} or raw_type in {"auth_required", "login_required"} or error_data.get("login_required"):
        code = ErrorCode.LOGIN_REQUIRED
        retryable = False
    elif platform_state in {"manual_action_required", "captcha_required", "security_verification", "platform_verification_required"}:
        code = ErrorCode.ANTI_BOT_VERIFICATION
        retryable = False
    elif (
        "verification" in platform_state
        or "风控" in message
        or "拦截" in message
        or "验证" in message
        or "扫码查看" in message
        or raw_type in {"security_block", "anti_bot_verification"}
        or "captcha" in raw_type
        or "verify" in raw_type
        or "verification" in raw_type
        or "anti_bot_verification" in raw_type
    ):
        code = ErrorCode.ANTI_BOT_VERIFICATION
        retryable = False
    elif "登录" in message or "cookie" in message.lower():
        code = ErrorCode.LOGIN_REQUIRED
        retryable = False
    elif "429" in message or "限流" in message or "rate" in raw_type:
        code = ErrorCode.RATE_LIMITED
        retryable = False
    elif "cdp" in raw_type or "chrome" in message.lower() or "933" in message:
        code = ErrorCode.CDP_UNAVAILABLE
        retryable = True
    elif "not_configured" in raw_type or "api_key" in message.lower() or "api key" in message.lower():
        code = ErrorCode.REQUEST_FAILED
        retryable = False
    elif "parse" in raw_type or "解析" in message:
        code = ErrorCode.PARSE_FAILED
    elif "空" in message or "empty" in raw_type:
        code = ErrorCode.EMPTY_RESULTS
        retryable = True
    else:
        code = ErrorCode.REQUEST_FAILED

    return KnowledgeRadarError(
        message,
        code=code,
        platform=platform,
        retryable=retryable,
        detail=str(error_data.get("detail") or error_data.get("hint") or ""),
        metadata={**{k: v for k, v in error_data.items() if k not in {"error", "message", "type", "retryable", "detail", "hint"}},
                  "failure_type": error_data.get("failure_type") or raw_type},
    )


def response_from_legacy(platform: str, data: Dict[str, Any]) -> SearchResponse:
    metadata = {k: v for k, v in data.items() if k not in {"items", "total", "platform", "error"}}
    if isinstance(metadata.get("metadata"), dict):
        nested_metadata = metadata.pop("metadata")
        metadata = {**nested_metadata, **metadata}
    if "error" in data and data.get("error"):
        error_data = data["error"] if isinstance(data["error"], dict) else {**metadata, "error": data["error"], "type": data.get("failure_type") or str(data["error"])}
        error_response = SearchResponse.from_error(platform, classify_legacy_error(platform, error_data))
        items = data.get("items") or []
        if items:
            return SearchResponse(
                platform=error_response.platform,
                items=[item for item in SearchResponse.from_legacy_items(platform, items).items],
                error=error_response.error,
                metadata=metadata,
            )
        if metadata:
            return SearchResponse(platform=error_response.platform, items=[], error=error_response.error, metadata=metadata)
        return error_response

    items = data.get("items") or []
    response = SearchResponse.from_legacy_items(platform, items)
    if metadata:
        return SearchResponse(platform=response.platform, items=response.items, metadata=metadata)
    return response


class LegacySearchAdapterMixin:
    """Adapter mixin for the current server.py search functions."""

    _search_func: Optional[LegacySearchCallable]

    def _call_legacy(self, request: SearchRequest) -> SearchResponse:
        if not self._search_func:
            raise KnowledgeRadarError(
                "平台适配器尚未绑定 legacy 搜索函数",
                code=ErrorCode.UNSUPPORTED,
                platform=self.platform,
                retryable=False,
            )
        try:
            return response_from_legacy(self.platform, self._search_func(request))
        except KnowledgeRadarError:
            raise
        except Exception as exc:
            from runtime.resource_concurrency import ResourceDeferred
            from runtime.execution_context import ExecutionStopped
            if isinstance(exc, (ResourceDeferred, ExecutionStopped)):
                raise
            raise KnowledgeRadarError(
                f"{self.platform} 搜索适配器异常: {exc}",
                code=ErrorCode.REQUEST_FAILED,
                platform=self.platform,
                retryable=True,
            ) from exc


class BorrowedSearchAdapter(LegacySearchAdapterMixin):
    """只有借用执行合同；站点模块自行决定选择器和认证判据。"""
    collector_module = ''

    @classmethod
    def collector(cls):
        from importlib import import_module
        return import_module('collectors.platform.' + cls.collector_module)

    def search(self, request):
        return self._call_legacy(request)

    @classmethod
    def runtime_identity(cls): return cls.collector().runtime_identity()

    @classmethod
    def interaction_status(cls): return cls.collector().interaction_status()

    @classmethod
    def prepare_detail(cls, request):
        # Resource selection belongs to the collector; HTTP routes need no Chrome.
        return request


    @classmethod
    def resource_ready(cls, resource):
        """借用通道的就绪含义由适配器解释，公共队列只消费布尔结果。"""
        if resource != 'browser_link:' + cls.collector_module:
            return None
        from runtime.browser_link import BrowserLink
        from runtime.leases import get_runtime_lease_coordinator
        if get_runtime_lease_coordinator().resource_leases('recruitment_platform', cls.collector_module):
            return False
        health = BrowserLink().health()
        if not health.get('connected'):
            return True  # 执行一次以返回准确的断连错误，不能永久排队。
        return not health.get('draining') and int(health.get('pending', 0)) < 2

def defer_browser_busy(platform, code, *, request_executed=False, resource=''):
    """Only unsent local contention can safely return to the existing durable queue."""
    from runtime.execution_context import execution_value
    from runtime.resource_concurrency import ResourceDeferred
    if (execution_value('KR_DURABLE_WORKER') == '1' and request_executed is False
            and code in {'browser_busy', 'platform_lease_busy', 'channel_draining'}):
        raise ResourceDeferred(resource or 'browser_link:' + platform)
