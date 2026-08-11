"""Model-visible affordances for search results."""

from __future__ import annotations

import re
from typing import Any, Dict, List
from urllib.parse import urlparse


AFFORDANCE_SCHEMA = "knowledgeradar-result-affordance/v1"
AFFORDANCE_FIELDS = {
    "affordance_schema",
    "detail_supported",
    "detail_tool",
    "content_modalities",
    "detail_capabilities",
    "expensive_capabilities",
    "detail_wait_policy",
    "detail_unavailable_reason",
    "recommended_extract_tool",
    "evidence_affordance_schema",
    "source_ecology",
    "evidence_role",
    "evidence_strength",
    "recommended_verification",
    "source_limitations",
}


def attach_result_affordance(platform: str, item: Dict[str, Any]) -> Dict[str, Any]:
    """Attach stable detail affordance fields without exposing internal routes."""

    data = dict(item)
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        cleaned = {key: value for key, value in metadata.items() if key not in AFFORDANCE_FIELDS}
        if len(cleaned) != len(metadata):
            data["metadata"] = cleaned
    affordance = result_affordance(platform, data)
    for key, value in affordance.items():
        if data.get(key) in (None, ""):
            data[key] = value
    return data


def result_affordance(platform: str, item: Dict[str, Any]) -> Dict[str, Any]:
    platform_name = str(item.get("platform") or platform or "").strip()
    url = str(item.get("url") or "").strip()
    content_type = _normalized_content_type(item)
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    evidence = evidence_affordance(platform_name, host, content_type)

    has_http_url = bool(url.startswith(("http://", "https://")))
    detail_supported = has_http_url and _is_supported_detail_platform(platform_name, host, url, content_type)
    modalities: List[str] = ["text"]
    capabilities: List[str] = ["metadata"]
    expensive: List[str] = []

    if _is_video_platform(platform_name, host, content_type):
        modalities = ["text", "video", "audio", "comments"]
        capabilities = ["metadata", "comments", "transcript", "direct_media", "video_analysis"]
        expensive = ["auto_multimodal", "enable_deep_analysis", "enable_comment_filtering"]
    elif _is_xiaohongshu(platform_name, host):
        modalities = ["text", "image", "video", "comments"]
        capabilities = ["metadata", "full_text", "image_ocr", "comments"]
        if content_type in {"video", "视频"}:
            capabilities.append("video_analysis")
        expensive = ["auto_multimodal", "enable_deep_analysis", "enable_comment_filtering"]
    elif _is_zhihu(platform_name, host):
        modalities = ["text", "comments"]
        capabilities = ["metadata", "full_text", "comments"]
        if content_type in {"zvideo", "video", "视频"}:
            modalities.extend(["video", "audio"])
            capabilities.extend(["transcript", "video_analysis"])
            expensive.extend(["auto_multimodal", "enable_deep_analysis"])
        expensive.append("enable_comment_filtering")
    elif has_http_url:
        capabilities = ["metadata", "full_text"]

    reason = ""
    if not has_http_url:
        reason = "missing_http_url"
    elif not detail_supported:
        reason = "generic_web_use_extract_web_page"
    wait_policy = {
        "may_return_background_tasks": bool(set(expensive) & {"auto_multimodal", "enable_deep_analysis"}),
        "wait_tool": "get_task_status" if detail_supported else "",
        "result_reread_tool": "get_content_detail" if detail_supported else "",
    }
    affordance = {
        "affordance_schema": AFFORDANCE_SCHEMA,
        "detail_supported": detail_supported,
        "detail_tool": "get_content_detail" if detail_supported else "",
        "content_modalities": _dedupe(modalities),
        "detail_capabilities": _dedupe(capabilities),
        "expensive_capabilities": _dedupe(expensive),
        "detail_wait_policy": wait_policy,
        **evidence,
        **({"detail_unavailable_reason": reason} if reason else {}),
    }
    if has_http_url and not detail_supported:
        affordance["recommended_extract_tool"] = "extract_web_page"
    return affordance


