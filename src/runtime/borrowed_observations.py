"""原生借用会话的业务观测，不发登录探针，不依赖其他浏览器驱动。"""
import hashlib


def observe_borrowed_result(platform, result):
    """账号事实随当前业务结果更新；不用旧Profile或Cookie存在性判登录。"""
    transport = result.get('metadata', {}).get('transport')
    if transport != 'content_script_native_messaging':
        return result
    from runtime.browser_link import _read_state
    native = _read_state()
    profile = str(native.get('profile_id') or '')
    if not profile or result.get('request_executed') is False:
        return result
    error = result.get('error') if isinstance(result.get('error'),dict) else {}
    kind = result.get('failure_type') or error.get('failure_type') or ''
    state = {'login_required':'needs_login','anti_bot_verification':'needs_verification','rate_limited':'cooldown'}.get(kind)
    if not state and not result.get('error') and (result.get('items') or len(str(result.get('content') or ''))>=80):
        state = 'business_verified'
    if state:
        from runtime.account_observations import observe_account
        identity='browser-link-'+platform+'-'+hashlib.sha256(profile.encode()).hexdigest()[:12]
        result['account_observation']=observe_account(identity,platform,state,source='borrowed_business',reason=kind)
    return result
