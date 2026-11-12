"""公共错误、证据视图及类型合同；不依赖MCP或控制台。"""
from __future__ import annotations
import json
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from runtime.application import TOOL_NAMES, catalog, groups
class FieldIssue(BaseModel):
    field: str
    constraint: str
    message: str


class AgentError(BaseModel):
    code: str
    fields: list[FieldIssue]=Field(default_factory=list)
    retryable: bool=False
    execution_state: str="unknown"
    next_action: str="Inspect the task/result before repeating a command."
    contract_tool: str="get_capabilities"
    layer: str="application"
    request_id: str | None=None
    automatic_investigation: bool | None=None


class ErrorResponse(BaseModel):
    model_config=ConfigDict(extra='forbid')
    status: str="error"
    error: AgentError
    runtime: dict | None=None
    execution_owner: dict | None=None


def error_result(exc, name, *, not_started=False, layer="application"):
    from pydantic import ValidationError
    current=exc
    while current.__cause__ is not None and not isinstance(current,ValidationError):current=current.__cause__
    fields=[]
    if isinstance(current,ValidationError):
        fields=[{"field":'.'.join(map(str,x['loc'])),"constraint":x['type'],"message":x['msg']} for x in current.errors(include_input=False,include_url=False)]
        code='invalid_arguments' if not_started else 'invalid_tool_result'
    else:
        import re
        text=str(current)
        code=text if re.fullmatch(r'[a-z][a-z0-9_]{1,100}',text) else type(current).__name__
    if code=='runtime_busy':not_started=True
    return ErrorResponse(error=AgentError(code=code,fields=fields,retryable=code=='runtime_busy',execution_state='not_started' if not_started else 'unknown',layer=layer,next_action=f"Read get_capabilities(tool_name='{name}') for legal fields; correct invalid inputs. For an uncertain execution, inspect its task/receipt before retry.")).model_dump(exclude_none=True)


def compact_evidence(value):
    """只裁剪已持久化原文；没有固定回读依据的内容绝不静默丢弃。"""
    if not isinstance(value,dict):return value
    refs=value.get('evidence_assets') or []
    by_kind={x.get('kind'):x for x in refs if isinstance(x,dict)}
    result=dict(value)
    acquired = result.get('acquired_items')
    association = (result.get('research_receipt') or {}).get('candidate_association') or {}
    if isinstance(acquired, list) and association.get('association') == 'discovery_candidates':
        result.pop('acquired_items', None)
        result['candidate_catalog'] = {'task_id': result['research_receipt']['research_task_id'],
            'acquired_count': association.get('count', len(acquired)), 'displayed_count': len(result.get('items', [])),
            'read_tool': 'read_knowledge', 'request': {'action': 'research', 'task_id': result['research_receipt']['research_task_id'], 'view': 'candidates'},
            'not_displayed_is_not_discarded': True}
    # Task envelopes carry the same immutable result under a named nested field.
    for nested in ('result','partial_result'):
        if isinstance(result.get(nested),dict):result[nested]=compact_evidence(result[nested])
    disclosed=[]
    if 'comments' in by_kind and isinstance(result.get('comments_view'),dict):
        view=result['comments_view']
        result['comments_view']={k:v for k,v in view.items() if k!='items'}
        if 'items' in view:
            result['comments']=view['items']
            ref=by_kind['comments']
            disclosed.append({'part':'comments','version_id':ref['version_id'],'read_tool':'read_knowledge',
                'request':{'action':'fetch','version_id':ref['version_id']},'original_retained':True})
    for key in ('content','transcript','ocr_text','comments','images','tables','media_manifest','media_ledger'):
        if key=='comments' and any(part['part']=='comments' for part in disclosed):continue
        raw=result.get(key)
        if key not in by_kind or not isinstance(raw,(str,list)):continue
        if len(json.dumps(raw,ensure_ascii=False))<=1800:continue
        ref=by_kind[key]
        result[key]=raw[:800] if isinstance(raw,str) else raw[:2]
        part = {'part':key,'version_id':ref['version_id'],'read_tool':'read_knowledge',
                'request':{'action':'fetch','version_id':ref['version_id']},'original_retained':True}
        if isinstance(raw,str):
            part.update(total_chars=len(raw), displayed_chars=len(result[key]), next_start=len(result[key]),
                        locator_unit='unicode_code_point')
            part['request']['start'] = len(result[key])
        else:
            part.update(total_items=len(raw), displayed_items=len(result[key]))
        disclosed.append(part)
    if disclosed:result['disclosure']={'partial_view':True,'parts':disclosed,'meaning':'View is shortened; source coverage is unchanged.'}
    if 'media_ledger' in by_kind and isinstance(result.get('downloaded_media'),list):
        rows=result.pop('downloaded_media')
        result['downloaded_media_summary']={'items':len(rows),
            'acquired':sum(row.get('status')=='acquired' for row in rows),
            'fixed_ledger_version':by_kind['media_ledger']['version_id'],
            'read_tool':'read_knowledge','request':{'action':'fetch','version_id':by_kind['media_ledger']['version_id']}}
    return result


class ResearchReadPayload(BaseModel):
    model_config=ConfigDict(extra='forbid')
    query: str=Field(default='',max_length=500)
    limit: int=Field(default=20,ge=1,le=100)
    version_id: str=''
    start: int=Field(default=0,ge=0)
    max_chars: int=Field(default=8000,ge=2000,le=24000)
    project_id: str=''
    task_id: str=''
    cursor: str=''
    history: bool=False


class EcologyOutcome(BaseModel):
    model_config=ConfigDict(extra='forbid')
    source_ecology: str
    outcome: Literal['used','strategic_skip','blocked','not_relevant','not_reached']
    reason: str=''
    receipt_ids: list[str]=Field(default_factory=list)
    claim_gap_ids: list[str]=Field(default_factory=list)
    reopen_condition: str=''


class ResearchClaim(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id: str
    claim: str
    importance: Literal['critical','high','medium','low']='medium'
    supporting_evidence_ids: list[str]=Field(default_factory=list)
    supporting_receipt_ids: list[str]=Field(default_factory=list)


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra='allow', strict=True)
    version_id: str = Field(min_length=1)
    source_id: str | None = None
    kind: str | None = None
    captured_at: float | None = None


class BusinessResult(BaseModel):
    """只约束公共字段，保留平台扩展；可选字段不会被自动补写。"""
    model_config = ConfigDict(extra='allow', strict=True)
    status: str | None = None
    error: dict | str | None = None
    task_id: str | None = None
    accepted: bool | None = None
    evidence_assets: list[EvidenceReference] | None = None
    content_coverage: dict | None = None
    result: dict | None = None


class DetailResult(BusinessResult):
    content: str | None = None
    comment_gaps: list[dict] | None = None
    comments: list[dict] | None = None


class TaskStatusResult(BusinessResult):
    task: dict | None = None
    tasks: list[dict] | None = None
    pending: list[dict] | None = None
    terminal: list[dict] | None = None


class ResearchWorkflowResult(BusinessResult):
    research_task_id: str | None = None
    selected_source_ecologies: list[str] | None = None
