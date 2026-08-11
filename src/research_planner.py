"""Lightweight research planning for cross-platform KnowledgeRadar tasks."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List


RECENT_TERMS = {
    "最新",
    "2025",
    "2026",
    "趋势",
    "现状",
    "模型",
    "agent",
    "mcp",
    "rag",
    "多模态",
    "联网搜索",
    "开源",
}

PRACTICE_TERMS = {"教程", "搭建", "部署", "实战", "上手", "配置", "操作", "演示", "踩坑", "评测"}
EXPERIENCE_TERMS = {"体验", "真实", "踩坑", "避坑", "推荐", "好用", "案例", "实践"}
DEPTH_TERMS = {"原理", "架构", "方案", "对比", "分析", "评估", "问题", "论文", "最佳实践"}


@dataclass(frozen=True)
class PlannedSearch:
    platform: str
    tool: str
    query: str
    purpose: str
    priority: int = 2
    freshness: str = "auto"
    use_detail: bool = True
    notes: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass(frozen=True)
class ResearchPlan:
    topic: str
    schema_version: str = "knowledgeradar-research-plan/v1"
    freshness_policy: str = "auto"
    assumptions: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    searches: List[PlannedSearch] = field(default_factory=list)
    report_requirements: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        data = asdict(self)
        data["searches"] = [item.to_dict() for item in self.searches]
        return data


def _contains_any(text: str, terms: set[str]) -> bool:
    lower = text.lower()
    return any(term.lower() in lower for term in terms)


def _clean_topic(topic: str) -> str:
    return re.sub(r"\s+", " ", topic.strip())


def _dedupe(items: List[str], limit: int) -> List[str]:
    seen = set()
    result: List[str] = []
    for item in items:
        item = _clean_topic(item)
        if item and item not in seen:
            seen.add(item)
            result.append(item)
        if len(result) >= limit:
            break
    return result


def build_research_plan(topic: str) -> ResearchPlan:
    topic = _clean_topic(topic)
    if not topic:
        return ResearchPlan(topic="")

    wants_recent = _contains_any(topic, RECENT_TERMS)
    looks_practical = _contains_any(topic, PRACTICE_TERMS)
    looks_experience = _contains_any(topic, EXPERIENCE_TERMS)
    looks_deep = _contains_any(topic, DEPTH_TERMS)

    freshness = "recent_first" if wants_recent else "balanced"
    assumptions = [
        "Use health_check before a report-grade run.",
        "Use get_content_detail for sources that will support key conclusions.",
    ]
    if wants_recent:
        assumptions.append("Prefer 2025-2026 sources for time-sensitive AI/tooling topics.")

    keywords = _dedupe(
        [
            topic,
            f"{topic} 最新趋势 2026" if wants_recent else f"{topic} 现状",
            f"{topic} 开源方案",
            f"{topic} 架构设计",
            f"{topic} 最佳实践",
            f"{topic} 对比评测",
            f"{topic} 教程 实战",
            f"{topic} 真实体验 踩坑",
        ],
        8,
    )

    searches: List[PlannedSearch] = [
        PlannedSearch(
            platform="web",
            tool="kr_web_search",
            query=keywords[1] if len(keywords) > 1 else topic,
            purpose="Find recent official docs, GitHub projects, papers, and broad web references.",
            priority=1,
            freshness="recent" if wants_recent else "auto",
        ),
        PlannedSearch(
            platform="web",
            tool="kr_web_search",
            query=f"{topic} GitHub arXiv benchmark",
            purpose="Find reusable open-source projects, papers, and evaluation material.",
            priority=1 if wants_recent or looks_deep else 2,
            freshness="recent" if wants_recent else "auto",
        ),
        PlannedSearch(
            platform="知乎",
            tool="search_zhihu",
            query=f"{topic} 方案 分析 对比",
            purpose="Collect long-form Chinese analysis, tradeoffs, and architecture opinions.",
            priority=1 if looks_deep else 2,
        ),
        PlannedSearch(
            platform="B站",
            tool="search_bilibili",
            query=f"{topic} 教程 实战",
            purpose="Find tutorials, demos, implementation walkthroughs, and visual workflows.",
            priority=1 if looks_practical else 2,
        ),
        PlannedSearch(
            platform="小红书",
            tool="search_xiaohongshu",
            query=f"{topic} 体验 踩坑",
            purpose="Find practical experience posts, user feedback, and lightweight case studies.",
            priority=1 if looks_experience else 3,
            notes="Skip or mark unavailable when login/search probe is degraded.",
        ),
    ]

    report_requirements = [
        "State health_check result and unavailable tools explicitly.",
        "Separate web, B站, 知乎, and 小红书 evidence.",
        "Attach evidence URL/platform/retrieved_at to key claims.",
        "Mark uncertain or unverified claims as 待验证.",
    ]
    if wants_recent:
        report_requirements.append("For AI/tooling claims, prefer recent sources and flag stale material.")

    return ResearchPlan(
        topic=topic,
        freshness_policy=freshness,
        assumptions=assumptions,
        keywords=keywords,
        searches=searches,
        report_requirements=report_requirements,
    )


def expand_keywords_for_topic(topic: str) -> List[str]:
    return build_research_plan(topic).keywords
