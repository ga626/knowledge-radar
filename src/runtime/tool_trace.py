"""MCP tool trace recording without changing tool signatures."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import wraps
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from typing import Any, Dict, Iterator, List, Optional

from .failure_tags import detect_failure_tags
from .paths import runtime_log_dir


def _runtime_dir() -> str:
    return str(runtime_log_dir())


def default_tool_trace_path() -> str:
    return os.environ.get("KR_TOOL_TRACE_PATH") or os.path.join(_runtime_dir(), "knowledgeradar-tool-trace.jsonl")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trace_id(data: Dict[str, Any]) -> str:
    raw = json.dumps(
        {
            "tool_name": data.get("tool_name", ""),
            "timestamp": data.get("timestamp", ""),
            "strategy": data.get("strategy", ""),
            "metadata": data.get("metadata", {}),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


@dataclass(frozen=True)
class ToolTraceEvent:
    schema_version: str = "knowledgeradar-tool-trace/v2"
    trace_id: str = ""
    parent_trace_id: str = ""
    event_type: str = "tool_execution"
    tool_name: str = ""
    strategy: str = ""
    status: str = "ok"
    elapsed_s: float = 0.0
    retry_count: int = 0
    failure_code: str = ""
    failure_tags: List[str] = field(default_factory=list)
    timestamp: str = field(default_factory=utc_now_iso)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if not data.get("trace_id"):
            data["trace_id"] = _trace_id(data)
        return data


class ToolTraceRecorder:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or default_tool_trace_path()
        self._lock = threading.RLock()
        self._last_event: Dict[str, Any] = {}

    def record(self, event: ToolTraceEvent) -> Dict[str, Any]:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        payload = event.to_dict()
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            self._last_event = payload
        return payload

    def last_event(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._last_event)

    def health(self) -> Dict[str, Any]:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write("")
            return {"status": "ok", "detail": "tool trace 可写", "path": self.path}
        except Exception as exc:
            return {"status": "degraded", "detail": f"tool trace 不可写: {exc}", "path": self.path}

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        if not os.path.isfile(self.path):
            return []
        limit = max(1, min(int(limit or 20), 200))
        with open(self.path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
        events: List[Dict[str, Any]] = []
        for line in lines:
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict):
                events.append(item)
        return events

    def summary(self, recent_limit: int = 20) -> Dict[str, Any]:
        events = self.recent(recent_limit)
        failures = [event for event in events if event.get("status") != "ok"]
        return {
            "status": "ok",
            "detail": f"tool trace 可用，recent={len(events)}, failures={len(failures)}",
            "path": self.path,
            "recent": events,
            "failure_count": len(failures),
        }


_DEFAULT_RECORDER = ToolTraceRecorder()
_LOCAL = threading.local()
_TRACE_FINGERPRINT_KEY = os.environ.get("KR_RESEARCH_FINGERPRINT_KEY", "").encode("utf-8") or secrets.token_bytes(32)


def privacy_fingerprint(value: Any) -> str:
    """Create a process-safe correlation label without retaining raw input."""
    return "hmac-sha256:" + hmac.new(_TRACE_FINGERPRINT_KEY, str(value or "").encode("utf-8", errors="ignore"), hashlib.sha256).hexdigest()[:24]


def _query_language(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    return "zh" if re.search(r"[\u4e00-\u9fff]", text) else "non_zh"

def get_tool_trace_recorder() -> ToolTraceRecorder:
    return _DEFAULT_RECORDER


def _set_current_trace(event: Dict[str, Any]) -> None:
    _LOCAL.current_trace = event


def set_current_tool_trace(event: Dict[str, Any]) -> None:
    _set_current_trace(event)


def current_tool_trace() -> Dict[str, Any]:
    return dict(getattr(_LOCAL, "current_trace", {}) or {})


def record_trace_child(
    event_type: str,
    *,
    tool_name: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Append a low-cardinality child receipt for the active tool execution.

    This intentionally omits query text, URLs, cookies and account identity.
    It is used for provider/wave and collector phase attribution, not payload
    capture.
    """
    parent = current_tool_trace()
    parent_trace_id = str(parent.get("trace_id") or "")
    if not parent_trace_id:
        return {}
    blocked_keys = {"query", "url", "urls", "cookie", "cookies", "authorization", "token", "profile_id", "account_id"}
    payload = {key: value for key, value in dict(metadata or {}).items() if str(key).lower() not in blocked_keys}
    child_tool = tool_name or str(parent.get("tool_name") or "")
    event = ToolTraceEvent(
        trace_id=_trace_id(
            {
                "tool_name": child_tool,
                "timestamp": utc_now_iso(),
                "strategy": str(parent.get("strategy") or ""),
                "metadata": {"parent_trace_id": parent_trace_id, "event_type": event_type, **payload},
            }
        ),
        parent_trace_id=parent_trace_id,
        event_type=str(event_type or "child_receipt"),
        tool_name=child_tool,
        strategy=str(parent.get("strategy") or ""),
        status=str(payload.pop("status", "ok") or "ok"),
        elapsed_s=float(payload.pop("elapsed_s", 0.0) or 0.0),
        failure_code=str(payload.pop("failure_code", "") or ""),
        failure_tags=list(payload.pop("failure_tags", []) or []),
        metadata=payload,
    )
    return get_tool_trace_recorder().record(event)


