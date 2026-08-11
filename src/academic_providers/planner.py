"""Profile-driven academic search route planning."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Dict, Iterable, List

from .models import AcademicSearchRequest
from .profile import AcademicProviderProfile


CHINESE_MAIN_CHAIN_ROLE = "default_chinese_fulltext"
GLOBAL_METADATA_ROLE = "global_metadata"


@dataclass(frozen=True)
class AcademicQueryIntent:
    language: str
    citation_import: bool = False
    arxiv_like: bool = False
    doi_like: bool = False
    chinese_like: bool = False
    disciplines: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class AcademicRoutePlan:
    provider_order: List[str]
    intent: AcademicQueryIntent
    waves: Dict[str, List[str]] = field(default_factory=dict)


def plan_academic_search(
    request: AcademicSearchRequest,
    provider_name: str,
    profiles: Dict[str, AcademicProviderProfile],
    *,
    baidu_available: bool = False,
    core_available: bool = False,
    serpapi_available: bool = False,
) -> AcademicRoutePlan:
    if provider_name != "auto":
        return AcademicRoutePlan(provider_order=[provider_name], intent=analyze_academic_query(request), waves={"explicit": [provider_name]})
    intent = analyze_academic_query(request)
    if intent.chinese_like:
        order = _chinese_order(profiles, citation_import_first=intent.citation_import, disciplines=intent.disciplines)
    else:
        order = _global_metadata_order(profiles, citation_import_first=intent.citation_import, disciplines=intent.disciplines)
    if intent.arxiv_like:
        if "arxiv" in profiles and "arxiv" not in order:
            order.append("arxiv")
        if "ar5iv" in profiles and "ar5iv" not in order:
            order.append("ar5iv")
    if intent.doi_like and "unpaywall" in profiles and "unpaywall" not in order:
        order.insert(0, "unpaywall")
    if baidu_available and "baidu_scholar" in profiles and intent.chinese_like:
        order.append("baidu_scholar")
    if not core_available:
        order = [name for name in order if name != "core"]
    if serpapi_available and "serpapi_scholar" in profiles:
        order.append("serpapi_scholar")
    return AcademicRoutePlan(provider_order=_dedupe_order(order), intent=intent, waves=_waves(order, profiles))


def analyze_academic_query(request: AcademicSearchRequest) -> AcademicQueryIntent:
    query = str(request.query or "")
    chinese_like = _looks_like_chinese_query(query)
    return AcademicQueryIntent(
        language="zh" if chinese_like else "en",
        citation_import=_looks_like_citation_import(request),
        arxiv_like=_looks_like_arxiv_query(query),
        doi_like=_looks_like_doi_query(query),
        chinese_like=chinese_like,
        disciplines=_detect_disciplines(query),
    )


def _chinese_order(profiles: Dict[str, AcademicProviderProfile], *, citation_import_first: bool, disciplines: List[str]) -> List[str]:
    prefix = ["citation_import"] if citation_import_first and "citation_import" in profiles else []
    main_chain = _sort_by_profile_priority(
        (
            profile
            for profile in profiles.values()
            if profile.role == CHINESE_MAIN_CHAIN_ROLE and profile.default_policy == "auto"
        ),
        priority_key="fulltext",
        disciplines=disciplines,
    )
    global_metadata = _global_metadata_order(profiles, citation_import_first=False, disciplines=disciplines)
    return prefix + [profile.id for profile in main_chain] + [name for name in global_metadata if name not in prefix]


def _global_metadata_order(profiles: Dict[str, AcademicProviderProfile], *, citation_import_first: bool, disciplines: List[str]) -> List[str]:
    prefix = ["citation_import"] if citation_import_first and "citation_import" in profiles else []
    preferred = ["openalex", "crossref", "semanticscholar"]
    ordered = [name for name in preferred if name in profiles]
    remaining = _sort_by_profile_priority(
        (
            profile
            for profile in profiles.values()
            if profile.role == GLOBAL_METADATA_ROLE and profile.id not in set(ordered)
        ),
        priority_key="discovery",
        disciplines=disciplines,
    )
    return prefix + ordered + [profile.id for profile in remaining]


def _sort_by_profile_priority(
    profiles: Iterable[AcademicProviderProfile],
    *,
    priority_key: str,
    disciplines: List[str] | None = None,
) -> List[AcademicProviderProfile]:
    discipline_set = set(disciplines or [])
    return sorted(
        profiles,
        key=lambda profile: (
            -int(profile.runtime.priority.get(priority_key, 0)),
            -len(discipline_set.intersection(profile.disciplines)),
            profile.id,
        ),
    )


def _waves(order: List[str], profiles: Dict[str, AcademicProviderProfile]) -> Dict[str, List[str]]:
    waves: Dict[str, List[str]] = {}
    for provider_id in order:
        profile = profiles.get(provider_id)
        if not profile:
            continue
        for wave in profile.wave:
            waves.setdefault(wave, []).append(provider_id)
    return waves


def _dedupe_order(order: List[str]) -> List[str]:
    seen = set()
    result = []
    for name in order:
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result


def _looks_like_citation_import(request: AcademicSearchRequest) -> bool:
    query = str(request.query or "").strip()
    source_path = str(request.options.get("source_path") or "").strip()
    if source_path:
        return True
    lowered = query.lower()
    if lowered.startswith("file:"):
        return True
    if any(marker in query for marker in ["TY  -", "ER  -", "@article", "@inproceedings", "%T ", "%A "]):
        return True
    return lowered.endswith((".ris", ".bib", ".enw", ".txt"))


def _looks_like_chinese_query(query: str) -> bool:
    text = str(query or "")
    if any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return True
    lowered = text.lower()
    return any(marker in lowered for marker in ["chinese paper", "china", "cnki", "baidu scholar", "中文", "国内论文"])


def _looks_like_arxiv_query(query: str) -> bool:
    lowered = str(query or "").lower()
    if any("\u4e00" <= ch <= "\u9fff" for ch in lowered):
        return False
    return any(marker in lowered for marker in ["ai", "machine learning", "deep learning", "cs.", "physics", "math", "preprint", "llm", "rag"])


def _looks_like_doi_query(query: str) -> bool:
    return bool(re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", str(query or ""), flags=re.IGNORECASE))


def _detect_disciplines(query: str) -> List[str]:
    lowered = str(query or "").lower()
    markers = {
        "biomed": ["biomed", "clinical", "medicine", "medical", "disease", "gene", "protein", "医学", "医疗", "临床", "疾病", "药物", "基因"],
        "cs": ["computer", "software", "algorithm", "machine learning", "deep learning", "llm", "rag", "人工智能", "知识图谱", "计算机", "算法", "软件"],
        "stem": ["physics", "chemistry", "materials", "geology", "engineering", "ocean", "地质", "找矿", "材料", "工程", "海洋", "物理", "化学"],
        "social_science": ["education", "economics", "policy", "governance", "psychology", "management", "law", "教育", "经济", "政策", "治理", "心理", "管理", "法律", "法学", "社会"],
        "humanities": ["history", "literature", "philosophy", "文化", "历史", "文学", "哲学"],
    }
    disciplines = [name for name, terms in markers.items() if any(term in lowered for term in terms)]
    return disciplines or ["general"]
