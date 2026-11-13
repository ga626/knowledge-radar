import pytest
import hashlib

from kr_core.evidence_assets import EvidenceAssets
from runtime.knowledge_delivery import Authorize, DeliveryStore
from runtime.research_archive import BindContext, ResearchArchive, SubmitReport
from runtime import research_ledger as ledger


@pytest.mark.parametrize('raw', [b'# Exact\n\n', b'# Exact\r\n\r\n'])
def test_submit_existing_file_preserves_exact_bytes(monkeypatch, tmp_path, raw):
    engine, request, local = _engine(monkeypatch, tmp_path, with_vault=True)
    engine.bind(request)
    path = local / 'report.md'
    path.write_bytes(raw)
    result = engine.submit(SubmitReport(action='submit_report', task_id=request.task_id,
        project_id='project', title='Exact', input_path=str(path), input_sha256=hashlib.sha256(raw).hexdigest()))
    assert result['completion'] == 'complete'
    assert path.read_bytes() == raw
    assert engine.assets.fetch(result['report']['version_id'])['text'].encode('utf-8') == raw
    assert not result['review_projection_stale']
    assert result['current_review']['full_text_review'] == 'not_requested'


def test_recovered_delivery_clears_current_failure_but_retains_history(monkeypatch, tmp_path):
    engine, request, local = _engine(monkeypatch, tmp_path)
    engine.bind(request)
    path = local / 'report.md'; path.write_text('# Existing\n\n', encoding='utf-8')
    first = engine.submit(SubmitReport(action='submit_report', task_id=request.task_id,
        project_id='project', title='Original', content='# Existing\n'))
    assert first['error_code'] == 'destination_modified_preserved'
    version = first['report']['version_id']
    raw = path.read_bytes()
    result = engine.submit(SubmitReport(action='submit_report', task_id=request.task_id,
        project_id='project', title='Exact', report_id='exact-file', input_path=str(path),
        input_sha256=hashlib.sha256(raw).hexdigest(), replace_pending_report_version=version))
    assert result['completion'] == 'complete'
    assert not result.get('failure_kind') and not result['error_code']
    assert 'review' not in result['next_action']
    old = engine._context(request.task_id)['report_history'][0]
    assert old['report']['version_id'] == version and old['error_code'] == 'destination_modified_preserved'
    assert engine.assets.fetch(version)['text'] == '# Existing\n'


def test_file_report_requires_matching_hash_and_explicit_input(monkeypatch, tmp_path):
    engine, request, local = _engine(monkeypatch, tmp_path); engine.bind(request)
    path = local/'report.md'; path.write_bytes(b'# Changed\n')
    with pytest.raises(ValueError, match='input_changed'):
        engine.submit(SubmitReport(action='submit_report', task_id=request.task_id, project_id='project',
            title='Changed', input_path=str(path), input_sha256='0'*64))
    assert not engine._context(request.task_id)['report']
    with pytest.raises(ValueError, match='exactly_one_report_input'):
        SubmitReport(action='submit_report', task_id=request.task_id, project_id='project',
            title='Ambiguous', content='# Two', input_path=str(path), input_sha256='0'*64)


def test_known_project_local_root_cannot_silently_create_topic_project(monkeypatch,tmp_path):
    engine,request,local=_engine(monkeypatch,tmp_path);engine.bind(request)
    ledger.open_task(objective='new topic',budget='fast',considered=[],task_id='topic-task',project_id='new-topic')
    grant=engine.store.authorize(Authorize(action='authorize_destination',approved_root=str(local),project_id='new-topic'))
    other=BindContext(action='bind_context',task_id='topic-task',project_id='new-topic',project_name='Topic',
        task_title='new topic',local_grant_id=grant['grant_id'],local_report_path=str(local/'topic.md'))
    with pytest.raises(ValueError,match='local_root_project_identity_conflict'):
        engine.bind(other)
    assert engine._context('topic-task') is None
    allowed=engine.bind(other.model_copy(update={'allow_shared_local_root':True}))
    assert allowed['project_identity']['source']=='explicit_shared_local_root'
    assert allowed['project_identity']['host_verified'] is False


