"""通用招聘平台策略门禁模块。

为 BOSS直聘、猎聘、脉脉等招聘平台提供统一的：
- 搜索冷却（失败后自动冷却）
- 频率控制（搜索间隔）
- 登录态预检

设计原则：
- 复用小红书的 SQLite 任务数据库
- 轻量级，无外部依赖
- 每个平台独立的冷却状态
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Any, Dict

from runtime.browser_sessions import browser_sessions_summary
from runtime.leases import LeaseResult, default_owner, get_runtime_lease_coordinator
from runtime.platform_risk import compute_platform_cooldown, normalize_platform_risk_event

log = logging.getLogger("mcp-server")

# 默认配置
DEFAULT_SEARCH_COOLDOWN_S = 1800  # 搜索失败后冷却 30 分钟
DEFAULT_MIN_SEARCH_INTERVAL_S = 10  # 最小搜索间隔 10 秒
DEFAULT_MAX_SEARCHES_PER_HOUR = 30  # 每小时最大搜索次数
DEFAULT_MAX_FAILURES_PER_HOUR = 5
DEFAULT_SEARCH_COOLDOWN_MAX_S = 7200
LEASED_BROWSER_PLATFORMS = {"boss", "liepin", "zhilian"}
PLATFORM_LEASE_TTL_S = 180
NO_COOLDOWN_REASONS = {
    "empty_results",
    "no_results",
    "zero_results",
    "cdp_runtime_error",
    "cdp_method_error",
    "cdp_target_error",
    "cdp_version_error",
    "no_page_target",
    "city_mismatch",
    "login_required",
    "platform_verification_required",
    "manual_action_required",
    "auth_preflight_failed",
    "tool_failure_needs_repair",
    "search_route_unreadable",
}

# 平台配置
PLATFORM_CONFIG: Dict[str, Dict[str, Any]] = {
    "boss": {
        "search_cooldown_s": 1800,
        "search_cooldown_max_s": 7200,
        "min_search_interval_s": 15,
        "max_searches_per_hour": 12,
        "max_failures_per_hour": 3,
    },
    "liepin": {
        "search_cooldown_s": 1800,
        "search_cooldown_max_s": 7200,
        "min_search_interval_s": 10,
        "max_searches_per_hour": 30,
    },
    "maimai": {
        "search_cooldown_s": 3600,  # 脉脉 WAF 更严格，冷却更长
        "search_cooldown_max_s": 14400,
        "min_search_interval_s": 20,
        "max_searches_per_hour": 15,
    },
}


def _get_db_path() -> str:
    return os.environ.get(
        "KR_TASK_DB_PATH",
        os.path.join(os.path.expanduser("~"), ".workbuddy", "runtime", "knowledgeradar-tasks.sqlite3"),
    )


def _get_conn() -> sqlite3.Connection:
    db_path = _get_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recruitment_search_gate (
            platform TEXT NOT NULL,
            ts REAL NOT NULL,
            outcome TEXT NOT NULL,
            reason TEXT DEFAULT '',
            cooldown_until REAL DEFAULT 0,
            account_slot TEXT DEFAULT '',
            keyword_norm TEXT DEFAULT '',
            city_norm TEXT DEFAULT '',
            PRIMARY KEY (platform, ts)
        )
    """)
    _ensure_optional_column(conn, "recruitment_search_gate", "account_slot", "TEXT DEFAULT ''")
    _ensure_optional_column(conn, "recruitment_search_gate", "keyword_norm", "TEXT DEFAULT ''")
    _ensure_optional_column(conn, "recruitment_search_gate", "city_norm", "TEXT DEFAULT ''")
    conn.commit()
    return conn


def _ensure_optional_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    try:
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    except Exception as exc:
        log.debug(f"招聘门禁表结构迁移失败: {table}.{column}: {exc}")


def _norm(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())[:120]


