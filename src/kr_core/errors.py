"""Shared error taxonomy for platform adapters."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional


class ErrorCode(str, Enum):
    LOGIN_REQUIRED = "login_required"
    ANTI_BOT = "anti_bot"
    ANTI_BOT_VERIFICATION = "anti_bot_verification"
    RATE_LIMITED = "rate_limited"
    CDP_UNAVAILABLE = "cdp_unavailable"
    PARSE_FAILED = "parse_failed"
    EMPTY_RESULTS = "empty_results"
    REQUEST_FAILED = "request_failed"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class KnowledgeRadarError(RuntimeError):
    """Standard adapter error that can be returned through MCP safely."""

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode = ErrorCode.UNKNOWN,
        platform: str = "",
        retryable: bool = False,
        detail: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.platform = platform
        self.retryable = retryable
        self.detail = detail or ""
        self.metadata = metadata or {}

    def to_mcp_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "error": self.message,
            "type": self.code.value,
            "retryable": self.retryable,
        }
        if self.platform:
            data["platform"] = self.platform
        if self.detail:
            data["detail"] = self.detail
        if self.metadata:
            data["metadata"] = dict(self.metadata)
        if self.code == ErrorCode.LOGIN_REQUIRED:
            data["login_required"] = True
        if self.code in {ErrorCode.ANTI_BOT, ErrorCode.ANTI_BOT_VERIFICATION}:
            data["platform_state"] = self.metadata.get("platform_state", "platform_verification_required")
            data["manual_action_required"] = bool(self.metadata.get("manual_action_required", True))
            fallback = self.metadata.get("recommended_fallback")
            if fallback:
                data["recommended_fallback"] = fallback
        return data
