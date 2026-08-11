"""Lightweight academic result relevance helpers."""

from __future__ import annotations

import re
from typing import Iterable, List

from .models import AcademicWork
from .profile import AcademicProviderProfile


def score_metadata_relevance(query: str, work: AcademicWork) -> float:
    return score_metadata_relevance_with_profiles(query, work)


def score_metadata_relevance_with_profiles(
    query: str,
    work: AcademicWork,
    profiles: dict[str, AcademicProviderProfile] | None = None,
) -> float:
    terms = _query_terms(query)
    if not terms:
        return 0.0
    title = _normalize_text(work.title)
    abstract = _normalize_text(work.abstract)
    source = _normalize_text(work.source)
    title_hits = sum(1 for term in terms if term in title)
    abstract_hits = sum(1 for term in terms if term in abstract)
    source_hits = sum(1 for term in terms if term in source)
    score = 0.0
    score += 0.5 * (title_hits / len(terms))
    score += 0.3 * (abstract_hits / len(terms))
    score += 0.05 * (source_hits / len(terms))
    score += min(max(float(work.provider_confidence or 0.0), 0.0), 1.0) * 0.1
    if work.doi:
        score += 0.03
    if work.full_text_status and work.full_text_status != "metadata_only":
        score += 0.02
    score -= _profile_overlap_penalty(work, profiles or {})
    return min(1.0, round(score, 4))


def rank_by_metadata_relevance(
    query: str,
    works: Iterable[AcademicWork],
    profiles: dict[str, AcademicProviderProfile] | None = None,
) -> List[AcademicWork]:
    return sorted(
        list(works),
        key=lambda work: (
            -score_metadata_relevance_with_profiles(query, work, profiles),
            -(work.year or 0),
            work.title.lower(),
        ),
    )


def select_fulltext_candidates(query: str, works: Iterable[AcademicWork], *, top_k: int = 3, min_score: float = 0.15) -> List[AcademicWork]:
    ranked = rank_by_metadata_relevance(query, works)
    return [work for work in ranked if score_metadata_relevance(query, work) >= min_score][: max(0, int(top_k))]


def _query_terms(query: str) -> List[str]:
    text = _normalize_text(query)
    terms = [term for term in re.split(r"\s+", text) if len(term) >= 2]
    if terms:
        return terms[:12]
    compact = "".join(ch for ch in text if not ch.isspace())
    if not compact:
        return []
    return [compact[i : i + 2] for i in range(0, max(0, len(compact) - 1))][:12]


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _profile_overlap_penalty(work: AcademicWork, profiles: dict[str, AcademicProviderProfile]) -> float:
    provider_id = str(work.source_database or work.source or "").strip().lower()
    profile = profiles.get(provider_id)
    if not profile:
        return 0.0
    if profile.role in {"abstract_discovery_supplement", "narrow_fulltext_supplement"}:
        return 0.03
    if "overlap" in profile.role or "supplement" in profile.role:
        return 0.05
    return 0.0
