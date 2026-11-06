"""各服务独立配额的发送准入：同账本、先预留、结果未知不退款或重发。"""
from __future__ import annotations
from runtime.research_storage import guarded_transaction

from runtime.execution_context import execution_value
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import copy
import hashlib
import json
import os
from pathlib import Path
import threading
import uuid
from urllib.parse import urlsplit

from runtime.paths import runtime_state_dir

_LOCK = threading.RLock()
_LOCAL_SCOPE = "local-" + uuid.uuid4().hex
_RATES = {
    "beijing": (Decimal("0.8"), Decimal("0.1"), Decimal("2.7")),
    "singapore": (Decimal("1.094"), Decimal("0.117"), Decimal("3.427")),
}
DEFAULT_POLICY = {
    "schema": "knowledgeradar-paid-policy/v3", "enabled": True, "usd_to_cny": "8",
    "providers": {
        "bailian": {"enabled": True, "mode": "money", "period": "day", "unit": "CNY", "limit": "10"},
        "tavily": {"enabled": True, "mode": "free_quota", "period": "month", "unit": "credits", "limit": "1000"},
        "brave": {"enabled": True, "mode": "free_quota", "period": "month", "unit": "USD", "limit": "5"},
        "exa": {"enabled": True, "mode": "free_quota", "period": "month", "unit": "USD", "limit": "10"},
        "anysearch": {"enabled": True, "mode": "free_quota", "period": "day", "unit": "calls", "limit": "1000", "per_second_limit": 10},
        "openalex": {"enabled": True, "mode": "free_quota", "period": "utc_day", "unit": "USD", "limit": "1"},
        "tikhub": {"enabled": True, "mode": "calls", "period": "day", "unit": "calls", "limit": "2"},
        "siliconflow_ocr": {"enabled": True, "mode": "remote_free"},
        "wanfang": {"enabled": False, "mode": "disabled"},
    },
}


class PaidAdmissionError(ValueError):
    def __init__(self, reason):
        super().__init__(reason)
        self.error_code = reason


def _amount(value, *, zero=False):
    number = Decimal(str(value))
    if not number.is_finite() or number < 0 or (not zero and number == 0):
        raise ValueError("invalid_paid_budget")
    return number


def load_policy():
    policy = copy.deepcopy(DEFAULT_POLICY)
    source = os.environ.get("KR_PAID_POLICY_PATH")
    try:
        if source:
            override = json.loads(Path(source).read_text(encoding="utf-8"))
            if override.get("schema") == "knowledgeradar-paid-policy/v2":
                # 只迁移开关；旧全局金额、异常估价不能继续参与任何准入。
                override = {"enabled": override.get("enabled", True), "providers": {
                    key: {"enabled": row.get("enabled", True)}
                    for key, row in override.get("providers", {}).items()
                    if key in DEFAULT_POLICY["providers"]}}
            elif override.get("schema") != policy["schema"]:
                raise ValueError("policy schema")
            policy["enabled"] = override.get("enabled", True)
            policy["usd_to_cny"] = override.get("usd_to_cny", policy["usd_to_cny"])
            for key, row in override.get("providers", {}).items():
                if key in policy["providers"]:
                    policy["providers"][key].update(row)
        _amount(policy["usd_to_cny"])
        if type(policy["enabled"]) is not bool:
            raise ValueError("policy enabled")
        for key, row in policy["providers"].items():
            expected = DEFAULT_POLICY["providers"][key]
            if type(row.get("enabled")) is not bool or any(row.get(k) != expected.get(k) for k in ("mode", "period", "unit")):
                raise ValueError("quota contract")
            if "limit" in expected:
                limit = _amount(row["limit"], zero=True)
                if row["mode"] != "money" and limit > _amount(expected["limit"]):
                    raise ValueError("free quota cannot exceed published allowance")
            if "external_used" in row:
                _amount(row["external_used"], zero=True)
                if not isinstance(row.get("external_period"), str):
                    raise ValueError("external usage period required")
            if "per_second_limit" in expected and (type(row.get("per_second_limit")) is not int or
                    not 1 <= row["per_second_limit"] <= expected["per_second_limit"]):
                raise ValueError("rate quota")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, InvalidOperation):
        raise PaidAdmissionError("paid_policy_invalid") from None
    return policy


