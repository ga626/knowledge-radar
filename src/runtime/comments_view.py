"""无需研究模型的可解释评论视图；折叠项仍在固定版本证据中。"""

import re
from collections import Counter


def tokens(text):
    text = re.sub(r"\s+", " ", text).lower()
    words = set(re.findall(r"[a-z0-9_]{2,}", text))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        words.update(run[i : i + 2] for i in range(max(1, len(run) - 1)))
    return words


def comment_view(comments, *, query="", limit=12):
    query_words = tokens(query)
    candidates = []
    exact = Counter()
    for index, row in enumerate(comments):
        body = re.sub(r"<[^>]+>", " ", str(row.get("content") or row.get("body") or ""))
        words = tokens(body)
        reasons = []
        score = 0.0
        overlap = len(words & query_words)
        if overlap:
            score += min(6, overlap) * 2
            reasons.append("query_terms")
        if re.search(r"https?://|\b\d+(?:\.\d+)?\b", body):
            score += 1
            reasons.append("checkable_pointer_or_measurement")
        if re.search(r"不对|并非|但是|失败|错误|反例|实测|复现|instead|however|incorrect|fails|tested", body, re.I):
            score += 2
            reasons.append("correction_or_experience_signal")
        if row.get("media") or row.get("images"):
            score += 1
            reasons.append("media_requires_inspection")
        norm = re.sub(r"\W+", "", body).lower()
        duplicate = exact[norm] if norm else 0
        if duplicate:
            score -= 4
            reasons.append("exact_duplicate_foldable")
        if norm:
            exact[norm] += 1
        candidates.append(
            {"index": index, "score": score, "reasons": reasons or ["exploration_sample"], "words": words}
        )
    chosen = []
    # 留一个探索位，避免词面匹配把未预料到的主题全部压下去。
    if limit >= 3:
        exploration = next((item for item in candidates if "exploration_sample" in item["reasons"]), None)
        if exploration:
            candidates.remove(exploration)
            chosen.append(exploration)
    while candidates and len(chosen) < limit:

        def value(item):
            similarity = max(
                (len(item["words"] & old["words"]) / max(1, len(item["words"] | old["words"])) for old in chosen),
                default=0,
            )
            return item["score"] - similarity * 3, -item["index"]

        best = max(candidates, key=value)
        candidates.remove(best)
        chosen.append(best)
    chosen.sort(key=lambda item: item["index"])
    selected = [dict(comments[item["index"]], view_reason=item["reasons"]) for item in chosen]
    by_id = {(str(row.get("discussion_kind") or "comments"), str(row.get("comment_id"))): row for row in comments}
    displayed = {(str(row.get("discussion_kind") or "comments"), str(row.get("comment_id"))) for row in selected}
    context = []
    for row in list(selected):
        parent = row.get("parent_comment_id") or (row.get("root_comment_id") if row.get("is_reply") else None)
        chain = set()
        while parent:
            key = (str(row.get("discussion_kind") or "comments"), str(parent))
            if key in chain or key in displayed:
                break
            chain.add(key)
            ancestor = by_id.get(key)
            if not ancestor:
                context.append({"comment_id": row.get("comment_id"), "missing_ancestor_id": str(parent)})
                break
            if len(selected) >= min(50, limit * 2):
                context.append(
                    {
                        "comment_id": row.get("comment_id"),
                        "missing_ancestor_id": str(parent),
                        "reason": "view_context_budget; read_fixed_evidence",
                    }
                )
                break
            selected.append(dict(ancestor, view_reason=["parent_context"]))
            displayed.add(key)
            parent = ancestor.get("parent_comment_id")
    # 视图只给导航字段和明确标记的节选；完整正文/附件只存在固定版本证据。
    previews = []
    remaining = 8000
    for row in selected:
        body = str(row.get("content") or row.get("body") or "")
        excerpt = body[: min(600, max(0, remaining))]
        remaining -= len(excerpt)
        preview = {
            key: row[key]
            for key in (
                "comment_id",
                "discussion_kind",
                "parent_comment_id",
                "root_comment_id",
                "is_reply",
                "relationship_confidence",
                "locator",
                "view_reason",
            )
            if key in row
        }
        preview.update(
            content=excerpt,
            content_is_excerpt=len(excerpt) < len(body),
            media_count=len(row.get("media") or row.get("images") or []),
        )
        previews.append(preview)
    return {
        "schema": "knowledgeradar-comment-view/v1",
        "items": previews,
        "original_count": len(comments),
        "folded_count": len(comments) - len(displayed),
        "context_gaps": context,
        "method": "lexical_signals_and_diversity",
        "text_budget_chars": 8000,
        "meaning": "reading_priority_only; not_truth_or_representative_sentiment",
        "original_retained": True,
    }
