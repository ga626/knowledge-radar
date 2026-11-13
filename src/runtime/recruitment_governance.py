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
import uuid
import threading
from contextvars import ContextVar
from typing import Any, Dict

from runtime.leases import LeaseResult, default_owner, get_runtime_lease_coordinator
from runtime.platform_risk import compute_platform_cooldown, normalize_platform_risk_event

log = logging.getLogger("mcp-server")

# 默认配置
DEFAULT_SEARCH_COOLDOWN_S = 60
DEFAULT_MIN_SEARCH_INTERVAL_S = 10  # 最小搜索间隔 10 秒
DEFAULT_MAX_SEARCHES_PER_HOUR = 30  # 每小时最大搜索次数
DEFAULT_MAX_FAILURES_PER_HOUR = 5
DEFAULT_SEARCH_COOLDOWN_MAX_S = 300
LEASED_BROWSER_PLATFORMS = {"boss", "liepin", "maimai"}
PLATFORM_LEASE_TTL_S = 180
NO_COOLDOWN_REASONS = {
    "city_mapping_missing",
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
    "manual_action_required",
    "auth_preflight_failed",
    "tool_failure_needs_repair",
    "search_route_unreadable",
    "request_outcome_unknown",
}
LOCAL_FAILURE_REASONS = {
    "page_identity_changed",
    "browser_navigation_interrupted",
    "opencli_session_page_ambiguous",
    "page_structure_or_not_ready",
    "search_query_not_applied",
    "empty_detail",
    "detail_response_unparsed",
    "search_response_unparsed",
    "csrf_handshake_failed",
    "opencli_dependency_or_output_invalid",
    "opencli_identity_mismatch",
    "opencli_pinned_dependency_missing",
    "opencli_daily_browser_binding_required",
    "browser_disconnected",
    "collector_exception",
    "invalid_boss_detail_url",
    "err_invalid_url",
    "unsupported_url",
    "native_host_unavailable", "native_host_unreachable", "native_command_timeout",
    "native_host_disconnected", "native_host_http_error", "native_host_invalid_result",
    "browser_busy", "cancelled", "invalid_boss_url", "content_unavailable",
}
NO_COOLDOWN_REASONS |= LOCAL_FAILURE_REASONS

