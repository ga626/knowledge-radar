"""Freshness weighting for routing decisions."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple


FAST_MOVING_TERMS = {
    "ai",
    "agent",
    "mcp",
    "llm",
    "rag",
    "模型",
    "智能体",
    "工具",
    "框架",
    "开源",
    "api",
    "大模型",
    "多模态",
    "deepseek",
    "claude",
    "openai",
    "gemini",
    "qwen",
}

CLASSIC_TERMS = {
    "哲学",
    "基础",
    "原理",
    "理论",
    "历史",
    "数学",
    "经典",
    "定义",
    "概念",
    "入门",
}


def _match_terms(text: str, terms: set[str]) -> List[str]:
    text_l = text.lower()
    return [term for term in sorted(terms) if term.lower() in text_l]


def _extract_year(text: str) -> int:
    years = [int(item) for item in re.findall(r"\b(20[1-3][0-9])\b", text)]
    return max(years) if years else 0


def score_freshness(raw: Dict[str, Any], *, current_year: int = 2026) -> Dict[str, Any]:
    title = str(raw.get("title") or "")
    desc = str(raw.get("desc") or raw.get("description") or raw.get("content") or "")
    text = f"{title}\n{desc}"
    fast_terms = _match_terms(text, FAST_MOVING_TERMS)
    classic_terms = _match_terms(text, CLASSIC_TERMS)
    year = _extract_year(text)

    requires_freshness = bool(fast_terms) and not (classic_terms and not fast_terms)
    if fast_terms and classic_terms:
        mode = "balanced"
    elif fast_terms:
        mode = "freshness_sensitive"
    elif classic_terms:
        mode = "timeless"
    else:
        mode = "neutral"

    recency_boost = 0.0
    if year:
        age = max(0, current_year - year)
        if requires_freshness:
            recency_boost = 1.0 if age <= 1 else 0.5 if age <= 2 else -0.6
        else:
            recency_boost = 0.3 if age <= 3 else 0.0
    elif requires_freshness:
        recency_boost = -0.2

    return {
        "schema_version": "knowledgeradar-freshness-score/v1",
        "mode": mode,
        "requires_freshness": requires_freshness,
        "recency_boost": round(recency_boost, 2),
        "matched_fast_moving_terms": fast_terms[:10],
        "matched_classic_terms": classic_terms[:10],
        "detected_year": year or None,
    }
