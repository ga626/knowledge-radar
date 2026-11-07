import hashlib
import json
import pytest
from runtime.research_ledger import open_task, record_tool_receipt, close_task
from runtime.research_workspace import ResearchRecords, research_action
from kr_core.evidence_assets import EvidenceAssets


def test_inspect_supports_documented_payload_identity_and_rejects_conflicts(tmp_path, monkeypatch):
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path))
    task = open_task(objective='payload identity', budget='balanced', considered=[])
    records = ResearchRecords()
    tid = task['research_task_id']
    assert research_action(records, 'inspect', payload={'task_id': tid})['research_task_id'] == tid
    with pytest.raises(ValueError, match='identity_conflict'):
        research_action(records, 'inspect', tid, payload={'task_id': 'kr-research-other'})


def test_source_metrics_exclude_route_planning_and_administrative_tools():
    from runtime.research_workspace import task_metrics
    metrics = task_metrics({'events': [{'kind': 'route_opened', 'source_ecology': 'unclassified'}],
        'tool_receipts': [{'tool': 'kr_research', 'source_ecology': 'research_route'},
            {'tool': 'finalize_research_task', 'source_ecology': 'unclassified'},
            {'tool': 'extract_web_page', 'source_ecology': ''},
            {'tool': 'record_research_candidates', 'source_ecology': 'youtube_video_ecology'}]})
    assert metrics['source_ecologies'] == ['generic_web_ecology', 'youtube_video_ecology']


def test_workspace_uses_explicit_question_status_without_rewriting_ledger(tmp_path, monkeypatch):
    from runtime.research_index import ResearchIndex, ResearchWrite
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path))
    task = open_task(objective='question status', budget='balanced', considered=[])
    tid = task['research_task_id']
    path = tmp_path / 'research_tasks' / (tid + '.json')
    before = path.read_bytes()
    index = ResearchIndex()
    index.annotate(ResearchWrite(action='question', task_id=tid, question_id='initial', text='question status', closed=True))
    snapshot = ResearchRecords().snapshot(tid)
    assert snapshot['claim_gaps'] == [{'claim_gap_id': 'initial', 'reason': 'question status', 'status': 'closed', 'origin': 'research_index'}]
    assert path.read_bytes() == before
    index.annotate(ResearchWrite(action='question', task_id=tid, question_id='next', text='new unresolved question'))
    assert any(g['status'] == 'open' for g in ResearchRecords().snapshot(tid)['claim_gaps'])


def test_files_include_explicit_report_reuse_without_unrelated_assets(tmp_path, monkeypatch):
    from runtime.knowledge import register_report
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path))
    task = open_task(objective='reuse', budget='balanced', considered=[])
    tid = task['research_task_id']
    assets = EvidenceAssets(tmp_path / 'assets')
    reused = assets.put('https://example.org/reused', 'old source text', kind='content', title='old')
    unrelated = assets.put('https://example.org/unrelated', 'unrelated source', kind='content')
    report = tmp_path / 'report.md'; report.write_text('summary', encoding='utf-8')
    register_report(assets=assets, input_path=str(report), approved_input_root=str(tmp_path), input_sha256=hashlib.sha256(report.read_bytes()).hexdigest(), project_id='project', task_id=tid, report_id='report', title='report', derived_from=[reused['version_id']])
    files = ResearchRecords(assets=assets).files(tid)['files']
    versions = {r.get('version_id') for r in files}
    assert reused['version_id'] in versions
    assert unrelated['version_id'] not in versions


def test_query_family_enum_is_public_and_invalid_input_is_rejected_before_execution():
    from runtime import composition
    from runtime.application import OPERATIONS
    from runtime.research_queries import query_families
    from pydantic import ValidationError
    op = OPERATIONS['kr_research']
    schema = op.card()['input_schema']
    assert schema['$defs']['ResearchQuery']['properties']['intent']['enum'] == [
        'discovery', 'counterevidence', 'implementation', 'experience', 'citation_follow', 'gap_follow', 'pagination']
    with pytest.raises(ValidationError):
        op.validate({'task': '查询', 'queries': [{'query': 'example', 'intent': '自然语言描述'}]})
    parsed = op.validate({'task': '查询', 'queries': [{'query': 'example', 'intent': 'counterevidence'}]})
    assert query_families('查询', parsed['queries'])[0]['intent'] == 'counterevidence'


