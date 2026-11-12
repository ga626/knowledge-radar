"""日历分层保持真实时间、旧绑定及人改保护。"""
from pathlib import Path
import pytest
from test_research_archive import archive, evidence, submit
from runtime.research_archive import calendar_layout, PlanArchiveLayout, ApplyArchiveLayout
from runtime.archive_layout import plan, apply


def test_calendar_separates_date_from_time_and_rejects_unknown_time():
    layout=calendar_layout({'created_at':'2026-11-12T17:57:00+08:00'},'任务名')
    assert layout['directory_layout']=='project-calendar-task-v3'
    assert layout['directory_month']=='11月'
    assert layout['directory_day']=='11月12日'
    assert layout['directory_title'].endswith('任务名')
    assert '.' not in layout['directory_title'] and len(layout['directory_title'].split()[0])==5
    with pytest.raises(ValueError,match='original_creation_time'):
        calendar_layout({'created_at':'bad'},'不得用恢复时间')


def test_new_binding_and_migration_use_only_time_in_task_name(archive):
    engine,request,_,vault=archive
    context=engine._context('task-a');folder=engine._task_directory(context)
    assert folder.parent.name==context['directory_day']
    assert folder.parent.parent.name==context['directory_month']
    assert folder.name[:5].count('：')==1
    # 模拟已归属的旧日历绑定；仅移动归属目录，后续仍沿同一任务追加。
    old=vault/'同名项目'/'2026年'/('11.02 '+folder.name)
    folder.rename(old)
    context.update(directory_layout='project-year-task-v2',directory_title=old.name,
                   vault_relative=str(old.relative_to(vault)))
    for key in ('directory_month','directory_day','calendar_date','calendar_offset'):
        context.pop(key,None)
    engine._save(context)
    evidence(engine);submit(engine)
    before={p.name:p.read_bytes() for p in old.iterdir() if p.is_file()}
    req=PlanArchiveLayout(action='plan_archive_layout',task_id='task-a',project_id='project-a')
    candidate=plan(engine,req)
    assert Path(candidate['destination']).parent.name=='11月02日'
    result=apply(engine,ApplyArchiveLayout(action='apply_archive_layout',task_id='task-a',project_id='project-a',plan_id=candidate['plan_id']))
    target=Path(result['path'])
    assert {p.name:p.read_bytes() for p in target.iterdir() if p.is_file()}==before
    assert not engine.bind(request)['error_code']
    evidence(engine,'迟到正文','https://example.org/late')
    assert not engine.run('task-a',retry=True)['error_code']
    assert not old.exists()


def test_plain_report_has_no_empty_review_log(archive):
    engine,*_=archive
    result=submit(engine)
    body=engine.assets.fetch(result['report']['version_id'],max_chars=24000)['text']
    assert '未登记关键结论' not in body
    assert '## 关键结论与撤证审阅' not in body


def test_explicit_review_is_available_but_cannot_change_a_fixed_report_in_place(archive):
    engine,*_=archive
    result=submit(engine,include_review=True)
    assert '## 关键结论与撤证审阅' in engine.assets.fetch(result['report']['version_id'])['text']
    with pytest.raises(ValueError,match='projection_request_changed'):
        submit(engine,include_review=False)


def test_layout_rollback_is_explicit_and_retains_file_identity(archive):
    engine,*_=archive;evidence(engine);submit(engine)
    before=engine._task_directory(engine._context('task-a'))
    req=PlanArchiveLayout(action='plan_archive_layout',task_id='task-a',project_id='project-a',target_layout='project-year-task-v2')
    candidate=plan(engine,req)
    assert Path(candidate['destination']).parent.name=='2026年'
    receipt=apply(engine,ApplyArchiveLayout(action='apply_archive_layout',task_id='task-a',project_id='project-a',plan_id=candidate['plan_id'],target_layout='project-year-task-v2'))
    assert Path(receipt['path']).name.startswith('11.02 ')
    forward=plan(engine,PlanArchiveLayout(action='plan_archive_layout',task_id='task-a',project_id='project-a'))
    apply(engine,ApplyArchiveLayout(action='apply_archive_layout',task_id='task-a',project_id='project-a',plan_id=forward['plan_id']))
    assert before.is_dir()


@pytest.mark.parametrize('tamper',[False,True])
def test_existing_recovered_report_is_adopted_in_place_after_fixed_registration(archive,tamper):
    from runtime.knowledge_delivery import RegisterReport, execute
    from runtime.archive_layout import plan_adoption, apply_adoption
    from types import SimpleNamespace
    engine,_,local,vault=archive
    existing=vault/'同名项目'/'2026年'/'11.02 08：00 完整测试'
    existing.mkdir();report=existing/'总报告.md';report.write_bytes('原来的正常报告'.encode())
    old_source=engine.assets.put('https://example.org/recovered','原来的完整资料',kind='content',title='历史正文')
    (existing/'已有正文.md').write_text('# 正常资料\n\n原来的完整资料\n',encoding='utf-8')
    source=local/'original.md';source.write_bytes(report.read_bytes())
    from runtime.research_archive import _hash_file
    ref=execute(RegisterReport(action='register_report',defer_delivery=True,input_path=str(source),approved_input_root=str(local),
        input_sha256=_hash_file(source),project_id='project-a',task_id='task-a',report_id='original',
        title='完整测试',derived_from=[old_source['version_id']]),store=engine.store)['asset']
    request=SimpleNamespace(task_id='task-a',project_id='project-a',
        relative_path=str(existing.relative_to(vault)),report_version_id=ref['version_id'],report_name='总报告.md',
        existing_sources=[{'file':'已有正文.md','version_id':old_source['version_id']}])
    candidate=plan_adoption(engine,request)
    assert engine._context('task-a')['report'] is None
    request.plan_id=candidate['plan_id']
    if tamper:
        report.write_text('用户后来修改了报告',encoding='utf-8')
        with pytest.raises(ValueError,match='modified|stale'):apply_adoption(engine,request)
        assert report.read_text(encoding='utf-8')=='用户后来修改了报告'
        return
    receipt=apply_adoption(engine,request)
    assert receipt['path']==str(existing) and report.read_bytes()=='原来的正常报告'.encode()
    assert engine._context('task-a')['vault']['path']==str(report)
    assert engine._context('task-a')['directory_title']=='11.02 08：00 完整测试'
    assert apply_adoption(engine,request)==receipt
    assert receipt['reconciled_source_files']==1
    assert not engine.run('task-a',retry=True)['error_code']
    assert len([p for p in existing.iterdir() if '正文' in p.name])==1
    migrated=plan(engine,PlanArchiveLayout(action='plan_archive_layout',task_id='task-a',project_id='project-a'))
    assert Path(migrated['destination']).name=='08：00 完整测试'