def _engine(monkeypatch, tmp_path, *, with_vault=False):
    state = tmp_path / "state"
    monkeypatch.setenv("KR_STATE_DIR", str(state))
    monkeypatch.setenv("KR_EVIDENCE_ROOT", str(state / "evidence"))
    assets = EvidenceAssets()
    store = DeliveryStore(state, assets)
    local = tmp_path / "local"
    local.mkdir()
    local_grant = store.authorize(Authorize(action="authorize_destination", approved_root=str(local), project_id="project"))
    vault_grant = ""
    vault = tmp_path / "vault"
    if with_vault:
        vault.mkdir()
        vault_grant = store.authorize(Authorize(action="authorize_destination", approved_root=str(vault), project_id="project"))["grant_id"]
    ledger.open_task(objective="archive", budget="fast", considered=[], task_id="archive-task", project_id="project", project_name="Project")
    engine = ResearchArchive(store)
    request = BindContext(
        action="bind_context",
        task_id="archive-task",
        project_id="project",
        project_name="Project",
        task_title="Archive",
        local_grant_id=local_grant["grant_id"],
        local_report_path=str(local / "report.md"),
        vault_grant_id=vault_grant,
    )
    return engine, request, local


def test_vault_required_cannot_be_bound_without_a_vault_grant(monkeypatch, tmp_path):
    engine, request, _ = _engine(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="vault_grant_required"):
        engine.bind(request.model_copy(update={"vault_required": True}))


def test_local_only_completion_is_distinct_from_vault_delivery(monkeypatch, tmp_path):
    engine, request, local = _engine(monkeypatch, tmp_path)
    bound = engine.bind(request)
    assert bound["vault"]["status"] == "not_configured"
    submitted = engine.submit(SubmitReport(
        action="submit_report",
        task_id="archive-task",
        project_id="project",
        title="Final",
        content="# Final\n\nLocal only",
    ))
    assert submitted["summary"] == "local_only"
    finished = engine.run("archive-task")
    assert finished["summary"] == "local_only"
    assert finished["vault"]["status"] == "not_configured"
    assert (local / "report.md").is_file()


def test_vault_required_completion_reports_delivered(monkeypatch, tmp_path):
    engine, request, _ = _engine(monkeypatch, tmp_path, with_vault=True)
    bound = engine.bind(request.model_copy(update={"vault_required": True}))
    assert bound["vault"]["status"] == "pending"
    engine.submit(SubmitReport(
        action="submit_report",
        task_id="archive-task",
        project_id="project",
        title="Final",
        content="# Final",
    ))
    finished = engine.run("archive-task")
    assert finished["summary"] == "delivered"


def test_orphan_report_is_visible_without_automatic_rebinding(monkeypatch, tmp_path):
    engine, request, local = _engine(monkeypatch, tmp_path)
    orphan = local / "reports" / "orphan.md"
    orphan.parent.mkdir()
    orphan.write_text("# Orphan report\n", encoding="utf-8")
    candidates = engine.orphan_report_candidates()
    assert len(candidates) == 1
    assert candidates[0]["path"] == str(orphan.resolve())
    assert candidates[0]["status"] == "awaiting_explicit_rebind"


def test_known_report_is_not_reported_as_orphan(monkeypatch, tmp_path):
    engine, request, local = _engine(monkeypatch, tmp_path)
    report = local / "reports" / "known.md"
    engine.bind(request.model_copy(update={"local_report_path": str(report)}))
    report.parent.mkdir(exist_ok=True)
    report.write_text("# Known report\n", encoding="utf-8")
    assert engine.orphan_report_candidates() == []


