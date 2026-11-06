"""TikHub 薄账本入口；唯一准入策略在 paid_execution。"""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from runtime import paid_execution as shared


def default_xhs_tikhub_usage_path():
    return shared.default_usage_path()


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def tikhub_usage_summary(path=None):
    try:
        policy = shared.load_policy()
        data = shared.read_usage(path)
        day = data.get(_today(), {})
        spec = policy["providers"]["tikhub"]
        return {"schema": "knowledgeradar-xhs-tikhub-usage/v3", "status": "estimated",
            "enabled": policy.get("enabled") is True and policy["providers"].get("tikhub", {}).get("enabled", True) is True,
            "date": _today(), "reserved_calls": int(shared._used(data, "tikhub", spec, _today(), policy)),
            "estimated_cny": str(shared.daily_accounted_cny(day, policy, "tikhub")),
            "daily_call_limit": spec["limit"], "real_billing_hard_cap": False}
    except (OSError, ValueError, TypeError, InvalidOperation):
        return {"status": "ledger_or_policy_invalid", "enabled": False}


def claim_paid_send(operation, *, reservation_id, task_id, path=None):
    try:
        policy = shared.load_policy()
        if not policy.get("enabled") or policy["providers"].get("tikhub", {}).get("enabled", True) is not True:
            return {"send": False, "reason_code": "TIKHUB_FALLBACK_DISABLED"}
        estimate = shared.estimate_request("tikhub", {}, 1, policy)
        result = shared.claim_send(operation, reservation_id=reservation_id, task_id=task_id,
            ceiling=Decimal("0.01"), currency="USD", provider="tikhub", price_version=policy["schema"],
            policy=policy, estimate=estimate, path=path, day=_today())
        if result.get("reason_code"):
            result["reason_code"] = "TIKHUB_" + result["reason_code"]
        return result
    except shared.PaidAdmissionError:
        return {"send": False, "reason_code": "TIKHUB_POLICY_INVALID"}


def settle_paid_send(reservation_id, result, *, path=None):
    return shared.settle_send(reservation_id, result, path=path)
