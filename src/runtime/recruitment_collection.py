"""招聘搜索的领域权限与地址富化；通用采集模块不反向依赖招聘。"""
from typing import Any, Dict
from kr_core.collection import CollectionTrace, GENERIC_AFFORDANCE_EVIDENCE_STRENGTHS
from kr_core.collection import format_search_response as base_response, format_search_error as base_error
from runtime.recruitment_address import attach_recruitment_address_contract

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
    elif value in {"recruitment_gate_unavailable", "cdp_unavailable", "cdp_runtime_error", "cdp_method_error", "cdp_target_error", "cdp_version_error", "collector_script_error", "parse_failed", "request_failed", "selector_miss", "network_timeout"}:
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



def format_search_response(platform, items, **kwargs):
    result = base_response(platform, items, **kwargs)
    for index, item in enumerate(result['items']):
        item = attach_recruitment_address_contract(item, platform=platform)
        for key, value in recruitment_item_evidence_contract(platform, item).items():
            if item.get(key) in (None, '') or (key == 'evidence_strength' and item.get(key) in GENERIC_AFFORDANCE_EVIDENCE_STRENGTHS):
                item[key] = value
        result['items'][index] = item
    return result

def format_search_error(platform, error_item, **kwargs):
    result = base_error(platform, error_item, **kwargs)
    contract = search_error_evidence_contract(platform, result['error'])
    result['error'].update(contract)
    result.update(contract)
    return result
