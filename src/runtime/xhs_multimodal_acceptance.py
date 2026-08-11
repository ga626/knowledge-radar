"""Read-only Xiaohongshu multimodal acceptance summary."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List


def xhs_multimodal_acceptance_summary(decision_summary: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Summarize XHS multimodal acceptance without running detail extraction."""
    recent = []
    if isinstance(decision_summary, dict):
        recent = [row for row in decision_summary.get("recent", []) or [] if isinstance(row, dict)]
    aggregate_xhs_success = 0
    if isinstance(decision_summary, dict):
        by_platform = decision_summary.get("by_platform") or {}
        try:
            aggregate_xhs_success = int(by_platform.get("小红书") or 0)
        except Exception:
            aggregate_xhs_success = 0
    xhs_success = [
        row for row in recent
        if str(row.get("platform") or "") == "小红书" and bool(row.get("success"))
    ]
    auto_mm_success = [
        row for row in xhs_success
        if bool((row.get("metadata") or {}).get("auto_multimodal"))
    ]
    return {
        "schema": "knowledgeradar-xhs-multimodal-acceptance/v1",
        "status": "partial_pass" if (xhs_success or aggregate_xhs_success) else "not_observed",
        "side_effects": {
            "detail_request": False,
            "browser_launch": False,
            "station_search": False,
            "api_call": False,
        },
        "capabilities": [
            _capability("text_detail", "pass" if (xhs_success or aggregate_xhs_success) else "pending", "XHS detail success observed" if (xhs_success or aggregate_xhs_success) else "need one detail success"),
            _capability("image_list", "pass" if _any_image_count(xhs_success) else "pending", "detail metadata includes image_count > 0" if _any_image_count(xhs_success) else "need image sample"),
            _capability("image_ocr", "conditional_pass" if auto_mm_success else "pending", "auto_multimodal detail success observed; OCR may be skipped when text is sufficient" if auto_mm_success else "need auto_multimodal=true sample"),
            _capability("video_understanding", "not_validated", "XHS video-specific understanding has no final acceptance sample"),
            _capability("comments", "not_validated", "XHS comment extraction has no final acceptance sample"),
        ],
        "evidence": {
            "recent_xhs_detail_success": len(xhs_success),
            "aggregate_xhs_detail_success": aggregate_xhs_success,
            "recent_xhs_auto_multimodal_success": len(auto_mm_success),
            "sample_refs": [
                {
                    "timestamp": row.get("timestamp"),
                    "url_hash": _short_hash(row.get("url")),
                    "content_chars": (row.get("metadata") or {}).get("content_chars"),
                    "image_count": (row.get("metadata") or {}).get("image_count"),
                    "auto_multimodal": (row.get("metadata") or {}).get("auto_multimodal"),
                }
                for row in xhs_success[:5]
            ],
        },
        "next_step": "Run one manual single-url detail probe with auto_multimodal=true only when platform risk is low.",
    }


def _capability(name: str, status: str, evidence: str) -> Dict[str, str]:
    return {"name": name, "status": status, "evidence": evidence}


def _any_image_count(rows: Iterable[Dict[str, Any]]) -> bool:
    for row in rows:
        try:
            if int((row.get("metadata") or {}).get("image_count") or 0) > 0:
                return True
        except Exception:
            pass
    return False


def _short_hash(value: object) -> str:
    import hashlib

    text = str(value or "")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12] if text else ""