def test_configured_project_cannot_silently_drop_vault_on_next_task(monkeypatch, tmp_path):
    engine, request, local = _engine(monkeypatch, tmp_path, with_vault=True)
    engine.bind(request)
    ledger.open_task(objective="second", budget="fast", considered=[], task_id="second-task", project_id="PROJECT")
    with pytest.raises(ValueError, match="vault_grant_required"):
        engine.bind(request.model_copy(update={"task_id": "second-task", "vault_grant_id": "", "local_report_path": str(local / "second.md")}))


def test_existing_legacy_false_delivery_is_corrected_by_read_only_status(monkeypatch, tmp_path):
    engine, request, local = _engine(monkeypatch, tmp_path)
    engine.bind(request)
    engine.submit(SubmitReport(action="submit_report", task_id="archive-task", project_id="project", title="Final", content="Local"))
    context = engine._context("archive-task")
    context.update(summary="delivered", vault_required=True)
    engine._save(context)
    before = engine.store.db.read_bytes()
    status = engine.status("archive-task")
    assert status["summary"] != "delivered"
    assert status["completion"] == "incomplete"
    assert status["delivery_gap"] == "vault_grant_required"
    assert engine.store.db.read_bytes() == before


def test_closeout_exposes_endpoint_receipt_and_local_only(monkeypatch, tmp_path):
    engine, request, local = _engine(monkeypatch, tmp_path)
    engine.bind(request)
    result = engine.submit(SubmitReport(action="submit_report", task_id="archive-task", project_id="project", title="Final", content="Local"))
    closed = ledger.close_task(task_id="archive-task", ecology_outcomes=[], stop_rationale="done", key_claims=[])
    facts = closed["closeout"]["completion_facts"]
    assert facts["delivery"] == "local_only"
    endpoint = result["delivery_receipt"]
    assert endpoint["project_id"] == "project"
    assert endpoint["local"]["grant_id"] == request.local_grant_id
    assert endpoint["local"]["sha256"]
    assert endpoint["vault"]["status"] == "not_configured"
    assert facts["delivery_receipt"] == endpoint


def test_missing_context_task_is_visible_in_archive_audit(monkeypatch, tmp_path):
    engine, request, local = _engine(monkeypatch, tmp_path, with_vault=True)
    engine.bind(request)
    ledger.open_task(objective="forgot bind", budget="fast", considered=[], task_id="unbound-task", project_id="Project")
    audit = engine.audit()
    row = next(row for row in audit["tasks"] if row["task_id"] == "unbound-task")
    assert row["delivery_gap"] == "archive_context_missing"
    assert audit["pending_count"] >= 1
    assert engine._context("unbound-task") is None


def test_orphan_scan_never_recurses_into_unrelated_directories(monkeypatch, tmp_path):
    engine, request, local = _engine(monkeypatch, tmp_path)
    unrelated = local / "private" / "reports"
    unrelated.mkdir(parents=True)
    (unrelated / "report.md").write_text("Private", encoding="utf-8")
    assert engine.orphan_report_candidates() == []


def test_recent_records_show_unbound_archive_gap(monkeypatch, tmp_path):
    engine, request, local = _engine(monkeypatch, tmp_path)
    from runtime.research_workspace import ResearchRecords
    from onboarding.research_api import user_projection
    records = ResearchRecords(engine.root.parent, assets=engine.assets).recent()
    row = user_projection(records)['tasks'][0]
    assert row['archive']['delivery_gap'] == 'archive_context_missing'
    assert row['archive']['completion'] == 'incomplete'


def test_user_projection_keeps_endpoint_receipts_and_completion_gap():
    from onboarding.research_api import user_projection
    receipt = {'project_id':'project', 'local':{'status':'local_committed','sha256':'a'*64},
               'vault':{'status':'not_configured'}}
    projected=user_projection({'archive':{'completion':'incomplete','delivery_gap':'vault_grant_required','delivery_receipt':receipt},
        'closeout':{'completion_facts':{'delivery':'pending','delivery_gap':'vault_grant_required'}}})
    assert projected['archive']['delivery_receipt'] == receipt
    assert projected['closeout']['completion_facts']['delivery_gap']=='vault_grant_required'


