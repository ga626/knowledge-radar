"""兼容Python入口只提交同一详情任务，不自建后台闭包/状态。"""


def submit_video_detail(platform, url, *, language=None, research_session_id='', scope=None, include_transcript=False, deep=False):
    from kr_core import DetailRequest
    from use_cases.content_detail import submit_detail_task
    request = DetailRequest(platform=platform, url=url, enable_deep_analysis=deep,
        research_session_id=research_session_id, options={'include_transcript': include_transcript,
        'language': language, 'task_scope': scope or {}})
    task, response = submit_detail_task(request, scope=scope)
    return {**response.get('result', {}), 'status': response['status'], 'task_id': task['task_id']}
