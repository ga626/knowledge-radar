"""Profile-driven academic search route planning."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Dict, List

from .models import AcademicSearchRequest
from .profile import AcademicProviderProfile


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
    required_providers: List[str] = field(default_factory=list)


def plan_academic_search(
    request: AcademicSearchRequest,
    provider_name: str,
    profiles: Dict[str, AcademicProviderProfile],
    *,
    core_available: bool = False,
    serpapi_available: bool = False,
    semantic_available: bool = False,
    wanfang_available: bool = False,
) -> AcademicRoutePlan:
    if provider_name != "auto":
        return AcademicRoutePlan(provider_order=[provider_name], intent=analyze_academic_query(request), waves={"explicit": [provider_name]})
    intent = analyze_academic_query(request)
    authorized = request.options.get('authorized_sources') or []
    enabled = {name for name, profile in profiles.items() if profile.enabled and (
        profile.default_policy != 'explicit' or (
            name == 'wanfang' and wanfang_available and isinstance(authorized,(list,tuple,set)) and name in authorized))}
    query = request.query.lower()
    # 引文导入和 DOI 核对有自己的合同，不执行无意义的关键词广撒网。
    if intent.citation_import:
        order = ["citation_import"]
        required = list(order)
    elif intent.doi_like:
        order = ["crossref"]
        if not request.options.get("has_readable_fulltext"):
            order.append("unpaywall")
        required = list(order)
    elif _exact_arxiv_id(request.query):
        order, required = ['arxiv'], ['arxiv']
    else:
        primary = ["pubscholar", "openalex"] if intent.chinese_like else ["openalex"]
        domain = []
        disciplines = set(intent.disciplines)
        if intent.chinese_like and disciplines & {"social_science", "humanities"}:
            domain.append("nssd")
        if intent.chinese_like and disciplines & {"cs", "stem", "biomed"}:
            domain.extend(["chinaxiv", "sciengine"])
        if intent.chinese_like:
            domain.append("napstic_oa")
        if intent.arxiv_like:
            domain.append("arxiv")
        if "biomed" in disciplines:
            domain.append("europepmc")
        # HKJO 是 explicit 来源；地域关键词不能越过画像中的显式选择边界。
        if any(term in query for term in ("台湾", "taiwan", "臺灣")):
            domain.extend(["toaj", "ntur"])
        if any(term in query for term in ("学位", "博士论文", "硕士论文", "thesis", "dissertation")):
            domain.append("calis_thesis")
        if any(term in query for term in ("科技报告", "技术报告", "technical report")):
            domain.append("nstrs")
        required = _dedupe_order(primary + domain)
        supplement = []
        if semantic_available:
            supplement.append("semanticscholar")
        if core_available and (request.options.get("resolve_fulltext") or any(
            term in query for term in ("仓储", "开放全文", "repository", "open access")
        )):
            supplement.append("core")
        authorized = request.options.get("authorized_sources") or []
        if wanfang_available and isinstance(authorized, (list, tuple, set)) and "wanfang" in authorized:
            supplement.append("wanfang")
        if serpapi_available:
            supplement.append("serpapi_scholar")
        order = _dedupe_order(required + supplement)
    order = [name for name in order if name in enabled]
    required = [name for name in required if name in enabled]
    waves = {"A": required, "C": [name for name in order if name not in required]}
    return AcademicRoutePlan(provider_order=order, intent=intent, waves=waves, required_providers=required)


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
    return bool(_exact_arxiv_id(query)) or bool(re.search(r"\b(ai|llm|rag|arxiv|physics|math|preprint)\b|machine learning|deep learning|cs\.", lowered)) or any(
        marker in lowered for marker in ("人工智能", "机器学习", "深度学习", "预印本", "大模型", "物理", "数学")
    )


def _looks_like_doi_query(query: str) -> bool:
    return bool(re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", str(query or ""), flags=re.IGNORECASE))


def _exact_arxiv_id(query):
    from .identity import arxiv_identifier
    return arxiv_identifier(query)


def _detect_disciplines(query: str) -> List[str]:
    lowered = str(query or "").lower()
    markers = {
        "biomed": ["biomed", "clinical", "medicine", "medical", "disease", "gene", "protein", "医学", "医疗", "临床", "疾病", "药物", "基因"],
        "cs": ["computer", "software", "algorithm", "machine learning", "deep learning", "llm", "rag", "人工智能", "知识图谱", "计算机", "算法", "软件"],
        "stem": ["physics", "chemistry", "materials", "geology", "engineering", "ocean", "地质", "找矿", "材料", "工程", "海洋", "物理", "化学"],
        "social_science": ["education", "economics", "policy", "governance", "psychology", "management", "law", "教育", "经济", "政策", "治理", "心理", "管理", "法律", "法学", "社会"],
        "humanities": ["history", "literature", "philosophy", "文化", "历史", "文学", "哲学"],
    }
    def matches(term: str) -> bool:
        if term.isascii():
            return bool(re.search(r"\b" + re.escape(term) + r"\b", lowered))
        return term in lowered
    disciplines = [name for name, terms in markers.items() if any(matches(term) for term in terms)]
    return disciplines or ["general"]
