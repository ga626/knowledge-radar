"""详情业务用例：短调用与持久进程共享同一执行链。"""
from typing import Dict
from kr_core import DetailRequest

def execute_detail_request(request: DetailRequest, *, failure_cache, handle, bind_tasks, record_evidence, chrome_operation) -> Dict:
    """单一详情执行用例；同步短路径和隔离任务共享此实现。"""
    from generic_web.url_policy import validate_detail_target, URLPolicyError
    try:
        validate_detail_target(request.url, request.platform)
    except URLPolicyError:
        return {"status": "failed", "failure_type": "restricted_url", "error": {"type": "restricted_url", "message": "详情目标不符合公开URL或平台主机约束。"}}
    session_id = request.research_session_id
    scope_data = request.options.get("task_scope") or {}
    failure_cache_key = failure_cache.key(platform=request.platform, url=request.url)
    cached_failure = failure_cache.get(failure_cache_key)
    if cached_failure is not None:
        cached_failure = bind_tasks(cached_failure, session_id, scope_data)
        record_evidence(str(cached_failure.get("platform") or request.platform), request.url, cached_failure)
        return cached_failure
    if request.platform == "小红书":
        with chrome_operation("xhs"):
            result = handle(request).to_legacy_dict()
    else:
        result = handle(request).to_legacy_dict()
    platform = {"知乎": "zhihu", "BOSS直聘": "boss", "猎聘": "liepin", "智联招聘": "zhilian"}.get(request.platform)
    if platform:
        from runtime.account_business import observe_business
        from runtime.chrome_manager import _managed_chrome_profile_dir
        result = observe_business(platform, result, profile_dir=_managed_chrome_profile_dir(platform), source=platform + "_detail_business")
    result = bind_tasks(result, session_id, scope_data)
    if isinstance(result, dict) and result.get("error"):
        failure_type = str(result.get("failure_type") or result.get("error_type") or "")
        cacheable_failures = {"empty_detail", "dead_link", "unsupported_url", "anti_bot_verification", "login_required"}
        if failure_type in cacheable_failures or bool(result.get("manual_action_required")):
            typed_key = failure_cache.key(platform=request.platform, url=request.url, failure_type=failure_type)
            failure_cache.set(failure_cache_key, result)
            if typed_key != failure_cache_key:
                failure_cache.set(typed_key, result)
    record_evidence(str(result.get("platform") or request.platform), request.url, result)
    return result