def infer_strategy(result: Any, fallback: str = "") -> str:
    if isinstance(result, dict):
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        collection = metadata.get("collection") if isinstance(metadata.get("collection"), dict) else {}
        strategy = collection.get("selected_strategy") or result.get("provider") or result.get("collector")
        if strategy:
            return str(strategy)
    return fallback


def infer_failure_code(result: Any, exc: Optional[BaseException] = None) -> str:
    if exc is not None:
        return type(exc).__name__
    if isinstance(result, dict):
        error = result.get("error")
        if isinstance(error, dict):
            return str(error.get("type") or error.get("code") or "")
        if error:
            return "error"
    return ""


@contextmanager
def trace_tool_call(tool_name: str, *, strategy: str = "", metadata: Optional[Dict[str, Any]] = None, research_task_id: str = "") -> Iterator[None]:
    started = time.time()
    started_iso = utc_now_iso()
    previous_trace = current_tool_trace()
    trace_id = _trace_id(
        {
            "tool_name": tool_name,
            "timestamp": started_iso,
            "strategy": strategy,
            "metadata": metadata or {},
        }
    )
    _set_current_trace(
        {
            "schema_version": "knowledgeradar-tool-trace/v2",
            "trace_id": trace_id,
            "tool_name": tool_name,
            "strategy": strategy,
            "status": "running",
            "elapsed_s": 0.0,
            "retry_count": 0,
            "failure_code": "",
            "failure_tags": [],
            "timestamp": started_iso,
            "metadata": metadata or {},
        }
    )
    result: Any = None
    exc: Optional[BaseException] = None

    def _capture_result(value: Any) -> Any:
        nonlocal result
        result = value
        return value

    try:
        yield lambda value: _capture_result(value)
    except Exception as error:
        exc = error
        raise
    finally:
        elapsed = round(time.time() - started, 3)
        failure_code = infer_failure_code(result, exc)
        status = "failed" if exc is not None or failure_code else "ok"
        event = ToolTraceEvent(
            trace_id=trace_id,
            tool_name=tool_name,
            strategy=strategy or infer_strategy(result),
            status=status,
            elapsed_s=elapsed,
            retry_count=0,
            failure_code=failure_code,
            failure_tags=detect_failure_tags(failure_code, exc, result),
            metadata=metadata or {},
        )
        get_tool_trace_recorder().record(event)
        if research_task_id:
            # Runtime import avoids a module cycle: the ledger is optional for
            # ordinary tools, and only an explicit task scope may create one.
            try:
                from .research_ledger import record_tool_receipt

                receipt_result = record_tool_receipt(
                    task_id=research_task_id,
                    trace_id=trace_id,
                    tool=tool_name,
                    status=status,
                    failure_code=failure_code,
                    source_ecology=str((metadata or {}).get("source_ecology") or ""),
                    association="explicit_task_scope",
                )
                receipt = receipt_result.get("receipt") if isinstance(receipt_result, dict) else None
                if isinstance(result, dict) and isinstance(receipt, dict):
                    result["research_receipt"] = {
                        "receipt_id": receipt.get("receipt_id"),
                        "trace_id": receipt.get("trace_id"),
                        "status": receipt.get("status"),
                        "association": receipt.get("association"),
                    }
            except Exception:
                # Observability must never turn a successful collector result
                # into a runtime failure.  The absent receipt remains visible
                # to the ledger contract as unverified, not silently trusted.
                pass
        # A completed root call must not become the implicit parent of the
        # next call on the same worker thread.  This is the minimal isolation
        # guarantee for concurrent and sequential native MCP execution.
        _set_current_trace(previous_trace)


def traced_tool(tool_name: str, *, strategy: str = ""):
    """Decorate an MCP tool with privacy-safe execution receipts.

    Standard MCP does not give a third-party server the Codex host call ID.
    Therefore this decorator never expands the public tool schema with an
    agent-supplied trace parameter.  Cross-boundary association is performed
    later from host transcript order, timestamps, tool name and redacted KR
    receipts; ambiguous concurrent calls stay explicitly partial.
    """
    def _decorator(func):
        @wraps(func)
        def _wrapped(*args, **kwargs):
            parent = current_tool_trace()
            is_nested = bool(parent.get("trace_id"))
            metadata: Dict[str, Any] = {
                "args_count": len(args),
                "kwargs": sorted(kwargs),
                "invocation_origin": "internal" if is_nested else "root",
            }
            raw_query = kwargs.get("query") or kwargs.get("keyword") or kwargs.get("task") or ""
            if raw_query:
                metadata["query_fingerprint"] = privacy_fingerprint(raw_query)
                metadata["query_language"] = _query_language(raw_query)
            research_task_id = kwargs.get("research_task_id") or kwargs.get("task_id") or ""
            if research_task_id:
                metadata["research_task_scope"] = privacy_fingerprint(research_task_id)
                metadata["research_association"] = "explicit_task_scope"
                if kwargs.get("source_ecology"):
                    metadata["source_ecology"] = str(kwargs["source_ecology"])
            elif raw_query:
                metadata["research_association"] = "partial_no_task_scope"
            if is_nested and parent.get("trace_id"):
                metadata["parent_trace_id"] = str(parent["trace_id"])
            with trace_tool_call(tool_name, strategy=strategy, metadata=metadata, research_task_id=str(research_task_id)) as capture:
                return capture(func(*args, **kwargs))
        return _wrapped

    return _decorator
