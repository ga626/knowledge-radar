"""保留评论原文并精确去重；相关性判断交给当前研究 Agent。"""

from typing import Dict, List


def filter_valuable_comments(
    comments: List[Dict[str, str]], api_url: str = "", api_key: str = "",
    model: str = "", verbose: bool = True,
) -> Dict:
    """兼容采集器返回结构；旧 API 参数不再读取、使用或回传。

    只在评论 ID 相同，或无 ID 且作者和全文完全相同时去重。
    """
    seen, kept, results = set(), [], []
    for index, comment in enumerate(comments, 1):
        identity = comment.get("comment_id") or comment.get("rpid") or comment.get("id")
        key = ("id", str(identity)) if identity else (
            "text", str(comment.get("user") or ""), str(comment.get("content") or "")
        )
        duplicate = key in seen
        seen.add(key)
        row = {**comment, "index": index,
               "verdict": "discard" if duplicate else "keep",
               "reason": "完全重复" if duplicate else "保留原文，相关性由研究 Agent 判断"}
        results.append(row)
        if not duplicate:
            kept.append(row)
    total = len(comments)
    return {"total": total, "kept": len(kept), "discarded": total-len(kept),
            "retention_rate": round(len(kept)/total*100, 1) if total else 0,
            "results": results, "kept_comments": kept, "provider": "deterministic",
            "model": "", "relevance_assessed": False, "fallback": False,
            "estimated_cost_rmb": 0}