def estimate_request(provider, payload, bound, policy, *, endpoint="", authenticated=True):
    """估价不冒充账单；未知服务仅记录，绝不占用其他出口额度。"""
    model = str(payload.get("model") or "")
    spec = policy["providers"].get(provider, {"enabled": True, "mode": "record"})
    units, price, basis = Decimal(0), Decimal(0), "unknown_price_record_only"
    region = "singapore" if "dashscope-intl." in (urlsplit(endpoint).hostname or "") else "beijing"
    if provider == "bailian":
        max_tokens = int(payload.get("max_tokens") or payload.get("parameters", {}).get("max_tokens") or 4096)
        if max_tokens <= 0:
            raise PaidAdmissionError("paid_invalid_output_bound")
        rates = _RATES[region]
        price = max(Decimal("0.01"), (Decimal(bound) * rates[0] + Decimal(max_tokens) * rates[2]) / 1000000)
        media = any(k in json.dumps(payload) for k in ('"image"', '"image_url"', '"video"', '"video_url"', '"audio"', '"audio_url"'))
        if media:
            price = max(price, Decimal("0.50"))
        basis = "regional_token_estimate_2026-10-28" if model == "qwen3.8-omni-flash" else "unknown_model_estimate"
        if media:
            basis += "_media_reservation"
        units = price
    elif provider == "siliconflow_ocr":
        basis = "published_free_model_2026-10-28"
    elif provider == "tikhub":
        price, units, basis = Decimal("0.01") * _amount(policy["usd_to_cny"]), Decimal(1), "endpoint_price_range_estimate"
    elif provider == "tavily":
        depth = payload.get("search_depth", "basic")
        if depth not in {"basic", "advanced", "fast", "ultra-fast"}:
            raise PaidAdmissionError("paid_unpriced_search_variant")
        units, basis = Decimal(2 if depth == "advanced" else 1), "published_search_credits"
    elif provider == "brave":
        units, basis = Decimal("0.005"), "published_search_usd"
    elif provider == "exa":
        if payload.get("type", "auto") != "auto" or payload.get("contents", {}).get("summary"):
            raise PaidAdmissionError("paid_unpriced_search_variant")
        units = Decimal("0.007") + max(0, int(payload.get("numResults", 10)) - 10) * Decimal("0.001")
        basis = "published_auto_search_usd"
    elif provider == "anysearch":
        if (urlsplit(endpoint).hostname or "").lower() == "api.anysearch.com":
            units, basis = Decimal(1), "published_standard_free_calls"
        else:
            spec = {"enabled": spec["enabled"], "mode": "record"}
    elif provider == "openalex":
        units, basis = Decimal("0.001" if payload.get("search") else "0.0001"), "published_search_usd"
        spec = {**spec, "limit": str(min(_amount(spec["limit"], zero=True), Decimal("1" if authenticated else "0.10")))}
    return {"price_cny": price, "units": units, "group": f"{provider}|{model}",
            "price_basis": basis, "region": region, "quota": spec}


def default_usage_path():
    explicit = os.environ.get("KR_PAID_USAGE_PATH") or os.environ.get("KR_XHS_TIKHUB_USAGE_PATH")
    if explicit:
        return Path(explicit)
    legacy = runtime_state_dir() / "xhs_tikhub_usage.json"
    current = runtime_state_dir() / "paid_usage.json"
    if legacy.exists() and current.exists():
        raise ValueError("paid_ledger_identity_conflict")
    return legacy if legacy.exists() else current


