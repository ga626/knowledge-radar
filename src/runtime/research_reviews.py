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
    dependencies: list[Dependency] = Field(default_factory=list,max_length=40)
    counterevidence_refs: list[Locator] = Field(default_factory=list,max_length=40)


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


REVIEW_TYPES=(PurposeWrite,ClaimWrite,AlternativesWrite,WithdrawalWrite,ReportReviewWrite,LocalObservationWrite)


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
    baseline=digest({'purpose':purpose,'claims':claims,'questions':questions,'freshness':freshness})
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
            signatures[cid]=digest([c,questions[c['question_id']],freshness[cid],upstream])
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
        all_versions={r['version_id'] for d in core for r in d['evidence_refs']}
        cb=signature(claim)
        scenarios=[]
        for dep in core:
            excluded=sorted({r['version_id'] for r in dep['evidence_refs']})
            scenario={'group_id':dep['group_id'],'scenario_id':'withdraw-'+digest([cid,dep['group_id']])[:24],
                'baseline':cb,'excluded_versions':excluded,'remaining_versions':sorted(all_versions-set(excluded)),
                'excluded_upstream_claim':dep.get('upstream_claim_id','')}
            matches=[r for r in records if r['_kind']=='withdrawal_review' and r['scenario_id']==scenario['scenario_id'] and r['baseline']==cb]
            scenario['review']=matches[-1] if matches else None
            if not matches:gaps.append('withdrawal_unreviewed:'+cid+':'+dep['group_id'])
            scenarios.append(scenario)
        alternatives=[r for r in records if r['_kind']=='alternatives_review' and r['claim_id']==cid and r['claim_revision']==claim['revision'] and r.get('purpose_revision',0)==(purpose or {}).get('revision',0) and r.get('claim_baseline')==cb]
        task_alts=[r for r in records if r['_kind']=='alternatives_review' and not r['claim_id'] and r.get('purpose_revision',0)==(purpose or {}).get('revision',0) and r.get('claim_baseline')==baseline]
        alt=alternatives[-1] if alternatives else task_alts[-1] if task_alts else None
        needed=decision or claim['claim_type'] in {'recommendation','comparison','novelty'}
        if needed and not alt:gaps.append('alternatives_unreviewed')
        if alt and alt['disposition'] in {'blocked','deferred'}:gaps.append('alternatives_'+alt['disposition']+':'+cid)
        output.append({**claim,'baseline':cb,'scenarios':scenarios,'alternatives':({**alt,'absence_proven':False} if alt else None),'alternatives_required':needed})
    if decision and not claims:gaps.append('alternatives_unreviewed')
    # 审阅事件变化也使全文审阅快照失效；原始执行回执不会无故使其失效。
    review_baseline=digest([baseline,[r for r in records if r['_kind'] in {'alternatives_review','withdrawal_review'}]])
    return {'baseline':review_baseline,'task_baseline':baseline,'claims':output,'purpose':purpose,'gaps':sorted(set(gaps)),
        'review_status':'gaps' if gaps else 'recorded','semantic_truth':'not_program_verified','records':records}


def validate(index,conn,req,value,assets):
    tid=req.task_id
    if isinstance(req,PurposeWrite):
        if req.record_id!='purpose':raise ValueError('single_task_purpose_required')
    if isinstance(req,ClaimWrite):
        question=index.question_at(conn,tid,req.question_id)
        if not question:raise ValueError('unknown_question')
        value['question_revision']=question.get('revision',1)
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
            allowed=[r for d in claim['dependencies'] if d['role']!='background' for r in d['evidence_refs'] if r['version_id'] in scenario['remaining_versions']]
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
        repository.reference_many([ref['version_id']],tid,task_id=tid)
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


