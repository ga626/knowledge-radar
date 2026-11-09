"""研究责任合同：复用ResearchIndex，不调度研究、不判断语义真实性。"""
from __future__ import annotations

from contextlib import contextmanager
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator


def validate_task_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,96}', value):
        raise ValueError('invalid_research_task_id')
    return value


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ResearchContext(Strict):
    action_id: str = Field(default='', max_length=200)
    question_id: str = Field(default='', max_length=128)
    requirement_id: str = Field(default='', max_length=128)
    exploration_id: str = Field(default='', max_length=128)

    @model_validator(mode='after')
    def explicit_intent(self):
        if bool(self.action_id) == bool(self.exploration_id):
            raise ValueError('choose_action_or_exploration')
        if self.action_id and not (self.question_id or self.requirement_id):
            raise ValueError('action_requires_question_or_requirement')
        return self


class ContractWrite(Strict):
    task_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,96}$')
    record_id: str = Field(min_length=1, max_length=128)
    expected_revision: int | None = Field(default=None, ge=0)


class RequirementWrite(ContractWrite):
    action: Literal['requirement']
    question_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=2000)
    required_parts: list[str] = Field(min_length=1, max_length=40)
    required: bool = True
    semantic_condition: str = Field(default='', max_length=2000)
    waived: bool = False
    rationale: str = Field(default='', max_length=2000)


class AttemptWrite(ContractWrite):
    action: Literal['attempt']
    requirement_id: str = Field(min_length=1, max_length=128)
    requirement_revision: int = Field(ge=1)
    receipt_id: str = Field(min_length=1, max_length=128)
    rationale: str = Field(min_length=1, max_length=2000)


class Locator(Strict):
    version_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class CoverageWrite(AttemptWrite):
    action: Literal['coverage']
    evidence_refs: list[Locator] = Field(min_length=1, max_length=40)
    covered_parts: list[str] = Field(min_length=1, max_length=40)
    partial: bool = False


class ExplorationWrite(ContractWrite):
    action: Literal['exploration']
    purpose: str = Field(min_length=1, max_length=2000)


class ExplorationDisposition(ContractWrite):
    action: Literal['exploration_disposition']
    exploration_id: str = Field(min_length=1, max_length=128)
    receipt_ids: list[str] = Field(min_length=1, max_length=100)
    disposition: Literal['linked', 'background', 'discarded', 'blocked']
    question_id: str = Field(default='', max_length=128)
    rationale: str = Field(min_length=1, max_length=2000)


def parse_write(value):
    from runtime.research_index import ResearchRequest
    return TypeAdapter(ResearchRequest).validate_python(value)


def current(conn, task_id, kind, oid):
    row = conn.execute('SELECT value FROM current WHERE task_id=? AND kind=? AND object_id=?',
                       (task_id, kind, oid)).fetchone()
    return json.loads(row[0]) if row else None


def rows_at(conn, task_id, high=None, *, include_seq=False):
    if high is None:
        return [dict(json.loads(r['value']), _kind=r['kind'], **({'_seq':r['seq']} if include_seq else {})) for r in conn.execute(
            'SELECT kind,value,seq FROM current WHERE task_id=?', (task_id,))]
    return [dict(json.loads(r['value']), _kind=r['kind'], **({'_seq':r['seq']} if include_seq else {})) for r in conn.execute('''
        SELECT c.kind,c.value,c.seq FROM changes c WHERE c.task_id=? AND c.seq<=? AND NOT EXISTS(
          SELECT 1 FROM changes n WHERE n.task_id=c.task_id AND n.kind=c.kind
          AND n.object_id=c.object_id AND n.seq>c.seq AND n.seq<=?)''', (task_id, high, high))]


def validate_context(task_id, value):
    from runtime.research_index import ResearchIndex
    ctx = value if isinstance(value, ResearchContext) else ResearchContext.model_validate(value)
    validate_task_id(task_id)
    with ResearchIndex().connection() as conn:
        if conn is None:
            raise ValueError('unknown_research_context')
        if ctx.question_id and not ResearchIndex.question_at(conn, task_id, ctx.question_id):
            raise ValueError('unknown_question')
        if ctx.requirement_id:
            requirement = current(conn, task_id, 'requirement', ctx.requirement_id)
            if not requirement:
                raise ValueError('unknown_requirement')
            if ctx.question_id and requirement['question_id'] != ctx.question_id:
                raise ValueError('requirement_question_mismatch')
        if ctx.exploration_id and not current(conn, task_id, 'exploration', ctx.exploration_id):
            raise ValueError('unknown_exploration')
    return ctx.model_dump(exclude_defaults=True)


@contextmanager
def context_scope(task_id, value):
    from runtime.research_queries import CURRENT_RESEARCH_ACTION
    ctx = validate_context(task_id, value)
    token = CURRENT_RESEARCH_ACTION.set({**ctx, '_task_id':task_id})
    try:
        yield ctx
    finally:
        CURRENT_RESEARCH_ACTION.reset(token)


