"""Thin source capability contracts shared by domain modules.

The contract describes what a source can prove, not how a platform is
collected. Runtime adapters stay responsible for network, browser, and selector
details.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


STRUCTURED_JOB_PLATFORM = "structured_job_platform"
COMMUNITY_JOB_BOARD = "community_job_board"
OPEN_WEB_SIGNAL = "open_web_signal"
UNKNOWN_SOURCE = "unknown_source"

JOB_CARD = "job_card"
JOB_DETAIL = "job_detail"
COMMUNITY_TOPIC = "community_topic"
OPEN_WEB_CANDIDATE = "open_web_candidate"

EMPTY_REQUIRES_FAILURE_CLASSIFICATION = "empty_results_requires_failure_classification"
VALID_NO_MATCH_FOR_SOURCE_TYPE = "valid_no_match_for_source_type"
UNKNOWN_EMPTY_SEMANTICS = "unknown_empty_semantics"

MARKET_CLAIM = "market_claim"
SALARY_CLAIM = "salary_claim"
REPRESENTATIVE_CLAIM = "representative_claim"
OPPORTUNITY_SIGNAL = "opportunity_signal"


@dataclass(frozen=True)
class ClaimPolicy:
    market_claim_allowed: bool = False
    salary_claim_allowed: bool = False
    representative_claim_allowed: bool = False
    opportunity_signal_allowed: bool = True

    def to_dict(self) -> dict[str, bool]:
        return {
            "market_claim_allowed": self.market_claim_allowed,
            "salary_claim_allowed": self.salary_claim_allowed,
            "representative_claim_allowed": self.representative_claim_allowed,
            "opportunity_signal_allowed": self.opportunity_signal_allowed,
        }


@dataclass(frozen=True)
class SourceCapability:
    source_id: str
    source_type: str
    native_outputs: tuple[str, ...]
    claim_policy: ClaimPolicy
    empty_semantics: str
    valid_failure_classes: tuple[str, ...] = ()
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "native_outputs": list(self.native_outputs),
            "claim_policy": self.claim_policy.to_dict(),
            "empty_semantics": self.empty_semantics,
            "valid_failure_classes": list(self.valid_failure_classes),
            "metadata": dict(self.metadata or {}),
        }


def make_capability(
    *,
    source_id: str,
    source_type: str,
    native_outputs: Iterable[str],
    claim_policy: ClaimPolicy | None = None,
    empty_semantics: str = UNKNOWN_EMPTY_SEMANTICS,
    valid_failure_classes: Iterable[str] = (),
    metadata: dict[str, Any] | None = None,
) -> SourceCapability:
    return SourceCapability(
        source_id=source_id,
        source_type=source_type,
        native_outputs=tuple(str(output) for output in native_outputs),
        claim_policy=claim_policy or ClaimPolicy(),
        empty_semantics=empty_semantics,
        valid_failure_classes=tuple(str(value) for value in valid_failure_classes),
        metadata=dict(metadata or {}),
    )


def claim_policy_metadata(capability: SourceCapability) -> dict[str, bool]:
    return capability.claim_policy.to_dict()


def source_boundary_metadata(capability: SourceCapability) -> dict[str, Any]:
    return {
        "source_type": capability.source_type,
        "native_outputs": list(capability.native_outputs),
        "empty_semantics": capability.empty_semantics,
        "claim_policy": capability.claim_policy.to_dict(),
        "metadata": dict(capability.metadata or {}),
    }
