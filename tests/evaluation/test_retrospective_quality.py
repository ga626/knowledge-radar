"""真实历史评判可先留可查诊断，不能假装独立金标或安装验收。"""
import hashlib
from pathlib import Path
import pytest
from scripts.kr_eval.core import Ledger
from scripts.kr_eval.maintenance import record_trial
from runtime.evaluation_maintenance import Maintenance


def test_existing_case_accepts_unscored_retro_without_creating_an_active_card(tmp_path):
    ledger=Ledger(tmp_path/'evaluation')
    manager=Maintenance(tmp_path/'candidates.sqlite3',asset_root=ledger.path/'assets')
    manager.asset_root.mkdir(parents=True)
    def asset(text):
        checksum=hashlib.sha256(text.encode()).hexdigest()
        (manager.asset_root/(checksum+'.asset')).write_text(text,encoding='utf-8')
        return checksum
    source=asset('历史任务和资料');answer=asset('实际旧报告');reason=asset('审查理由：两份职位无法支持整体人才市场')
    import json
    cases=json.loads((Path(__file__).parents[2]/'config/evaluation/cases.json').read_text(encoding='utf-8'))['cases']
    case=next(c for c in cases if c['case_id']=='knowledge.agent_delivery')
    payload={'case_id':case['case_id'],'diagnostic_only':True,'diagnostic_layer':'delivery',
        'identity':{'case_revision':case['version'],'rubric':'retro-v1','judge':'same-agent',
            'skill':'installed-skill','profile':'knowledge_reuse','source_commit':'old-source',
            'session_id':'same-context','trial_id':'real-retro','inputs':[source]},
        'criteria':[{'name':'结论是否有对应材料','verdict':'FAIL','rationale':'历史材料覆盖不足','evidence':[reason]}],
        'answer_hash':answer,'validity':{'input_verified':True,'oracle_verified':True,'contamination':'authorized_reuse'}}
    run=record_trial(ledger,manager,payload)
    assert run['quality_assessment']['verdict']=='NOT_SCORED'
    assert 'retrospective_diagnostic_not_calibrated' in run['quality_assessment']['reasons']
    assert not manager.path.exists()
    assert record_trial(ledger,manager,payload)['run_id']==run['run_id']
    with pytest.raises(ValueError,match='configured_case'):
        record_trial(ledger,manager,{**payload,'case_id':'not.a.real.case'})


@pytest.mark.parametrize("with_source_run", [False, True])
def test_regression_plan_uses_source_history_not_later_quality_trials(tmp_path, monkeypatch, with_source_run):
    from types import SimpleNamespace
    from scripts.kr_eval import runner, core
    from scripts import kr_test_selector
    history = []
    if with_source_run:
        history.append({'target': {'kind': 'development_source'},
                        'snapshot': {'files': {'src/example.py': 'old'}}})
    history.append({'target': {'kind': 'research_quality_trial'}, 'quality_trial': {}})
    monkeypatch.setattr(core, 'status', lambda *a: {'cases': [], 'inventory': {'issues': []}, 'gate': 'INCOMPLETE'})
    monkeypatch.setattr(runner, 'snapshot', lambda *a: {'files': {'src/example.py': 'new'}})
    selected = []
    monkeypatch.setattr(kr_test_selector, 'select_tests',
                        lambda changes, **kw: selected.extend(changes) or {'selected_tests': []})
    result = runner.plan(tmp_path, {'cases': []}, SimpleNamespace(history=lambda: history))
    assert result['status'] == 'INCOMPLETE'
    assert selected == (['src/example.py'] if with_source_run else [])
