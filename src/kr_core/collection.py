"""Unified collection strategy trace and error formatting helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from runtime.failure_tags import detect_failure_tags, describe_failure_tags
from .affordance import attach_result_affordance


ERROR_TYPE_ALIASES = {
    "verification_required": "anti_bot",
    "captcha_required": "anti_bot",
    "captcha": "anti_bot",
    "verify": "anti_bot",
    "login": "login_required",
    "auth": "login_required",
    "unauthorized": "login_required",
    "timeout": "request_failed",
    "scrapling_error": "request_failed",
}

ERROR_TYPE_NORMALIZED = {
    "recruitment_gate_unavailable": "recruitment_gate_unavailable",
    "anti_bot": "anti_bot_verification",
    "ambiguous_page_state": "ambiguous_page_state",
    "auth_state_unconfirmed": "auth_state_unconfirmed",
    "city_mismatch": "city_mismatch",
    **{name: name for name in ("rate_limited", "dependency_missing", "configuration_error", "city_mapping_missing",
                               "login_required", "anti_bot_verification", "cdp_unavailable", "empty_results", "request_failed", "parse_failed")},
}

NON_RESULT_STRATEGIES = {"chrome_cdp_preflight", "persistent_profile_cookie"}
GENERIC_AFFORDANCE_EVIDENCE_STRENGTHS = {
    "depends_on_source_authority_and_extracted_content",
    "weak_until_url_or_source_verified",
}


@dataclass
class StrategyAttempt:
    """One executed or skipped collection strategy."""

    name: str
    status: str
    detail: str = ""
    error_type: str = ""
    retryable: bool = False
    item_count: int = 0
    elapsed_s: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "name": self.name,
            "status": self.status,
        }
        if self.detail:
            data["detail"] = self.detail
        if self.error_type:
            data["error_type"] = self.error_type
        if self.retryable:
            data["retryable"] = True
        if self.item_count:
            data["item_count"] = self.item_count
        if self.elapsed_s:
            data["elapsed_s"] = round(self.elapsed_s, 3)
        if self.metadata:
            data["metadata"] = dict(self.metadata)
        return data


@dataclass
class CollectionTrace:
    """Strategy tree and attempt trace exposed to Agents as metadata."""

    platform: str
    strategy_tree: List[str]
    attempts: List[StrategyAttempt] = field(default_factory=list)
    selected_strategy: str = ""

    def add(
        self,
        name: str,
        status: str,
        *,
        detail: str = "",
        error_type: str = "",
        retryable: bool = False,
        item_count: int = 0,
        elapsed_s: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.attempts.append(
            StrategyAttempt(
                name=name,
                status=status,
                detail=detail,
                error_type=normalize_error_type(error_type) if error_type else "",
                retryable=retryable,
                item_count=item_count,
                elapsed_s=elapsed_s,
                metadata=metadata or {},
            )
        )
        if status == "ok" and name not in NON_RESULT_STRATEGIES and not self.selected_strategy:
            self.selected_strategy = name

    def to_metadata(self) -> Dict[str, Any]:
        failed = [attempt for attempt in self.attempts if attempt.status == "failed"]
        ok = [
            attempt
            for attempt in self.attempts
            if attempt.status == "ok" and attempt.name not in NON_RESULT_STRATEGIES
        ]
        failure_tags = []
        for attempt in failed:
            failure_tags.extend(detect_failure_tags(attempt.error_type, attempt.detail, attempt.metadata))
        failure_tags = sorted(set(failure_tags))
        return {
            "collection": {
                "platform": self.platform,
                "strategy_tree": list(self.strategy_tree),
                "selected_strategy": self.selected_strategy or (ok[-1].name if ok else ""),
                "fallback_count": max(0, len(failed)),
                "attempts": [attempt.to_dict() for attempt in self.attempts],
                "failure_tags": failure_tags,
                "failure_tag_details": describe_failure_tags(failure_tags),
            }
        }


def normalize_error_type(raw_type: Any, message: str = "", platform_state: str = "") -> str:
    value = str(raw_type or "").strip().lower()
    text = f"{value} {message or ''} {platform_state or ''}".lower()
    if value in ERROR_TYPE_ALIASES:
        value = ERROR_TYPE_ALIASES[value]
    if value in ERROR_TYPE_NORMALIZED:
        return ERROR_TYPE_NORMALIZED[value]
    if any(token in text for token in ("verification", "captcha", "风控", "拦截", "验证", "扫码查看")):
        return "anti_bot_verification"
    if any(token in text for token in ("login", "cookie", "登录", "鉴权", "401", "403", "unauthorized")):
        return "login_required"
    if any(token in text for token in ("429", "rate", "限流")):
        return "rate_limited"
    if any(token in text for token in ("cdp", "chrome")):
        return "cdp_unavailable"
    if any(token in text for token in ("parse", "解析")):
        return "parse_failed"
    if any(token in text for token in ("empty", "空结果", "无结果")):
        return "empty_results"
    return value or "request_failed"


def normalize_search_error(
    platform: str,
    error_item: Dict[str, Any],
    *,
    strategy: str = "",
    stage: str = "search",
) -> Dict[str, Any]:
    message = str(error_item.get("error") or error_item.get("message") or "平台采集失败")
    error_type = normalize_error_type(
        error_item.get("type") or error_item.get("failure_type"),
        message=message,
        platform_state=str(error_item.get("platform_state") or ""),
    )
    retryable = bool(error_item.get("retryable"))
    if error_type in {"login_required", "rate_limited", "cdp_unavailable", "empty_results"}:
        retryable = True
    if error_type in {"anti_bot_verification", "recruitment_gate_unavailable"}:
        retryable = False

    normalized = dict(error_item)
    normalized["error"] = message
    normalized["type"] = error_type
    failure_tags = detect_failure_tags(error_type, message, error_item)
    if not bool(error_item.get("manual_action_required")) and error_type not in {"login_required", "anti_bot_verification"}:
        failure_tags = [tag for tag in failure_tags if tag not in {"login_required", "anti_bot_verification"}]
    normalized["failure_tags"] = failure_tags
    normalized["retryable"] = retryable
    normalized["platform"] = platform
    normalized["stage"] = stage
    if strategy:
        normalized["strategy"] = strategy
    if error_type == "login_required":
        normalized["login_required"] = True
    if error_type == "anti_bot_verification":
        normalized.setdefault("platform_state", "platform_verification_required")
        normalized.setdefault("manual_action_required", True)
        normalized.setdefault("login_required", False)
        normalized.setdefault("recommended_fallback", "external_search_then_detail")
    return normalized


def trace_metadata(trace: Optional[CollectionTrace]) -> Dict[str, Any]:
    return trace.to_metadata() if trace else {}


def format_search_response(
    platform: str,
    items: List[Dict[str, Any]],
    *,
    trace: Optional[CollectionTrace] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    enriched_items = []
    for item in items:
        next_item = attach_result_affordance(platform, item)
        enriched_items.append(next_item)
    data: Dict[str, Any] = {
        "items": enriched_items,
        "total": len(items),
        "platform": platform,
    }
    merged_metadata = trace_metadata(trace)
    if metadata:
        merged_metadata.update(metadata)
    if merged_metadata:
        data["metadata"] = merged_metadata
    return data


def format_search_error(
    platform: str,
    error_item: Dict[str, Any],
    *,
    trace: Optional[CollectionTrace] = None,
    strategy: str = "",
    stage: str = "search",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    normalized_error = normalize_search_error(platform, error_item, strategy=strategy, stage=stage)
    data: Dict[str, Any] = {"items": [], "total": 0, "platform": platform, "error": normalized_error}
    for key in ("failure_class", "evidence_strength", "market_claim_allowed", "salary_claim_allowed"):
        if key in normalized_error:
            data[key] = normalized_error[key]
    merged_metadata = trace_metadata(trace)
    if metadata:
        merged_metadata.update(metadata)
    if merged_metadata:
        data["metadata"] = merged_metadata
    return data


def strategy_tree(*strategies: Iterable[str] | str) -> List[str]:
    names: List[str] = []
    for strategy in strategies:
        if isinstance(strategy, str):
            names.append(strategy)
        else:
            names.extend(str(item) for item in strategy)
    return names
