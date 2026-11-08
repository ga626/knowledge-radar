"""详情业务用例：短调用与持久进程共享同一执行链。"""
from typing import Dict
from kr_core import DetailRequest, DetailResponse


def resolve_detail_response(request: DetailRequest, *, strategy, collected=None, unsupported=None, decorate=None):
    """把平台策略结果装配成统一响应；诊断装饰由调用面注入，避免门面反向成为worker依赖。"""
    from dataclasses import replace

    if strategy is None:
        data = unsupported(request) if unsupported else {
            "error": f"不支持的 URL: {request.url}",
            "failure_type": "unsupported_url",
            "platform": request.platform or "unknown",
            "url": request.url,
        }
        response = DetailResponse.from_legacy(
            request.platform or "unknown", request.url, data,
            metadata={"strategy": "unsupported"},
        )
    else:
        response = extract_detail_response(request, strategy=strategy, collected=collected)
    if decorate:
        response = replace(response, metadata=decorate(response))
    return response


def submit_detail_task(request: DetailRequest, *, scope=None, timeout_s=600):
    """委托既有任务服务准备、冻结及提交；这里不重复准备请求。"""
    from dataclasses import asdict
    from runtime.task_service import TaskService

    service = TaskService()
    task = service.submit("content_detail", asdict(request), timeout_s=timeout_s, scope=scope)
    return task, service.result(task["task_id"])


def request_content_detail(
    url, *, infer_platform, execute_sync, enable_deep_analysis=False,
    enable_comment_filtering=False, auto_multimodal=False, research_session_id="",
    work_scope_id="", task_scope_id="", research_task_id="", include_transcript=None,
    include_comments=None, include_ocr=None, language=None, deadline_s=600, github=None, xhs=None, wechat=None, document=None, comments=None,
):
    """接收详情请求并选择短调用或持久任务；平台采集和任务调度各守原边界。"""
    from runtime.redaction import redact_url
    from runtime.asr_policy import AsrPolicy
    from runtime.task_scope import make_task_scope
    from runtime.tasks import compact_task_ref

    from runtime.read_options import DocumentReadOptions, CommentReadOptions, normalized_options
    document = normalized_options(DocumentReadOptions, document)
    comments_explicit=comments is not None
    comments = normalized_options(CommentReadOptions, comments)
    if document.get('include_ocr') is None and include_ocr is not None:
        document['include_ocr']=include_ocr
    platform = infer_platform(url)
    if github is not None and platform != 'GitHub':
        raise ValueError('github_options_require_github_url')
    if xhs is not None:
        if platform != '小红书':
            raise ValueError('xhs_options_require_xiaohongshu_url')
        from runtime.xhs_opencli import XhsReadOptions
        xhs = XhsReadOptions.model_validate(xhs.model_dump() if hasattr(xhs, 'model_dump') else xhs).model_dump()
        if xhs['image_indexes'] is not None and (any(i < 0 for i in xhs['image_indexes']) or len(set(xhs['image_indexes'])) != len(xhs['image_indexes'])):
            raise ValueError('image_indexes_must_be_unique_nonnegative_integers')
    if wechat is not None:
        if platform != '微信':
            raise ValueError('wechat_options_require_wechat_url')
        from collectors.platform.wechat import WechatReadOptions
        wechat = WechatReadOptions.model_validate(wechat.model_dump() if hasattr(wechat, 'model_dump') else wechat).model_dump()
        indexes = wechat.get('image_indexes')
        if indexes is not None and (any(i < 0 for i in indexes) or len(set(indexes)) != len(indexes)):
            raise ValueError('image_indexes_must_be_unique_nonnegative_integers')
    if platform=='GitHub' and comments_explicit:
        github=github.model_dump() if hasattr(github,'model_dump') else dict(github or {})
        github.setdefault('max_pages',min(10,comments['max_pages']))
        github.setdefault('per_page',min(100,comments['total_limit']))
    effective_language = AsrPolicy.for_request(language).language
    scope = make_task_scope(
        source_url=redact_url(url), platform=platform, work_scope_id=work_scope_id,
        task_scope_id=task_scope_id, research_session_id=research_session_id,
    )
    request = DetailRequest(
        url=url, enable_deep_analysis=enable_deep_analysis,
        enable_comment_filtering=enable_comment_filtering, auto_multimodal=auto_multimodal,
        platform=platform, research_session_id=scope.research_session_id_alias,
        work_scope_id=scope.work_scope_id, task_scope_id=scope.task_scope_id,
        scope_kind=scope.scope_kind,
        options={"document":document,"comments":comments,"timeout_s":deadline_s,"task_scope": scope.compact(), "research_task_id": research_task_id,
                 "language": effective_language,
                 "include_transcript": include_transcript if include_transcript is not None else (auto_multimodal or enable_deep_analysis),
                 "include_comments": include_comments if include_comments is not None else enable_comment_filtering,
                 "include_ocr": include_ocr if include_ocr is not None else (auto_multimodal or enable_deep_analysis),
                 **({'github': github.model_dump() if hasattr(github, 'model_dump') else github} if github is not None else {}),
                 **({'xhs': xhs} if xhs is not None else {}),
                 **({'wechat': wechat} if wechat is not None else {}),
                 **({'resume_checkpoint': True} if (wechat or {}).get('refresh') else {})},
    )
    if document.get('page_numbers') or document.get('include_ocr') or platform == 'GitHub' or (wechat or {}).get('include_attachments') or enable_deep_analysis or auto_multimodal or enable_comment_filtering or any(
        request.options.get(k) for k in ("include_transcript", "include_comments", "include_ocr")
    ):
        task, response = submit_detail_task(
            request, timeout_s=deadline_s,
            scope={**scope.to_metadata(), "research_task_id": research_task_id},
        )
        if response.get("status") == "completed":
            return {**response["result"], "task_id": task["task_id"],
                    "durable_task": {"status": "completed", "artifact_sha256": (task.get("metadata") or {}).get("artifact_sha256"),
                                     "reused": True}}
        return {"status": response["status"], "accepted": True, "task_id": task["task_id"], "task": compact_task_ref(task),
                **({"result": response["result"], "partial_result": True} if "result" in response else {}),
                "result_tool": "get_task_status", "cancel": {"tool":"control_task","arguments":{"task_id":task["task_id"],"action":"cancel"}}, "retry": {"tool":"control_task","arguments":{"task_id":task["task_id"],"action":"resume"}},
                "source": "durable_process", "scope_binding": scope.compact()}
    return execute_sync(request)


