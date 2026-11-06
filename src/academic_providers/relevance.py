"""Lightweight academic result relevance helpers."""

from __future__ import annotations

import re
from typing import Iterable, List

from .models import AcademicWork
from .profile import AcademicProviderProfile
from .models import normalize_title, normalize_doi

STOP_WORDS = {"is", "the", "all", "you", "and", "for", "of", "to", "in", "an", "on",
              "a", "with", "by", "from", "study", "research", "review", "analysis",
              "关于", "研究", "分析", "综述", "探讨", "如何", "哪些", "我们", "以及", "中的"}


def has_query_evidence(query: str, work: AcademicWork) -> bool:
    doi = exact_doi_query(query)
    if doi:
        return normalize_doi(work.doi) == doi
    pmcid = re.fullmatch(r'(?i)(PMC[0-9]+)', query.strip())
    if pmcid:
        return str(work.raw.get('pmcid') or '').upper() == pmcid.group(1).upper()
    from .identity import arxiv_identifier
    identifier = arxiv_identifier(query)
    if identifier:
        actual = arxiv_identifier(work.url)
        return actual == identifier if re.search(r'v\d+$', identifier) else re.sub(r'v\d+$', '', actual) == identifier
    if work.raw.get("identity_match") is True:
        return True
    terms = [term for term in _query_terms(query) if term not in STOP_WORDS]
    if not terms:
        return True
    metadata = _normalize_text(work.title + " " + work.abstract)
    # 只做保守的词面筛选，不能冒充 Codex 的语义相关性判断。
    hits = sum(term in metadata for term in terms)
    threshold = min(2, len(terms)) if len(terms) > 3 else 1
    return hits >= threshold


def exact_doi_query(query: str) -> str:
    value = normalize_doi(query)
    if re.fullmatch(r"10\.\d{4,9}/\S+", value):
        return value
    matches = re.findall(r'10\.\d{4,9}/[-._;()/:A-Z0-9]+', str(query or ''), flags=re.I)
    return normalize_doi(matches[0].rstrip('.,;')) if len(matches) == 1 else ''


def score_metadata_relevance(query: str, work: AcademicWork) -> float:
    return score_metadata_relevance_with_profiles(query, work)


def score_metadata_relevance_with_profiles(
    query: str,
    work: AcademicWork,
    profiles: dict[str, AcademicProviderProfile] | None = None,
) -> float:
    if (exact_doi_query(query) or re.fullmatch(r'(?i)PMC[0-9]+', query.strip())) and has_query_evidence(query, work):
        return 1.0
    terms = _query_terms(query)
    if not terms:
        return 0.0
    if normalize_title(query) == normalize_title(work.title):
        return 1.0 - _profile_overlap_penalty(work, profiles or {})
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
    terms = []
    for token in re.findall(r"[a-z0-9][a-z0-9_-]+|[\u4e00-\u9fff]+", text):
        if re.fullmatch(r"[\u4e00-\u9fff]+", token) and len(token) > 3:
            terms.extend(token[i:i + 2] for i in range(len(token) - 1))
        else:
            terms.append(token)
    return list(dict.fromkeys(term for term in terms if term not in STOP_WORDS))[:48]


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