def test_projection_reads_original_ledger_and_does_not_create_files(tmp_path, monkeypatch):
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path))
    view = ResearchRecords()
    assert view.recent()['tasks'] == []
    assert not list(tmp_path.iterdir())
    task = open_task(objective='在Codex对话中调研', budget='balanced', considered=[])
    path = tmp_path / 'research_tasks' / (task['research_task_id'] + '.json')
    before = path.read_bytes()
    assert view.recent()['tasks'][0]['research_task_id'] == task['research_task_id']
    assert view.snapshot(task['research_task_id']) == task
    assert path.read_bytes() == before
    assert not (tmp_path / 'tasks.sqlite3').exists()
    assert not (tmp_path / 'evidence_assets').exists()


@pytest.mark.skipif(__import__('os').name!='nt',reason='Windows file sharing contract')
@pytest.mark.parametrize('reader',['workspace','ledger','archive'])
def test_ledger_readers_share_delete_during_atomic_replacement(tmp_path,monkeypatch,reader):
    import ctypes
    from ctypes import wintypes
    import json
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path))
    task_id='kr-research-shared-read'
    path=tmp_path/'research_tasks'/(task_id+'.json')
    path.parent.mkdir()
    payload={'research_task_id':task_id,'status':'open','tool_receipts':[]}
    original=json.dumps(payload).encode('utf-8');path.write_bytes(original)
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    api.CreateFileW.restype=wintypes.HANDLE
    api.CloseHandle.argtypes=[wintypes.HANDLE]
    # A replacement can hold DELETE access while readers must share it. This
    # reproduces the Windows race deterministically, without scheduling sleeps.
    handle=api.CreateFileW(str(path),0x00010000,7,None,3,0,None)
    assert handle!=wintypes.HANDLE(-1).value,ctypes.get_last_error()
    try:
        if reader=='workspace':
            actual=ResearchRecords(tmp_path).snapshot(task_id)
        elif reader=='ledger':
            from runtime.research_ledger import read_task
            actual=read_task(task_id=task_id)
        else:
            from runtime.research_archive import ResearchArchive
            from types import SimpleNamespace
            actual=ResearchArchive._ledger(SimpleNamespace(root=tmp_path/'knowledge_delivery'),task_id)
        assert actual==payload
    finally:
        api.CloseHandle(handle)
    assert path.read_bytes()==original


@pytest.mark.parametrize('action', ['create','claim','heartbeat','handoff','cancel','event','reserve','settle','finish','review','request_expansion','evidence_withdraw'])
def test_projection_rejects_second_lifecycle(tmp_path, action):
    with pytest.raises(ValueError, match='read_only'):
        research_action(ResearchRecords(tmp_path), action)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('task_id', ['../secret','research-old','kr-research-../../secret','kr-research-'+'a'*100])
def test_projection_rejects_invalid_identity(tmp_path, task_id):
    with pytest.raises(ValueError, match='invalid_research_task_id'):
        ResearchRecords(tmp_path).snapshot(task_id)


def test_projection_marks_unreadable_record_instead_of_synthesizing(tmp_path):
    directory = tmp_path / 'research_tasks'; directory.mkdir()
    (directory / 'kr-research-broken.json').write_text('{', encoding='utf-8')
    result = ResearchRecords(tmp_path).recent()
    assert result['tasks'] == [] and result['unreadable_records'] == 1