def test_audit_is_pageable_and_never_claims_whole_history(monkeypatch, tmp_path):
    engine, request, local = _engine(monkeypatch, tmp_path)
    ledger.open_task(objective='other',budget='fast',considered=[],task_id='second',project_id='project')
    first = engine.audit(limit=1)
    assert first['truncated'] and first['next_cursor']
    second = engine.audit(limit=1, cursor=first['next_cursor'])
    assert len(second['tasks']) == 1
    assert second['tasks'][0]['task_id'] != first['tasks'][0]['task_id']


def test_legacy_context_reads_canonical_evidence_without_renaming_outputs(monkeypatch, tmp_path):
    engine, request, local = _engine(monkeypatch, tmp_path)
    engine.bind(request)
    context = engine._context('archive-task')
    context['project_id'] = 'Project'
    engine._save(context)
    with engine.store.connection() as conn:
        conn.execute('UPDATE grants SET project=?',('Project',))
    ref = engine.assets.put('https://example.org/one','真实正文',title='Evidence')
    engine.assets.reference_many([ref['version_id']],'archive-task',project_id='project',task_id='archive-task')
    before = context['vault_relative']
    assert engine._content_watermark(context) > 0
    engine.bind(request)
    assert engine._context('archive-task')['vault_relative'] == before
    assert engine._context('archive-task')['project_id'] == 'Project'


def test_new_task_reuses_legacy_project_directory_owner(monkeypatch, tmp_path):
    engine, request, local = _engine(monkeypatch, tmp_path, with_vault=True)
    engine.bind(request)
    context=engine._context('archive-task')
    vault=engine._grant(request.vault_grant_id, request.project_id)
    for role,tail in [('project',[]),('year',[context['directory_year']])]:
        before=engine._owner_key(vault,[role,'project',*tail])
        after=engine._owner_key(vault,[role,'Project',*tail])
        with engine._connection() as conn:
            conn.execute('UPDATE archive_display_names SET owner=? WHERE owner=?',(after,before))
    context['project_id']='Project'
    engine._save(context)
    ledger.open_task(objective='next',budget='fast',considered=[],task_id='next',project_id='PROJECT')
    next_request=request.model_copy(update={'task_id':'next','task_title':'Next','local_report_path':str(local/'next.md')})
    result=engine.bind(next_request)
    assert result['project_id']=='project'
    assert engine._context('next')['vault_relative'].split('\\')[0]==context['vault_relative'].split('\\')[0]
    assert len(list(vault.iterdir()))==1


def test_project_alias_published_while_waiting_for_display_lock_is_reused(monkeypatch,tmp_path):
    from contextlib import contextmanager
    from runtime import research_archive
    engine,request,_=_engine(monkeypatch,tmp_path,with_vault=True)
    engine.bind(request)
    context=engine._context('archive-task');context['project_id']='Project';engine._save(context)
    vault=engine._grant(request.vault_grant_id,request.project_id)
    original_lock=research_archive.file_write_lock
    canonical=engine._owner_key(vault,['project','project'])
    legacy=engine._owner_key(vault,['project','Project'])
    @contextmanager
    def publish_before_enter(path):
        with original_lock(path):
            # Another serialized writer publishes its legacy owner before this
            # caller acquires the display lock. Recheck the alias after waiting.
            with engine._connection() as conn:
                conn.execute('UPDATE archive_display_names SET owner=? WHERE owner=?',(legacy,canonical))
            yield
    monkeypatch.setattr(research_archive,'file_write_lock',publish_before_enter)
    result=engine._display_path(vault,vault,['project','project'],'Project',directory=True)
    assert result.is_dir()
    assert len(list(vault.iterdir()))==1


