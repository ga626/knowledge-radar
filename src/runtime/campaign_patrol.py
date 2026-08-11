"""Periodic campaign patrol runner and compact failure reports."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import os
from runtime.process import silent_subprocess_run
import sys
from typing import Any

from runtime.campaign_gates import CAMPAIGN_PROFILES


PATROL_SCHEMA = "knowledgeradar-campaign-patrol/v1"
REPORT_DIR = Path("runtime") / "reports" / "campaign-patrol"
NOTIFICATION_DIR = REPORT_DIR / "notifications"
VALID_PROFILES = tuple(profile for profile in CAMPAIGN_PROFILES if profile in {"smoke", "deep", "destructive"})


def run_campaign_patrol(
    root: Path,
    profile: str,
    *,
    now: datetime | None = None,
    reports_dir: Path | None = None,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Run one campaign profile and write a human report only on non-PASS results."""
    if profile not in VALID_PROFILES:
        raise ValueError(f"unsupported patrol profile: {profile}")
    timestamp = now or datetime.now(timezone.utc)
    target_dir = reports_dir or root / REPORT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    command = _campaign_quality_gate_command(root, profile, extra_args=extra_args)
    started = datetime.now(timezone.utc)
    proc = silent_subprocess_run(
        command,
        cwd=str(root),
        env={
            **os.environ,
            "PYTHONIOENCODING": os.environ.get("PYTHONIOENCODING", "utf-8"),
            "PYTHONUTF8": os.environ.get("PYTHONUTF8", "1"),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_timeout_for_profile(profile),
    )
    finished = datetime.now(timezone.utc)
    result = _parse_gate_result(proc.stdout)
    status = "PASS" if proc.returncode == 0 and str(result.get("status") or "").upper() == "PASS" else "FAIL"
    status_class = _status_class(result, proc.returncode)
    slug = _timestamp_slug(timestamp)
    raw_path = target_dir / f"{slug}-{profile}.json"
    raw_payload = {
        "schema": PATROL_SCHEMA,
        "profile": profile,
        "status": status,
        "status_class": status_class,
        "returncode": proc.returncode,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "command": _display_command(command),
        "result": result,
        "stdout_tail": (proc.stdout or "")[-6000:],
        "stderr_tail": (proc.stderr or "")[-3000:],
    }
    report_path: Path | None = None
    notification_path: Path | None = None
    report = ""
    if status != "PASS" or proc.returncode != 0:
        report_path = target_dir / f"{slug}-{profile}.md"
        raw_payload["report_path"] = str(report_path)
        raw_payload["raw_result_path"] = str(raw_path)
        report = format_campaign_patrol_report(raw_payload)
        report_path.write_text(report, encoding="utf-8")
        notification_path = write_campaign_patrol_notification(root, raw_payload, slug=slug)
    else:
        raw_payload["resolved_notification_count"] = _resolve_pending_notifications(root, profile)

    raw_path.write_text(json.dumps(raw_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "schema": PATROL_SCHEMA,
        "profile": profile,
        "status": status,
        "status_class": status_class,
        "returncode": proc.returncode,
        "raw_result_path": str(raw_path),
        "report_path": str(report_path) if report_path else None,
        "report_written": report_path is not None,
        "notification_path": str(notification_path) if notification_path else None,
        "notification_written": notification_path is not None,
        "summary": summarize_campaign_result(raw_payload),
    }


def format_campaign_patrol_report(payload: dict[str, Any]) -> str:
    """Format a short human-readable report for failed patrols."""
    profile = str(payload.get("profile") or "unknown")
    execution_status = str(payload.get("status") or "FAIL").upper()
    status_class = str(payload.get("status_class") or execution_status).upper()
    finished_at = str(payload.get("finished_at") or "")
    summary = summarize_campaign_result(payload)
    lines = [
        f"# KnowledgeRadar {profile} 巡检异常",
        "",
        f"- 时间：{finished_at}",
        f"- 执行结果：{execution_status}",
        f"- 分类：{status_class}",
        f"- 影响：{summary['impact']}",
        f"- 建议：{summary['recommendation']}",
        "",
        "## 问题",
    ]
    problems = summary["problems"] or ["未能解析具体失败项，请查看原始 JSON。"]
    for item in problems[:5]:
        lines.append(f"- {item}")
    lines.extend(
        [
            "",
            "## 证据",
            f"- 原始结果：{payload.get('raw_result_path', 'see adjacent json')}",
            f"- 命令：{payload.get('command', '')}",
            "",
            "不会自动修复；需要用户确认后再处理。",
        ]
    )
    return "\n".join(lines) + "\n"


def write_campaign_patrol_notification(root: Path, payload: dict[str, Any], *, slug: str | None = None) -> Path | None:
    """Write a compact pending notification for Codex automation."""
    profile = str(payload.get("profile") or "unknown")
    timestamp = slug or _timestamp_slug(datetime.now(timezone.utc))
    notification_dir = root / NOTIFICATION_DIR
    notification_dir.mkdir(parents=True, exist_ok=True)
    fingerprint = _notification_fingerprint(payload)
    for existing_path in _pending_notification_paths(root):
        existing = _load_notification_payload(existing_path)
        if str(existing.get("profile") or "") != profile:
            continue
        if str(existing.get("failure_fingerprint") or "") == fingerprint:
            return None
        _archive_notification(root, existing_path, existing, state="superseded")

    notification_path = notification_dir / f"pending-{timestamp}-{profile}.json"
    message = format_campaign_patrol_notification(payload)
    notification = {
        "schema": "knowledgeradar-campaign-patrol-notification/v1",
        "profile": profile,
        "execution_status": str(payload.get("status") or "FAIL").upper(),
        "status_class": str(payload.get("status_class") or payload.get("status") or "FAIL"),
        "failure_fingerprint": fingerprint,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "message": message,
        "report_path": payload.get("report_path"),
        "raw_result_path": payload.get("raw_result_path"),
    }
    notification_path.write_text(json.dumps(notification, ensure_ascii=False, indent=2), encoding="utf-8")
    return notification_path


def claim_next_campaign_patrol_notification(root: Path) -> dict[str, object]:
    """Claim one pending notification for a Codex automation run."""
    return _next_campaign_patrol_notification(root, claim=True)


def peek_next_campaign_patrol_notification(root: Path) -> dict[str, object]:
    """Peek at the newest pending notification without consuming it."""
    return _next_campaign_patrol_notification(root, claim=False)


def _load_notification_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        payload = {
            "schema": "knowledgeradar-campaign-patrol-notification/v1",
            "status_class": "FAIL",
            "message": f"KnowledgeRadar 巡检通知读取失败：{path.name} ({exc})",
        }
    return payload if isinstance(payload, dict) else {}


def _pending_notification_paths(root: Path) -> list[Path]:
    pending_dir = root / NOTIFICATION_DIR
    pending = sorted(pending_dir.glob("pending-*.json"), reverse=True) if pending_dir.is_dir() else []
    return pending


def _archive_notification(root: Path, path: Path, payload: dict[str, Any], *, state: str) -> Path:
    archive_dir = root / NOTIFICATION_DIR / "archive" / state
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived = archive_dir / path.name.replace("pending-", f"{state}-", 1)
    payload[f"{state}_at"] = datetime.now(timezone.utc).isoformat()
    payload[f"{state}_path"] = str(archived)
    archived.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    path.unlink(missing_ok=True)
    return archived


def _resolve_pending_notifications(root: Path, profile: str) -> int:
    resolved = 0
    for path in _pending_notification_paths(root):
        payload = _load_notification_payload(path)
        if str(payload.get("profile") or "") != profile:
            continue
        _archive_notification(root, path, payload, state="resolved")
        resolved += 1
    return resolved


def _archive_stale_or_invalid_notifications(root: Path) -> int:
    archived = 0
    now = datetime.now(timezone.utc)
    for path in _pending_notification_paths(root):
        payload = _load_notification_payload(path)
        status_class = str(payload.get("status_class") or "").upper()
        created_at = str(payload.get("created_at") or "")
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            created = None
        if status_class == "PASS":
            _archive_notification(root, path, payload, state="invalid")
            archived += 1
        elif created and (now - created).days >= 7:
            _archive_notification(root, path, payload, state="stale")
            archived += 1
    return archived


def _next_campaign_patrol_notification(root: Path, *, claim: bool) -> dict[str, object]:
    pending_dir = root / NOTIFICATION_DIR
    claimed_dir = pending_dir / "claimed"
    archived_count = _archive_stale_or_invalid_notifications(root) if claim else 0
    pending = _pending_notification_paths(root)
    if not pending:
        return {
            "schema": "knowledgeradar-campaign-patrol-notification-probe/v1",
            "status": "none",
            "message": "",
            "archived_pending_count": archived_count,
        }
    path = pending[0]
    payload = _load_notification_payload(path)
    result = {
        "schema": "knowledgeradar-campaign-patrol-notification-probe/v1",
        "status": "pending",
        "profile": payload.get("profile"),
        "status_class": payload.get("status_class"),
        "message": payload.get("message", ""),
        "pending_path": str(path),
        "pending_count": len(pending),
        "archived_pending_count": archived_count,
    }
    if not claim:
        return result

    claimed_dir.mkdir(parents=True, exist_ok=True)
    profile = str(payload.get("profile") or "")
    superseded = 0
    claimed_path = claimed_dir / path.name.replace("pending-", "claimed-", 1)
    payload["claimed_at"] = datetime.now(timezone.utc).isoformat()
    payload["claimed_path"] = str(claimed_path)
    claimed_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    path.unlink(missing_ok=True)

    for older_path in pending[1:]:
        older_payload = _load_notification_payload(older_path)
        if str(older_payload.get("profile") or "") != profile:
            continue
        superseded += 1
        superseded_path = _archive_notification(root, older_path, older_payload, state="superseded")
        older_payload["superseded_by"] = str(claimed_path)
        superseded_path.write_text(json.dumps(older_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    result.update(
        {
            "claimed_path": str(claimed_path),
            "superseded_pending_count": superseded,
        }
    )
    return result


def format_campaign_patrol_notification(payload: dict[str, Any]) -> str:
    """Return a few-line user-facing notification suitable for Codex automation."""
    profile = str(payload.get("profile") or "unknown")
    execution_status = str(payload.get("status") or "FAIL").upper()
    status_class = str(payload.get("status_class") or execution_status).upper()
    summary = summarize_campaign_result(payload)
    problems = summary["problems"] or ["未能解析具体失败项，请查看本地报告。"]
    lines = [
        f"KnowledgeRadar {profile} 巡检未完成：{execution_status}",
        f"分类：{status_class}",
        f"影响：{summary['impact']}",
        f"建议：{summary['recommendation']}",
        f"主要问题：{problems[0]}",
    ]
    report_path = payload.get("report_path")
    if report_path:
        lines.append(f"报告：{report_path}")
    lines.append("未自动修复，等待用户审批。")
    return "\n".join(lines)


def summarize_campaign_result(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    failed = [item for item in result.get("results") or [] if _is_failed_result(item)]
    problems: list[str] = []
    for item in failed:
        problems.append(_problem_line(item))
    if not problems and payload.get("returncode", 0) != 0:
        stderr = str(payload.get("stderr_tail") or "").strip()
        stdout = str(payload.get("stdout_tail") or "").strip()
        detail = _compact_text(stderr or stdout or "命令返回非零退出码", 220)
        problems.append(f"巡检命令执行失败：{detail}")

    status_class = str(payload.get("status_class") or "").upper()
    if status_class == "PASS" and str(payload.get("status") or "").upper() == "PASS" and payload.get("returncode", 0) == 0:
        impact = "无异常。"
        recommendation = "无需处理。"
    elif status_class == "NEEDS_INTERACTION":
        impact = "需要人工登录、验证码或配置确认，自动巡检无法继续验证相关平台。"
        recommendation = "先确认账号/验证码/API 配置，再由用户批准重跑对应巡检。"
    elif status_class == "EXPECTED_DEGRADED":
        impact = "存在已声明的外部限制或平台限制，当前不视为代码阻断。"
        recommendation = "确认降级原因是否仍合理；如环境已具备条件，再批准重跑或升级验证。"
    else:
        impact = "至少一个质量检查失败，相关平台或治理语义可能不可信。"
        recommendation = "先查看失败项和原始结果，定位后由用户审批修复；不要自动改代码。"

    return {
        "problems": problems,
        "impact": impact,
        "recommendation": recommendation,
    }


def _status_class(result: dict[str, Any], returncode: int) -> str:
    state = result.get("quality_state") if isinstance(result.get("quality_state"), dict) else {}
    if returncode == 0 and str(result.get("status") or "").upper() == "PASS":
        return "PASS"
    for item in result.get("results") or []:
        if not _is_failed_result(item):
            continue
        text = json.dumps(item, ensure_ascii=False).lower()
        if any(marker in text for marker in ("needs_interaction", "login_required", "captcha_required", "anti_bot_verification")):
            return "NEEDS_INTERACTION"
    state_class = str(state.get("status_class") or "").upper()
    if state_class in {"NEEDS_INTERACTION", "EXPECTED_DEGRADED"}:
        return state_class
    return "FAIL"


def _notification_fingerprint(payload: dict[str, Any]) -> str:
    summary = summarize_campaign_result(payload)
    source = {
        "profile": str(payload.get("profile") or "unknown"),
        "status": str(payload.get("status") or "FAIL").upper(),
        "status_class": str(payload.get("status_class") or "FAIL").upper(),
        "problems": summary.get("problems") or [],
    }
    serialized = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def _campaign_quality_gate_command(root: Path, profile: str, *, extra_args: list[str] | None = None) -> list[str]:
    """Build the side-effect-free command used by scheduled patrols."""
    return [
        _python_executable(root),
        "scripts/kr_quality_gate.py",
        "--campaign",
        "--profile",
        profile,
        "--json",
        "--no-write-state",
        *(extra_args or []),
    ]


def _is_failed_result(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    return item.get("status") == "fail" or int(item.get("returncode") or 0) != 0


def _problem_line(item: dict[str, Any]) -> str:
    name = str(item.get("command") or item.get("schema") or "unknown check")
    text = str(item.get("stderr") or item.get("stdout") or item.get("campaign_status") or "无详情")
    reason = _compact_text(text, 180)
    return f"{name}：{reason}"


def _parse_gate_result(stdout: str) -> dict[str, Any]:
    try:
        parsed = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _python_executable(root: Path) -> str:
    bundled = root / ".python312" / "python.exe"
    return str(bundled) if bundled.exists() else sys.executable


def _timeout_for_profile(profile: str) -> int:
    return {"smoke": 1200, "deep": 1800, "destructive": 2400}.get(profile, 1200)


def _timestamp_slug(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _display_command(command: list[str]) -> str:
    return " ".join(command)


def _compact_text(value: str, limit: int) -> str:
    normalized = " ".join(str(value).split())
    return normalized if len(normalized) <= limit else normalized[: limit - 3] + "..."