def read_usage(path=None):
    target = Path(path or default_usage_path())
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("ledger")
        for day in data.values():
            if not isinstance(day, dict) or not isinstance(day.get("reservations", {}), dict):
                raise ValueError("ledger day")
            for kind in ("search", "detail"):
                if int(day.get(kind, 0)) < 0:
                    raise ValueError("negative calls")
            for row in day.get("reservations", {}).values():
                if not isinstance(row, dict) or row.get("currency", "USD") not in {"USD", "CNY"}:
                    raise ValueError("ledger row")
                for key in ("accounted_cny", "reserved_amount", "reserved_usd", "estimate_cny", "quota_units", "reserved_epoch"):
                    if row.get(key) is not None:
                        _amount(row[key], zero=True)
        return data
    except (OSError, ValueError, TypeError, InvalidOperation) as exc:
        raise ValueError("paid_usage_corrupt") from exc


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix("." + uuid.uuid4().hex + ".pending")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@guarded_transaction
@contextmanager
def usage_lock(path):
    target = Path(str(path) + ".lock")
    target.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, target.open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def accounted_cny(row, policy):
    if row.get("accounted_cny") is not None:
        return _amount(row["accounted_cny"], zero=True)
    value = row.get("reserved_amount", row.get("reserved_usd", 0))
    return _amount(value or 0, zero=True) * (_amount(policy["usd_to_cny"]) if row.get("currency", "USD") == "USD" else 1)


def daily_accounted_cny(day, policy, provider=None):
    return sum((accounted_cny(row, policy) for row in day.get("reservations", {}).values()
                if provider is None or row.get("provider", "tikhub") == provider), Decimal(0))


def _period(spec, day=None):
    current = datetime.now(timezone.utc) if spec.get("period") in {"utc_day", "month"} else datetime.now()
    date = day or current.strftime("%Y-%m-%d")
    return date[:7] if spec.get("period") == "month" else date


def _used(data, provider, spec, period, policy):
    total = Decimal(0)
    for date, day in data.items():
        for row in day.get("reservations", {}).values():
            if row.get("provider", "tikhub") != provider:
                continue
            row_period = row.get("quota_period") or (date[:7] if spec.get("period") == "month" else date)
            if row_period != period:
                continue
            if spec["mode"] == "money":
                total += accounted_cny(row, policy)
            elif "quota_units" in row:
                total += _amount(row["quota_units"], zero=True)
            else:
                # 旧账仍计入；旧搜索未记credits时按当前已知较高消耗保守迁移。
                total += {"tikhub": Decimal(1), "tavily": Decimal(2), "brave": Decimal(".005"),
                          "exa": Decimal(".017"), "anysearch": Decimal(1), "openalex": Decimal(".001")}.get(provider, Decimal(0))
        if provider == "tikhub" and date == period:
            rows = day.get("reservations", {}).values()
            represented = sum(row.get("provider", "tikhub") == "tikhub" and row.get("kind") in {"search", "detail"} for row in rows)
            total += max(0, sum(int(day.get(k, 0)) for k in ("search", "detail")) - represented)
    if spec.get("external_period") == period:
        total += _amount(spec.get("external_used", 0), zero=True)
    return total


def _import_search_history(data, target, policy):
    """只吸收旧Tavily账本的已知余额，不继续运行旧33次/日计数器。"""
    source = target.parent / "search_quota_state.json"
    if not source.exists():
        return False
    try:
        raw = source.read_bytes()
        legacy = json.loads(raw.decode("utf-8-sig"))["providers"].get("tavily", {})
        date = str(legacy.get("date", ""))
        spec = policy["providers"]["tavily"]
        period = _period(spec)
        if date[:7] != period:
            return False
        identity = "legacy-search-quota-" + hashlib.sha256(raw).hexdigest()
        if any(identity in day.get("reservations", {}) for day in data.values()):
            return False
        observed = _amount(legacy.get("used_today", 0), zero=True) * 2
        if legacy.get("remote_remaining") is not None:
            observed = max(observed, _amount(spec["limit"], zero=True) - _amount(legacy["remote_remaining"], zero=True))
        adjustment = max(Decimal(0), observed - _used(data, "tavily", spec, period, policy))
        data.setdefault(date, {}).setdefault("reservations", {})[identity] = {
            "provider": "tavily", "kind": "legacy_quota_balance", "quota_mode": "free_quota",
            "quota_period": period, "quota_unit": "credits", "quota_units": str(adjustment),
            "accounted_cny": "0", "outcome_status": "historical_estimate", "send_count": 0,
            "price_basis": "legacy_success_count_upper_credits_or_remote_balance",
        }
        return True
    except (OSError, ValueError, TypeError, KeyError, AttributeError, InvalidOperation):
        raise ValueError("legacy_search_quota_invalid") from None