def test_readonly_assets_fetch_search_and_no_schema_or_write(tmp_path):
    root = tmp_path / 'assets'
    readonly = EvidenceAssets(root, read_only=True)
    assert readonly.search() == [] and not root.exists()
    assets = EvidenceAssets(root)
    ref = assets.put('https://example.org/original', '完整原文🙂末尾')
    before = hashlib.sha256((root / 'index.sqlite3').read_bytes()).hexdigest()
    assert readonly.search('末尾')[0]['version_id'] == ref['version_id']
    assert readonly.fetch(ref['version_id'])['text'] == '完整原文🙂末尾'
    with pytest.raises(ValueError, match='read_only'):
        readonly.put('https://example.org/no', '不能写入')
    with readonly.connection() as connection:
        with pytest.raises(Exception, match='readonly'):
            connection.execute('DELETE FROM assets')
    assert hashlib.sha256((root / 'index.sqlite3').read_bytes()).hexdigest() == before


def test_original_finalizer_cannot_accept_unsupported_claims(tmp_path, monkeypatch):
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path))
    task = open_task(objective='研究原始完成门槛', budget='deep', considered=[{'source_ecology':'web'}])
    result = close_task(task_id=task['research_task_id'], ecology_outcomes=[{'source_ecology':'web','outcome':'used'}], stop_rationale='继续核验', key_claims=[{'id':'important','importance':'critical','supporting_evidence_ids':['invented']}], quality_status='pass')
    assert result['status'] == 'recorded_with_gaps'
    assert result['closeout']['critical_claims_without_receipts'] == ['important']
    assert not result['closeout']['quality_receipt_valid']


def test_readable_projection_groups_rounds_metrics_and_files(tmp_path, monkeypatch):
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path / 'state'))
    evidence_root = tmp_path / 'assets'
    assets = EvidenceAssets(evidence_root)
    report = tmp_path / 'report.md'; report.write_text('# report', encoding='utf-8')
    sidecar = tmp_path / 'report.evidence.json'; sidecar.write_text('{}', encoding='utf-8')
    task = open_task(objective='按任务查看调研记录', budget='balanced', considered=[{'source_ecology': 'web'}])
    ref = assets.put('https://example.org/source', '固定证据正文', title='source')
    receipt = record_tool_receipt(task_id=task['research_task_id'], trace_id='trace-route', tool='kr_research', status='ok', elapsed_s=0.25)['receipt']
    detail = record_tool_receipt(task_id=task['research_task_id'], trace_id='trace-detail', tool='get_content_detail', status='partial', result={'evidence_assets': [ref]}, elapsed_s=1.75)['receipt']
    quality = tmp_path / 'quality.json'
    quality.write_text(json.dumps({'status': 'pass', 'report_path': str(report), 'evidence_path': str(sidecar)}), encoding='utf-8')
    close_task(task_id=task['research_task_id'], ecology_outcomes=[{'source_ecology': 'web', 'outcome': 'used', 'receipt_ids': [detail['receipt_id']]}], stop_rationale='完成检查', key_claims=[], quality_status='pass', report_path=str(report), evidence_path=str(sidecar), quality_receipt_path=str(quality))
    ledger_path = tmp_path / 'state' / 'research_tasks' / (task['research_task_id'] + '.json')
    legacy = json.loads(ledger_path.read_text(encoding='utf-8'))
    legacy['closeout'].pop('report_path', None); legacy['closeout'].pop('evidence_path', None)
    ledger_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding='utf-8')
    records = ResearchRecords(tmp_path / 'state', assets=EvidenceAssets(evidence_root, read_only=True))
    inspected = research_action(records, 'inspect', task['research_task_id'])
    assert inspected['readable_summary']['definition'].startswith('一次明确')
    assert [round['label'] for round in inspected['rounds']] == ['首轮发现', '详情与身份核验', '完成检查']
    assert inspected['metrics']['tool_calls'] == 2
    assert inspected['metrics']['successful_calls'] == 1
    assert inspected['metrics']['partial_calls'] == 1
    assert inspected['metrics']['total_elapsed_s'] == 2.0
    assert inspected['metrics']['evidence_version_count'] == 1
    types = {item['type'] for item in inspected['files']}
    assert {'report', 'evidence_sidecar', 'quality_receipt', 'tool_receipts', 'fixed_evidence'} <= types
    files = research_action(records, 'files')
    assert any(item['type'] == 'fixed_evidence' and item['status'] == 'available' for item in files['files'])
    assert not (tmp_path / 'state' / 'tasks.sqlite3').exists()


