import shutil
import json
from pathlib import Path

from runtime.data_relocation import relocate_owned_state
from runtime.knowledge_delivery import DeliveryStore
from runtime.research_archive import ResearchArchive
from test_research_archive import archive,evidence,submit,reconstruct


def test_external_vault_is_unchanged_when_runtime_state_is_moved(archive,tmp_path):
    engine,_,_,vault=archive
    item=evidence(engine);submit(engine)
    before={p.relative_to(vault):p.read_bytes() for p in vault.rglob('*.md')}
    source=engine.store.root.parent
    target=tmp_path/'moved-state'
    shutil.copytree(source,target)
    relocate_owned_state(source,target)
    moved=ResearchArchive(DeliveryStore(root=target,assets=engine.assets))
    assert not moved.run('task-a',retry=True)['error_code']
    assert before=={p.relative_to(vault):p.read_bytes() for p in vault.rglob('*.md')}


def test_internal_vault_rebinds_identity_and_display_ownership(archive,tmp_path):
    from runtime.knowledge_delivery import Authorize
    from runtime.research_archive import BindContext
    engine,_,local,_=archive
    source=engine.store.root.parent
    internal=source/'vault';internal.mkdir()
    grant=engine.store.authorize(Authorize(action='authorize_destination',approved_root=str(internal),project_id='project-internal'))
    localgrant=engine.store.authorize(Authorize(action='authorize_destination',approved_root=str(local),project_id='project-internal'))
    from runtime import research_ledger
    research_ledger.open_task(objective='路径迁移',budget='balanced',considered=[],task_id='internal',project_id='project-internal',project_name='内部项目')
    engine.bind(BindContext(action='bind_context',task_id='internal',project_id='project-internal',project_name='内部项目',task_title='路径迁移',
        local_grant_id=localgrant['grant_id'],local_report_path=str(local/'internal.md'),vault_grant_id=grant['grant_id'],allow_shared_local_root=True))
    folder=engine._task_directory(engine._context('internal'))
    from runtime.archive_layout import plan,apply
    from runtime.research_archive import PlanArchiveLayout,ApplyArchiveLayout
    candidate=plan(engine,PlanArchiveLayout(action='plan_archive_layout',task_id='internal',project_id='project-internal'))
    request=ApplyArchiveLayout(action='apply_archive_layout',task_id='internal',project_id='project-internal',plan_id=candidate['plan_id'])
    apply(engine,request)
    relative=folder.relative_to(source)
    target=tmp_path/'new-root';shutil.copytree(source,target)
    relocate_owned_state(source,target)
    moved=ResearchArchive(DeliveryStore(root=target,assets=engine.assets))
    assert moved._task_directory(moved._context('internal'))==target/relative
    assert apply(moved,request)['path']==str(target/relative)
    assert len(list((target/'vault').rglob('路径迁移*')))==0  # no competing flat task directory


def test_moved_waiting_task_uses_new_database_and_quoted_runtime_paths(tmp_path):
    source=tmp_path/'old';target=tmp_path/'new'
    job=source/'logs/task_artifacts/durable-fixture/input.json';job.parent.mkdir(parents=True)
    job.write_text(json.dumps({'db_path':str(source/'logs/tasks.sqlite3'),'payload':{'text':'original source provenance'}}))
    config=source/'config/runtime.env';config.parent.mkdir()
    config.write_text('KR_XHS_OPENCLI_ROOT="'+str(source/'dependency')+'"\nAPI_KEY=fixture-preserved\n')
    shutil.copytree(source,target);relocate_owned_state(source,target)
    moved=json.loads((target/job.relative_to(source)).read_text())
    assert moved['db_path']==str(target/'logs/tasks.sqlite3')
    assert moved['payload']['text']=='original source provenance'
    assert '"'+str(target/'dependency')+'"' in (target/'config/runtime.env').read_text()
    assert 'API_KEY=fixture-preserved' in (target/'config/runtime.env').read_text()


def test_data_migration_refuses_pending_vault_layout_transaction(archive,tmp_path):
    import pytest
    from runtime.research_archive import encoded
    engine,*_=archive
    with engine._connection() as c:
        c.execute('CREATE TABLE archive_layout_transactions(plan_id TEXT PRIMARY KEY,task_id TEXT,payload TEXT)')
        c.execute('INSERT INTO archive_layout_transactions VALUES(?,?,?)',('pending','task-a',encoded({'status':'moving'})))
    source=engine.store.root.parent;target=tmp_path/'new-root'
    shutil.copytree(source,target)
    with pytest.raises(ValueError,match='finish_archive_layout_transaction'):relocate_owned_state(source,target)


def test_custom_task_database_inputs_follow_the_moved_root(tmp_path):
    from runtime.tasks import TaskStore
    source=tmp_path/'source';target=tmp_path/'target'
    database=source/'custom-runtime/jobs.sqlite3'
    TaskStore(str(database)).initialize()
    job=database.parent/'task_artifacts/custom/input.json';job.parent.mkdir(parents=True)
    job.write_text(json.dumps({'db_path':str(database),'payload':{'text':'immutable wording'}}),encoding='utf-8')
    shutil.copytree(source,target)
    relocate_owned_state(source,target)
    moved=json.loads((target/job.relative_to(source)).read_text(encoding='utf-8'))
    assert moved['db_path']==str(target/database.relative_to(source))
    assert moved['payload']['text']=='immutable wording'
