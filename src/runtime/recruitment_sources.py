"""Recruitment source capability registry.

This module maps recruitment-specific platforms to the thin source capability
contract. It is intentionally local and side-effect free.
"""

from __future__ import annotations

from kr_core.source_capability import (
    COMMUNITY_JOB_BOARD,
    COMMUNITY_TOPIC,
    EMPTY_REQUIRES_FAILURE_CLASSIFICATION,
    JOB_CARD,
    JOB_DETAIL,
    OPEN_WEB_CANDIDATE,
    OPEN_WEB_SIGNAL,
    STRUCTURED_JOB_PLATFORM,
    UNKNOWN_EMPTY_SEMANTICS,
    UNKNOWN_SOURCE,
    VALID_NO_MATCH_FOR_SOURCE_TYPE,
    ClaimPolicy,
    SourceCapability,
    make_capability,
    source_boundary_metadata,
)


STRUCTURED_FAILURE_CLASSES = (
    "empty_results",
    "city_mismatch",
    "city_mapping_missing",
    "selector_miss",
    "login_required",
    "anti_bot_verification",
    "cdp_unavailable",
    "cdp_runtime_error",
    "collector_script_error",
    "network_timeout",
    "parse_failed",
    "request_failed",
)

ADDRESS_CAPABILITIES = {
    "boss": {
        "address_candidate_sources": ["list_json", "list_dom", "detail_json", "detail_dom", "map_component"],
        "default_address_source": "list_json",
        "structured_location_fields": ["cityName", "areaDistrict", "businessDistrict", "gps"],
        "supports_district_claim": True,
        "supports_full_address_claim": "detail_enrichment_only",
        "detail_enrichment_cost": "medium_high",
        "manual_boundary": "login_or_security_verification",
    },
    "liepin": {
        "address_candidate_sources": ["list_json", "list_dom", "detail_dom"],
        "default_address_source": "list_json",
        "structured_location_fields": ["dq", "area", "location"],
        "supports_district_claim": True,
        "supports_full_address_claim": "detail_enrichment_only",
        "detail_enrichment_cost": "medium",
        "manual_boundary": "login_or_security_verification",
    },
    "zhilian": {
        "address_candidate_sources": ["list_dom", "list_json", "detail_dom"],
        "default_address_source": "list_dom",
        "structured_location_fields": ["area", "location"],
        "supports_district_claim": True,
        "supports_full_address_claim": "detail_enrichment_only",
        "detail_enrichment_cost": "medium",
        "manual_boundary": "selector_or_runtime_failure",
    },
    "maimai": {
        "address_candidate_sources": ["open_web_signal"],
        "default_address_source": "open_web_signal",
        "structured_location_fields": [],
        "supports_district_claim": False,
        "supports_full_address_claim": False,
        "detail_enrichment_cost": "not_default",
        "manual_boundary": "not_structured_job_platform",
    },
    "v2ex": {
        "address_candidate_sources": ["community_topic_text"],
        "default_address_source": "community_topic_text",
        "structured_location_fields": [],
        "supports_district_claim": False,
        "supports_full_address_claim": False,
        "detail_enrichment_cost": "not_default",
        "manual_boundary": "not_structured_job_platform",
    },
}