def test_pending_submission_is_visible_as_safe_repair_state(tmp_path, monkeypatch):
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path / 'state'))
    task = open_task(objective='查看回执补偿状态', budget='fast', considered=[])
    ledger_path = tmp_path / 'state' / 'research_tasks' / (task['research_task_id'] + '.json')
    payload = json.loads(ledger_path.read_text(encoding='utf-8'))
    payload['pending_submissions'] = [{
        'trace_id': 'trace-pending', 'tool': 'search_web', 'status': 'ok',
        'failure_code': 'recording_failed', 'source_ecology': 'generic_web_ecology',
        'association': 'explicit_task_scope', 'receipt_kind': 'tool_call',
        'attempts': 2, 'reason': 'PermissionError', 'updated_at': 'now',
        'result': {'url': 'https://example.org/private', 'content': 'must not leak'},
    }]
    ledger_path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    records = ResearchRecords(tmp_path / 'state')
    inspected = research_action(records, 'inspect', task['research_task_id'])
    assert inspected['metrics']['pending_submission_count'] == 1
    assert inspected['metrics']['pending_submission_attempts'] == 2
    public = __import__('onboarding.research_api', fromlist=['user_projection']).user_projection(inspected)
    assert public['pending_submissions'] == [{
        'trace_id': 'trace-pending', 'tool': 'search_web', 'status': 'ok',
        'failure_code': 'recording_failed', 'source_ecology': 'generic_web_ecology',
        'association': 'explicit_task_scope', 'receipt_kind': 'tool_call',
        'attempts': 2, 'reason': 'PermissionError', 'updated_at': 'now',
    }]
    assert 'result' not in public['pending_submissions'][0]


def test_inspect_reports_current_gaps_without_review_events_or_repair(tmp_path, monkeypatch):
    from runtime import research_ledger as ledger
    from onboarding.research_api import workspace_api
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path / 'state'))
    task = open_task(objective='只读缺口辅助', budget='fast', considered=[])
    path = ledger._path(task['research_task_id'])
    payload = json.loads(path.read_text(encoding='utf-8'))
    payload['pending_submissions'] = [{'trace_id': 'pending', 'result': {'content': 'PRIVATE_BODY'}}]
    path.write_text(json.dumps(payload), encoding='utf-8')
    before = {str(item): item.read_bytes() for item in tmp_path.rglob('*') if item.is_file()}

    def forbidden(*args, **kwargs):
        pytest.fail('query must not lock, write, review or repair the research ledger')
    for name in ('_ledger_guard', '_write', 'record_event', 'review_task', 'repair_pending_submissions'):
        monkeypatch.setattr(ledger, name, forbidden)
    records = ResearchRecords(tmp_path / 'state', assets=EvidenceAssets(tmp_path / 'assets', read_only=True))
    for _ in range(2):
        inspected = workspace_api(records, {'action': 'inspect', 'task_id': task['research_task_id']}, developer=False)
        assessment = inspected['gap_assessment']
        assert assessment['read_only'] is True and 'event' not in assessment
        assert assessment['counts']['pending_submission_count'] == 1
        assert {row['code'] for row in assessment['gaps']} >= {'pending_receipt_submissions', 'no_candidates_recorded'}
        assert 'PRIVATE_BODY' not in json.dumps(inspected)
    after = {str(item): item.read_bytes() for item in tmp_path.rglob('*') if item.is_file()}
    assert before == after


def test_gap_projection_drops_unregistered_nested_payload():
    from onboarding.research_api import user_projection
    projected = user_projection({'gap_assessment': {
        'status': 'assessed', 'read_only': True, 'event': {'content': 'PRIVATE_EVENT'},
        'gaps': [{'code': 'candidate_only', 'meaning': '尚未核验', 'url': 'PRIVATE_URL'}],
        'counts': {'candidate_count': 1, 'content': 'PRIVATE_COUNT'},
        'result': {'content': 'PRIVATE_RESULT'},
    }})
    assert projected['gap_assessment'] == {
        'status': 'assessed', 'read_only': True,
        'gaps': [{'code': 'candidate_only', 'meaning': '尚未核验'}],
        'counts': {'candidate_count': 1},
    }
    assert 'PRIVATE' not in json.dumps(projected)


