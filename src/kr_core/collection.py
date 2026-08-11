"""Unified collection strategy trace and error formatting helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from runtime.chrome_manager import XHS_CHROME_DEBUG_PORT
from runtime.failure_tags import detect_failure_tags, describe_failure_tags
from runtime.recruitment_address import attach_recruitment_address_contract
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
    "anti_bot": "anti_bot_verification",
    "ambiguous_page_state": "ambiguous_page_state",
    "city_mismatch": "city_mismatch",
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
        return ERROR_TYPE_ALIASES[value]
    if value in ERROR_TYPE_NORMALIZED:
        return ERROR_TYPE_NORMALIZED[value]
    if any(token in text for token in ("verification", "captcha", "风控", "拦截", "验证", "扫码查看")):
        return "anti_bot_verification"
    if any(token in text for token in ("login", "cookie", "登录", "鉴权", "401", "403", "unauthorized")):
        return "login_required"
    if any(token in text for token in ("429", "rate", "限流")):
        return "rate_limited"
    if any(token in text for token in ("cdp", "chrome", XHS_CHROME_DEBUG_PORT, "12734")):
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
    if error_type in {"anti_bot_verification", "login_required", "rate_limited", "cdp_unavailable", "empty_results"}:
        retryable = True

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
    normalized.update(search_error_evidence_contract(platform, normalized, error_type=error_type))
    return normalized


def _is_recruitment_platform(platform: str) -> bool:
    text = str(platform or "").strip().lower()
    return any(token in text for token in ("boss", "boss直聘", "猎聘", "liepin", "智联", "zhilian", "脉脉", "maimai", "v2ex"))


def _is_web_fallback(item: Dict[str, Any]) -> bool:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    text = " ".join(str(value or "") for value in (item.get("source"), item.get("strategy"), metadata.get("strategy"), metadata.get("fallback_from")))
    return "web_search_fallback" in text


def recruitment_item_evidence_contract(platform: str, item: Dict[str, Any]) -> Dict[str, Any]:
    """Attach claim permissions for recruitment search candidates."""

    if not _is_recruitment_platform(platform):
        return {}
    if _is_web_fallback(item):
        return {
            "failure_class": "",
            "evidence_strength": "weak_open_index",
            "market_claim_allowed": False,
            "salary_claim_allowed": False,
        }
    normalized_platform = str(platform or "").strip().lower()
    if normalized_platform in {"v2ex"} or str(platform or "").strip() == "V2EX":
        return {
            "failure_class": "",
            "evidence_strength": "community_job_post",
            "market_claim_allowed": False,
            "salary_claim_allowed": False,
        }
    return {
        "failure_class": "",
        "evidence_strength": "medium_search_summary",
        "market_claim_allowed": True,
        "salary_claim_allowed": False,
    }


def search_error_evidence_contract(platform: str, error_item: Dict[str, Any], *, error_type: str = "") -> Dict[str, Any]:
    """Attach stable semantics for errors so reports do not infer market facts."""

    if not _is_recruitment_platform(platform):
        return {}
    value = str(error_type or error_item.get("type") or error_item.get("failure_type") or "").lower()
    manual = bool(error_item.get("manual_action_required"))
    if manual or value in {"login_required", "anti_bot_verification"}:
        failure_class = "platform_boundary_or_manual_lifecycle"
        evidence_strength = "blocked_no_claim"
    elif value in {"cdp_unavailable", "cdp_runtime_error", "cdp_method_error", "cdp_target_error", "cdp_version_error", "collector_script_error", "parse_failed", "request_failed", "selector_miss", "network_timeout"}:
        failure_class = "tool_error"
        evidence_strength = "tool_error"
    elif value in {"rate_limited"}:
        failure_class = "rate_limited"
        evidence_strength = "blocked_no_claim"
    elif value in {"city_mapping_missing"}:
        failure_class = "city_mapping_missing"
        evidence_strength = "blocked_no_claim"
    elif value in {"empty_results", "city_mismatch"}:
        failure_class = value
        evidence_strength = "no_result_signal"
    else:
        failure_class = "tool_or_platform_unknown"
        evidence_strength = "tool_error"
    return {
        "failure_class": failure_class,
        "evidence_strength": evidence_strength,
        "market_claim_allowed": False,
        "salary_claim_allowed": False,
    }


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
        if _is_recruitment_platform(platform):
            next_item = attach_recruitment_address_contract(next_item, platform=platform)
        for key, value in recruitment_item_evidence_contract(platform, next_item).items():
            if next_item.get(key) in (None, "") or (
                key == "evidence_strength" and next_item.get(key) in GENERIC_AFFORDANCE_EVIDENCE_STRENGTHS
            ):
                next_item[key] = value
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