RECRUITMENT_SOURCE_CAPABILITIES: dict[str, SourceCapability] = {
    "boss": make_capability(
        source_id="boss",
        source_type=STRUCTURED_JOB_PLATFORM,
        native_outputs=(JOB_CARD, JOB_DETAIL),
        claim_policy=ClaimPolicy(market_claim_allowed=True, opportunity_signal_allowed=True),
        empty_semantics=EMPTY_REQUIRES_FAILURE_CLASSIFICATION,
        valid_failure_classes=STRUCTURED_FAILURE_CLASSES,
        metadata={"address_capability": ADDRESS_CAPABILITIES["boss"]},
    ),
    "liepin": make_capability(
        source_id="liepin",
        source_type=STRUCTURED_JOB_PLATFORM,
        native_outputs=(JOB_CARD, JOB_DETAIL),
        claim_policy=ClaimPolicy(market_claim_allowed=True, opportunity_signal_allowed=True),
        empty_semantics=EMPTY_REQUIRES_FAILURE_CLASSIFICATION,
        valid_failure_classes=STRUCTURED_FAILURE_CLASSES,
        metadata={"address_capability": ADDRESS_CAPABILITIES["liepin"]},
    ),
    "zhilian": make_capability(
        source_id="zhilian",
        source_type=STRUCTURED_JOB_PLATFORM,
        native_outputs=(JOB_CARD,),
        claim_policy=ClaimPolicy(market_claim_allowed=True, opportunity_signal_allowed=True),
        empty_semantics=EMPTY_REQUIRES_FAILURE_CLASSIFICATION,
        valid_failure_classes=STRUCTURED_FAILURE_CLASSES,
        metadata={"address_capability": ADDRESS_CAPABILITIES["zhilian"]},
    ),
    "maimai": make_capability(
        source_id="maimai",
        source_type=OPEN_WEB_SIGNAL,
        native_outputs=(OPEN_WEB_CANDIDATE,),
        claim_policy=ClaimPolicy(opportunity_signal_allowed=True),
        empty_semantics=VALID_NO_MATCH_FOR_SOURCE_TYPE,
        valid_failure_classes=("empty_results", "request_failed", "parse_failed"),
        metadata={"address_capability": ADDRESS_CAPABILITIES["maimai"]},
    ),
    "v2ex": make_capability(
        source_id="v2ex",
        source_type=COMMUNITY_JOB_BOARD,
        native_outputs=(COMMUNITY_TOPIC,),
        claim_policy=ClaimPolicy(opportunity_signal_allowed=True),
        empty_semantics=VALID_NO_MATCH_FOR_SOURCE_TYPE,
        valid_failure_classes=("empty_results", "request_failed", "parse_failed"),
        metadata={"address_capability": ADDRESS_CAPABILITIES["v2ex"]},
    ),
}

RECRUITMENT_SOURCE_ALIASES = {
    "BOSS直聘": "boss",
    "boss直聘": "boss",
    "boss": "boss",
    "猎聘": "liepin",
    "liepin": "liepin",
    "智联招聘": "zhilian",
    "智联": "zhilian",
    "zhilian": "zhilian",
    "脉脉": "maimai",
    "maimai": "maimai",
    "V2EX": "v2ex",
    "v2ex": "v2ex",
}


def normalize_recruitment_source(source_id: str) -> str:
    value = str(source_id or "").strip()
    return RECRUITMENT_SOURCE_ALIASES.get(value, value.lower())


def recruitment_source_capability(source_id: str) -> SourceCapability:
    normalized = normalize_recruitment_source(source_id)
    return RECRUITMENT_SOURCE_CAPABILITIES.get(
        normalized,
        make_capability(
            source_id=normalized or "unknown",
            source_type=UNKNOWN_SOURCE,
            native_outputs=(),
            claim_policy=ClaimPolicy(),
            empty_semantics=UNKNOWN_EMPTY_SEMANTICS,
        ),
    )


def recruitment_source_type(source_id: str) -> str:
    return recruitment_source_capability(source_id).source_type


def recruitment_expected_outputs(source_id: str) -> list[str]:
    return list(recruitment_source_capability(source_id).native_outputs)


def recruitment_empty_result_reason(source_id: str) -> str:
    return recruitment_source_capability(source_id).empty_semantics


def is_structured_job_platform(source_id: str) -> bool:
    return recruitment_source_type(source_id) == STRUCTURED_JOB_PLATFORM


def recruitment_source_metadata(source_id: str) -> dict[str, object]:
    return source_boundary_metadata(recruitment_source_capability(source_id))
