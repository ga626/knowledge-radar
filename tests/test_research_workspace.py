import hashlib
import json
import pytest
from runtime.research_ledger import open_task, record_tool_receipt, close_task
from runtime.research_workspace import ResearchRecords, research_action
from kr_core.evidence_assets import EvidenceAssets


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
    assert result['status'] == 'needs_repair'
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


def test_readonly_console_export_preserves_source_db_and_permission(tmp_path):
    import base64
    import io
    import zipfile
    from onboarding.research_api import workspace_api
    root = tmp_path / 'assets'
    asset = EvidenceAssets(root).put('https://example.org/evidence', '完整原文🙂末尾')
    before = (root / 'index.sqlite3').read_bytes()
    records = ResearchRecords(tmp_path, assets=EvidenceAssets(root, read_only=True))
    payload = {'action':'export', 'version_ids':[asset['version_id']]}
    with pytest.raises(ValueError, match='permission'):
        workspace_api(records, payload)
    exported = workspace_api(records, {**payload,'export_acknowledged':True})
    raw = base64.b64decode(exported['data_base64'])
    assert hashlib.sha256(raw).hexdigest() == exported['sha256']
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['source_reference_registration'] == 'not_registered_read_only_export'
        document = manifest['sources'][0]['documents'][0]['file']
        assert archive.read(document).decode() == '完整原文🙂末尾'
    assert (root / 'index.sqlite3').read_bytes() == before


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