def report_appendix(index,task_id,content,assets=None):
    with index.connection() as conn:snap=state(index,conn,task_id,assets)
    body_hash=hashlib.sha256(content.encode()).hexdigest()
    reviews=[r for r in snap['records'] if r['_kind']=='report_review' and r['report_sha256']==body_hash and r['baseline']==snap['baseline']]
    def cell(text):return str(text).replace('|',r'\|').replace('\r','').replace('\n','<br>')
    def refs_text(refs):
        parts=[]
        for ref in refs:
            try:
                from kr_core.evidence_assets import EvidenceAssets
                fetched=(assets or EvidenceAssets(read_only=True)).fetch(ref['version_id'],start=ref['start'],max_chars=min(120,ref['end']-ref['start']))
                parts.append(f"{fetched['asset']['title'] or '固定原文'}：{fetched['text']}（kr:{ref['version_id']}#chars={ref['start']}-{ref['end']}）")
            except (ValueError,OSError):parts.append('原依据当前不可用')
        return '；'.join(parts) or '未登记'
    lines=['','---','','## 关键结论与撤证审阅','',
        '全文关键结论核对：'+('已记录' if reviews else '未审阅或已失效')+'。',
        '语义正确性：由 Codex 声明，程序未判真。带缺口交付不等于审阅完成。','',
        '| 关键结论 | 核心依据 | 最强反证／直接替代 | 撤除后的判断与理由 | 尚未解决事项 |',
        '| --- | --- | --- | --- | --- |']
    changes={'changed':'改变','unchanged':'保留','weakened':'降级','reversed':'反转','unknown':'未知'}
    dispositions={'compared':'已比较','reused':'复用旧比较','not_found':'在所查范围未确认，不能证明不存在','blocked':'比较受阻','deferred':'比较推迟','not_applicable':'声明不适用'}
    for claim in snap['claims']:
        deps=[]
        for dep in claim['dependencies']:
            if dep['role']=='background':continue
            deps.append((refs_text(dep['evidence_refs']) if dep['evidence_refs'] else '依赖上游结论 '+dep['upstream_claim_id'])+'；作用：'+dep['basis'])
        alt=claim['alternatives'];counter=refs_text(claim['counterevidence_refs'])
        if alt:counter+='；直接替代：'+dispositions[alt['disposition']]+'；'+alt['rationale']+'；范围：'+alt['search_scope']
        elif claim['alternatives_required']:counter+='；直接替代尚未审阅'
        outcomes=[]
        for s in claim['scenarios']:
            r=s['review'];dep=next(d for d in claim['dependencies'] if d['group_id']==s['group_id'])
            excluded=refs_text(dep['evidence_refs']) if dep['evidence_refs'] else '上游结论 '+dep['upstream_claim_id']
            remaining=refs_text(r['remaining_refs']) if r else '尚未声明'
            outcomes.append('撤去 '+excluded+' → '+(changes[r['change']]+'：'+r['conclusion']+'；理由：'+r['rationale']+'；撤后实际引用：'+remaining if r else '尚未审阅'))
        unresolved=[gap_label(g) for g in snap['gaps'] if g.endswith(':'+claim['record_id']) or ':'+claim['record_id']+':' in g]
        unresolved.append('语义判断理由：'+claim['basis'])
        lines.append('| '+' | '.join(map(cell,[claim['text']+'；适用范围：'+claim['applicability'],'；'.join(deps) or '未登记核心依据',counter,'<br>'.join(outcomes) or '没有可审的核心依赖','；'.join([claim['uncertainty'] or '未声明',*unresolved])]))+' |')
    if not snap['claims']:lines.append('| 未登记关键结论 | 无法核对 | 尚未审阅 | 尚未审阅 | 不能视为全部通过 |')
    if snap['gaps']:
        lines+=['','仍有审阅缺口：'+'；'.join(dict.fromkeys(gap_label(g) for g in snap['gaps']))+'。不据此自动宣布饱和。']
    lines+=['','审阅快照：`'+snap['baseline']+'`。上述记录由 Agent 声明；来源与结论可能仍存在语义错误。','']
    return content.rstrip()+'\n'+'\n'.join(lines),{'baseline':snap['baseline'],'body_sha256':body_hash,'full_text_review':'recorded' if reviews else 'unreviewed_or_stale','gaps':snap['gaps']}


def gap_label(gap):
    labels={'task_purpose_unknown':'任务目的尚未声明','critical_claims_unreviewed':'关键结论尚未登记',
        'core_support_missing':'缺少可审的核心依据','claim_scope_changed':'适用范围改变，需复核',
        'upstream_claim_changed':'上游结论或范围改变，需复核','evidence_unavailable':'原依据当前不可用',
        'source_version_changed':'来源出现更新版本，需复核','withdrawal_unreviewed':'撤证情景尚未审阅',
        'alternatives_unreviewed':'直接替代尚未审阅','alternatives_blocked':'直接替代比较受阻',
        'alternatives_deferred':'直接替代比较推迟'}
    labels.update(semantic_support_unreviewed='语义支持尚未审阅',semantic_support_insufficient='语义依据不足')
    return labels.get(gap.split(':')[0],'存在未处置的审阅事项')