def test_legacy_unconfigured_task_in_vault_project_is_not_reported_delivered(monkeypatch,tmp_path):
    engine, request, local = _engine(monkeypatch,tmp_path,with_vault=True)
    engine.bind(request)
    ledger.open_task(objective='legacy',budget='fast',considered=[],task_id='legacy',project_id='PROJECT')
    engine.bind(request.model_copy(update={'task_id':'legacy','vault_grant_id':'','vault_required':False,
        'local_report_path':str(local/'legacy.md')}))
    engine.submit(SubmitReport(action='submit_report',task_id='legacy',project_id='project',title='Legacy',content='Legacy'))
    context=engine._context('legacy');context.pop('vault_required');context['summary']='delivered';engine._save(context)
    before=engine.store.db.read_bytes()
    result=engine.status('legacy')
    assert result['delivery_gap']=='vault_grant_required'
    assert result['completion']=='incomplete'
    assert engine.store.db.read_bytes()==before


def test_orphan_scan_pagination_is_read_only_and_continues(monkeypatch,tmp_path):
    engine, request, local = _engine(monkeypatch,tmp_path)
    directory=local/'reports';directory.mkdir()
    for i in range(3):(directory/f'{i}.md').write_text('Report',encoding='utf-8')
    before=engine.store.db.read_bytes()
    first=engine.orphan_report_candidates(limit=1,details=True)
    second=engine.orphan_report_candidates(limit=1,details=True,cursor=first['next_cursor'])
    assert first['truncated'] and second['candidates'][0]['path']!=first['candidates'][0]['path']
    assert engine.store.db.read_bytes()==before


@pytest.mark.parametrize('source_links',[False,True])
def test_explicit_maintenance_accepts_legacy_project_and_owner_identity(monkeypatch,tmp_path,source_links):
    from runtime import archive_layout
    from runtime.research_archive import PlanArchiveLayout,ApplyArchiveLayout
    engine,request,_=_engine(monkeypatch,tmp_path,with_vault=True)
    engine.bind(request);context=engine._context('archive-task')
    vault=engine._grant(request.vault_grant_id,request.project_id)
    for role,tail in [('project',[]),('year',[context['directory_year']]),('task',['archive-task'])]:
        before=engine._owner_key(vault,[role,'project',*tail]);after=engine._owner_key(vault,[role,'Project',*tail])
        with engine._connection() as conn:
            conn.execute('UPDATE archive_display_names SET owner=? WHERE owner=?',(after,before))
    context['project_id']='Project';engine._save(context)
    plan=archive_layout.plan_source_links if source_links else archive_layout.plan
    apply=archive_layout.apply_source_links if source_links else archive_layout.apply
    candidate=plan(engine,PlanArchiveLayout(action='plan_archive_layout',task_id='archive-task',project_id='PROJECT'))
    result=apply(engine,ApplyArchiveLayout(action='apply_archive_layout',task_id='archive-task',project_id='project',plan_id=candidate['plan_id']))
    assert result['status']=='committed'
    assert engine._context('archive-task')['project_id']=='Project'


def test_backup_captures_legacy_project_user_versions(monkeypatch,tmp_path):
    import json
    from runtime.research_backup import _capture_user_versions
    engine,request,local=_engine(monkeypatch,tmp_path)
    engine.bind(request)
    engine.submit(SubmitReport(action='submit_report',task_id='archive-task',project_id='project',title='Report',content='固定报告'))
    context=engine._context('archive-task');context['project_id']='Project';engine._save(context)
    (local/'report.md').write_text('用户编辑后的版本',encoding='utf-8')
    stage=tmp_path/'backup';stage.mkdir()
    _capture_user_versions(engine.store,stage,'project')
    mapping=json.loads((stage/'user-version-map.json').read_text(encoding='utf-8'))
    assert mapping and (stage/mapping[0]['snapshot_file']).read_text(encoding='utf-8')=='用户编辑后的版本'