# 平台配置
PLATFORM_CONFIG: Dict[str, Dict[str, Any]] = {
    'v2ex': {'min_search_interval_s': 2, 'max_searches_per_hour': 120},
    "boss": {
        "search_cooldown_s": 60,
        "search_cooldown_max_s": 300,
        "min_search_interval_s": 15,
        "max_searches_per_hour": 12,
        "max_failures_per_hour": 3,
    },
    "liepin": {
        "search_cooldown_s": 60,
        "search_cooldown_max_s": 300,
        "min_search_interval_s": 10,
        "max_searches_per_hour": 30,
    },
    "maimai": {
        "search_cooldown_s": 60,
        "search_cooldown_max_s": 300,
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
    _ensure_optional_column(conn, "recruitment_search_gate", "operation", "TEXT DEFAULT 'search'")
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
    operation: str = "search",
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
    min_interval = config.get("min_search_interval_s", DEFAULT_MIN_SEARCH_INTERVAL_S) if operation == "search" else 0
    max_per_hour = config.get("max_searches_per_hour", DEFAULT_MAX_SEARCHES_PER_HOUR) if operation == "search" else config.get("max_operations_per_hour", 120)
    max_failures_per_hour = config.get("max_failures_per_hour", DEFAULT_MAX_FAILURES_PER_HOUR)
    account_norm = _norm(account_slot)
    keyword_norm = _norm(keyword)
    city_norm = _norm(city)

    now = time.time()

    try:
        conn = _get_conn()
        # 检查冷却状态
        # 本表仅记录KR自己的退避；服务端Retry-After由站点适配器单独保存。
        # 对历史长退避作只读解释，不重写历史，也不裁短服务端期限。
        maximum = int(config.get("search_cooldown_max_s", DEFAULT_SEARCH_COOLDOWN_MAX_S))
        query = "SELECT MIN(cooldown_until, ts + ?) AS effective_until, outcome, reason FROM recruitment_search_gate WHERE platform = ?"
        params: list[Any] = [maximum, platform]
        if account_norm:
            query += " AND (account_slot = ? OR account_slot = '')"
            params.append(account_norm)
        # 工具/解析错误不构成远端冷却，也不能遮住另一条真实风控记录。
        excluded = sorted(NO_COOLDOWN_REASONS)
        query += " AND lower(reason) NOT IN (" + ",".join("?" for _ in excluded) + ")"
        params.extend(excluded)
        query += " ORDER BY effective_until DESC LIMIT 1"
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
            "SELECT ts FROM recruitment_search_gate WHERE platform = ? AND operation = ? ORDER BY ts DESC LIMIT 1",
            (platform, operation)
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
            "SELECT COUNT(*) FROM recruitment_search_gate WHERE platform = ? AND operation = ? AND ts > ?",
            (platform, operation, hour_ago)
        ).fetchone()[0]

        count_excluded = sorted(LOCAL_FAILURE_REASONS | {'city_mapping_missing'})
        failure_query = (
            "SELECT COUNT(*) FROM recruitment_search_gate WHERE platform = ? AND ts > ? "
            "AND outcome IN ('blocked', 'failed') AND lower(reason) NOT IN (" + ",".join("?" for _ in count_excluded) + ")"
        )
        failure_params: list[Any] = [platform, hour_ago, *count_excluded]
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

        # 失败次数保留为诊断数据；冷却到期后不叠加一小时的隐含失败锁。

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
            "allowed": False,
            "reason": "门禁状态不可读取；停止本次采集以保护账号",
            "reason_code": "recruitment_gate_unavailable",
            "error_type": type(e).__name__,
            "cooldown_remaining_s": 0,
            "searches_this_hour": 0,
            "max_searches_per_hour": max_per_hour,
            "max_failures_per_hour": max_failures_per_hour,
            "scope": {"account_slot": account_norm, "keyword": keyword_norm, "city": city_norm},
        }


