import pytest
from kr_core.evidence_assets import EvidenceAssets
from kr_core.evidence_locators import transcript_locators
from runtime.tasks import TaskStore


def test_subtitle_mapping_requires_original_text_and_finite_monotonic_time():
    mapped = transcript_locators('中🙂\n反例', [{'text': '中🙂', 'start': 0, 'end': 2}, {'text': '反例', 'start': 3, 'end': 4}])
    assert mapped['status'] == 'aligned'
    assert mapped['segments'][1]['start'] == 3
    assert transcript_locators('中🙂', [{'text': '改写', 'start': 0, 'end': 2}])['status'] == 'unaligned'
    assert transcript_locators('中🙂', [{'text': '中🙂', 'start': float('nan'), 'end': 2}])['status'] == 'unaligned'
    assert transcript_locators('字幕', None)['status'] == 'missing'


def test_query_families_are_agent_supplied_and_unknown_ecology_not_discarded():
    from runtime.research_queries import query_families, query_for_ecology
    families = query_families('中文目标', [{'query':'counterexample','language':'en','intent':'counterevidence','source_ecology':'new_ecology'}])
    assert query_for_ecology(families, 'new_ecology')['query'] == 'counterexample'
    assert not families[0]['translation_verified']
    assert query_families('中文目标')[0]['origin'] == 'agent_draft'


def test_original_finalizer_blocks_pending_and_withdrawn_evidence(tmp_path, monkeypatch):
    from runtime.research_ledger import open_task, record_tool_receipt, close_task
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path/'state'))
    monkeypatch.setenv('KR_EVIDENCE_ROOT',str(tmp_path/'assets'))
    assets=EvidenceAssets()
    store=TaskStore(str(tmp_path/'background.sqlite3'))
    monkeypatch.setattr('runtime.tasks.get_task_store',lambda:store)
    task=open_task(objective='检查真实后台任务',budget='balanced',considered=[{'source_ecology':'web'}])
    ref=assets.put('https://example.org/full','原文反例')
    store.upsert_task(task_id='background-1',task_type='asr',platform='fixture',target='https://example.org/full')
    receipt=record_tool_receipt(task_id=task['research_task_id'],trace_id='trace-1',tool='fixture',status='ok',result={'evidence_assets':[ref],'task':{'task_id':'background-1','status':'completed'}})['receipt']
    kwargs=dict(task_id=task['research_task_id'],ecology_outcomes=[{'source_ecology':'web','outcome':'used','receipt_ids':[receipt['receipt_id']]}],stop_rationale='fixture only',key_claims=[{'id':'claim-1','importance':'high','supporting_evidence_ids':[ref['version_id']],'supporting_receipt_ids':[receipt['receipt_id']]}],quality_status='pass')
    result=close_task(**kwargs)
    assert result['status']=='needs_repair' and result['closeout']['pending_background_tasks']==['background-1']
    store.mark_completed('background-1')
    assert close_task(**kwargs)['status']=='accepted_for_decision'
    assets.withdraw(ref['source_id'])
    result=close_task(**kwargs)
    assert result['status']=='needs_repair' and result['closeout']['unavailable_evidence_versions']==[ref['version_id']]


def test_optional_components_block_before_execution(tmp_path, monkeypatch):
    from runtime.optional_media import COMPONENTS, component_plan, run_optional
    monkeypatch.setenv('KR_OPTIONAL_COMPONENT_RECEIPTS_DIR', str(tmp_path / 'receipts'))
    source = tmp_path / 'source.wav'
    source.write_bytes(b'authorized input')
    for component in COMPONENTS:
        plan = component_plan(component)
        assert plan['status'] == 'not_installed'
        assert not plan['automatic_install'] and not plan['automatic_model_download']
        with pytest.raises(ValueError, match='not_installed_and_approved'):
            run_optional({'component': component, 'input_path': str(source),
                          'approved_input_root': str(tmp_path),
                          'input_sha256': __import__('hashlib').sha256(source.read_bytes()).hexdigest()})
    assert not (tmp_path / 'receipts').exists()


@pytest.mark.parametrize('tool_name,collector', [('extract_web_page', 'collect_url'), ('extract_dynamic_page', 'collect_dynamic_url')])
def test_web_extractors_save_complete_tail(tmp_path, monkeypatch, tool_name, collector):
    import server
    from kr_core.evidence_store import EvidenceStore
    from types import SimpleNamespace
    store = EvidenceStore(str(tmp_path / 'events.jsonl'))
    monkeypatch.setattr(server, 'evidence_store', store)
    # The accessor used by the real record path is patched, not the record function.
    monkeypatch.setattr(server, '_get_evidence_store', lambda: store, raising=False)
    body = '中文🙂\n' * 4000 + '末尾反例必须保存'
    monkeypatch.setattr(server, collector, lambda *a, **kw: SimpleNamespace(to_mcp_dict=lambda: {'content': body, 'title': 'fixture', 'status': 'ok'}))
    response = getattr(server, tool_name)(url='https://example.org/fixture')
    ref = next(r for r in response['evidence_assets'] if r['kind'] == 'content')
    assert store.assets.fetch(ref['version_id'], start=len(body) - 9)['text'] == body[-9:]