def check_search_gate(
    platform: str,
    *,
    account_slot: str = "",
    keyword: str = "",
    city: str = "",
) -> Dict[str, Any]:
    """检查平台搜索门禁状态。

    Returns:
        {
            "allowed": bool,
            "reason": str,  # 如果不允许，说明原因
            "cooldown_remaining_s": float,
            "searches_this_hour": int,
            "max_searches_per_hour": int,
        }
    """
    config = PLATFORM_CONFIG.get(platform, {})
    min_interval = config.get("min_search_interval_s", DEFAULT_MIN_SEARCH_INTERVAL_S)
    max_per_hour = config.get("max_searches_per_hour", DEFAULT_MAX_SEARCHES_PER_HOUR)
    max_failures_per_hour = config.get("max_failures_per_hour", DEFAULT_MAX_FAILURES_PER_HOUR)
    account_norm = _norm(account_slot)
    keyword_norm = _norm(keyword)
    city_norm = _norm(city)

    now = time.time()

    try:
        conn = _get_conn()
        # 检查冷却状态
        query = "SELECT cooldown_until, outcome, reason FROM recruitment_search_gate WHERE platform = ?"
        params: list[Any] = [platform]
        if account_norm:
            query += " AND (account_slot = ? OR account_slot = '')"
            params.append(account_norm)
        query += " ORDER BY ts DESC LIMIT 1"
        row = conn.execute(query, tuple(params)).fetchone()

        if row and row[0] and row[0] > now and str(row[2] or "").lower() not in NO_COOLDOWN_REASONS:
            remaining = row[0] - now
            conn.close()
            return {
                "allowed": False,
                "reason": f"冷却中（上次搜索失败，{int(remaining)}秒后恢复）",
                "cooldown_remaining_s": round(remaining, 1),
                "searches_this_hour": 0,
                "max_searches_per_hour": max_per_hour,
                "max_failures_per_hour": max_failures_per_hour,
                "next_retry_at": row[0],
                "scope": {"account_slot": account_norm, "keyword": keyword_norm, "city": city_norm},
            }

        # 检查最小间隔
        row = conn.execute(
            "SELECT ts FROM recruitment_search_gate WHERE platform = ? ORDER BY ts DESC LIMIT 1",
            (platform,)
        ).fetchone()

        if row and (now - row[0]) < min_interval:
            wait = min_interval - (now - row[0])
            conn.close()
            return {
                "allowed": False,
                "reason": f"搜索间隔过短（需等待{int(wait)}秒）",
                "cooldown_remaining_s": 0,
                "searches_this_hour": 0,
                "max_searches_per_hour": max_per_hour,
            }

        # 检查每小时频率
        hour_ago = now - 3600
        count = conn.execute(
            "SELECT COUNT(*) FROM recruitment_search_gate WHERE platform = ? AND ts > ?",
            (platform, hour_ago)
        ).fetchone()[0]

        failure_query = "SELECT COUNT(*) FROM recruitment_search_gate WHERE platform = ? AND ts > ? AND outcome IN ('blocked', 'failed')"
        failure_params: list[Any] = [platform, hour_ago]
        if account_norm:
            failure_query += " AND (account_slot = ? OR account_slot = '')"
            failure_params.append(account_norm)
        if keyword_norm:
            failure_query += " AND keyword_norm = ?"
            failure_params.append(keyword_norm)
        if city_norm:
            failure_query += " AND city_norm = ?"
            failure_params.append(city_norm)
        failure_count = conn.execute(failure_query, tuple(failure_params)).fetchone()[0]

        conn.close()

        if count >= max_per_hour:
            return {
                "allowed": False,
                "reason": f"每小时搜索次数已达上限（{count}/{max_per_hour}）",
                "cooldown_remaining_s": 0,
                "searches_this_hour": count,
                "max_searches_per_hour": max_per_hour,
                "max_failures_per_hour": max_failures_per_hour,
                "scope": {"account_slot": account_norm, "keyword": keyword_norm, "city": city_norm},
            }

        if failure_count >= max_failures_per_hour:
            return {
                "allowed": False,
                "reason": f"账号/关键词/城市失败次数已达上限（{failure_count}/{max_failures_per_hour}）",
                "cooldown_remaining_s": 0,
                "searches_this_hour": count,
                "failures_this_hour": failure_count,
                "max_searches_per_hour": max_per_hour,
                "max_failures_per_hour": max_failures_per_hour,
                "scope": {"account_slot": account_norm, "keyword": keyword_norm, "city": city_norm},
            }

        return {
            "allowed": True,
            "reason": "ok",
            "cooldown_remaining_s": 0,
            "searches_this_hour": count,
            "failures_this_hour": failure_count,
            "max_searches_per_hour": max_per_hour,
            "max_failures_per_hour": max_failures_per_hour,
            "scope": {"account_slot": account_norm, "keyword": keyword_norm, "city": city_norm},
        }

    except Exception as e:
        log.debug(f"招聘策略门禁检查失败: {e}")
        return {
            "allowed": True,
            "reason": f"门禁检查异常，放行: {e}",
            "cooldown_remaining_s": 0,
            "searches_this_hour": 0,
            "max_searches_per_hour": max_per_hour,
            "max_failures_per_hour": max_failures_per_hour,
            "scope": {"account_slot": account_norm, "keyword": keyword_norm, "city": city_norm},
        }


