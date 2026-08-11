"""Health summary for Xiaohongshu detail/search stability."""

from __future__ import annotations

import os
from typing import Any, Dict, List

from kr_core.decision_log import DecisionLogger
from runtime.paths import runtime_log_dir

XHS_DETAIL_HEALTH_SCHEMA = "knowledgeradar-xhs-detail-health/v2"
XHS_DETAIL_HEALTH_GENERATION = "xhs-image-aware-detail-20260609"


def _runtime_dir() -> str:
    return str(runtime_log_dir())


def default_xhs_detail_health_path() -> str:
    return os.environ.get("KR_XHS_DETAIL_HEALTH_PATH") or os.path.join(_runtime_dir(), "knowledgeradar-xhs-detail-health.jsonl")


def default_xhs_regression_samples_path() -> str:
    return os.environ.get("KR_XHS_REGRESSION_SAMPLES_PATH") or os.path.join(_runtime_dir(), "knowledgeradar-xhs-regression-samples.jsonl")


class XhsDetailHealthTracker:
    def __init__(self, path: str | None = None):
        self.path = path or default_xhs_detail_health_path()
        self._logger = DecisionLogger(self.path)

    def record(
        self,
        *,
        success: bool,
        elapsed_s: float,
        error_type: str = "",
        note_id: str = "",
        url: str = "",
        failure_subtype: str = "",
        page_state: Dict[str, Any] | None = None,
        selector_hit_count: int | None = None,
        text_len: int | None = None,
        fallback_attempts: List[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        event = {
            "schema": XHS_DETAIL_HEALTH_SCHEMA,
            "generation": _current_generation(),
            "success": bool(success),
            "elapsed_s": round(float(elapsed_s or 0.0), 3),
            "error_type": error_type,
            "failure_subtype": failure_subtype or error_type,
            "note_id": note_id,
            "url": url,
        }
        if page_state:
            event["page_state"] = {
                "platform_state": str(page_state.get("platform_state") or ""),
                "manual_action_required": bool(page_state.get("manual_action_required")),
            }
        if selector_hit_count is not None:
            event["selector_hit_count"] = int(selector_hit_count or 0)
        if text_len is not None:
            event["text_len"] = int(text_len or 0)
        if fallback_attempts:
            event["fallback_attempts"] = [
                {
                    "strategy": str(attempt.get("strategy") or ""),
                    "status": str(attempt.get("status") or ""),
                    "reason": str(attempt.get("reason") or ""),
                    "failure_subtype": str(attempt.get("failure_subtype") or ""),
                    "selector_hit_count": int(attempt.get("selector_hit_count") or 0),
                    "text_len": int(attempt.get("text_len") or 0),
                }
                for attempt in fallback_attempts[:6]
                if isinstance(attempt, dict)
            ]
        self._logger.record(
            type("Evt", (), {
                "to_json_line": lambda self=event: __import__("json").dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            })()
        )
        return event

    def recent_events(self, recent_limit: int = 12) -> List[Dict[str, Any]]:
        return self._current_events(self._logger.read_recent(recent_limit))

    def summary(self, recent_limit: int = 12) -> Dict[str, Any]:
        raw_events = self._logger.read_recent(recent_limit)
        events = self._current_events(raw_events)
        legacy_ignored_count = max(0, len(raw_events) - len(events))
        if not events:
            return {
                "status": "ok",
                "detail": "暂无当前代小红书详情健康样本",
                "schema": XHS_DETAIL_HEALTH_SCHEMA,
                "generation": _current_generation(),
                "path": self.path,
                "recent_limit": recent_limit,
                "raw_total": len(raw_events),
                "legacy_ignored_count": legacy_ignored_count,
                "total": 0,
                "success_rate": None,
                "avg_latency_s": None,
                "anti_bot_count": 0,
                "empty_detail_count": 0,
                "recent": [],
            }
        total = len(events)
        success_count = sum(1 for event in events if bool(event.get("success")))
        latencies = [float(event.get("elapsed_s") or 0.0) for event in events if isinstance(event.get("elapsed_s"), (int, float))]
        anti_bot_count = sum(1 for event in events if str(event.get("error_type") or "") in {"anti_bot", "anti_bot_verification"})
        empty_detail_count = sum(1 for event in events if str(event.get("error_type") or "") in {"empty_results", "parse_failed", "empty_detail"})
        selector_alert = _selector_contract_alert(events)
        recent = [
            {
                "success": bool(event.get("success")),
                "elapsed_s": float(event.get("elapsed_s") or 0.0),
                "error_type": str(event.get("error_type") or ""),
                "failure_subtype": str(event.get("failure_subtype") or event.get("error_type") or ""),
                "note_id": str(event.get("note_id") or ""),
                "url": str(event.get("url") or ""),
                "selector_hit_count": int(event.get("selector_hit_count") or 0),
                "text_len": int(event.get("text_len") or 0),
            }
            for event in events[-5:]
        ]
        success_rate = round(success_count / total, 3) if total else None
        avg_latency = round(sum(latencies) / len(latencies), 2) if latencies else None
        status = "ok" if success_rate is None or success_rate >= 0.5 else "degraded"
        if selector_alert.get("active"):
            status = "degraded"
        return {
            "status": status,
            "detail": f"小红书详情健康样本={total}, success_rate={success_rate}, avg_latency_s={avg_latency}",
            "schema": XHS_DETAIL_HEALTH_SCHEMA,
            "generation": _current_generation(),
            "path": self.path,
            "recent_limit": recent_limit,
            "raw_total": len(raw_events),
            "legacy_ignored_count": legacy_ignored_count,
            "total": total,
            "success_rate": success_rate,
            "avg_latency_s": avg_latency,
            "anti_bot_count": anti_bot_count,
            "empty_detail_count": empty_detail_count,
            "selector_contract_alert": selector_alert,
            "recent": recent,
        }

    def _current_events(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        generation = _current_generation()
        return [
            event
            for event in events
            if isinstance(event, dict)
            and str(event.get("schema") or "") == XHS_DETAIL_HEALTH_SCHEMA
            and str(event.get("generation") or "") == generation
        ]


class XhsChainHealthTracker:
    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(_runtime_dir(), "knowledgeradar-xhs-chain-health.jsonl")

    def summary(self, recent_limit: int = 20) -> Dict[str, Any]:
        tracker = get_xhs_detail_health_tracker()
        detail = tracker.summary(recent_limit=recent_limit)
        events = tracker.recent_events(recent_limit)
        recent = [
            {
                "success": bool(event.get("success")),
                "elapsed_s": float(event.get("elapsed_s") or 0.0),
                "error_type": str(event.get("error_type") or ""),
                "failure_subtype": str(event.get("failure_subtype") or event.get("error_type") or ""),
                "note_id": str(event.get("note_id") or ""),
                "url": str(event.get("url") or ""),
                "selector_hit_count": int(event.get("selector_hit_count") or 0),
                "text_len": int(event.get("text_len") or 0),
            }
            for event in events
        ]
        detail_success = 0
        detail_failure = 0
        dead_link = 0
        bridge_timeout = 0
        bridge_parse_failed = 0
        empty_detail = 0
        for event in recent:
            if bool(event.get("success")):
                detail_success += 1
                continue
            detail_failure += 1
            error_type = str(event.get("error_type") or "")
            if error_type == "dead_link":
                dead_link += 1
            elif error_type == "bridge_timeout":
                bridge_timeout += 1
            elif error_type == "bridge_parse_failed":
                bridge_parse_failed += 1
            elif error_type == "empty_detail":
                empty_detail += 1
        discovery = {
            "status": "ok" if dead_link < max(1, recent_limit // 2) else "degraded",
            "detail": "小红书发现层健康摘要",
            "recent_limit": recent_limit,
            "dead_link_count": dead_link,
            "failure_count": detail_failure,
            "success_count": detail_success,
        }
        detail_layer = {
            "status": detail.get("status", "ok"),
            "detail": detail.get("detail", ""),
            "success_rate": detail.get("success_rate"),
            "avg_latency_s": detail.get("avg_latency_s"),
            "dead_link_count": dead_link,
            "empty_detail_count": empty_detail,
            "bridge_timeout_count": bridge_timeout,
            "bridge_parse_failed_count": bridge_parse_failed,
            "recent": recent,
            "selector_contract_alert": detail.get("selector_contract_alert", {}),
        }
        return {
            "status": "ok" if discovery["status"] == "ok" and detail_layer["status"] == "ok" else "degraded",
            "detail": "小红书链路健康摘要",
            "generation": detail.get("generation", _current_generation()),
            "legacy_ignored_count": detail.get("legacy_ignored_count", 0),
            "discovery": discovery,
            "detail_layer": detail_layer,
            "path": self.path,
        }


_DEFAULT_XHS_DETAIL_HEALTH = XhsDetailHealthTracker()
_DEFAULT_XHS_CHAIN_HEALTH = XhsChainHealthTracker()
_DEFAULT_XHS_REGRESSION_SAMPLES_PATH = default_xhs_regression_samples_path()


def get_xhs_detail_health_tracker() -> XhsDetailHealthTracker:
    return _DEFAULT_XHS_DETAIL_HEALTH


def get_xhs_chain_health_tracker() -> XhsChainHealthTracker:
    return _DEFAULT_XHS_CHAIN_HEALTH


def _current_generation() -> str:
    return os.environ.get("KR_XHS_DETAIL_HEALTH_GENERATION") or XHS_DETAIL_HEALTH_GENERATION


def _selector_contract_alert(events: List[Dict[str, Any]], *, window: int = 5, threshold: int = 3) -> Dict[str, Any]:
    recent = events[-window:]
    suspect = 0
    for event in recent:
        page_state = event.get("page_state") if isinstance(event.get("page_state"), dict) else {}
        platform_state = str(page_state.get("platform_state") or "")
        selector_hit_count = int(event.get("selector_hit_count") or 0)
        if platform_state == "ok" and selector_hit_count <= 0:
            suspect += 1
    return {
        "schema": "knowledgeradar-xhs-selector-contract-alert/v1",
        "active": len(recent) >= window and suspect >= threshold,
        "window": window,
        "threshold": threshold,
        "selector_zero_hit_count": suspect,
        "scheduled_patrol": False,
        "reason_code": "SELECTOR_CONTRACT_SUSPECT" if len(recent) >= window and suspect >= threshold else "OK",
    }


def record_xhs_regression_sample(*, kind: str, url: str, title: str = "", note_id: str = "", status: str = "", detail: str = "", content_chars: int = 0, ocr_text_chars: int = 0, transcript_chars: int = 0) -> Dict[str, Any]:
    entry = {
        "kind": kind,
        "url": url,
        "title": title,
        "note_id": note_id,
        "status": status,
        "detail": detail,
        "content_chars": int(content_chars or 0),
        "ocr_text_chars": int(ocr_text_chars or 0),
        "transcript_chars": int(transcript_chars or 0),
        "created_at": __import__("time").time(),
    }
    try:
        os.makedirs(os.path.dirname(_DEFAULT_XHS_REGRESSION_SAMPLES_PATH), exist_ok=True)
        with open(_DEFAULT_XHS_REGRESSION_SAMPLES_PATH, "a", encoding="utf-8") as f:
            f.write(__import__("json").dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        pass
    return entry


def read_xhs_regression_samples(limit: int = 50) -> List[Dict[str, Any]]:
    if not os.path.isfile(_DEFAULT_XHS_REGRESSION_SAMPLES_PATH):
        return []
    rows: List[Dict[str, Any]] = []
    limit = max(1, min(int(limit or 50), 500))
    try:
        with open(_DEFAULT_XHS_REGRESSION_SAMPLES_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = __import__("json").loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    except Exception:
        return []
    return rows
