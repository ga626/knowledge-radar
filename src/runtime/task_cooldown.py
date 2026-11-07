"""只读平台任务的持久等待计划；登录和未知执行结果不自动重放。"""
import math
import time


def cooldown_plan(result, *, operation, attempts=0, now=None):
    if not isinstance(result,dict) or operation not in {'content_detail','academic_fulltext','academic_discovery','research_query','platform_discovery'}:
        return None
    def unsafe(value):
        if isinstance(value,dict):
            if value.get('request_executed')=='unknown' or value.get('outcome_unknown') is True:return True
            if any('outcome_unknown' in str(value.get(key,'')).lower() for key in ('reason','reason_code','failure_type','type','code')):return True
            return any(unsafe(item) for item in value.values())
        if isinstance(value,list):return any(unsafe(item) for item in value)
        return False
    if unsafe(result):return None
    error=result.get('error') if isinstance(result.get('error'),dict) else {}
    risk=result.get('risk_event') if isinstance(result.get('risk_event'),dict) else {}
    reason=str(result.get('failure_type') or error.get('failure_type') or error.get('type') or result.get('reason_code') or result.get('reason') or risk.get('reason_code') or '')
    text=reason.lower()
    # 缺少认证不能靠等待变成已授权；未知/已计费请求不能被重复发送。
    if any(word in text for word in ('login','auth','qr','cookie','password','outcome_unknown','登录','扫码')):
        return None
    if result.get('resume_policy')=='stop_no_auto_resume' and not any(word in text for word in ('cooldown','rate_limit','429')):
        return None
    if not any(word in text for word in ('cooldown','rate_limit','429','too_many','frequent','captcha','verification','security_block','risk','频繁','风控','验证码')):
        return None
    raw=result.get('retry_after_s') or error.get('retry_after_s') or risk.get('retry_after_s') or result.get('cooldown_remaining_s') or 0
    try:seconds=float(raw)
    except (TypeError,ValueError):return None
    if not math.isfinite(seconds) or seconds<0:return None
    # 保留服务端Retry-After，不将其裁成客户端上限；本地退避仅60至300秒。
    seconds=math.ceil(seconds) if seconds else min(300,60*2**min(attempts,3))
    return {'not_before':float(time.time() if now is None else now)+seconds,'cooldown_reason':reason,
        'cooldown_attempts':attempts,'cooldown_limit':3,'resume_policy':'bounded_read_only_probe'}
