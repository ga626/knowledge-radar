"""统一评测入口的维护适配；资产与试次沿用现有 Ledger。"""
from pathlib import Path
import hashlib
import json
import os
import uuid

from runtime.knowledge import guarded_path
from runtime.evaluation_maintenance import Maintenance, digest, now
from runtime.evaluation_quality import evaluate_trial
from .core import write_new


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
    assessment = evaluate_trial(payload)
    case_id = payload['case_id']
    cards = manager.view()['cases']
    card = next((r for r in cards if r['case_id']==case_id), None)
    if not card or card['revision'] != payload['identity']['case_revision']:
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
    run = ledger.save({'schema':'knowledgeradar-evaluation/v1','run_id':run_id,'started_at':now(),'finished_at':now(),
                      'target':{'kind':'research_quality_trial','source_commit':payload['identity']['source_commit']},
                      'results':[result],'counts':{assessment['verdict']:1},'quality_trial':payload,'quality_assessment':assessment})
    publish_quality(ledger)
    return run


def publish_quality(ledger):
    rows = [{'run_id':r['run_id'],'at':r['finished_at'],'case_id':r['quality_trial']['case_id'],
             'identity':r['quality_trial']['identity'],'assessment':r['quality_assessment']}
            for r in ledger.history() if r.get('quality_trial')]
    value = {'schema':'kr-evaluation-quality-summary/v1','generated_at':now(),'trials':rows[-200:],
             'total_trials':len(rows),'truncated':len(rows)>200,'boundary':'historical_agent_judgments_not_current_install_acceptance'}
    payload = {**value,'integrity':digest(value)}
    temp = ledger.path/('.quality-summary-'+uuid.uuid4().hex)
    write_new(temp,payload)
    os.replace(temp,ledger.path/'quality-summary.json')
    return payload
