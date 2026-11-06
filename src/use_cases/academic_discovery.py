"""慢学术发现使用现有任务账本；不阻塞快来源，不顺带获取正文。"""
from dataclasses import asdict


def submit_discovery(request, provider):
    from runtime.task_service import TaskService
    from runtime.tool_trace import current_tool_trace
    row = asdict(request)
    row['provider'] = provider
    # 显式全文快捷方式在书目返回后只接单，不在发现worker里下载或OCR。
    # 保留用户的top-k/独立deadline；默认无该选项时仍是纯发现。
    row['options']['_background_discovery'] = True
    timeout = float(row['options'].get('deadline_s', 180))
    return TaskService().submit('academic_discovery', row, timeout_s=timeout,
        scope={'research_task_id': current_tool_trace().get('research_task_id',''),
               'source_ecology': 'academic_literature_ecology'})


def execute(payload):
    from academic_providers.models import AcademicSearchRequest
    from academic_providers.service import search_academic_metadata
    from kr_core.evidence_store import EvidenceStore
    result = search_academic_metadata(AcademicSearchRequest(**payload)).to_mcp_dict()
    EvidenceStore().append_academic_search(query=payload['query'], response=result)
    return result