def provider_quota_summary(provider, *, path=None):
    policy = load_policy()
    spec = policy["providers"].get(provider)
    if spec is None or "limit" not in spec:
        return {"provider": provider, "status": "record_only", "unit": None}
    target = Path(path or default_usage_path())
    with usage_lock(target):
        data = read_usage(target)
        if provider == "tavily":
            _import_search_history(data, target, policy)
        period = _period(spec)
        used = _used(data, provider, spec, period, policy)
    remaining = max(Decimal(0), _amount(spec["limit"], zero=True) - used)
    return {"provider": provider, "status": "disabled" if not policy["enabled"] or not spec["enabled"] else "available" if remaining else "quota_exhausted",
            "period": period, "period_kind": spec["period"], "unit": spec["unit"], "limit": spec["limit"],
            "used": str(used), "remaining": str(remaining), "account_balance_verified": False}


def claim_send(operation, *, reservation_id, task_id, ceiling, currency="USD", provider="tikhub",
               price_version="knowledgeradar-paid-policy/v3", path=None, day=None, policy=None, estimate=None):
    try:
        target = Path(path or default_usage_path())
        price = _amount(ceiling, zero=True)
        policy = policy or load_policy()
        estimate = estimate or estimate_request(provider, {}, 1, policy)
        spec = estimate.get("quota") or policy["providers"].get(provider, {"mode": "record", "enabled": True})
        if not policy["enabled"] or spec.get("enabled") is not True or spec["mode"] == "disabled":
            return {"send": False, "reason_code": "PROVIDER_DISABLED"}
        if not task_id or not reservation_id or not price_version or currency not in {"USD", "CNY"}:
            raise ValueError("invalid request identity")
        with usage_lock(target):
            data = read_usage(target)
            if provider == "tavily" and _import_search_history(data, target, policy):
                _write(target, data)
            for previous in data.values():
                row = previous.get("reservations", {}).get(reservation_id)
                if row:
                    return {"send": False, "reason_code": "ALREADY_SENT_OR_RESERVED", "cached_result": row.get("result"), "state": row.get("outcome_status", "unknown")}
            date = day or datetime.now().strftime("%Y-%m-%d")
            period = _period(spec, day)
            candidate = price * (_amount(policy["usd_to_cny"]) if currency == "USD" else 1)
            units = candidate if spec["mode"] == "money" else _amount(estimate.get("units", 0), zero=True)
            sent_at = datetime.now().timestamp()
            if spec.get("per_second_limit") and sum(
                    row.get("provider") == provider and float(row.get("reserved_epoch", 0)) > sent_at - 1
                    for previous in data.values() for row in previous.get("reservations", {}).values()
            ) >= spec["per_second_limit"]:
                return {"send": False, "reason_code": "RATE_LIMIT"}
            if "limit" in spec and _used(data, provider, spec, period, policy) + units > _amount(spec["limit"], zero=True):
                return {"send": False, "reason_code": "MONEY_LIMIT" if spec["mode"] == "money" else "CALL_LIMIT" if spec["mode"] == "calls" else "FREE_QUOTA_LIMIT"}
            today = data.setdefault(date, {})
            reservations = today.setdefault("reservations", {})
            from runtime.application import current_request_id
            reservations[reservation_id] = {
                "kind": operation, "provider": provider, "task_id": task_id, "currency": currency,
                "origin_request_id": current_request_id(),
                "reserved_amount": str(price), "reserved_usd": str(price) if currency == "USD" else None,
                "accounted_cny": str(candidate), "estimate_cny": str(candidate),
                "cost_status": "free" if spec["mode"] == "remote_free" else "unknown_price" if spec["mode"] == "record" else "estimated",
                "cost_group": estimate.get("group", provider), "price_basis": estimate.get("price_basis", "unknown_price_record_only"),
                "region": estimate.get("region", "beijing"), "price_version": price_version,
                "quota_mode": spec["mode"], "quota_period": period, "quota_units": str(units), "quota_unit": spec.get("unit"),
                "reserved_at": datetime.now().isoformat(), "reserved_epoch": sent_at, "outcome_status": "sent", "send_count": 1,
            }
            if provider == "tikhub":
                today[operation] = int(today.get(operation) or 0) + 1
            _write(target, data)
            return {"send": True, "reservation_id": reservation_id, "reserved_usd": str(price) if currency == "USD" else None,
                    "estimated_cny": str(candidate), "price_basis": reservations[reservation_id]["price_basis"]}
    except (OSError, ValueError, InvalidOperation, TypeError, KeyError):
        return {"send": False, "reason_code": "BUDGET_OR_LEDGER_INVALID"}


