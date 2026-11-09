"""质量试次的有效性与可比性；只检合同，不代替 Agent 判断证据的语义。"""
from __future__ import annotations
from .evaluation_maintenance import hashes


def evaluate_trial(value):
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
        validity, reasons = 'UNVERIFIED', [*reasons,'input_or_oracle_unverified']
    profile = identity['profile']
    if profile == 'cold_replay' and check.get('isolation') != 'enforced_access_receipt':
        if validity != 'INVALID': validity = 'UNVERIFIED'
        reasons.append('prompt_is_not_access_isolation')
    if profile == 'installed_live':
        expected, observed = check.get('expected_artifact'), check.get('observed_artifact')
        if not expected or not observed or expected != observed:
            validity = 'INVALID'; reasons.append('installed_identity_mismatch')
        elif check.get('isolation') != 'native_receipt':
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
    independent = bool(a.get('session_id') and b.get('session_id') and a['session_id'] != b['session_id'])
    return {'comparable':not changed, 'changed_contracts':changed, 'independent':independent,
            'boundary':'paired_anchor_only_no_aggregate_improvement_claim',
            'before':evaluate_trial(before), 'after':evaluate_trial(after)}