def evidence_affordance(platform: str, host: str, content_type: str = "") -> Dict[str, Any]:
    """Return model-visible evidence semantics for a search result."""

    if _is_wechat_article(platform, host):
        return {
            "evidence_affordance_schema": "knowledgeradar-evidence-affordance/v1",
            "source_ecology": "wechat_public_article_ecology",
            "evidence_role": "public_article_candidate",
            "evidence_strength": "candidate_until_detail_or_cross_checked",
            "recommended_verification": ["extract_article_body", "verify_account_or_source", "cross_check_with_other_sources"],
            "source_limitations": ["open_web_index_coverage_varies", "article_body_extraction_may_fail"],
        }
    if _is_video_platform(platform, host, content_type):
        ecology = "youtube_video_ecology" if "youtube" in f"{platform} {host}".lower() or "youtu.be" in host else "bilibili_video_ecology"
        return {
            "evidence_affordance_schema": "knowledgeradar-evidence-affordance/v1",
            "source_ecology": ecology,
            "evidence_role": "media_context_candidate",
            "evidence_strength": "candidate_until_transcript_detail_or_cross_checked",
            "recommended_verification": ["inspect_metadata", "extract_transcript_or_detail", "cross_check_key_claims"],
            "source_limitations": ["transcript_or_comment_tasks_may_be_costly", "engagement_metrics_are_not_representative_proof"],
        }
    if _is_xiaohongshu(platform, host):
        return {
            "evidence_affordance_schema": "knowledgeradar-evidence-affordance/v1",
            "source_ecology": "xiaohongshu_experience_ecology",
            "evidence_role": "experience_signal_candidate",
            "evidence_strength": "weak_to_contextual_until_sample_and_detail_checked",
            "recommended_verification": ["inspect_detail_when_available", "watch_for_marketing_or_sampling_bias", "avoid_population_level_claims_from_single_posts"],
            "source_limitations": ["login_or_cooldown_can_block_detail", "sample_bias_is_expected"],
        }
    if _is_zhihu(platform, host):
        return {
            "evidence_affordance_schema": "knowledgeradar-evidence-affordance/v1",
            "source_ecology": "zhihu_discussion_ecology",
            "evidence_role": "viewpoint_or_argument_candidate",
            "evidence_strength": "contextual_until_identity_and_facts_checked",
            "recommended_verification": ["inspect_answer_or_article_detail", "separate_viewpoint_from_fact", "cross_check_factual_claims"],
            "source_limitations": ["votes_are_not_accuracy", "login_or_anti_bot_can_limit_detail"],
        }
    if "github.com" in host or "github" in platform.lower():
        return {
            "evidence_affordance_schema": "knowledgeradar-evidence-affordance/v1",
            "source_ecology": "github_repository_ecology",
            "evidence_role": "implementation_or_project_candidate",
            "evidence_strength": "stronger_after_repository_detail_inspection",
            "recommended_verification": ["inspect_repository_metadata", "check_readme_or_recent_activity", "avoid_using_stars_as_quality_proof"],
            "source_limitations": ["rate_limits_or_auth_can_affect_search", "popularity_metrics_are_indirect"],
        }
    if host:
        return {
            "evidence_affordance_schema": "knowledgeradar-evidence-affordance/v1",
            "source_ecology": "generic_web_ecology",
            "evidence_role": "open_web_candidate",
            "evidence_strength": "depends_on_source_authority_and_extracted_content",
            "recommended_verification": ["extract_page_body", "assess_source_authority", "cross_check_important_claims"],
            "source_limitations": ["search_snippets_are_not_evidence", "dynamic_pages_may_need_fallback_extraction"],
        }
    return {
        "evidence_affordance_schema": "knowledgeradar-evidence-affordance/v1",
        "source_ecology": "unknown",
        "evidence_role": "metadata_only_candidate",
        "evidence_strength": "weak_until_url_or_source_verified",
        "recommended_verification": ["find_source_url", "cross_check_with_citable_sources"],
        "source_limitations": ["missing_url"],
    }


def _normalized_content_type(item: Dict[str, Any]) -> str:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    return str(item.get("content_type") or metadata.get("type") or metadata.get("content_type") or "").strip().lower()


def _is_video_platform(platform: str, host: str, content_type: str) -> bool:
    text = f"{platform} {host} {content_type}".lower()
    return "b站" in text or "bilibili" in text or "youtube" in text or "youtu.be" in text


def _is_xiaohongshu(platform: str, host: str) -> bool:
    text = f"{platform} {host}".lower()
    return "小红书" in text or "xiaohongshu" in text or "xhslink" in text


def _is_zhihu(platform: str, host: str) -> bool:
    text = f"{platform} {host}".lower()
    return "知乎" in text or "zhihu" in text


def _is_wechat_article(platform: str, host: str) -> bool:
    text = f"{platform} {host}".lower()
    return "微信公众号" in text or "wechat" in text or "mp.weixin.qq.com" in text


def _is_recruitment_detail_platform(platform: str, host: str) -> bool:
    text = f"{platform} {host}".lower()
    return "猎聘" in text or "liepin" in text or "boss直聘" in text or "zhipin" in text


def _is_supported_detail_platform(platform: str, host: str, url: str, content_type: str) -> bool:
    return (
        _is_video_platform(platform, host, content_type)
        or _is_xiaohongshu(platform, host)
        or _is_zhihu(platform, host)
        or _is_recruitment_detail_platform(platform, host)
        or _looks_like_bilibili_id(url)
    )


def _looks_like_bilibili_id(url: str) -> bool:
    return bool(re.search(r"\b(?:BV[a-zA-Z0-9]{10,12}|av\d+)\b", url, flags=re.IGNORECASE))


def _dedupe(values: List[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