def settle_send(reservation_id, result, *, path=None):
    from generic_web.url_policy import public_evidence_payload
    target = Path(path or default_usage_path())
    with usage_lock(target):
        data = read_usage(target)
        for day in data.values():
            row = day.get("reservations", {}).get(reservation_id)
            if row:
                row.update(outcome_status="response_received" if result.get("transport_only") else "ok" if result.get("ok") else "unknown_or_failed",
                           result=public_evidence_payload(result))
                _write(target, data)
                return


def settle_usage(reservation_id, *, provider, model, usage, path=None):
    """返回token核算的估价，不是服务商账单；缺用量保留发送前预留。"""
    if provider != "bailian" or model != "qwen3.8-omni-flash" or not isinstance(usage, dict) or usage.get("usage_reported") is False:
        return
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion = usage.get("completion_tokens", usage.get("output_tokens"))
    if prompt is None or completion is None:
        return
    try:
        prompt, completion = _amount(prompt, zero=True), _amount(completion, zero=True)
        details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
        cached = _amount(usage.get("cached_tokens", details.get("cached_tokens", 0)), zero=True)
        if cached > prompt:
            return
        target = Path(path or default_usage_path())
        with usage_lock(target):
            data = read_usage(target)
            for day in data.values():
                row = day.get("reservations", {}).get(reservation_id)
                if row and row.get("provider") == provider:
                    rates = _RATES[row.get("region", "beijing")]
                    cost = ((prompt - cached) * rates[0] + cached * rates[1] + completion * rates[2]) / 1000000
                    row.update(accounted_cny=str(cost), quota_units=str(cost), cost_status="usage_priced_estimate",
                               usage={"input_tokens": int(prompt), "cached_tokens": int(cached), "output_tokens": int(completion)})
                    _write(target, data)
                    return
    except (InvalidOperation, TypeError, ValueError, KeyError):
        return


@contextmanager
def paid_send(provider, payload, *, bound, task_id="", endpoint="", authenticated=True):
    policy = load_policy()
    if not 0 < int(bound) <= 32 * 1024 * 1024:
        raise PaidAdmissionError("paid_request_too_large")
    estimate = estimate_request(provider, payload, bound, policy, endpoint=endpoint, authenticated=authenticated)
    from runtime.tool_trace import current_tool_trace
    trace = current_tool_trace()
    task = task_id or trace.get("research_task_id") or execution_value("KR_PAID_TASK_SCOPE") or execution_value("KR_DURABLE_TASK_ID") or trace.get("trace_id") or _LOCAL_SCOPE
    # 保留既有任务/载荷身份，升级策略不能让旧未知请求重新发出。
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    reservation = hashlib.sha256(f"{provider}|{task}|{digest}".encode("utf-8")).hexdigest()
    result = claim_send(provider, reservation_id=reservation, task_id=task, ceiling=estimate["price_cny"],
                        currency="CNY", provider=provider, price_version=policy["schema"], policy=policy, estimate=estimate)
    if not result["send"]:
        raise PaidAdmissionError("paid_" + result["reason_code"].lower())
    yield result
    settle_send(reservation, {"ok": True, "transport_only": True})


def policy_summary():
    try:
        policy = load_policy()
        return {"schema": policy["schema"], "status": "independent_provider_quotas",
                "automatic_paid_send": policy["enabled"], "providers": policy["providers"],
                "global_budget": False, "unknown_outcome": "reserved_no_resend", "unknown_price": "record_only",
                "serpapi": "excluded_from_cost_management", "real_billing_hard_cap": False}
    except PaidAdmissionError:
        return {"status": "policy_invalid", "automatic_paid_send": False}