def test_console_rejects_legacy_export_without_modifying_source(tmp_path):
    from onboarding.research_api import workspace_api
    assets=EvidenceAssets(tmp_path/'assets')
    assets.put('https://example.org/readonly','text')
    before=(assets.root/'index.sqlite3').read_bytes()
    records=ResearchRecords(tmp_path,assets=EvidenceAssets(assets.root,read_only=True))
    with pytest.raises(ValueError,match='agent_command_required'):
        workspace_api(records,{'action':'export','export_acknowledged':True})
    assert (assets.root/'index.sqlite3').read_bytes()==before


def test_task_name_survives_continuation_and_legacy_titles_do_not_merge(tmp_path, monkeypatch):
    from runtime.research_workspace import summarize_task, human_task_title
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path))
    task = open_task(objective='Obsidian 两个目录的导入方法', budget='fast', considered=[], display_title='笔记目录导入方法')
    reopened = open_task(objective='续查导入', budget='fast', considered=[], task_id=task['research_task_id'], display_title='另一个名字')
    assert reopened['display_title'] == '笔记目录导入方法'
    assert summarize_task(reopened)['title_source'] == 'codex'
    assert human_task_title('Obsidian 导入方法：比较目录') != human_task_title('Obsidian 删除恢复：核对备份')
    assert human_task_title('核对 Obsidian Vault.create 方法。附加要求') == 'Obsidian Vault.create 方法'
    assert summarize_task({'objective': '原目标', 'display_title': '  '})['title'] == '原目标'
    with pytest.raises(ValueError, match='title_too_long'):
        open_task(objective='目标', budget='fast', considered=[], display_title='名' * 81)


def test_project_name_is_persisted_and_exposed_for_history_grouping(tmp_path, monkeypatch):
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path))
    task = open_task(objective='项目内调研', budget='fast', considered=[], project_name='产品可靠性')
    records = ResearchRecords(tmp_path)
    listed = records.recent()['tasks'][0]
    inspected = research_action(records, 'inspect', task['research_task_id'])
    assert task['project_name'] == '产品可靠性'
    assert listed['project_name'] == '产品可靠性'
    assert inspected['readable_summary']['project_name'] == '产品可靠性'


def test_console_file_reader_is_task_scoped_paginated_and_read_only(tmp_path, monkeypatch):
    from onboarding.research_api import workspace_api
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path / 'state'))
    task = open_task(objective='逐个查看报告', budget='fast', considered=[])
    body = '报告🙂\n' * 3000 + '末尾'
    report = tmp_path / 'report.md'
    report.write_text(body, encoding='utf-8', newline='')
    path = tmp_path / 'state' / 'research_tasks' / (task['research_task_id'] + '.json')
    task['closeout'] = {'report_path': str(report)}
    path.write_text(json.dumps(task, ensure_ascii=False), encoding='utf-8')
    before = path.read_bytes(), report.read_bytes()
    records = ResearchRecords(tmp_path / 'state')
    file = records.files(task['research_task_id'])['files'][0]
    request = {'action': 'file_fetch', 'task_id': task['research_task_id'], 'file_id': file['file_id']}
    first = workspace_api(records, request)
    second = workspace_api(records, {**request, 'start': first['next_start']})
    assert hashlib.sha256((first['text'] + second['text']).encode()).hexdigest() == hashlib.sha256(body.encode()).hexdigest()
    other = open_task(objective='另一个任务', budget='fast', considered=[])
    with pytest.raises(ValueError, match='registered_research_file_required'):
        workspace_api(records, {**request, 'task_id': other['research_task_id']})
    with pytest.raises(ValueError, match='registered_research_file_required'):
        workspace_api(records, {**request, 'file_id': str(tmp_path / 'unregistered.txt')})
    assert (path.read_bytes(), report.read_bytes()) == before
