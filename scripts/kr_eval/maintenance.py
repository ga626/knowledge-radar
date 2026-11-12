"""统一评测入口的维护适配；资产与试次沿用现有 Ledger。"""
from pathlib import Path
import hashlib
import json
import os
import uuid
from fnmatch import fnmatchcase

from runtime.knowledge import guarded_path
from runtime.evaluation_maintenance import Maintenance, digest, now
from runtime.evaluation_candidates import _readonly
from contextlib import closing
from runtime.evaluation_quality import evaluate_trial
from .core import write_new


def dependency_identity(card, source_snapshot):
    patterns=card.get('dependencies') or []
    files={name:checksum for name,checksum in source_snapshot['files'].items()
           if any(fnmatchcase(name,p) for p in patterns)}
    return {'patterns':patterns,'files':files,'identity':digest(files)}


def plan_quality(manager, ledger, source_snapshot):
    """受影响建议并入现有plan；不会执行语义评测或放行发布。"""
    cards=[]
    if manager.path.is_file():
        with closing(_readonly(manager.path)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='evaluation_cards'").fetchone():
                cards=[json.loads(r[0]) for r in db.execute('SELECT body FROM evaluation_cards ORDER BY case_id LIMIT 1001')]
    runs=[r for r in ledger.history() if r.get('quality_trial')]
    items=[]
    for card in cards[:1000]:
        if card['lifecycle']!='active': continue
        dep=dependency_identity(card,source_snapshot)
        prior=next((r for r in reversed(runs) if r['quality_trial']['case_id']==card['case_id']
                    and r['quality_trial']['identity']['case_revision']==card['revision']),None)
        reason=('no_matching_trial' if not prior else 'dependency_coverage_unknown' if not dep['patterns'] or not dep['files']
                else 'dependencies_changed' if prior.get('quality_dependency_identity')!=dep
                else 'reevaluation_required' if card.get('reevaluation')
                else 'scheduled_'+card['frequency'] if card['frequency']!='affected' else 'unchanged')
        items.append({'case_id':card['case_id'],'revision':card['revision'],'title':card['title'],
                      'reason':reason,'suggested':reason!='unchanged','runnability':card['runnability'],
                      'dependency_identity':dep,'previous_run':prior and prior['run_id']})
    return {'items':items,'truncated':len(cards)>1000,'execution':'agent_required_no_automatic_platform_calls'}


def import_asset(ledger, source, approved_root, checksum):
    path = guarded_path(str(approved_root), str(source), require_file=True)
    if path.stat().st_size > 2*1024*1024:
        raise ValueError('asset_too_large')
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != checksum:
        raise ValueError('asset_identity_changed')
    data.decode('utf-8-sig')  # 首批仅脱敏文本，不导入账号、Profile或大媒体。
    dest = ledger.path/'assets'/(checksum+'.asset')
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with dest.open('xb') as stream: stream.write(data)
    except FileExistsError:
        if dest.read_bytes() != data: raise ValueError('existing_asset_corrupt')
    return {'status':'registered','sha256':checksum,'bytes':len(data),'boundary':'caller_authorized_sanitized_text'}


def record_trial(ledger, manager, payload):
    native_verified=False
    native_id=payload.get('validity',{}).get('native_run_id')
    if payload['identity'].get('profile')=='installed_live' and native_id:
        native=ledger.read(native_id)
        native_verified=bool(native.get('importer_version',0)>=2 and native.get('import_receipt_sha256')
            and native.get('target',{}).get('artifact_sha256')==payload['validity'].get('expected_artifact')
            and native.get('target',{}).get('source_commit')==payload['identity']['source_commit']
            and any(r.get('verdict')=='PASS' and r.get('evidence_kind')=='bound_external_receipt' for r in native['results']))
    assessment = evaluate_trial(payload,native_run_verified=native_verified)
    case_id = payload['case_id']
    card=None
    if manager.path.is_file():
        with closing(_readonly(manager.path)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='evaluation_cards'").fetchone():
                card=manager._case(db,case_id)
    if payload.get('diagnostic_only'):
        if payload['identity']['profile']!='knowledge_reuse':raise ValueError('retrospective_requires_knowledge_reuse')
        source_root=Path(__file__).resolve().parents[2]
        configured=json.loads((source_root/'config/evaluation/cases.json').read_text(encoding='utf-8'))['cases']
        case=next((c for c in configured if c['case_id']==case_id),None)
        if not case or case['version']!=payload['identity']['case_revision']:raise ValueError('retrospective_configured_case_revision_required')
        # 同上下文回看不是独立金标；保留逐项判断，却绝不计为能力/安装通过。
        assessment.update(validity='UNVERIFIED',verdict='NOT_SCORED',
            reasons=assessment['reasons']+['retrospective_diagnostic_not_calibrated'])
        from runtime.evaluation_maintenance import LAYERS
        layer=payload.get('diagnostic_layer')
        if layer not in LAYERS:raise ValueError('retrospective_diagnostic_layer_required')
        card={'case_id':case_id,'layer':layer,
            'examples':[{'input_hash':v} for v in payload['identity']['inputs']],
            'dependencies':case.get('dependencies',[])}
    elif not card or card['revision'] != payload['identity']['case_revision']:
        raise ValueError('trial_case_revision_changed')
    manager.validate_assets(card)
    for checksum in [*payload['identity']['inputs'], payload['answer_hash'],
                     *(h for r in payload['criteria'] for h in r['evidence'])]:
        path = manager.asset_root/(checksum+'.asset')
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            raise ValueError('trial_asset_missing_or_corrupt')
    if sorted(payload['identity']['inputs']) != sorted({x['input_hash'] for x in card['examples']}):
        raise ValueError('trial_inputs_do_not_match_card')
    for run in ledger.history():
        old = run.get('quality_trial')
        if old and old['identity']['trial_id'] == payload['identity']['trial_id']:
            if old != payload: raise ValueError('trial_idempotency_conflict')
            return run
    run_id,folder = ledger.begin()
    write_new(folder/'quality-trial.json',payload)
    raw = (folder/'quality-trial.json').read_bytes()
    result = {'case_id':case_id,'layer':card['layer'], 'verdict':assessment['verdict'],
              'readiness':'ready' if assessment['validity']=='VALID' else 'unknown',
              'evidence':[{'path':'quality-trial.json','sha256':hashlib.sha256(raw).hexdigest()}],
              'summary':assessment['judgment']}
    from .core import snapshot
    source_root=Path(__file__).resolve().parents[2]
    dependency=dependency_identity(card,snapshot(source_root))
    run = ledger.save({'schema':'knowledgeradar-evaluation/v1','run_id':run_id,'started_at':now(),'finished_at':now(),
                      'target':{'kind':'research_quality_trial','source_commit':payload['identity']['source_commit']},
                      'results':[result],'counts':{assessment['verdict']:1},'quality_trial':payload,'quality_assessment':assessment,
                      'quality_dependency_identity':dependency,'dependency_observation_scope':'recording_checkout_not_native_source_proof'})
    publish_quality(ledger)
    return run


def publish_quality(ledger):
    rows = [{'run_id':r['run_id'],'at':r['finished_at'],'case_id':r['quality_trial']['case_id'],
             'identity':r['quality_trial']['identity'],'assessment':r['quality_assessment'], 'layer':r['results'][0]['layer']}
            for r in ledger.history() if r.get('quality_trial')]
    value = {'schema':'kr-evaluation-quality-summary/v1','generated_at':now(),'trials':rows[-200:],
             'total_trials':len(rows),'truncated':len(rows)>200,'boundary':'historical_agent_judgments_not_current_install_acceptance'}
    payload = {**value,'integrity':digest(value)}
    temp = ledger.path/('.quality-summary-'+uuid.uuid4().hex)
    write_new(temp,payload)
    os.replace(temp,ledger.path/'quality-summary.json')
    return payload