def _pending_manual_session(platform: str) -> Dict[str, Any]:
    try:
        summary = browser_sessions_summary(limit=10)
    except Exception as exc:
        return {"present": False, "error": str(exc)}
    platform_norm = _norm(platform)
    for item in summary.get("pending_interactions") or []:
        if not isinstance(item, dict):
            continue
        if _norm(str(item.get("platform") or "")) != platform_norm:
            continue
        status = str(item.get("status") or "")
        if status in {"pending_user", "waiting_for_user", "verifying"}:
            return {
                "present": True,
                "status": status,
                "interaction": item,
                "reason": str(item.get("reason_code") or "manual_action_required"),
            }
    return {"present": False}


def _recover_pending_manual_session(platform: str, pending: Dict[str, Any]) -> Dict[str, Any]:
    """Clear stale manual sessions when a background auth probe proves login is ready."""

    try:
        from runtime.chrome_manager import complete_browser_interaction, probe_browser_auth

        probe = probe_browser_auth(platform)
        if probe.get("status") == "ok" and not probe.get("manual_action_required"):
            complete = complete_browser_interaction(platform, probe_result=probe)
            return {
                "recovered": True,
                "probe": probe,
                "complete": {
                    "status": complete.get("status"),
                    "manual_state_auto_recovered": bool(complete.get("manual_state_auto_recovered")),
                },
            }
        return {"recovered": False, "probe": probe}
    except Exception as exc:
        log.debug(f"招聘平台人工会话自愈探针失败: platform={platform}, pending={pending.get('reason')}: {exc}")
        return {
            "recovered": False,
            "probe": {
                "status": "degraded",
                "auth_state": "probe_error",
                "manual_action_required": False,
                "detail": str(exc),
            },
        }


def check_platform_admission(
    platform: str,
    *,
    keyword: str = "",
    city: str = "",
    account_slot: str = "",
) -> Dict[str, Any]:
    """Cheap read-only red/yellow/green check before browser-heavy recruitment calls."""

    platform_norm = _norm(platform)
    gate = check_search_gate(platform_norm, account_slot=account_slot, keyword=keyword, city=city)
    if not gate.get("allowed"):
        retry_after = float(gate.get("cooldown_remaining_s") or 0)
        return {
            "schema": "knowledgeradar-recruitment-admission/v1",
            "platform": platform_norm,
            "admission": "red",
            "allowed": False,
            "reason_code": "search_gate_blocked",
            "reason": str(gate.get("reason") or "search_gate_blocked"),
            "retry_after_s": round(max(0.0, retry_after), 3),
            "manual_action_required": False,
            "failure_class": "blocked_no_claim",
            "evidence_strength": "blocked_no_claim",
            "market_claim_allowed": False,
            "salary_claim_allowed": False,
            "gate": gate,
        }

    if platform_norm in LEASED_BROWSER_PLATFORMS:
        pending = _pending_manual_session(platform_norm)
        if pending.get("present"):
            recovery = _recover_pending_manual_session(platform_norm, pending)
            if recovery.get("recovered"):
                return {
                    "schema": "knowledgeradar-recruitment-admission/v1",
                    "platform": platform_norm,
                    "admission": "open",
                    "allowed": True,
                    "reason_code": "pending_manual_interaction_auto_recovered",
                    "reason": "已有人工会话已通过后台登录态探针自动恢复。",
                    "retry_after_s": 0,
                    "manual_action_required": False,
                    "market_claim_allowed": True,
                    "salary_claim_allowed": False,
                    "gate": gate,
                    "recovery": recovery,
                }
            return {
                "schema": "knowledgeradar-recruitment-admission/v1",
                "platform": platform_norm,
                "admission": "red",
                "allowed": False,
                "reason_code": "pending_manual_interaction",
                "reason": str(pending.get("reason") or "manual_action_required"),
                "retry_after_s": 0,
                "manual_action_required": True,
                "failure_class": "blocked_no_claim",
                "evidence_strength": "blocked_no_claim",
                "market_claim_allowed": False,
                "salary_claim_allowed": False,
                "manual_interaction": pending.get("interaction") or {},
                "gate": gate,
                "recovery": recovery,
            }

    return {
        "schema": "knowledgeradar-recruitment-admission/v1",
        "platform": platform_norm,
        "admission": "open",
        "allowed": True,
        "reason_code": "ok",
        "reason": "ok",
        "retry_after_s": 0,
        "manual_action_required": False,
        "gate": gate,
    }


