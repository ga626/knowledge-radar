"""Rule-based routing decisions."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from .freshness_scorer import score_freshness
from .models import ContentEnvelope, ContentKind, RouteDecision


VISUAL_DEPENDENCY_TERMS = {
    "操作": 1.2,
    "演示": 1.2,
    "实操": 1.2,
    "教程": 0.8,
    "步骤": 1.0,
    "搭建": 0.8,
    "部署": 0.8,
    "配置": 0.8,
    "评测": 1.1,
    "对比": 0.8,
    "开箱": 1.2,
    "屏幕": 1.1,
    "录屏": 1.2,
    "剪辑": 1.0,
    "设计": 1.0,
    "绘画": 1.0,
    "UI": 0.8,
    "界面": 0.9,
    "实测": 1.2,
    "建模": 1.2,
    "生成": 0.8,
    "视频生成": 1.2,
    "入门": 0.7,
    "上手": 0.8,
    "安装": 0.9,
    "使用": 0.7,
    "指南": 0.8,
    "全流程": 1.0,
    "制作": 0.9,
    "模型": 0.7,
    "软件": 0.8,
    "开发": 0.8,
    "代码": 0.9,
    "调试": 0.8,
}

LANGUAGE_FIRST_TERMS = {
    "访谈": -1.2,
    "播客": -1.4,
    "讲座": -1.0,
    "演讲": -0.9,
    "观点": -0.6,
    "解读": -0.5,
    "复盘": -0.4,
    "分享": -0.2,
}

VALUE_TERMS = {
    "教程": 1.0,
    "保姆级": 1.0,
    "完整": 0.8,
    "实战": 0.8,
    "经验": 0.5,
    "避坑": 0.8,
    "对比": 0.5,
    "清单": 0.4,
    "方法": 0.5,
    "搭建": 0.7,
    "部署": 0.7,
    "实测": 0.7,
    "评测": 0.7,
    "入门": 0.6,
    "上手": 0.6,
    "安装": 0.5,
    "使用": 0.5,
    "指南": 0.6,
    "全流程": 0.8,
    "制作": 0.6,
    "开发": 0.6,
    "学习": 0.5,
    "基础": 0.5,
    "新手": 0.5,
    "案例": 0.5,
}


def _match_terms(text: str, terms: Dict[str, float]) -> Tuple[float, List[str]]:
    score = 0.0
    matched: List[str] = []
    text_l = text.lower()
    for term, weight in terms.items():
        if term.lower() in text_l:
            score += weight
            matched.append(term)
    return score, matched


def _clamp_score(value: float) -> float:
    return max(0.0, min(10.0, round(value, 2)))


def _confidence_from_scores(value_score: float, visual_score: float, recommended_path: str) -> float:
    if recommended_path == "recommend_l2_video":
        raw = (value_score * 0.55 + visual_score * 0.45) / 10.0
    elif recommended_path == "l1_transcript_enough":
        raw = (value_score * 0.65 + (10.0 - visual_score) * 0.35) / 10.0
    elif recommended_path == "need_more_probe":
        raw = (visual_score * 0.55 + value_score * 0.25 + 2.0) / 10.0
    else:
        raw = (10.0 - max(value_score, visual_score) + min(value_score, visual_score) * 0.2) / 10.0
    return max(0.0, min(1.0, round(raw, 3)))


def _reason_codes(
    *,
    value_terms: List[str],
    visual_terms: List[str],
    language_terms: List[str],
    transcript_len: int,
    recommended_path: str,
    probes: Dict[str, Any],
    freshness: Dict[str, Any],
) -> List[str]:
    codes: List[str] = []
    if value_terms:
        codes.append("VALUE_TERMS_MATCHED")
    if visual_terms:
        codes.append("VISUAL_DEPENDENCY_TERMS_MATCHED")
    if language_terms:
        codes.append("LANGUAGE_FIRST_TERMS_MATCHED")
    codes.append("TRANSCRIPT_AVAILABLE" if transcript_len else "TRANSCRIPT_MISSING_OR_PENDING")
    if probes.get("needs_keyframe_probe"):
        codes.append("KEYFRAME_PROBE_RECOMMENDED")
    if freshness.get("requires_freshness"):
        codes.append("FRESHNESS_REQUIRED")
    elif freshness.get("mode") == "timeless":
        codes.append("FRESHNESS_NOT_REQUIRED")
    path_code = {
        "recommend_l2_video": "PATH_RECOMMEND_L2_VIDEO",
        "l1_transcript_enough": "PATH_L1_TRANSCRIPT_ENOUGH",
        "need_more_probe": "PATH_NEED_MORE_PROBE",
        "l1_only": "PATH_L1_ONLY",
    }.get(recommended_path, "PATH_UNKNOWN")
    codes.append(path_code)
    return codes


def _duration_minutes(value: Any) -> float:
    try:
        seconds = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, seconds / 60.0)


def _number(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _heat_score(raw: Dict[str, Any]) -> Tuple[float, Dict[str, int]]:
    stats = raw.get("stats") if isinstance(raw.get("stats"), dict) else {}
    view = _number(raw.get("video_play_count") or raw.get("play") or raw.get("view") or stats.get("view"))
    like = _number(raw.get("liked_count") or raw.get("like") or stats.get("like"))
    coin = _number(raw.get("video_coin_count") or raw.get("coin") or stats.get("coin"))
    favorite = _number(raw.get("video_favorite_count") or raw.get("favorite") or stats.get("favorite"))
    reply = _number(raw.get("video_comment") or raw.get("reply") or stats.get("reply"))

    score = 0.0
    if view >= 1_000_000:
        score += 1.2
    elif view >= 300_000:
        score += 0.9
    elif view >= 100_000:
        score += 0.6
    elif view >= 30_000:
        score += 0.3
    if like >= 20_000:
        score += 0.5
    elif like >= 5_000:
        score += 0.3
    if coin >= 5_000 or favorite >= 10_000:
        score += 0.3
    if reply >= 500:
        score += 0.2

    return score, {
        "view": view,
        "like": like,
        "coin": coin,
        "favorite": favorite,
        "reply": reply,
    }


def _transcript_coverage(transcript_len: int, duration_min: float, transcript: str) -> Dict[str, Any]:
    if transcript.startswith("[transcribe]"):
        status = "pending"
        reason = "transcript background task is still pending"
    elif transcript_len <= 0:
        status = "missing"
        reason = "no transcript text is available"
    elif duration_min and duration_min <= 240:
        chars_per_min = round(transcript_len / max(duration_min, 1.0), 2)
        if chars_per_min >= 80:
            status = "strong"
            reason = "transcript density is high enough for L1 understanding"
        elif chars_per_min >= 30:
            status = "partial"
            reason = "transcript exists but may be sparse"
        else:
            status = "weak"
            reason = "transcript density is low for the video length"
        return {
            "status": status,
            "chars_per_min": chars_per_min,
            "reason": reason,
        }
    else:
        status = "available"
        reason = "transcript exists but duration is unavailable or out of expected range"

    return {
        "status": status,
        "chars_per_min": None,
        "reason": reason,
    }


def _build_l15_probe(
    *,
    value_score: float,
    visual_score: float,
    visual_terms: List[str],
    language_terms: List[str],
    transcript_len: int,
    transcript: str,
    duration_min: float,
    text: str,
) -> Dict[str, Any]:
    coverage = _transcript_coverage(transcript_len, duration_min, transcript)
    visual_markers = list(visual_terms)
    if re.search(r"(PPT|代码|界面|流程图|截图|实测|实录)", text, re.I):
        visual_markers.append("structured_visual_marker")

    needs_keyframe_probe = False
    probe_reasons: List[str] = []
    if visual_score >= 6.5:
        needs_keyframe_probe = True
        probe_reasons.append("visual dependency score is high")
    if coverage["status"] in {"missing", "pending", "weak"} and visual_score >= 5.5:
        needs_keyframe_probe = True
        probe_reasons.append(f"transcript coverage is {coverage['status']}")
    if value_score >= 6.5 and 5.5 <= visual_score < 6.5:
        needs_keyframe_probe = True
        probe_reasons.append("valuable content has borderline visual dependency")
    if language_terms and coverage["status"] in {"strong", "available"}:
        probe_reasons.append("language-first markers reduce keyframe urgency")

    return {
        "stage": "l1_5_probe_plan",
        "executed": False,
        "transcript_coverage": coverage,
        "visual_markers": visual_markers,
        "needs_keyframe_probe": needs_keyframe_probe,
        "probe_reasons": probe_reasons,
        "next_probe": "keyframe_sample" if needs_keyframe_probe else "none",
    }


def decide_route(envelope: ContentEnvelope, raw: Dict[str, Any]) -> RouteDecision:
    if envelope.kind != ContentKind.VIDEO:
        return RouteDecision(
            stage="l1_signal_ready",
            content_kind=envelope.kind,
            recommended_path="l1_only",
            should_run_l2=False,
            reasons=["non-video content keeps the L1-only recommendation in stage 3"],
            signals=envelope.signals,
            scores={},
            probes={},
            confidence=0.85,
            reason_codes=["CONTENT_NON_VIDEO", "PATH_L1_ONLY"],
        )

    title = str(raw.get("title") or envelope.title or "")
    desc = str(raw.get("desc") or raw.get("description") or "")
    transcript = str(raw.get("transcript") or "")
    text = f"{title}\n{desc}"
    value_boost, value_terms = _match_terms(text, VALUE_TERMS)
    visual_boost, visual_terms = _match_terms(text, VISUAL_DEPENDENCY_TERMS)
    language_adjust, language_terms = _match_terms(text, LANGUAGE_FIRST_TERMS)
    heat_boost, heat_stats = _heat_score(raw)
    freshness = score_freshness(raw)

    duration_min = _duration_minutes(raw.get("duration"))
    transcript_len = envelope.signals.transcript_length
    comment_count = envelope.signals.comment_count

    value_score = 3.0 + value_boost + heat_boost
    value_score += float(freshness.get("recency_boost") or 0.0)
    if len(title) >= 12:
        value_score += 0.5
    if len(desc) >= 80:
        value_score += 0.5
    if transcript_len >= 800:
        value_score += 1.0
    elif transcript_len >= 200:
        value_score += 0.5
    if comment_count >= 8:
        value_score += 0.4
    if 8 <= duration_min <= 240:
        value_score += 0.6

    visual_score = 3.0 + visual_boost + language_adjust
    if transcript.startswith("[transcribe]") or transcript_len == 0:
        visual_score += 0.6
    if re.search(r"(PPT|代码|界面|流程图|截图|实测|实录)", text, re.I):
        visual_score += 1.0

    value_score = _clamp_score(value_score)
    visual_score = _clamp_score(visual_score)
    probes = _build_l15_probe(
        value_score=value_score,
        visual_score=visual_score,
        visual_terms=visual_terms,
        language_terms=language_terms,
        transcript_len=transcript_len,
        transcript=transcript,
        duration_min=duration_min,
        text=text,
    )

    reasons: List[str] = []
    if value_terms:
        reasons.append("value terms: " + ", ".join(value_terms[:6]))
    if freshness.get("requires_freshness"):
        reasons.append(f"freshness mode={freshness.get('mode')} boost={freshness.get('recency_boost')}")
    if visual_terms:
        reasons.append("visual dependency terms: " + ", ".join(visual_terms[:6]))
    if language_terms:
        reasons.append("language-first terms lower visual dependency: " + ", ".join(language_terms[:6]))
    if transcript_len:
        reasons.append(f"transcript length={transcript_len}")
    else:
        reasons.append("transcript unavailable or pending")

    if value_score >= 6.3 and visual_score >= 6.3:
        recommended_path = "recommend_l2_video"
        reasons.append("both value and visual dependency are high")
    elif value_score >= 5.8 and visual_score >= 7.0 and probes.get("needs_keyframe_probe"):
        recommended_path = "recommend_l2_video"
        reasons.append("visual dependency is high and L1.5 keyframe probe is recommended")
    elif value_score >= 6.5 and visual_score < 6.5:
        recommended_path = "l1_transcript_enough"
        reasons.append("content looks valuable but visual dependency is not high")
    elif visual_score >= 6.5 and value_score < 6.5:
        recommended_path = "need_more_probe"
        reasons.append("visual dependency looks high but content value is uncertain")
    else:
        recommended_path = "l1_only"
        reasons.append("scores do not justify L2 recommendation")

    return RouteDecision(
        stage="dual_score_routing",
        content_kind=envelope.kind,
        recommended_path=recommended_path,
        should_run_l2=False,
        reasons=reasons,
        signals=envelope.signals,
        scores={
            "value_score": value_score,
            "visual_dependency_score": visual_score,
            "matched_value_terms": value_terms,
            "matched_visual_terms": visual_terms,
            "matched_language_first_terms": language_terms,
            "duration_minutes": round(duration_min, 2),
            "heat_boost": round(heat_boost, 2),
            "heat_stats": heat_stats,
            "freshness": freshness,
        },
        probes=probes,
        confidence=_confidence_from_scores(value_score, visual_score, recommended_path),
        reason_codes=_reason_codes(
            value_terms=value_terms,
            visual_terms=visual_terms,
            language_terms=language_terms,
            transcript_len=transcript_len,
            recommended_path=recommended_path,
            probes=probes,
            freshness=freshness,
        ),
    )