def extract_detail_response(request: DetailRequest, *, strategy, collected=None):
    """采集与增强共用结果整形；不准备请求、不重采已收集的增强输入。"""
    from dataclasses import replace
    from kr_core.registry import registry
    from routing import build_agent_native_fields
    from understanding.evidence import attach_detail_evidence, build_detail_evidence

    response = (registry.enhance_detail(request, collected) if collected is not None
                else strategy.extract(request))
    data = dict(response.data)
    if request.options.get('include_comments', request.enable_comment_filtering):
        from runtime.comments_contract import normalize_comments
        from runtime.comments_view import comment_view
        comment_options=request.options.get('comments') or {}
        normalize_comments(data,platform=response.platform or request.platform,source_url=request.url,options=comment_options)
        has_media=any(row.get('media') for row in data['comments'])
        if has_media and registry.supports_staged_detail(request.platform) and request.options.get('_detail_stage') == 'collection':
            data.update(status='partial',_enhancement_pending=True,enhancement_status='queued')
        elif has_media:
            from runtime.comment_media import process_comment_media
            process_comment_media(data,source_url=request.url,options=comment_options,language=request.options.get('language'))
        data['comments_view']=comment_view(data['comments'],query=comment_options.get('query',''),limit=comment_options.get('view_limit',12))
    if response.error:
        data.update(error=response.error.message, error_detail=response.error.to_mcp_dict())
    data = attach_detail_evidence(request.url, data,
                                  infer_platform=lambda _: response.platform or request.platform)
    data.update(build_agent_native_fields(data, strategy=str((response.metadata or {}).get("strategy") or "")))
    return replace(
        response, url=request.url, data=data,
        evidence=build_detail_evidence(request.url, response.platform, data),
        metadata=dict(response.metadata),
    )


def execute_detail_request(request: DetailRequest, *, failure_cache, handle, bind_tasks, record_evidence) -> Dict:
    """单一详情执行用例；同步短路径和隔离任务共享此实现。"""
    from generic_web.url_policy import validate_detail_target, URLPolicyError
    try:
        validate_detail_target(request.url, request.platform)
    except URLPolicyError:
        return {"status": "failed", "failure_type": "restricted_url", "error": {"type": "restricted_url", "message": "详情目标不符合公开URL或平台主机约束。"}}
    session_id = request.research_session_id
    scope_data = request.options.get("task_scope") or {}
    failure_cache_key = failure_cache.key(platform=request.platform, url=request.url)
    from kr_core.registry import registry
    import json
    identity = json.dumps(registry.runtime_identity(request.platform), sort_keys=True)
    failure_cache_key += "|adapter=" + identity
    cached_failure = failure_cache.get(failure_cache_key)
    if cached_failure is not None and not request.options.get("resume_checkpoint"):
        cached_failure = bind_tasks(cached_failure, session_id, scope_data)
        from kr_core.evidence_coverage import content_coverage
        cached_failure["content_coverage"] = content_coverage(cached_failure, request.options)
        record_evidence(str(cached_failure.get("platform") or request.platform), request.url, cached_failure)
        return cached_failure
    result = registry.after_detail(request.platform, handle(request).to_legacy_dict())
    result = bind_tasks(result, session_id, scope_data)
    from kr_core.evidence_coverage import content_coverage
    result["content_coverage"] = content_coverage(result, request.options)
    from routing import build_agent_native_fields
    result.update(build_agent_native_fields(result))
    if isinstance(result, dict) and result.get("error"):
        failure_type = str(result.get("failure_type") or result.get("error_type") or "")
        cacheable_failures = {"empty_detail", "dead_link", "unsupported_url", "anti_bot_verification", "login_required"}
        if failure_type in cacheable_failures or bool(result.get("manual_action_required")):
            typed_key = failure_cache.key(platform=request.platform, url=request.url, failure_type=failure_type)
            typed_key += "|adapter=" + identity
            failure_cache.set(failure_cache_key, result)
            if typed_key != failure_cache_key:
                failure_cache.set(typed_key, result)
    record_evidence(str(result.get("platform") or request.platform), request.url, result)
    return result
