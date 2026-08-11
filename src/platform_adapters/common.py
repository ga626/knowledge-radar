"""Shared helpers for wrapping legacy platform search functions."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from kr_core.errors import ErrorCode, KnowledgeRadarError
from kr_core.models import SearchRequest, SearchResponse

LegacySearchCallable = Callable[[SearchRequest], Dict[str, Any]]


def classify_legacy_error(platform: str, error_data: Dict[str, Any]) -> KnowledgeRadarError:
    message = str(error_data.get("error") or error_data.get("message") or "平台搜索失败")
    raw_type = str(error_data.get("type") or "").lower()
    retryable = bool(error_data.get("retryable"))

    platform_state = str(error_data.get("platform_state") or "").lower()
    if platform_state == "empty_results" or raw_type == "empty_results":
        code = ErrorCode.EMPTY_RESULTS
        retryable = True
    elif platform_state in {"login_required", "first_login_required"} or error_data.get("login_required"):
        code = ErrorCode.LOGIN_REQUIRED
        retryable = True
    elif platform_state in {"manual_action_required", "captcha_required", "security_verification", "platform_verification_required"}:
        code = ErrorCode.ANTI_BOT_VERIFICATION
        retryable = True
    elif (
        "verification" in platform_state
        or "风控" in message
        or "拦截" in message
        or "验证" in message
        or "扫码查看" in message
        or "captcha" in raw_type
        or "verify" in raw_type
        or "verification" in raw_type
        or "anti_bot_verification" in raw_type
    ):
        code = ErrorCode.ANTI_BOT_VERIFICATION
        retryable = True
    elif "登录" in message or "cookie" in message.lower():
        code = ErrorCode.LOGIN_REQUIRED
        retryable = True
    elif "429" in message or "限流" in message or "rate" in raw_type:
        code = ErrorCode.RATE_LIMITED
        retryable = True
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
        metadata={k: v for k, v in error_data.items() if k not in {"error", "message", "type", "retryable", "detail", "hint"}},
    )


def response_from_legacy(platform: str, data: Dict[str, Any]) -> SearchResponse:
    metadata = {k: v for k, v in data.items() if k not in {"items", "total", "platform", "error"}}
    if isinstance(metadata.get("metadata"), dict):
        nested_metadata = metadata.pop("metadata")
        metadata = {**nested_metadata, **metadata}
    if "error" in data and data.get("error"):
        error_response = SearchResponse.from_error(platform, classify_legacy_error(platform, data["error"]))
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
            raise KnowledgeRadarError(
                f"{self.platform} 搜索适配器异常: {exc}",
                code=ErrorCode.REQUEST_FAILED,
                platform=self.platform,
                retryable=True,
            ) from exc
