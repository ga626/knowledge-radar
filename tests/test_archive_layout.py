"""旧任务显式迁移、字节/身份保护以及断电后的同事务恢复。"""
import json
from pathlib import Path

import pytest

from test_research_archive import archive, evidence, submit, reconstruct, TASK_RELATIVE
from runtime.archive_layout import plan, apply
from runtime.research_archive import PlanArchiveLayout, ApplyArchiveLayout
from runtime.research_archive import _hash_file
from runtime.knowledge_delivery import identity


def legacy(engine):
    # 将尚未交付的fixture登记为旧规范；模拟升级前数据库。
    context=engine._context('task-a')
    folder=engine._task_directory(context)
    old=folder.parent.parent/'2026-11-02 完整测试'
    folder.rename(old)
    context.pop('directory_layout',None);context.pop('directory_year',None)
    context['directory_title']=old.name
    context['vault_relative']=str(old.relative_to(old.parent.parent))
    engine._save(context)
    return old


def request():
    return PlanArchiveLayout(action='plan_archive_layout',task_id='task-a',project_id='project-a')


def apply_request(candidate):
    return ApplyArchiveLayout(action='apply_archive_layout',task_id='task-a',project_id='project-a',plan_id=candidate['plan_id'])


def test_migration_preserves_fixed_sources_and_future_incremental_delivery(archive):
    engine,_,_,vault=archive
    old=legacy(engine);first=evidence(engine);result=submit(engine)
    report_hash=result['report']['sha256']
    candidate=plan(engine,request())
    assert old.exists() and not Path(candidate['destination']).exists()
    receipt=apply(engine,apply_request(candidate))
    destination=vault/'同名项目'/TASK_RELATIVE
    assert Path(receipt['path'])==destination and not old.exists()
    assert reconstruct(destination)[first['version_id']]==engine.assets.fetch(first['version_id'])['text'].encode()
    assert engine.run('task-a',retry=True)['report']['sha256']==report_hash
    assert apply(engine,apply_request(candidate))==receipt
    second=evidence(engine,'迁移后迟到正文','https://example.org/late-migration')
    result=engine.run('task-a',retry=True)
    assert not result['error_code']
    assert reconstruct(destination)[second['version_id']]=='迁移后迟到正文'.encode()
    assert not old.exists()


@pytest.mark.parametrize('layout,title',[
    ('project-year-task-v1','11月07日 01：24 完整测试'),
    ('project-year-task-v2','11-07 01-24 完整测试'),
    ('project-year-task-v2','11.07 01：24 完整测试'),
])
def test_naming_migration_keeps_bound_calendar_time_and_is_idempotent(archive,layout,title):
    engine,*_=archive
    context=engine._context('task-a');folder=engine._task_directory(context)
    old=folder.with_name(title);folder.rename(old)
    context.update(directory_layout=layout,directory_title=title)
    engine._save(context)
    # fixture ledger是11月2日；迁移必须保留绑定的11月7日01：24，不重算时区。
    candidate=plan(engine,request())
    assert Path(candidate['destination']).name=='11.07 01：24 完整测试'
    receipt=apply(engine,apply_request(candidate))
    assert engine._context('task-a')['directory_title']=='11.07 01：24 完整测试'
    assert apply(engine,apply_request(candidate))==receipt
    assert plan(engine,request())['source']==plan(engine,request())['destination']


def test_stale_plan_preserves_human_edit(archive):
    engine,*_=archive;old=legacy(engine);evidence(engine);submit(engine)
    candidate=plan(engine,request())
    note=old/'自己的笔记.md';note.write_text('人在计划之后新增',encoding='utf-8')
    with pytest.raises(ValueError,match='plan_stale'):
        apply(engine,apply_request(candidate))
    assert note.read_text(encoding='utf-8')=='人在计划之后新增' and old.exists()


def test_migration_resumes_after_directory_rename_before_database_commit(archive,monkeypatch):
    engine,*_=archive;old=legacy(engine);evidence(engine);submit(engine)
    candidate=plan(engine,request());original=Path.rename
    def rename_then_crash(path,target):
        result=original(path,target)
        if path==old:raise OSError('simulated power loss')
        return result
    monkeypatch.setattr(Path,'rename',rename_then_crash)
    with pytest.raises(OSError):apply(engine,apply_request(candidate))
    monkeypatch.setattr(Path,'rename',original)
    assert apply(engine,apply_request(candidate))['status']=='committed'
    assert not engine.run('task-a',retry=True)['error_code']


def test_historical_english_report_is_renamed_without_changing_version(archive):
    engine,*_=archive;old=legacy(engine);submit(engine)
    context=engine._context('task-a');path=Path(context['vault']['path'])
    english=path.with_name('final-report.md');path.rename(english)
    context['vault']['path']=str(english);context['report']['vault_name']=english.name;engine._save(context)
    candidate=plan(engine,request())
    assert candidate['renames']=={'final-report.md':'总报告.md'}
    apply(engine,apply_request(candidate))
    result=engine.run('task-a',retry=True)
    assert not result['error_code'] and Path(result['vault']['path']).name=='总报告.md'


@pytest.mark.parametrize('crash_point',['after_replace','before_identity_journal'])
def test_migration_rewrites_owned_navigation_and_recovers_after_replace(archive,monkeypatch,crash_point):
    import runtime.archive_layout as module
    engine,*_=archive;old=legacy(engine);evidence(engine);submit(engine)
    context=engine._context('task-a');report=Path(context['vault']['path'])
    english=report.with_name('final-report.md');report.rename(english)
    context['vault']['path']=str(english);context['report']['vault_name']=english.name
    # 用户任务题名即便恰好等于文件名，也不是可改写的路径字段。
    context['task_title']='final-report.md';engine._save(context)
    round_id=engine.status('task-a')['rounds'][0]['round_id'];record=engine._round_get(round_id)
    navigation=old/record['navigation']
    navigation.write_text(navigation.read_text(encoding='utf-8')+'\n- [报告](final-report.md)\n',encoding='utf-8')
    record['file_hashes'][navigation.name]=_hash_file(navigation);engine._round_save(round_id,record)
    candidate=plan(engine,request());replace=module.os.replace
    def replace_then_crash(source,target):
        replace(source,target)
        raise OSError('after navigation replace')
    original_identity=module.identity
    def identity_after_crash(path):
        if Path(path).name.startswith('.kr-layout-'):raise OSError('before navigation identity journal')
        return original_identity(path)
    if crash_point=='after_replace':monkeypatch.setattr(module.os,'replace',replace_then_crash)
    else:monkeypatch.setattr(module,'identity',identity_after_crash)
    with pytest.raises(OSError,match='navigation'):apply(engine,apply_request(candidate))
    monkeypatch.setattr(module.os,'replace',replace)
    monkeypatch.setattr(module,'identity',original_identity)
    receipt=apply(engine,apply_request(candidate));target=Path(receipt['path'])/navigation.name
    assert 'final-report.md)' not in target.read_text(encoding='utf-8')
    assert '%E6%80%BB%E6%8A%A5%E5%91%8A.md)' in target.read_text(encoding='utf-8')
    updated=engine._round_get(round_id)
    assert updated['file_hashes'][target.name]==_hash_file(target)
    assert updated['file_records'][target.name]['identity']==identity(target)
    assert engine._context('task-a')['task_title']=='final-report.md'
    assert apply(engine,apply_request(candidate))==receipt
    assert not engine.run('task-a',retry=True)['error_code']