def acquire_platform_lease(
    platform: str,
    *,
    keyword: str = "",
    city: str = "",
    ttl_s: int = PLATFORM_LEASE_TTL_S,
) -> LeaseResult:
    """Acquire an exclusive browser-platform lease for a recruitment call."""

    platform_norm = _norm(platform)
    owner = default_owner(
        "search_recruitment",
        project_root=os.environ.get("KR_PROJECT_ROOT", ""),
        request_id=f"{platform_norm}:{_norm(keyword)}:{_norm(city)}",
    )
    return get_runtime_lease_coordinator().acquire_exclusive(
        "recruitment_platform",
        platform_norm,
        owner=owner,
        ttl_s=ttl_s,
        metadata={"keyword": _norm(keyword), "city": _norm(city), "purpose": "search_recruitment"},
    )


def release_platform_lease(lease_id: str) -> bool:
    return get_runtime_lease_coordinator().release(lease_id)


def record_search_outcome(
    platform: str,
    outcome: str,
    reason: str = "",
    *,
    account_slot: str = "",
    keyword: str = "",
    city: str = "",
) -> None:
    """记录搜索结果，失败时自动设置冷却。

    Args:
        platform: 平台名称（boss/liepin/maimai）
        outcome: 搜索结果（ok/blocked/failed/degraded）
        reason: 原因说明
    """
    config = PLATFORM_CONFIG.get(platform, {})
    base_cooldown_s = int(config.get("search_cooldown_s", DEFAULT_SEARCH_COOLDOWN_S))
    max_cooldown_s = int(config.get("search_cooldown_max_s", DEFAULT_SEARCH_COOLDOWN_MAX_S))

    now = time.time()
    normalized_reason = str(reason or "").lower()
    should_cooldown = outcome in {"blocked", "failed"} and normalized_reason not in NO_COOLDOWN_REASONS
    event = normalize_platform_risk_event(
        platform=platform,
        operation="search",
        reason_code=reason,
        outcome=outcome,
        scope={"account_slot": account_slot, "keyword": keyword, "city": city},
        manual_action_required=normalized_reason in {
            "login_required",
            "platform_verification_required",
            "manual_action_required",
            "auth_preflight_failed",
        },
    )
    previous_cooldown_s = _latest_cooldown_seconds(platform, account_slot=account_slot, keyword=keyword, city=city)
    cooldown = compute_platform_cooldown(
        event,
        base_s=base_cooldown_s,
        maximum_s=max_cooldown_s,
        previous_cooldown_s=previous_cooldown_s,
        jitter_ratio=0.0,
        now=now,
    )
    cooldown_s = int(cooldown["cooldown_seconds"]) if should_cooldown else 0
    cooldown_until = float(cooldown["cooldown_until"]) if should_cooldown else 0

    try:
        conn = _get_conn()
        conn.execute(
            """
            INSERT INTO recruitment_search_gate
              (platform, ts, outcome, reason, cooldown_until, account_slot, keyword_norm, city_norm)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (platform, now, outcome, reason, cooldown_until, _norm(account_slot), _norm(keyword), _norm(city)),
        )
        conn.commit()
        conn.close()

        if cooldown_until > now:
            log.warning(f"招聘平台 {platform} 搜索失败，冷却 {cooldown_s} 秒: {reason}")

    except Exception as e:
        log.debug(f"记录招聘搜索结果失败: {e}")


def get_gate_summary(platform: str) -> Dict[str, Any]:
    """获取平台门禁摘要（用于 health_check 和诊断）。"""
    gate = check_search_gate(platform)
    config = PLATFORM_CONFIG.get(platform, {})
    return {
        "platform": platform,
        "gate_status": "open" if gate["allowed"] else "blocked",
        "reason": gate["reason"],
        "cooldown_remaining_s": gate["cooldown_remaining_s"],
        "searches_this_hour": gate["searches_this_hour"],
        "max_searches_per_hour": gate["max_searches_per_hour"],
        "config": config,
    }


def _latest_cooldown_seconds(
    platform: str,
    *,
    account_slot: str = "",
    keyword: str = "",
    city: str = "",
) -> int:
    try:
        conn = _get_conn()
        query = "SELECT cooldown_until, ts FROM recruitment_search_gate WHERE platform = ?"
        params: list[Any] = [platform]
        account_norm = _norm(account_slot)
        keyword_norm = _norm(keyword)
        city_norm = _norm(city)
        if account_norm:
            query += " AND (account_slot = ? OR account_slot = '')"
            params.append(account_norm)
        if keyword_norm:
            query += " AND keyword_norm = ?"
            params.append(keyword_norm)
        if city_norm:
            query += " AND city_norm = ?"
            params.append(city_norm)
        query += " ORDER BY ts DESC LIMIT 1"
        row = conn.execute(query, tuple(params)).fetchone()
        conn.close()
        if not row or not row[0] or not row[1]:
            return 0
        return max(0, int(float(row[0]) - float(row[1])))
    except Exception:
        return 0