def record(index, req, assets=None):
    from runtime.research_ledger import read_task
    from runtime.research_index import encoded
    task = read_task(task_id=req.task_id)
    if task.get('status') == 'unknown_task':
        raise ValueError('unknown_research_task')
    value = req.model_dump(exclude={'expected_revision'})
    with index.connection(write=True) as conn:
        prior = current(conn, req.task_id, req.action, req.record_id)
        if prior and all(prior.get(k) == v for k, v in value.items()):
            from runtime.research_reviews import REVIEW_TYPES, validate
            if isinstance(req,REVIEW_TYPES):validate(index,conn,req,dict(value),assets,project_id=str(task.get('project_id') or ''))
            return {'status':'recorded','record_id':req.record_id,'revision':prior['revision'],
                    **({'version_id':prior['version_id']} if 'version_id' in prior else {})}
        revision = int((prior or {}).get('revision', 0))
        if prior and req.action not in {'requirement','claim','task_purpose'}:
            raise ValueError('immutable_record_use_new_id')
        if req.expected_revision is not None and req.expected_revision != revision:
            raise ValueError('revision_conflict')
        if prior and req.expected_revision is None:
            raise ValueError('expected_revision_required')
        from runtime.research_reviews import REVIEW_TYPES, validate
        if isinstance(req,REVIEW_TYPES):validate(index,conn,req,value,assets,project_id=str(task.get('project_id') or ''))
        if isinstance(req, RequirementWrite):
            if not index.question_at(conn, req.task_id, req.question_id):
                raise ValueError('unknown_question')
            if not all(x.strip() for x in req.required_parts) or len(set(req.required_parts)) != len(req.required_parts):
                raise ValueError('invalid_requirement_parts')
            if req.waived and not req.rationale.strip():
                raise ValueError('waiver_reason_required')
        if isinstance(req, AttemptWrite):
            requirement = current(conn, req.task_id, 'requirement', req.requirement_id)
            if not requirement or requirement['revision'] != req.requirement_revision:
                raise ValueError('requirement_revision_conflict')
            receipt = current(conn, req.task_id, 'receipt', req.receipt_id)
            if not receipt:
                raise ValueError('unknown_receipt')
            if isinstance(req, CoverageWrite):
                if receipt.get('status') not in {'ok','completed','partial'}:
                    raise ValueError('failed_receipt_cannot_cover_requirement')
                if not set(req.covered_parts) <= set(requirement['required_parts']):
                    raise ValueError('coverage_outside_requirement')
                for ref in req.evidence_refs:
                    if ref.version_id not in receipt.get('evidence_versions', []):
                        raise ValueError('evidence_not_in_receipt')
                    index.validate_ref(ref.model_dump(), assets)
        if isinstance(req, ExplorationDisposition):
            if not current(conn, req.task_id, 'exploration', req.exploration_id):
                raise ValueError('unknown_exploration')
            for rid in req.receipt_ids:
                receipt = current(conn, req.task_id, 'receipt', rid)
                if not receipt:
                    raise ValueError('unknown_receipt')
                if (receipt.get('research_action') or {}).get('exploration_id') != req.exploration_id:
                    raise ValueError('post_hoc_exploration_forbidden')
            if req.disposition == 'linked' and not index.question_at(conn, req.task_id, req.question_id):
                raise ValueError('linked_exploration_requires_question')
        value.update(revision=revision+1, author='agent', assessment='agent_declared_not_program_verified')
        index.put(conn, req.task_id, req.action, req.record_id, value, encoded(value))
    return {'status':'recorded','record_id':req.record_id,'revision':value['revision'],**({'version_id':value['version_id']} if 'version_id' in value else {})}


def requirement_projection(index, conn, task_id, high=None, assets=None):
    records = rows_at(conn, task_id, high)
    result = []
    for req in [r for r in records if r['_kind'] == 'requirement']:
        covered = set()
        receipts = []
        partial = False
        for c in records:
            if c['_kind'] != 'coverage' or c['requirement_id'] != req['record_id'] or c['requirement_revision'] != req['revision']:
                continue
            try:
                for ref in c['evidence_refs']:
                    index.validate_ref(ref, assets)
            except (ValueError, OSError):
                continue
            covered.update(c['covered_parts'])
            receipts.append(c['receipt_id'])
            partial |= c['partial']
        missing = [p for p in req['required_parts'] if p not in covered]
        state = 'waived' if req['waived'] else 'partial' if covered and (missing or partial) else 'fulfilled' if not missing else 'open'
        result.append({k:v for k,v in req.items() if k != '_kind'} | {
            'acquisition':state,'missing_parts':missing,'covering_receipt_ids':receipts,'semantic_review':'unreviewed'})
    return result


def feedback(index, conn, task_id, high=None, assets=None):
    records = rows_at(conn, task_id, high)
    requirements = requirement_projection(index, conn, task_id, high, assets)
    dispositions = [r for r in records if r['_kind'] == 'exploration_disposition']
    pending = [r['receipt_id'] for r in records if r['_kind'] == 'receipt'
        and (r.get('research_action') or {}).get('exploration_id')
        and not any(r['receipt_id'] in d['receipt_ids'] for d in dispositions)]
    gaps = [r['record_id'] for r in requirements if r['required'] and r['acquisition'] != 'fulfilled']
    original_unassigned = [r['receipt_id'] for r in records if r['_kind']=='receipt' and r.get('tool')!='kr_research'
        and not (r.get('research_action') or {}).get('action_id') and not (r.get('research_action') or {}).get('exploration_id')]
    post_hoc = [r['receipt_id'] for r in records if r['_kind']=='action' and r.get('association_timing')=='post_hoc']
    return {'requirement_gaps':gaps,'requirement_gaps_count':len(gaps),
            'exploration_pending_receipts':pending,'exploration_pending_receipts_count':len(pending),
            'requirements_registered':len(requirements),'semantic_saturation':'not_computed',
            'original_unassigned_receipts_count':len(original_unassigned),'post_hoc_associations_count':len(set(post_hoc))}
