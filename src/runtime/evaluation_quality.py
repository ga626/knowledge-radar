"""质量试次的有效性与可比性；只检合同，不代替 Agent 判断证据的语义。"""
from __future__ import annotations
from .evaluation_maintenance import hashes


def evaluate_trial(value, *, native_run_verified=False):
    identity = value['identity']
    required = {'case_revision', 'rubric', 'judge', 'skill', 'profile', 'source_commit', 'session_id', 'trial_id', 'inputs'}
    if not required <= identity.keys() or any(not identity[k] for k in required):
        raise ValueError('trial_identity_incomplete')
    if identity['profile'] not in {'controlled_fixture', 'cold_replay', 'knowledge_reuse', 'installed_live'}:
        raise ValueError('unknown_trial_profile')
    hashes(identity['inputs']); hashes([value['answer_hash']])
    criteria = value['criteria']
    if not isinstance(criteria, list) or not 0 < len(criteria) <= 100:
        raise ValueError('bounded_criteria_required')
    for row in criteria:
        if row.get('verdict') not in {'PASS','FAIL','UNKNOWN','NOT_APPLICABLE'} or not row.get('name') or not str(row.get('rationale','')).strip():
            raise ValueError('criterion_requires_reason_and_verdict')
        hashes(row.get('evidence'))
    validity, reasons = 'VALID', []
    check = value.get('validity', {})
    if check.get('contamination') not in {'none', 'authorized_reuse'}:
        validity, reasons = 'INVALID', ['contaminated_or_unknown']
    if check.get('contamination') == 'authorized_reuse' and identity['profile'] != 'knowledge_reuse':
        validity, reasons = 'INVALID', ['reuse_outside_profile']
    if not check.get('input_verified') or not check.get('oracle_verified'):
        if validity != 'INVALID': validity = 'UNVERIFIED'
        reasons.append('input_or_oracle_unverified')
    profile = identity['profile']
    # 当前宿主没有可核验的工具访问隔离回执，调用者自填布尔不能提升为冷盲测。
    if profile == 'cold_replay':
        if validity != 'INVALID': validity = 'UNVERIFIED'
        reasons.append('host_access_isolation_not_verified')
    if profile == 'installed_live':
        expected, observed = check.get('expected_artifact'), check.get('observed_artifact')
        if not expected or not observed or expected != observed:
            validity = 'INVALID'; reasons.append('installed_identity_mismatch')
        elif check.get('isolation') != 'native_receipt' or not native_run_verified:
            if validity != 'INVALID': validity = 'UNVERIFIED'
            reasons.append('native_receipt_missing')
    applicable = [r for r in criteria if r['verdict'] != 'NOT_APPLICABLE']
    verdict = 'FAIL' if any(r['verdict']=='FAIL' for r in applicable) else 'INCOMPLETE' if not applicable or any(r['verdict']=='UNKNOWN' for r in applicable) else 'PASS'
    if validity != 'VALID': verdict = 'NOT_SCORED'
    return {'validity':validity,'verdict':verdict,'reasons':reasons,
            'criteria_count':len(criteria),'applicable_count':len(applicable),
            'critical_failures':[r['name'] for r in applicable if r.get('critical') and r['verdict']=='FAIL'],
            'judgment':'agent_declared_requires_calibration', 'profile':profile}


def compare_trials(before, after):
    a,b = before['identity'],after['identity']
    fixed = ('case_revision','rubric','judge','skill','profile','inputs')
    changed = [k for k in fixed if a.get(k) != b.get(k)]
    if before.get('case_id') != after.get('case_id'):
        changed.append('case_id')
    independent = bool(a.get('session_id') and b.get('session_id') and a['session_id'] != b['session_id'])
    return {'comparable':not changed, 'changed_contracts':changed, 'independent':independent,
            'boundary':'paired_anchor_only_no_aggregate_improvement_claim',
            'before':evaluate_trial(before), 'after':evaluate_trial(after)}
