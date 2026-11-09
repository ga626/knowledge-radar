"""既有评测资产的事务维护；Agent 提交语义依据，程序不推断真伪或金标。"""
from __future__ import annotations

from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from .evaluation_candidates import (_connect, _readonly, candidate_path, list_candidates,
                                    observation_summary, candidate_members)


LAYERS = {'contract': '合同可靠性', 'materials': '材料获取', 'conclusions': '研究结论', 'delivery': '用户成果'}
LIFECYCLES = {'draft', 'shadow', 'active', 'merged_archive', 'retired_archive'}
FREQUENCIES = {'affected', 'release', 'sample'}
RUNNABILITY = {'ready', 'external_blocked', 'input_invalid', 'oracle_disputed'}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value):
        raise ValueError('bounded_identifier_required')
    return value


def hashes(values):
    if not isinstance(values, list) or not 0 < len(values) <= 100 or any(not isinstance(v, str) or not re.fullmatch('[a-f0-9]{64}', v) for v in values):
        raise ValueError('fixed_evidence_hashes_required')
    return values


class EvaluationRead(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['evaluation']
    view: Literal['overview', 'cases', 'changes', 'feedback', 'queue', 'candidates', 'members'] = 'overview'
    candidate_id: str = Field(default='', max_length=128)
    after: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class EvaluationWrite(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['evaluation'] = 'evaluation'
    operation: Literal['upsert', 'state', 'merge', 'compensate', 'lock', 'unlock', 'resolve_feedback', 'review_candidate']
    case_id: str = Field(pattern=r'^[A-Za-z0-9_.:-]{1,128}$')
    expected_revision: int = Field(ge=0)
    request_id: str = Field(pattern=r'^[A-Za-z0-9_.:-]{1,128}$')
    actor: str = Field(min_length=1, max_length=128)
    rule_version: str = Field(min_length=1, max_length=128)
    skill_version: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)
    evidence: list[str] = Field(min_length=1, max_length=100)
    mode: Literal['apply', 'shadow'] = 'apply'
    card: dict = Field(default_factory=dict)
    patch: dict = Field(default_factory=dict)
    review: dict = Field(default_factory=dict)
    target_id: str = Field(default='', max_length=128)
    target_revision: int = Field(default=0, ge=0)
    coverage_map: dict = Field(default_factory=dict)
    change_id: str = Field(default='', max_length=128)
    feedback_id: str = Field(default='', max_length=128)
    candidate_id: str = Field(default='', max_length=128)
    disposition: Literal['', 'linked_existing_case', 'rejected', 'needs_evidence', 'disputed'] = ''
    human: bool = False


def validate_card(card, case_id):
    allowed = {'case_id', 'title', 'prevents', 'layer', 'case_type', 'critical', 'coverage', 'examples', 'dependencies'}
    if set(card) - allowed or card.get('case_id') != case_id:
        raise ValueError('card_identity_or_fields')
    if card.get('layer') not in LAYERS or card.get('case_type') not in {'regression', 'capability'}:
        raise ValueError('card_layer_and_type_required')
    for field in ('title', 'prevents'):
        if not isinstance(card.get(field), str) or not card[field].strip() or len(card[field]) > 1000:
            raise ValueError('human_readable_card_required')
    coverage = card.get('coverage')
    if not isinstance(coverage, list) or not 0 < len(coverage) <= 50:
        raise ValueError('coverage_required')
    for label in coverage:
        identifier(label)
    examples = card.get('examples')
    if not isinstance(examples, list) or not 0 < len(examples) <= 100:
        raise ValueError('replayable_examples_required')
    for ex in examples:
        if set(ex) - {'input_hash', 'bad_hash', 'good_hash', 'oracle', 'provenance'}:
            raise ValueError('unknown_example_field')
        hashes([ex.get('input_hash'), ex.get('good_hash')])
        if card['case_type'] == 'regression':
            hashes([ex.get('bad_hash')])
            if len({ex['input_hash'], ex['good_hash'], ex['bad_hash']}) != 3:
                raise ValueError('distinct_input_positive_negative_required')
        for field in ('oracle', 'provenance'):
            if not isinstance(ex.get(field), str) or not ex[field].strip() or len(ex[field]) > 500:
                raise ValueError('oracle_and_legal_provenance_required')
    return {**card, 'critical': bool(card.get('critical', False))}


def independent_review(req):
    review = req.review
    if not review.get('reviewer') or review['reviewer'] == req.actor or review.get('verdict') != 'accepted':
        raise ValueError('independent_review_required')
    hashes(review.get('evidence'))


class Maintenance:
    def __init__(self, path=None):
        self.path = Path(path or candidate_path())

    @contextmanager
    def connection(self):
        with closing(_connect(self.path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS evaluation_cards (case_id TEXT PRIMARY KEY, revision INTEGER, body TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS evaluation_changes (seq INTEGER PRIMARY KEY AUTOINCREMENT, change_id TEXT UNIQUE, request_id TEXT UNIQUE, request_hash TEXT, body TEXT, result TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS evaluation_feedback (feedback_id TEXT PRIMARY KEY, request_id TEXT UNIQUE, request_hash TEXT, body TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS evaluation_rules (rule_version TEXT PRIMARY KEY, state TEXT, change_id TEXT)')
            db.commit()
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _case(db, case_id):
        row = db.execute('SELECT body FROM evaluation_cards WHERE case_id=?', (case_id,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _put(db, card):
        db.execute('INSERT INTO evaluation_cards VALUES (?,?,?) ON CONFLICT(case_id) DO UPDATE SET revision=excluded.revision,body=excluded.body',
                   (card['case_id'], card['revision'], encoded(card)))

    def execute(self, request):
        req = request if isinstance(request, EvaluationWrite) else EvaluationWrite.model_validate(request)
        payload = req.model_dump()
        if len(encoded(payload)) > 60000:
            raise ValueError('bounded_request_required')
        hashes(req.evidence)
        with self.connection() as db:
            old_request = db.execute('SELECT request_hash,result FROM evaluation_changes WHERE request_id=?', (req.request_id,)).fetchone()
            if old_request:
                if old_request[0] != digest(payload):
                    raise ValueError('idempotency_conflict')
                return json.loads(old_request[1])
            rule = db.execute('SELECT state FROM evaluation_rules WHERE rule_version=?', (req.rule_version,)).fetchone()
            if rule and rule[0] == 'paused' and not req.human:
                raise ValueError('rule_paused_after_correction')
            old = self._case(db, req.case_id)
            if (old or {}).get('revision', 0) != req.expected_revision:
                raise ValueError('expected_revision_conflict')
            if old and old.get('locked') and req.operation != 'unlock':
                raise ValueError('human_locked')
            before, after = {}, {}
            if old:
                before[req.case_id] = old
            op = req.operation
            if op == 'upsert':
                card = validate_card(req.card, req.case_id)
                if req.mode == 'apply':
                    independent_review(req)
                if old and (old['lifecycle'] in {'merged_archive', 'retired_archive'} or old.get('alias_of')):
                    raise ValueError('restore_via_compensation')
                after[req.case_id] = {**(old or {}), **card, 'revision': req.expected_revision + 1,
                    'lifecycle': (old or {}).get('lifecycle', 'shadow'), 'frequency': (old or {}).get('frequency', 'affected'),
                    'runnability': (old or {}).get('runnability', 'ready'), 'locked': (old or {}).get('locked', False),
                    'confidence': 'agent_reviewed', 'admitted_by': req.actor, 'review': req.review}
            elif op == 'state':
                if not old or set(req.patch) - {'lifecycle', 'frequency', 'runnability'}:
                    raise ValueError('state_axes_required')
                proposed = {**old, **req.patch, 'revision': old['revision'] + 1}
                if proposed['lifecycle'] not in LIFECYCLES or proposed['frequency'] not in FREQUENCIES or proposed['runnability'] not in RUNNABILITY:
                    raise ValueError('invalid_state_axis')
                if proposed['lifecycle'] == 'merged_archive' or old['lifecycle'] in {'merged_archive', 'retired_archive'}:
                    raise ValueError('use_merge_or_compensation')
                if proposed['lifecycle'] == 'retired_archive':
                    # 长期绿不是退役依据；关键不变量仅能由显式合同退出或等价覆盖替换。
                    replacement = self._case(db, req.target_id) if req.target_id else None
                    if not replacement or replacement['revision'] != req.target_revision or replacement['lifecycle'] != 'active' or not set(old['coverage']) <= set(replacement['coverage']):
                        raise ValueError('retirement_requires_verified_replacement')
                    independent_review(req)
                if proposed['lifecycle'] == 'active' and old['lifecycle'] != 'active':
                    independent_review(req)
                after[req.case_id] = proposed
            elif op in {'lock', 'unlock'}:
                if not req.human or not old:
                    raise ValueError('human_correction_required')
                after[req.case_id] = {**old, 'locked': op == 'lock', 'revision': old['revision'] + 1}
            elif op == 'merge':
                independent_review(req)
                target = self._case(db, req.target_id)
                if not old or not target or req.target_id == req.case_id or target['revision'] != req.target_revision:
                    raise ValueError('merge_target_revision_conflict')
                if target.get('locked') or any(c['lifecycle'] in {'merged_archive', 'retired_archive'} for c in (old, target)):
                    raise ValueError('merge_locked_or_archived')
                if set(req.coverage_map) != set(old['coverage']) or not set(req.coverage_map.values()) <= set(target['coverage']):
                    raise ValueError('independent_coverage_mapping_required')
                before[req.target_id] = target
                examples = {digest(x): x for x in [*target['examples'], *old['examples']]}
                after[req.case_id] = {**old, 'revision': old['revision'] + 1, 'lifecycle': 'merged_archive', 'alias_of': req.target_id}
                after[req.target_id] = {**target, 'revision': target['revision'] + 1, 'examples': list(examples.values()),
                    'coverage': sorted(set(target['coverage']) | set(old['coverage'])),
                    'critical': old['critical'] or target['critical'],
                    'aliases': sorted(set(target.get('aliases', [])) | {old['case_id']})}
            elif op == 'compensate':
                if not req.human:
                    raise ValueError('human_correction_required')
                change = db.execute('SELECT body FROM evaluation_changes WHERE change_id=?', (req.change_id,)).fetchone()
                if not change:
                    raise ValueError('unknown_change')
                previous = json.loads(change[0])
                if req.case_id not in previous['after'] or previous['mode'] != 'apply' or not previous['before']:
                    raise ValueError('change_not_reversible')
                before = {}
                for cid, proposed in previous['after'].items():
                    current = self._case(db, cid)
                    if not current or current['revision'] != proposed['revision'] or current.get('locked'):
                        raise ValueError('compensation_downstream_revision_conflict')
                    before[cid] = current
                    if cid in previous['before']:
                        after[cid] = {**previous['before'][cid], 'revision': current['revision'] + 1,
                                      'reevaluation': 'required'}
                    else:
                        after[cid] = {**current, 'lifecycle': 'retired_archive', 'revision': current['revision'] + 1,
                                      'reevaluation': 'required'}
                db.execute('INSERT INTO evaluation_rules VALUES (?,?,?) ON CONFLICT(rule_version) DO UPDATE SET state=excluded.state,change_id=excluded.change_id',
                           (previous['rule_version'], 'paused', req.change_id))
            elif op == 'resolve_feedback':
                row = db.execute('SELECT body FROM evaluation_feedback WHERE feedback_id=?', (req.feedback_id,)).fetchone()
                if not row:
                    raise ValueError('unknown_feedback')
                fb = json.loads(row[0])
                if fb['case_id'] != req.case_id or not req.change_id or not db.execute('SELECT 1 FROM evaluation_changes WHERE change_id=?', (req.change_id,)).fetchone():
                    raise ValueError('feedback_requires_audited_disposition')
                if req.mode == 'apply':
                    db.execute('UPDATE evaluation_feedback SET body=? WHERE feedback_id=?', (encoded({**fb, 'status':'processed', 'resolution_change':req.change_id}), req.feedback_id))
            elif op == 'review_candidate':
                row = db.execute('SELECT review FROM candidates WHERE candidate_id=?', (req.candidate_id,)).fetchone()
                if not row or req.disposition not in {'linked_existing_case','rejected','needs_evidence','disputed'}:
                    raise ValueError('candidate_and_disposition_required')
                if req.disposition == 'linked_existing_case' and not old:
                    raise ValueError('linked_case_required')
                if row[0]:
                    raise ValueError('candidate_already_reviewed')
                if req.mode == 'apply':
                    db.execute('UPDATE candidates SET review=? WHERE candidate_id=?', (encoded({'decision':req.disposition, 'case_id':req.case_id if old else '', 'reason':req.reason, 'evidence':req.evidence, 'reviewer':req.actor, 'at':now(), 'gold_promoted':False}), req.candidate_id))
            if req.mode == 'apply':
                for row in after.values():
                    self._put(db, row)
            change_id = 'change-' + digest(payload)[:24]
            result = {'status': req.mode if req.mode == 'shadow' else 'recorded', 'case_id': req.case_id,
                      'revision': after.get(req.case_id, old or {}).get('revision', 0) if req.mode == 'apply' else req.expected_revision,
                      'change_id': change_id, 'semantic_verdict': 'agent_declared_not_program_verified'}
            if op == 'compensate':
                result['affected_trials'] = 'requires_reevaluation'
            record = {**payload, 'change_id': change_id, 'before': before, 'after': after, 'at': now()}
            db.execute('INSERT INTO evaluation_changes(change_id,request_id,request_hash,body,result) VALUES (?,?,?,?,?)',
                       (change_id, req.request_id, digest(payload), encoded(record), encoded(result)))
            return result

    def feedback(self, *, case_id, revision, text, request_id):
        identifier(case_id); identifier(request_id)
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError('bounded_correction_required')
        payload = {'case_id': case_id, 'revision': revision, 'text': text}
        with self.connection() as db:
            previous = db.execute('SELECT request_hash,body FROM evaluation_feedback WHERE request_id=?', (request_id,)).fetchone()
            if previous:
                if previous[0] != digest(payload):
                    raise ValueError('feedback_idempotency_conflict')
                return json.loads(previous[1])
            row = self._case(db, case_id)
            if not row or row['revision'] != revision:
                raise ValueError('feedback_revision_changed_refresh')
            receipt = {**payload, 'feedback_id': 'feedback-' + digest([request_id, payload])[:24],
                       'status': 'waiting_for_agent', 'at': now(), 'user_content': True}
            db.execute('INSERT INTO evaluation_feedback VALUES (?,?,?,?)', (receipt['feedback_id'], request_id, digest(payload), encoded(receipt)))
            return receipt

    def view(self, *, limit=100, after=0):
        result = {'schema':'kr-evaluation-maintenance/v1', 'cases':[], 'changes':[], 'feedback':[], 'rules':[],
                  'executor':'waiting_for_agent', 'historical_results':'immutable', 'after': after}
        if not self.path.is_file():
            return result
        limit = max(1, min(100, int(limit)))
        with closing(_readonly(self.path)) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, key, order in [('evaluation_cards','cases','case_id'), ('evaluation_changes','changes','seq DESC'), ('evaluation_feedback','feedback','rowid DESC')]:
                if table not in tables:
                    continue
                result[key] = [json.loads(r[0]) for r in db.execute(f'SELECT body FROM {table} ORDER BY {order} LIMIT ? OFFSET ?', (limit,after))]
                result[key+'_total'] = db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
            if 'evaluation_rules' in tables:
                result['rules'] = [dict(r) for r in db.execute('SELECT * FROM evaluation_rules ORDER BY rule_version LIMIT 100')]
        result['health'] = {k: dict(Counter(c[k] for c in result['cases'])) for k in ('lifecycle', 'frequency', 'runnability')}
        result['truncated'] = any(result.get(k+'_total',0) > after+limit for k in ('cases','changes','feedback'))
        return result

    def work_batch(self, *, limit=20, max_seconds=120, max_cost=0):
        limit = max(0,min(20,int(limit)))
        view = self.view()
        items = [{'kind':'feedback', **r} for r in view['feedback'] if r['status'] == 'waiting_for_agent']
        items += [{'kind':'candidate', **r} for r in list_candidates(self.path) if not r['review']]
        return {'status':'waiting_for_agent', 'items':items[:limit], 'pending_visible':len(items),
                'limit':limit, 'max_seconds':max(0,min(600,int(max_seconds))), 'max_cost':max(0,float(max_cost)),
                'checkpoint':digest([(r['kind'],r.get('candidate_id') or r.get('feedback_id')) for r in items[:limit]]),
                'boundary':'read_only_no_claim_no_model_no_external_calls'}

    def read(self, request):
        req = request if isinstance(request, EvaluationRead) else EvaluationRead.model_validate(request)
        if req.view == 'queue':
            data = self.work_batch(limit=req.limit)
        elif req.view == 'candidates':
            rows = list_candidates(self.path)
            data = {'items':rows[req.after:req.after+req.limit], 'total':len(rows)}
        elif req.view == 'members':
            rows = candidate_members(req.candidate_id,self.path)
            data = {'items':rows[req.after:req.after+req.limit], 'total_returned':len(rows), 'boundary':'up_to_100_retained_members'}
        else:
            data = self.view(limit=req.limit, after=req.after)
            if req.view != 'overview':
                data = {'items':data[req.view], 'total':data.get(req.view+'_total',0)}
            else:
                data['observations'] = observation_summary(self.path)
        return {'kind':'research', 'status':'ok', 'read_only':True, **data}
