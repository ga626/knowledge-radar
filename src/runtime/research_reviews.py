"""Agent声明的命题和审阅；程序只核身份、版本、依赖及可读投影，不判真假。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from runtime.research_contracts import ContractWrite, Strict, Locator, current, rows_at


class PurposeWrite(ContractWrite):
    action: Literal['task_purpose']
    purposes: list[Literal['explanation','comparison','decision','causal']] = Field(min_length=1,max_length=4)
    task_card: str = Field(min_length=1,max_length=2000,description='同一目标任务：使用者、输入、期望结果、必要限制。')


class InvestigationWrite(ContractWrite):
    """可修订调查笔记；不调度搜索，不宣布客观饱和。"""
    action: Literal['investigation']
    question_id: str = Field(min_length=1,max_length=128)
    decision_use: str = Field(min_length=1,max_length=1000)
    analysis_unit: str = Field(min_length=1,max_length=1000)
    object_frame: str = Field(min_length=1,max_length=2000)
    selection_basis: str = Field(min_length=1,max_length=2000)
    explanations: list[str] = Field(default_factory=list,max_length=20)
    discriminating_evidence: list[str] = Field(default_factory=list,max_length=20)
    next_action: str = Field(default='',max_length=1000)
    next_action_reason: str = Field(default='',max_length=1000)
    changed_because: str = Field(default='',max_length=2000)
    evidence_refs: list[Locator] = Field(default_factory=list,max_length=40)
    state: Literal['active','sufficient_for_use','desk_incomplete','blocked','needs_fieldwork','deferred'] = 'active'
    rationale: str = Field(min_length=1,max_length=2000)
    unresolved_questions: list[str] = Field(default_factory=list,max_length=40)
    reopen_conditions: str = Field(default='',max_length=2000)


class Dependency(Strict):
    group_id: str = Field(min_length=1,max_length=128)
    role: Literal['necessary','support','background']
    evidence_refs: list[Locator] = Field(default_factory=list,max_length=40)
    upstream_claim_id: str = Field(default='',max_length=128)
    upstream_revision: int = Field(default=0,ge=0)
    basis: str = Field(min_length=1,max_length=2000)

    @model_validator(mode='after')
    def one_path(self):
        if bool(self.evidence_refs)==bool(self.upstream_claim_id):raise ValueError('choose_evidence_or_upstream_claim')
        if self.upstream_claim_id and not self.upstream_revision:raise ValueError('upstream_revision_required')
        return self


class WithdrawalSet(Strict):
    set_id: str = Field(min_length=1,max_length=128)
    kind: Literal['same_origin','shared_bias']
    label: str = Field(min_length=1,max_length=200)
    group_ids: list[str] = Field(min_length=2,max_length=40)
    basis: str = Field(min_length=1,max_length=2000)


class ClaimWrite(ContractWrite):
    action: Literal['claim']
    question_id: str = Field(min_length=1,max_length=128)
    text: str = Field(min_length=1,max_length=2000)
    claim_type: Literal['fact','explanation','recommendation','hypothesis','comparison','novelty']
    importance: Literal['high','normal'] = 'high'
    support_assessment: Literal['unreviewed','supports','supports_with_limits','insufficient'] = Field(default='unreviewed',description='Codex对相关性、可信度及交叉验证的声明；basis说明理由，程序不判真。')
    applicability: str = Field(min_length=1,max_length=2000)
    basis: str = Field(min_length=1,max_length=2000)
    uncertainty: str = Field(default='',max_length=2000)
    requirement_ids: list[str] = Field(default_factory=list,max_length=40,description='显式复用本次评审的需求ID，绑定当时需求版本；不改变取得状态。')
    question_relation: Literal['background','supports','supports_with_limits','contradicts','uncertain'] = Field(default='background',description='显式声明本命题如何回答问题；未声明不自动填覆盖。')
    languages: list[str] = Field(default_factory=list,max_length=20)
    populations: list[str] = Field(default_factory=list,max_length=20)
    regions: list[str] = Field(default_factory=list,max_length=20)
    information_roles: list[str] = Field(default_factory=list,max_length=20)
    time_scope: str = Field(default='',max_length=500)
    content_scope: str = Field(default='',max_length=500)
    dependencies: list[Dependency] = Field(default_factory=list,max_length=40)
    counterevidence_refs: list[Locator] = Field(default_factory=list,max_length=40)
    counterevidence_effect: Literal['unreviewed','limits','overturns','no_change'] = 'unreviewed'
    counterevidence_reason: str = Field(default='',max_length=2000,description='解释反证针对哪个命题、适用性与强度；引用不等于已处置。')
    withdrawal_sets: list[WithdrawalSet] = Field(default_factory=list,max_length=20,description='显式来源组/共同偏差组；后者不代表同源。程序不按域名自动合并。')


class AlternativesWrite(ContractWrite):
    action: Literal['alternatives_review']
    claim_id: str = Field(default='',max_length=128,description='空值审阅任务级替代问题；否则绑定当前关键命题。')
    claim_revision: int = Field(default=0,ge=0)
    disposition: Literal['compared','reused','not_found','blocked','deferred','not_applicable']
    rationale: str = Field(min_length=1,max_length=2000)
    search_scope: str = Field(min_length=1,max_length=2000)
    options: list[str] = Field(default_factory=list,max_length=40,description='同一任务上的替代方案及结果：区分宣称、适用性、实测。')
    evidence_refs: list[Locator] = Field(default_factory=list,max_length=40)
    reuse_id: str = Field(default='',max_length=128)
    reopen_conditions: str = Field(default='',max_length=2000)


class WithdrawalWrite(ContractWrite):
    action: Literal['withdrawal_review']
    claim_id: str = Field(min_length=1,max_length=128)
    claim_revision: int = Field(ge=1)
    scenario_id: str = Field(min_length=1,max_length=128)
    baseline: str = Field(pattern=r'^[a-f0-9]{64}$')
    conclusion: str = Field(min_length=1,max_length=2000)
    change: Literal['changed','unchanged','weakened','reversed','unknown']
    rationale: str = Field(min_length=1,max_length=2000)
    result_kind: Literal['empirical','hypothesis','value','unknown']
    remaining_refs: list[Locator] = Field(default_factory=list,max_length=40)


class ReportReviewWrite(ContractWrite):
    action: Literal['report_review']
    report_sha256: str = Field(pattern=r'^[a-f0-9]{64}$',description='未附程序投影前的报告正文 UTF-8 SHA256。')
    baseline: str = Field(pattern=r'^[a-f0-9]{64}$')
    claim_ids: list[str] = Field(max_length=100)
    coverage_statement: str = Field(min_length=1,max_length=2000,description='Codex反向核对报告关键结论覆盖的范围与限制。')


class LocalObservationWrite(ContractWrite):
    action: Literal['local_observation']
    input_path: str
    approved_input_root: str
    input_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    title: str = Field(min_length=1,max_length=200)
    method: str = Field(min_length=1,max_length=2000)


REVIEW_TYPES=(PurposeWrite,InvestigationWrite,ClaimWrite,AlternativesWrite,WithdrawalWrite,ReportReviewWrite,LocalObservationWrite)


def digest(value):
    from runtime.research_index import encoded
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def check_refs(index,task_id,refs,assets):
    from kr_core.evidence_assets import EvidenceAssets
    assets=assets or EvidenceAssets(read_only=True)
    for ref in refs:
        index.validate_ref(ref,assets)
        with assets.connection() as conn:
            if not conn.execute('SELECT 1 FROM asset_scopes WHERE version_id=? AND task_id=?',(ref['version_id'],task_id)).fetchone():
                raise ValueError('evidence_not_registered_to_research_task')
            row=conn.execute('SELECT kind,metadata FROM assets WHERE version_id=?',(ref['version_id'],)).fetchone()
            if row['kind']=='report':raise ValueError('self_report_is_not_independent_evidence')


def dependency_refs(dep, claims, seen=None):
    """沿已登记命题依赖展开原文；撤证不能经上游结论绕回被排除来源。"""
    refs=list(dep['evidence_refs'])
    cid=dep.get('upstream_claim_id');seen=set(seen or ())
    if cid and cid not in seen and cid in claims:
        seen.add(cid)
        for child in claims[cid]['dependencies']:
            if child['role']!='background':refs.extend(dependency_refs(child,claims,seen))
    return refs


def state(index,conn,task_id,assets=None,high=None):
    records=sorted(rows_at(conn,task_id,high,include_seq=True),key=lambda r:r['_seq']) if conn is not None else []
    claims=[r for r in records if r['_kind']=='claim']
    purpose=next((r for r in records if r['_kind']=='task_purpose'),None)
    questions={c['question_id']:index.question_at(conn,task_id,c['question_id'],high or 9223372036854775807) for c in claims}
    gaps=[]
    if not purpose:gaps.append('task_purpose_unknown')
    if not claims:gaps.append('critical_claims_unreviewed')
    decision=bool(purpose and set(purpose['purposes']) & {'decision','comparison'})
    freshness={};signatures={}
    for c in claims:
        refs=[r for d in c['dependencies'] for r in d['evidence_refs']]
        try:
            check_refs(index,task_id,refs+c['counterevidence_refs'],assets)
            checked=index.assessment_review({'evidence_refs':refs,'counterevidence_refs':c['counterevidence_refs'],
                'question_revision':c['question_revision']},questions[c['question_id']],assets,task_id)
            freshness[c['record_id']]={k:checked[k] for k in ('review_reason','newer_versions') if k in checked}
        except (ValueError,OSError):freshness[c['record_id']]={'review_reason':'evidence_unavailable'}
    # 旧审阅保持历史有效性，不自动升级为新的撤证/反证合同。
    baseline=digest({'review_contract':2,'purpose':purpose,'claims':claims,'questions':questions,'freshness':freshness})
    def signature(c,seen=None):
        cid=c['record_id'];seen=set(seen or ())
        if cid in seen:return 'cyclic_legacy_dependency'
        if cid not in signatures:
            seen.add(cid)
            upstream=[]
            for d in c['dependencies']:
                if d.get('upstream_claim_id'):
                    up=next((u for u in claims if u['record_id']==d['upstream_claim_id']),None)
                    upstream.append(signature(up,seen) if up else 'missing')
            signatures[cid]=digest([2,c,questions[c['question_id']],freshness[cid],upstream])
        return signatures[cid]
    def upstream_stale(c,seen=None):
        seen=set(seen or ())
        if c['record_id'] in seen:return True
        seen.add(c['record_id'])
        if freshness[c['record_id']]:return True
        for d in c['dependencies']:
            if d.get('upstream_claim_id'):
                up=next((u for u in claims if u['record_id']==d['upstream_claim_id']),None)
                if not up or up['revision']!=d['upstream_revision'] or upstream_stale(up,seen):return True
        return False
    output=[]
    for claim in claims:
        cid=claim['record_id'];core=[d for d in claim['dependencies'] if d['role']!='background']
        if claim.get('support_assessment','unreviewed') in {'unreviewed','insufficient'}:
            gaps.append('semantic_support_'+claim.get('support_assessment','unreviewed')+':'+cid)
        if claim.get('counterevidence_refs') and claim.get('counterevidence_effect','unreviewed')=='unreviewed':gaps.append('counterevidence_unreviewed:'+cid)
        if claim.get('counterevidence_effect')=='overturns':gaps.append('counterevidence_overturns:'+cid)
        if claim['importance']=='high' and not core:gaps.append('core_support_missing:'+cid)
        if questions[claim['question_id']].get('revision',1)!=claim['question_revision']:gaps.append('claim_scope_changed:'+cid)
        invalid=[]
        for dep in claim['dependencies']:
            if dep.get('upstream_claim_id'):
                up=next((c for c in claims if c['record_id']==dep['upstream_claim_id']),None)
                if not up or up['revision']!=dep['upstream_revision'] or upstream_stale(up):gaps.append('upstream_claim_changed:'+cid)
            try:check_refs(index,task_id,dep['evidence_refs'],assets)
            except (ValueError,OSError):invalid.append(dep['group_id'])
        if invalid:gaps.append('evidence_unavailable:'+cid)
        if freshness[cid].get('review_reason')=='source_version_changed':gaps.append('source_version_changed:'+cid)
        claim_map={c['record_id']:c for c in claims}
        all_refs=[r for d in core for r in dependency_refs(d,claim_map)]
        all_versions={r['version_id'] for r in all_refs}
        cb=signature(claim)
        scenarios=[]
        sets=[{'group_id':d['group_id'],'scenario_id':'withdraw-'+digest([cid,d['group_id']])[:24],
            'withdrawal_kind':'dependency','label':'','members':[d]} for d in core]
        sets.extend({'group_id':'set:'+group['set_id'],'scenario_id':'withdraw-'+digest([cid,'set',group['set_id']])[:24],
            'withdrawal_kind':group['kind'],'label':group['label'],'basis':group['basis'],
            'members':[d for d in core if d['group_id'] in group['group_ids']]} for group in claim.get('withdrawal_sets',[]))
        for group in sets:
            members=group.pop('members')
            excluded_refs=[r for d in members for r in dependency_refs(d,claim_map)]
            excluded=sorted({r['version_id'] for r in excluded_refs})
            scenario={**group,'baseline':cb,'excluded_versions':excluded,
                'excluded_refs':excluded_refs,
                'remaining_refs':[r for r in all_refs if r['version_id'] not in excluded],
                'remaining_versions':sorted(all_versions-set(excluded)),
                'excluded_upstream_claim':members[0].get('upstream_claim_id','') if len(members)==1 else '',
                'excluded_upstream_claims':[d['upstream_claim_id'] for d in members if d.get('upstream_claim_id')]}
            matches=[r for r in records if r['_kind']=='withdrawal_review' and r['scenario_id']==scenario['scenario_id'] and r['baseline']==cb]
            scenario['review']=matches[-1] if matches else None
            if not matches:gaps.append('withdrawal_unreviewed:'+cid+':'+group['group_id'])
            scenarios.append(scenario)
        alternatives=[r for r in records if r['_kind']=='alternatives_review' and r['claim_id']==cid and r['claim_revision']==claim['revision'] and r.get('purpose_revision',0)==(purpose or {}).get('revision',0) and r.get('claim_baseline')==cb]
        task_alts=[r for r in records if r['_kind']=='alternatives_review' and not r['claim_id'] and r.get('purpose_revision',0)==(purpose or {}).get('revision',0) and r.get('claim_baseline')==baseline]
        alt=alternatives[-1] if alternatives else task_alts[-1] if task_alts else None
        needed=claim['claim_type'] in {'recommendation','comparison','novelty'}
        if needed and not alt:gaps.append('alternatives_unreviewed')
        if alt and alt['disposition'] in {'blocked','deferred'}:gaps.append('alternatives_'+alt['disposition']+':'+cid)
        output.append({**claim,'baseline':cb,'scenarios':scenarios,'alternatives':({**alt,'absence_proven':False} if alt else None),'alternatives_required':needed})
    if decision and not any(c['alternatives_required'] for c in output):
        task_alts=[r for r in records if r['_kind']=='alternatives_review' and not r['claim_id']
            and r.get('purpose_revision',0)==purpose['revision'] and r.get('claim_baseline')==baseline]
        if not task_alts:gaps.append('task_alternatives_unreviewed')
        elif task_alts[-1]['disposition'] in {'blocked','deferred'}:gaps.append('task_alternatives_'+task_alts[-1]['disposition'])
    # 审阅事件变化也使全文审阅快照失效；原始执行回执不会无故使其失效。
    review_baseline=digest([baseline,[r for r in records if r['_kind'] in {'alternatives_review','withdrawal_review'}]])
    return {'baseline':review_baseline,'task_baseline':baseline,'claims':output,'purpose':purpose,'gaps':sorted(set(gaps)),
        'review_status':'gaps' if gaps else 'recorded','semantic_truth':'not_program_verified','records':records}


def question_assessments(index,conn,task_id,assets=None,high=None):
    snap=state(index,conn,task_id,assets,high)
    claims={c['record_id']:c for c in snap['claims']}
    def refs_for(c,seen=None):
        seen=set(seen or ())
        if c['record_id'] in seen:return []
        seen.add(c['record_id']);refs=[]
        for d in c['dependencies']:
            if d['role']=='background':continue
            refs.extend(d['evidence_refs'])
            if d.get('upstream_claim_id') in claims:refs.extend(refs_for(claims[d['upstream_claim_id']],seen))
        return refs
    result=[]
    for c in claims.values():
        if c.get('question_relation','background')=='background' and not c.get('requirement_ids'):continue
        relevant=[g for g in snap['gaps'] if g.endswith(':'+c['record_id']) and g.split(':')[0] in {
            'claim_scope_changed','source_version_changed','upstream_claim_changed','evidence_unavailable',
            'semantic_support_unreviewed','semantic_support_insufficient','core_support_missing','counterevidence_unreviewed','counterevidence_overturns'}]
        value={**c,'record_id':'claim:'+c['record_id'],'claim_id':c['record_id'],'origin':'claim',
            'relation':c['question_relation'],'evidence_refs':refs_for(c)}
        if relevant or not value['evidence_refs']:
            value.update(review_required=True,review_reason=';'.join(relevant) or 'core_support_missing')
        result.append(value)
    return result


def validate(index,conn,req,value,assets,*,project_id=''):
    tid=req.task_id
    if isinstance(req,PurposeWrite):
        if req.record_id!='purpose':raise ValueError('single_task_purpose_required')
    if isinstance(req,InvestigationWrite):
        question=index.question_at(conn,tid,req.question_id)
        if not question:raise ValueError('unknown_question')
        if req.record_id!=req.question_id:raise ValueError('investigation_identity_must_match_question')
        prior=current(conn,tid,'investigation',req.record_id)
        if prior and any(prior.get(k)!=v for k,v in value.items()) and not req.changed_because.strip():raise ValueError('investigation_revision_reason_required')
        if req.state!='active' and (not req.reopen_conditions.strip() or (req.state!='sufficient_for_use' and not req.unresolved_questions)):
            raise ValueError('investigation_stop_requires_gaps_and_reopen_condition')
        if bool(req.next_action.strip())!=bool(req.next_action_reason.strip()):raise ValueError('next_action_reason_required')
        check_refs(index,tid,[r.model_dump() for r in req.evidence_refs],assets)
        value['question_revision']=question.get('revision',1)
    if isinstance(req,ClaimWrite):
        if (req.requirement_ids or req.question_relation in {'supports','supports_with_limits'}) and (req.support_assessment not in {'supports','supports_with_limits'} or not any(d.role!='background' for d in req.dependencies)):
            raise ValueError('question_support_requires_reviewed_core')
        question=index.question_at(conn,tid,req.question_id)
        if not question:raise ValueError('unknown_question')
        value['question_revision']=question.get('revision',1)
        if req.counterevidence_effect!='unreviewed' and (not req.counterevidence_refs or not req.counterevidence_reason.strip()):raise ValueError('counterevidence_requires_reason_and_refs')
        if req.counterevidence_effect=='overturns' and (req.question_relation in {'supports','supports_with_limits'} or req.requirement_ids):raise ValueError('question_support_conflicts_with_counterevidence')
        core_ids={d.group_id for d in req.dependencies if d.role!='background'}
        if len({w.set_id for w in req.withdrawal_sets})!=len(req.withdrawal_sets):raise ValueError('duplicate_withdrawal_set')
        for group in req.withdrawal_sets:
            if len(set(group.group_ids))!=len(group.group_ids) or not set(group.group_ids)<=core_ids:raise ValueError('withdrawal_set_members_must_be_core')
        value['requirement_revisions']={}
        for rid in req.requirement_ids:
            requirement=current(conn,tid,'requirement',rid)
            if not requirement or requirement['question_id']!=req.question_id:raise ValueError('requirement_question_mismatch')
            value['requirement_revisions'][rid]=requirement['revision']
        if len({d.group_id for d in req.dependencies})!=len(req.dependencies):raise ValueError('duplicate_dependency_group')
        check_refs(index,tid,[r.model_dump() for d in req.dependencies for r in d.evidence_refs]+[r.model_dump() for r in req.counterevidence_refs],assets)
        def visit(cid,seen):
            if cid==req.record_id:raise ValueError('cyclic_claim_dependency')
            if cid in seen:return
            seen.add(cid);up=current(conn,tid,'claim',cid)
            if not up:raise ValueError('unknown_upstream_claim')
            for d in up['dependencies']:
                if d.get('upstream_claim_id'):visit(d['upstream_claim_id'],seen)
        for dep in req.dependencies:
            if dep.upstream_claim_id:
                visit(dep.upstream_claim_id,set())
                if current(conn,tid,'claim',dep.upstream_claim_id)['revision']!=dep.upstream_revision:raise ValueError('upstream_revision_conflict')
    if isinstance(req,(AlternativesWrite,WithdrawalWrite,ReportReviewWrite)):
        snap=state(index,conn,tid,assets)
        claim=next((c for c in snap['claims'] if c['record_id']==getattr(req,'claim_id','')),None)
        if getattr(req,'claim_id','') and (not claim or claim['revision']!=req.claim_revision):raise ValueError('claim_revision_conflict')
        if isinstance(req,AlternativesWrite):
            if not snap['purpose']:raise ValueError('task_card_required')
            value.update(purpose_revision=snap['purpose']['revision'],claim_baseline=claim['baseline'] if claim else snap['task_baseline'])
            if req.disposition=='compared' and (len(req.options)<2 or not req.evidence_refs):raise ValueError('comparison_options_and_evidence_required')
            if req.disposition=='reused':
                old=current(conn,tid,'alternatives_review',req.reuse_id)
                if not old or old['disposition'] not in {'compared','reused'}:raise ValueError('reuse_snapshot_required')
                check_refs(index,tid,old['evidence_refs'],assets)
                value['reused_snapshot']=old
            if req.disposition in {'blocked','deferred'} and not req.reopen_conditions.strip():raise ValueError('reopen_conditions_required')
            check_refs(index,tid,[r.model_dump() for r in req.evidence_refs],assets)
        if isinstance(req,WithdrawalWrite):
            if req.conclusion.strip().lower() in {'','通过','pass','ok'}:raise ValueError('withdrawal_conclusion_required')
            scenario=next((s for s in claim['scenarios'] if s['scenario_id']==req.scenario_id),None)
            if not scenario or scenario['baseline']!=req.baseline:raise ValueError('stale_withdrawal_baseline')
            refs=[r.model_dump() for r in req.remaining_refs]
            if any(r['version_id'] in scenario['excluded_versions'] for r in refs):raise ValueError('excluded_evidence')
            if any(r['version_id'] not in scenario['remaining_versions'] for r in refs):raise ValueError('evidence_outside_scenario')
            allowed=scenario['remaining_refs']
            if any(not any(r['version_id']==a['version_id'] and a['start']<=r['start']<r['end']<=a['end'] for a in allowed) for r in refs):raise ValueError('reference_outside_registered_support')
            if req.result_kind=='empirical' and not refs:raise ValueError('remaining_support_required')
            check_refs(index,tid,refs,assets)
            value.update(excluded_versions=scenario['excluded_versions'],remaining_versions=scenario['remaining_versions'])
        if isinstance(req,ReportReviewWrite):
            if not req.claim_ids:raise ValueError('empty_claim_review_forbidden')
            if set(req.claim_ids)!={c['record_id'] for c in snap['claims']}:raise ValueError('report_claim_coverage_mismatch')
            if req.baseline!=snap['baseline']:raise ValueError('stale_report_review_baseline')
    if isinstance(req,LocalObservationWrite):
        from runtime.knowledge import guarded_path
        from kr_core.evidence_assets import EvidenceAssets
        path=guarded_path(req.approved_input_root,req.input_path,require_file=True)
        with path.open('rb') as handle:raw=handle.read(2*1024*1024+1)
        if len(raw)>2*1024*1024:raise ValueError('observation_too_large')
        if hashlib.sha256(raw).hexdigest()!=req.input_sha256:raise ValueError('input_changed')
        repository=assets or EvidenceAssets()
        ref=repository.put('kr-observation:'+req.input_sha256,raw.decode('utf-8'),kind='content',title=req.title,
            metadata={'acquisition_path':'authorized_local_observation','input_sha256':req.input_sha256,'method':req.method})
        repository.reference_many([ref['version_id']],tid,project_id=project_id,task_id=tid)
        value['version_id']=ref['version_id']


def review_projection(index,task_id,assets=None):
    with index.connection() as conn:result=state(index,conn,task_id,assets)
    result.pop('records',None)
    if not result['claims']:result['review_status']='unreviewed'
    return result


def review_page(index,conn,task_id,base,assets,max_chars,limit,after_seq,high,*,scenarios=False):
    """摘要与情景按事件水位分页；过大的命题可经claims视图定位，不膨胀工具信封。"""
    from runtime.research_index import encoded
    snap=state(index,conn,task_id,assets,high)
    result={**base,'baseline':snap['baseline'],'review_status':snap['review_status'],
        'semantic_truth':snap['semantic_truth'],'gaps_count':len(snap['gaps']),'gaps':snap['gaps'][:limit]}
    candidates=[]
    for c in snap['claims']:
        if scenarios:
            for number,s in enumerate(c['scenarios']):
                candidates.append((c['_seq']*100+number+1,{'claim_id':c['record_id'],'claim_revision':c['revision'],**s}))
        else:
            candidates.append((c['_seq'],{k:c[k] for k in ('record_id','revision','text','baseline','importance','claim_type')} |
                {'scenarios_count':len(c['scenarios']),'withdrawal_pending':sum(not s['review'] for s in c['scenarios']),
                 'alternatives_required':c['alternatives_required'],'alternatives_disposition':(c['alternatives'] or {}).get('disposition','unreviewed')}))
    pending=[(seq,row) for seq,row in candidates if seq>after_seq]
    for seq,row in pending[:limit]:
        result['items'].append(row)
        if len(encoded(result))>max_chars:
            result['items'].pop();break
        result['next_after_seq']=seq
    result['truncated']=len(result['items'])<len(pending) or len(snap['gaps'])>limit
    while len(encoded(result))>max_chars and result['gaps']:result['gaps'].pop();result['truncated']=True
    if pending and not result['items']:raise ValueError('budget_too_small_use_claims_and_withdrawal_reviews')
    return result


def report_appendix(index,task_id,content,assets=None,*,requested=False):
    with index.connection() as conn:snap=state(index,conn,task_id,assets)
    body_hash=hashlib.sha256(content.encode()).hexdigest()
    if not snap['claims'] and not requested:
        return content,{'baseline':snap['baseline'],'body_sha256':body_hash,
            'full_text_review':'not_requested','gaps':snap['gaps'],
            'projection_transforms':[],'dependency_versions':[]}
    reviews=[r for r in snap['records'] if r['_kind']=='report_review' and r['report_sha256']==body_hash and r['baseline']==snap['baseline']]
    def cell(text):return ' '.join(str(text).replace('|',r'\|').splitlines())
    # 编号按固定版本和摘录范围去重，仅改变展示，不改账本和原始资料。
    references={}
    synthetic_marker='【合成数据，非真实市场证据】'
    def reference(ref):
        key=(ref['version_id'],ref['start'],ref['end'])
        if key not in references:
            title,excerpt,synthetic='原依据当前不可用','原文当前不可用',False
            try:
                from kr_core.evidence_assets import EvidenceAssets
                store=assets or EvidenceAssets(read_only=True)
                fetched=store.fetch(ref['version_id'],start=ref['start'],max_chars=min(120,ref['end']-ref['start']))
                title=fetched['asset']['title'] or '固定原文'
                excerpt=fetched['text']
                synthetic=store.fetch(ref['version_id'],start=0,max_chars=len(synthetic_marker))['text']==synthetic_marker
                if synthetic and excerpt.startswith(synthetic_marker):excerpt=excerpt[len(synthetic_marker):]
                if ref['end']-ref['start']>120:excerpt+='…（摘录）'
            except (ValueError,OSError):pass
            references[key]={'number':len(references)+1,'title':title,'excerpt':excerpt,'synthetic':synthetic}
        return references[key]['number']
    def refs_text(refs):
        numbers=dict.fromkeys(reference(ref) for ref in refs)
        return '、'.join(f'[{number}](#资料{number})' for number in numbers) or '未登记'
    claim_numbers={c['record_id']:n for n,c in enumerate(snap['claims'],1)}
    def upstream(dep):return '上游结论'+str(claim_numbers.get(dep['upstream_claim_id'],'（未列出）'))
    def row(values):return '| '+' | '.join(map(cell,values))+' |'
    def notes(parts):return '；'.join(part.rstrip('。；; ') for part in parts if part)
    lines=['','---','','## 关键结论与撤证审阅','',
        '先看每条建议依赖什么、还缺什么，再看撤去关键资料后是否仍能成立。请据此判断建议是否值得采纳，以及投入行动前还需要补哪些验证。','',
        '全文关键结论核对：'+('已记录' if reviews else '未审阅或已失效')+'。',
        '语义正确性：由 Codex 声明，程序未判真。带缺口交付不等于审阅完成。','',
        '| 关键结论 | 核心依据 | 最强反证／直接替代 | 尚未解决事项 |',
        '| --- | --- | --- | --- |']
    changes={'changed':'改变','unchanged':'保留','weakened':'降级','reversed':'反转','unknown':'未知'}
    dispositions={'compared':'已比较','reused':'复用旧比较','not_found':'在所查范围未确认，不能证明不存在','blocked':'比较受阻','deferred':'比较推迟','not_applicable':'声明不适用'}
    details=[];scenario_rows=[]
    for number,claim in enumerate(snap['claims'],1):
        deps=[]
        for dep in claim['dependencies']:
            if dep['role']=='background':continue
            deps.append((refs_text(dep['evidence_refs']) if dep['evidence_refs'] else upstream(dep))+'：'+dep['basis'])
        alt=claim['alternatives'];counter=refs_text(claim['counterevidence_refs'])
        effect=claim.get('counterevidence_effect','unreviewed')
        if claim['counterevidence_refs']:
            counter+='；反证判断：'+{'unreviewed':'尚未评估强度','limits':'限制适用范围','overturns':'已被所登记反证推翻','no_change':'声明不改变结论'}[effect]
            if claim.get('counterevidence_reason'):counter+='：'+claim['counterevidence_reason']
        details+=['',f'### 结论{number}','',f"适用范围：{claim['applicability']}",'',f"判断理由：{claim['basis']}"]
        if alt:
            counter+='；直接替代：'+dispositions[alt['disposition']]
            details+=['','替代比较：'+alt['rationale'],'','检索范围：'+alt['search_scope']]
            if alt['options']:details+=['','对比选项及结果：','',*['- '+option for option in alt['options']]]
            details+=['','比较依据：'+refs_text(alt['evidence_refs'])]
            if alt['reopen_conditions']:details+=['','重新检查条件：'+alt['reopen_conditions']]
        elif claim['alternatives_required']:counter+='；直接替代尚未审阅'
        for s in claim['scenarios']:
            r=s['review']
            excluded='资料 '+refs_text(s['excluded_refs']) if s['excluded_refs'] else '、'.join('上游结论'+str(claim_numbers.get(cid,'（未列出）')) for cid in s['excluded_upstream_claims'])
            if s['withdrawal_kind']!='dependency':excluded=s['label']+'（'+{'same_origin':'同源','shared_bias':'共同偏差，非同源判定'}[s['withdrawal_kind']]+'）：'+excluded
            allowed=s['remaining_refs']
            remaining=(refs_text(r['remaining_refs']) if r['remaining_refs'] else '未引用固定证据') if r else '尚未声明'
            scenario_rows.append(row([f'结论{number}','撤去'+excluded,
                refs_text(allowed) if allowed else '无剩余固定证据',
                changes[r['change']]+'：'+r['conclusion'] if r else '尚未审阅',
                notes([r['rationale'] if r else '尚未声明理由','实际引用：'+remaining])]))
        unresolved=[gap_label(g) for g in snap['gaps'] if g.endswith(':'+claim['record_id']) or ':'+claim['record_id']+':' in g]
        lines.append(row([f"结论{number}：{claim['text']}",notes(deps) or '未登记核心依据',counter,
            notes(dict.fromkeys([claim['uncertainty'] or '未声明',*unresolved]))]))
    if not snap['claims']:lines.append('| 未登记关键结论 | 无法核对 | 尚未审阅 | 不能视为全部通过 |')
    lines+=['','## 撤证情景','',
        '每行撤去一组核心依据、已声明同源组或共同偏差类别。“仍可用资料”是允许引用的范围；“实际引用”是本次判断真正使用的资料，两者不等于结论已经得到支持。','',
        '| 对应结论 | 撤去资料 | 仍可用资料 | 撤后判断 | 判断理由与实际引用 |',
        '| --- | --- | --- | --- | --- |',
        *(scenario_rows or ['| 暂无 | 没有可审的核心依赖 | 无 | 尚未审阅 | 不能视为通过 |']),
        '','## 适用范围与替代比较',*details]
    if snap['gaps']:
        lines+=['','仍有审阅缺口：'+'；'.join(dict.fromkeys(gap_label(g) for g in snap['gaps']))+'。不据此自动宣布饱和。']
    synthetic_numbers=[str(r['number']) for r in references.values() if r['synthetic']]
    if synthetic_numbers:
        lines[5:5]=['**示例说明：合成数据，非真实市场证据。合成资料编号：'+
            '、'.join('资料'+number for number in synthetic_numbers)+'。**','']
    lines+=['','## 资料附录','', '编号指向固定原文摘录；版本与字符范围供核验使用。']
    for (version,start,end),ref in references.items():
        lines+=['',f"### 资料{ref['number']}",'',ref['title'],'',
            '> '+ref['excerpt'].replace('\r','').replace('\n','\n> '),'',
            f'固定版本：`{version}`；字符范围：{start}–{end}（左闭右开）。']
    lines+=['','审阅快照：`'+snap['baseline']+'`。上述记录由 Agent 声明；来源与结论可能仍存在语义错误。','']
    from kr_core.source_urls import public_document_text
    report=content.rstrip()+'\n'+'\n'.join(lines)
    projected=public_document_text(report)
    transforms=[]
    if projected!=report:
        transforms=['protected_source_urls_redacted']
        projected+='\n链接投影已移除临时访问参数；原固定资料及定位未改写。\n'
    versions={r['version_id'] for c in snap['claims'] for d in c['dependencies'] for r in d['evidence_refs']}
    versions.update(r['version_id'] for c in snap['claims'] for r in c['counterevidence_refs'])
    return projected,{'baseline':snap['baseline'],'body_sha256':body_hash,'full_text_review':'recorded' if reviews else 'unreviewed_or_stale','gaps':snap['gaps'],
        'projection_transforms':transforms,'dependency_versions':sorted(versions)}


def gap_label(gap):
    labels={'task_purpose_unknown':'任务目的尚未声明','critical_claims_unreviewed':'关键结论尚未登记',
        'core_support_missing':'缺少可审的核心依据','claim_scope_changed':'适用范围改变，需复核',
        'upstream_claim_changed':'上游结论或范围改变，需复核','evidence_unavailable':'原依据当前不可用',
        'source_version_changed':'来源出现更新版本，需复核','withdrawal_unreviewed':'撤证情景尚未审阅',
        'alternatives_unreviewed':'直接替代尚未审阅','alternatives_blocked':'直接替代比较受阻',
        'alternatives_deferred':'直接替代比较推迟'}
    labels.update(counterevidence_unreviewed='反证强度及影响尚未审阅',counterevidence_overturns='已被所登记反证推翻，不得按原结论使用',task_alternatives_unreviewed='任务决策的直接替代尚未处置',task_alternatives_blocked='任务替代比较受阻',task_alternatives_deferred='任务替代比较推迟',semantic_support_unreviewed='语义支持尚未审阅',semantic_support_insufficient='语义依据不足')
    return labels.get(gap.split(':')[0],'存在未处置的审阅事项')