def check_platform_admission(
    platform: str,
    *,
    keyword: str = "",
    city: str = "",
    account_slot: str = "",
    operation: str = "search",
    resource: str = "browser",
) -> Dict[str, Any]:
    """Cheap read-only red/yellow/green check before browser-heavy recruitment calls."""

    platform_norm = _norm(platform)
    if platform_norm in LEASED_BROWSER_PLATFORMS:
        from importlib import import_module
        module = import_module('collectors.platform.' + platform_norm)
        browser = module.admission()
        # A site risk latch also stops HTTP, but an unrelated login wait does not.
        if resource == 'http' and browser.get('reason') == 'pending_manual_interaction':
            browser = {'allowed': True}
        if not browser['allowed']:
            reason = {'SECURITY_BLOCK': 'anti_bot_verification', 'rate_limited_remote': 'rate_limited',
                'outcome_latched': 'request_outcome_unknown', 'opencli_caller_timeout': 'request_outcome_unknown'}.get(browser['reason'], browser['reason'])
            return {'allowed': False, 'reason_code': reason, 'reason': reason,
                'manual_action_required': bool(browser.get('manual_action_required')), 'retry_after_s': browser.get('retry_after_s', 0)}
    gate = check_search_gate(platform_norm, account_slot=account_slot, keyword=keyword, city=city, operation=operation)
    if not gate.get("allowed"):
        retry_after = float(gate.get("cooldown_remaining_s") or 0)
        return {
            "schema": "knowledgeradar-recruitment-admission/v1",
            "platform": platform_norm,
            "admission": "red",
            "allowed": False,
            "reason_code": gate.get('reason_code') or "rate_limited",
            "reason": str(gate.get("reason") or "search_gate_blocked"),
            "retry_after_s": round(max(0.0, retry_after), 3),
            "manual_action_required": False,
            "failure_class": "blocked_no_claim",
            "evidence_strength": "blocked_no_claim",
            "market_claim_allowed": False,
            "salary_claim_allowed": False,
            "gate": gate,
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
        request_id=uuid.uuid4().hex,
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
    operation: str = "search",
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
        operation=operation,
        reason_code=reason,
        outcome=outcome,
        scope={"account_slot": account_slot, "keyword": keyword, "city": city},
        manual_action_required=normalized_reason in {
            "login_required",
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
        conn.execute('BEGIN IMMEDIATE')
        latest = conn.execute('SELECT MAX(ts) FROM recruitment_search_gate WHERE platform = ?', (platform,)).fetchone()[0]
        timestamp = max(now, float(latest) + .000001) if latest is not None else now
        conn.execute(
            """
            INSERT INTO recruitment_search_gate
              (platform, ts, outcome, reason, cooldown_until, account_slot, keyword_norm, city_norm, operation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (platform, timestamp, outcome, reason, cooldown_until, _norm(account_slot), _norm(keyword), _norm(city), operation),
        )
        conn.commit()
        conn.close()

        if cooldown_until > now:
            log.warning(f"招聘平台 {platform} 搜索失败，冷却 {cooldown_s} 秒: {reason}")

    except Exception as e:
        raise RuntimeError('recruitment_accounting_unavailable') from e
    finally:
        if 'conn' in locals():
            conn.close()


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
        maximum = int(PLATFORM_CONFIG.get(platform, {}).get("search_cooldown_max_s", DEFAULT_SEARCH_COOLDOWN_MAX_S))
        effective_until = min(float(row[0]), float(row[1]) + maximum) if row and row[0] and row[1] else 0
        if not effective_until or effective_until <= time.time() - 300:
            return 0
        return max(0, int(effective_until - float(row[1])))
    except Exception:
        return 0


_active_operations = ContextVar('kr_recruitment_operations', default=())


def execute_operation(platform, operation, call, *, keyword='', city='', resource='browser'):
    """唯一招聘执行出口；嵌套门面不双计，站点知识不进入公共层。"""
    key = (platform, operation)
    if key in _active_operations.get():
        return call()
    admission = check_platform_admission(platform, keyword=keyword, city=city, operation=operation, resource=resource)
    def blocked(reason, manual=False, retry=0):
        return {'platform': {'boss':'BOSS直聘', 'liepin':'猎聘', 'maimai':'脉脉', 'v2ex':'V2EX'}.get(platform, platform),
            'status': 'needs_interaction' if manual else 'blocked', 'items': [], 'failure_type': reason,
            'error': {'failure_type': reason, 'type': reason, 'reason_code': reason, 'manual_action_required': manual, 'retry_after_s': retry},
            'manual_action_required': manual, 'retryable': False, 'retry_after_s': retry,
            'market_claim_allowed': False, 'salary_claim_allowed': False,
            'metadata': {'admission': admission, 'request_executed': False}}
    pending_login = (platform in LEASED_BROWSER_PLATFORMS and resource == 'browser'
                     and admission.get('reason_code') == 'pending_manual_interaction')
    if not admission.get('allowed') and not pending_login:
        return blocked(admission.get('reason_code', 'search_gate_blocked'), bool(admission.get('manual_action_required')), admission.get('retry_after_s', 0))
    lease = acquire_platform_lease(platform, keyword=keyword, city=city) if platform in LEASED_BROWSER_PLATFORMS and resource == 'browser' else None
    if lease and not lease.acquired:
        from platform_adapters.common import defer_browser_busy
        defer_browser_busy(platform, 'platform_lease_busy')
        return blocked('platform_lease_busy', retry=lease.retry_after_s)
    stop = threading.Event()
    def renew():
        while not stop.wait(45):
            if not get_runtime_lease_coordinator().heartbeat(lease.lease_id, ttl_s=PLATFORM_LEASE_TTL_S):
                return
    heart = threading.Thread(target=renew, daemon=True, name='kr-recruitment-lease') if lease else None
    if heart:
        heart.start()
    token = _active_operations.set((*_active_operations.get(), key))
    started = time.monotonic()
    result = None
    try:
        # 第二次准入在独占租约内，防止并行请求同时通过频控。
        if lease:
            admission = check_platform_admission(platform, keyword=keyword, city=city, operation=operation, resource=resource)
            if admission.get('reason_code') == 'pending_manual_interaction':
                # 仅真实新业务动作触发一次核验，放在独占租约内；纯状态查询
                # 不探站，不建定时器，也不把风控/未知结果当作登录等待重放。
                from importlib import import_module
                site = import_module('collectors.platform.' + platform)
                verification = site.complete_login()
                admission = check_platform_admission(platform, keyword=keyword, city=city, operation=operation, resource=resource)
                if not admission.get('allowed'):
                    admission = {**admission, 'interaction_verification': {
                        'status': verification.get('status', 'unknown'),
                        'failure_type': verification.get('failure_type', '')}}
            if not admission.get('allowed'):
                return blocked(admission.get('reason_code', 'search_gate_blocked'), bool(admission.get('manual_action_required')))
        try:
            result = call()
        except BaseException as exc:
            from runtime.resource_concurrency import ResourceDeferred
            if isinstance(exc, ResourceDeferred):
                raise
            record_search_outcome(platform, 'failed', 'collector_exception', keyword=keyword, city=city, operation=operation)
            raise
        if not isinstance(result, dict):
            raise TypeError('recruitment_result_must_be_mapping')
        if platform in LEASED_BROWSER_PLATFORMS:
            from runtime.borrowed_observations import observe_borrowed_result
            result = observe_borrowed_result(platform, result)
        error = result.get('error') if isinstance(result.get('error'), dict) else {}
        reason = str(result.get('failure_type') or error.get('failure_type') or '').split(':', 1)[0]
        outcome = 'blocked' if result.get('manual_action_required') or error.get('manual_action_required') else 'failed' if result.get('error') else 'ok'
        # 准入、参数及连接阶段没碰站点的失败不消耗站点频次。
        executed = result.get('request_executed', result.get('metadata', {}).get('request_executed'))
        if executed is not False and reason != 'city_mapping_missing':
            record_search_outcome(platform, outcome, reason, keyword=keyword, city=city, operation=operation)
        result.setdefault('metadata', {}).update(operation=operation, admission=admission)
        result['metadata']['operation_elapsed_s'] = round(time.monotonic() - started, 3)
        return result
    finally:
        _active_operations.reset(token)
        stop.set()
        if heart:
            heart.join(timeout=2)
        if lease:
            release_platform_lease(lease.lease_id)


def execute_browser_operation(platform, call):
    """HTTP-first collectors acquire browser resources only at the dynamic fallback."""
    from importlib import import_module
    site = import_module('collectors.platform.' + platform)
    admission = site.admission()
    if not admission.get('allowed') and admission.get('reason') != 'pending_manual_interaction':
        return site._site().failure(admission.get('reason', 'browser_blocked'), request_executed=False)
    lease = acquire_platform_lease(platform)
    if not lease.acquired:
        from platform_adapters.common import defer_browser_busy
        defer_browser_busy(platform, 'platform_lease_busy')
        return site._site().failure('platform_lease_busy', request_executed=False)
    try:
        admission = site.admission()
        if admission.get('reason') == 'pending_manual_interaction':
            site.complete_login()
            admission = site.admission()
        if not admission.get('allowed'):
            return site._site().failure(admission.get('reason', 'browser_blocked'), request_executed=False)
        return call()
    finally:
        release_platform_lease(lease.lease_id)
