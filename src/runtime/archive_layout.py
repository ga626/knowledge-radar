"""显式迁移已归属的Vault任务；目录移动和路径登记由持久事务协调。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import quote

from runtime.file_write_lock import file_write_lock
from runtime.knowledge import encoded, guarded_path
from runtime.research_archive import calendar_layout, sha, _hash_file
from runtime.knowledge_delivery import identity


def _binding(engine, root, owner):
    key=engine._owner_key(root,owner)
    with engine._connection() as c:
        row=c.execute('SELECT payload FROM archive_display_names WHERE owner=?',(key,)).fetchone()
    if not row:raise ValueError('archive_directory_ownership_required')
    return key, engine._find_committed(root,json.loads(row[0]))


def _inventory(root, folder):
    files={}
    for base,dirs,names in os.walk(folder,followlinks=False):
        for name in dirs+names:
            item=guarded_path(root,Path(base)/name)
            if item.is_symlink() or (hasattr(item,'is_junction') and item.is_junction()):
                raise ValueError('archive_migration_links_not_allowed')
        for name in names:
            item=Path(base)/name
            files[str(item.relative_to(folder))]={'sha256':_hash_file(item),'identity':identity(item)}
    return dict(sorted(files.items()))


def _plan(engine, request):
    context=engine._context(request.task_id)
    if not context or context['project_id']!=request.project_id:raise ValueError('research_project_mismatch_use_new_task')
    if not context.get('vault_grant_id'):raise ValueError('vault_grant_required')
    root=engine._grant(context['vault_grant_id'],request.project_id)
    task_key,folder=_binding(engine,root,['task',request.project_id,request.task_id])
    _,project=_binding(engine,root,['project',request.project_id])
    metadata=engine._ledger(request.task_id)
    if not metadata.get('created_at') and context.get('directory_layout')!='project-year-task-v1':
        raise ValueError('archive_original_creation_time_required')
    layout=calendar_layout(metadata,context['task_title'])
    # 首次创建时间缺失时固定绑定时的时间，不能让稍后重试产生另一目标。
    if context.get('directory_layout')=='project-year-task-v1':
        layout.update({key:context[key] for key in layout})
    year=project/layout['directory_year']
    if year.exists():
        _,owned_year=_binding(engine,root,['year',request.project_id,layout['directory_year']])
        if year!=owned_year:raise ValueError('archive_year_directory_changed')
    destination=guarded_path(root,year/layout['directory_title'])
    if destination!=folder and destination.exists():raise ValueError('archive_migration_destination_exists')
    files=_inventory(root,folder)
    # 迁移不是修复人改内容；已有回执对应的文件必须仍为原版本。
    with engine._connection() as c:
        rounds=[json.loads(row[0]) for row in c.execute('SELECT payload FROM archive_rounds WHERE task_id=?',(request.task_id,))]
    for record in rounds:
        for name,digest in record.get('file_hashes',{}).items():
            item=record.get('file_records',{}).get(name,{})
            actual=engine._find_committed(root,item) if item.get('identity') else Path(record['path'])/name
            if _hash_file(actual)!=digest:raise ValueError('destination_modified_preserved')
    renames={}
    for record in [context]+context.get('report_history',[]):
        endpoint=record.get('vault',{})
        if not endpoint.get('identity'):continue
        actual=engine._find_committed(root,endpoint.copy())
        report=record.get('report') or {}
        if report.get('sha256') and _hash_file(actual)!=report['sha256']:raise ValueError('destination_modified_preserved')
        if actual.parent==folder and actual.name.casefold() in {'final-report.md','final_report.md'}:
            target='总报告.md';number=2
            while target in files or target in renames.values():
                target=f'总报告（{number}）.md';number+=1
            renames[actual.name]=target
    navigation_updates={}
    for record in rounds:
        name=record.get('navigation')
        if not name or name not in files:continue
        original=(folder/name).read_text(encoding='utf-8')
        updated=original
        for old,new in renames.items():
            for target in {old,quote(old)}:
                updated=updated.replace(']('+target+')',']('+quote(new)+')')
        if updated!=original:
            navigation_updates[name]={'text':updated,'sha256':sha(updated.encode('utf-8'))}
    payload={'task_id':request.task_id,'project_id':request.project_id,'source':str(folder),'destination':str(destination),
        'folder_identity':identity(folder),'task_binding':task_key,'layout':layout,'files':files,'renames':renames,
        'project_path':str(project),'context_revision':context['context_revision'],'navigation_updates':navigation_updates}
    return {'status':'planned','plan_id':sha(encoded(payload).encode()),**payload}


def plan(engine,request):
    with file_write_lock(engine.root/('archive-'+sha(request.task_id.encode()))):
        return _plan(engine,request)


def _rewrite(value, source, destination, renames, field=''):
    if isinstance(value,dict):
        return {(renames.get(key,key) if field in {'file_hashes','file_records'} else key):
                _rewrite(item,source,destination,renames,key) for key,item in value.items()}
    if isinstance(value,list):return [_rewrite(item,source,destination,renames,field) for item in value]
    if isinstance(value,str):
        if field in {'file','navigation','vault_name'} and value in renames:return renames[value]
        if field not in {'path','local_report_path','vault_report_path','staging_path'}:return value
        try:
            path=Path(value)
            if path.is_absolute() and path.is_relative_to(source):
                relative=path.relative_to(source)
                if len(relative.parts)==1:relative=Path(renames.get(str(relative),str(relative)))
                return str(destination/relative)
        except (ValueError,OSError):pass
    return value


def _expected_files(candidate, journal):
    result={candidate['renames'].get(name,name):dict(record) for name,record in candidate['files'].items()}
    for name,record in journal.get('navigation_files',{}).items():result[name]=record
    return result


def _reconcile_inventory(actual, original, renames, navigation_files=None):
    """Each rename may have completed independently before a power loss."""
    remaining=dict(actual)
    for name,record in original.items():
        target=renames.get(name,name)
        present=[key for key in {name,target} if key in remaining]
        current=remaining.pop(present[0]) if len(present)==1 else None
        if len(present)!=1 or (current!=record and current!=(navigation_files or {}).get(target)):
            raise ValueError('archive_migration_plan_stale')
    if remaining:raise ValueError('archive_migration_plan_stale')


def apply(engine,request):
    """journal先落盘，rename保留文件身份；重复调用对账同一事务。"""
    with file_write_lock(engine.root/('archive-'+sha(request.task_id.encode()))):
        context=engine._context(request.task_id)
        if not context or context['project_id']!=request.project_id:raise ValueError('research_project_mismatch_use_new_task')
        root=engine._grant(context['vault_grant_id'],request.project_id)
        with engine._connection() as c:
            c.execute('CREATE TABLE IF NOT EXISTS archive_layout_transactions(plan_id TEXT PRIMARY KEY,task_id TEXT,payload TEXT)')
            row=c.execute('SELECT payload FROM archive_layout_transactions WHERE plan_id=? AND task_id=?',(request.plan_id,request.task_id)).fetchone()
        if row:
            journal=json.loads(row[0])
            if journal['status']=='committed':
                candidate=journal['plan'];destination=Path(candidate['destination'])
                if identity(destination)!=candidate['folder_identity']:
                    raise ValueError('archive_migration_identity_changed')
                expected=_expected_files(candidate,journal)
                actual=_inventory(root,destination)
                # 后续增量交付可以新增文件，原有文件仍须逐一核对。
                if any(actual.get(name)!=record for name,record in expected.items()):
                    raise ValueError('destination_modified_preserved')
                return journal['receipt']
        else:
            candidate=_plan(engine,request)
            if candidate['plan_id']!=request.plan_id:raise ValueError('archive_migration_plan_stale')
            journal={'status':'moving','plan':candidate}
            with engine._connection() as c:c.execute('INSERT INTO archive_layout_transactions VALUES(?,?,?)',(request.plan_id,request.task_id,encoded(journal)))
        candidate=journal['plan'];source=Path(candidate['source']);destination=Path(candidate['destination'])
        guarded_path(root,source);guarded_path(root,destination)
        if context['context_revision']!=candidate['context_revision']:raise ValueError('archive_migration_context_changed')
        active=source if source.exists() else destination
        if identity(active)!=candidate['folder_identity']:raise ValueError('archive_migration_identity_changed')
        # 中断后允许报告已经改名，其余文件必须和计划一致；禁止吞掉新写入。
        inventory=_inventory(root,active)
        _reconcile_inventory(inventory,candidate['files'],candidate['renames'],journal.get('navigation_files'))
        if active==source and source!=destination:
            parent=engine._display_path(root,Path(candidate['project_path']),
                ['year',request.project_id,candidate['layout']['directory_year']],candidate['layout']['directory_year'],directory=True)
            if parent!=destination.parent:raise ValueError('archive_migration_destination_changed')
            if destination.exists():raise ValueError('archive_migration_destination_exists')
            source.rename(destination)
        for old,new in candidate['renames'].items():
            if (destination/old).exists():
                if (destination/new).exists():raise ValueError('archive_migration_destination_exists')
                (destination/old).rename(destination/new)
        # 只改计划内、已校验的受管导航。替换前将新文件身份写入journal，断电后可对账。
        for number,(name,change) in enumerate(candidate.get('navigation_updates',{}).items()):
            target=guarded_path(root,destination/name)
            expected_navigation=journal.get('navigation_files',{}).get(name)
            if expected_navigation and {'identity':identity(target),'sha256':_hash_file(target)}==expected_navigation:continue
            staged=guarded_path(root,destination.parent/('.kr-layout-'+request.plan_id+'-'+str(number)+'.tmp'))
            if expected_navigation:
                if not staged.is_file() or {'identity':identity(staged),'sha256':_hash_file(staged)}!=expected_navigation:
                    raise ValueError('archive_navigation_staging_changed')
            else:
                intent={'path':str(staged),'sha256':change['sha256']}
                recorded=journal.get('navigation_intents',{}).get(name)
                if staged.exists():
                    if recorded!=intent or not staged.is_file() or _hash_file(staged)!=change['sha256']:
                        raise ValueError('archive_navigation_staging_conflict')
                else:
                    if recorded and recorded!=intent:raise ValueError('archive_navigation_staging_conflict')
                    journal.setdefault('navigation_intents',{})[name]=intent
                    with engine._connection() as c:c.execute('UPDATE archive_layout_transactions SET payload=? WHERE plan_id=?',(encoded(journal),request.plan_id))
                    with staged.open('xb') as stream:
                        stream.write(change['text'].encode('utf-8'));stream.flush();os.fsync(stream.fileno())
                journal.setdefault('navigation_files',{})[name]={'identity':identity(staged),'sha256':change['sha256']}
                with engine._connection() as c:c.execute('UPDATE archive_layout_transactions SET payload=? WHERE plan_id=?',(encoded(journal),request.plan_id))
            os.replace(staged,target)
        expected=_expected_files(candidate,journal)
        if _inventory(root,destination)!=expected:raise ValueError('archive_migration_verification_failed')
        # 路径映射与事务完成同一SQLite提交；固定版本和正文字节完全不变。
        context=_rewrite(context,source,destination,candidate['renames'])
        context.update(candidate['layout']);context['vault_relative']=str(destination.relative_to(root));context['context_revision']+=1
        receipt={'status':'committed','plan_id':request.plan_id,'task_id':request.task_id,'path':str(destination),'files_verified':len(expected),'renames':candidate['renames']}
        with engine._connection() as c:
            for table,key_col in [('archive_rounds','id'),('archive_display_names','owner')]:
                query='SELECT '+key_col+',payload FROM '+table
                if table=='archive_rounds':rows=c.execute(query+' WHERE task_id=?',(request.task_id,)).fetchall()
                else:rows=c.execute(query).fetchall()
                for key,raw in rows:
                    original=json.loads(raw);updated=_rewrite(original,source,destination,candidate['renames'])
                    for name,record in journal.get('navigation_files',{}).items():
                        if name in updated.get('file_hashes',{}):updated['file_hashes'][name]=record['sha256']
                        if name in updated.get('file_records',{}):updated['file_records'][name]['identity']=record['identity']
                        if table=='archive_display_names' and updated.get('path')==str(destination/name):updated['identity']=record['identity']
                    if key==candidate['task_binding']:updated.update(path=str(destination))
                    if updated!=original:
                        c.execute('UPDATE '+table+' SET payload=? WHERE '+key_col+'=?',(encoded(updated),key))
                        if table=='archive_display_names':
                            path=Path(updated['path']);c.execute('UPDATE archive_display_names SET parent=?,name_key=? WHERE owner=?',(str(path.parent).casefold(),path.name.casefold(),key))
            for key,ordinal,raw in c.execute('SELECT p.round_id,p.ordinal,p.payload FROM archive_parts p JOIN archive_rounds r ON r.id=p.round_id WHERE r.task_id=?',(request.task_id,)).fetchall():
                c.execute('UPDATE archive_parts SET payload=? WHERE round_id=? AND ordinal=?',(encoded(_rewrite(json.loads(raw),source,destination,candidate['renames'])),key,ordinal))
            for key,raw in c.execute('SELECT id,payload FROM deliveries').fetchall():
                original=json.loads(raw)
                if original.get('task_id')!=request.task_id:continue
                updated=_rewrite(original,source,destination,candidate['renames'])
                for name,record in journal.get('navigation_files',{}).items():
                    if name in updated.get('file_hashes',{}):updated['file_hashes'][name]=record['sha256']
                c.execute('UPDATE deliveries SET payload=? WHERE id=?',(encoded(updated),key))
            c.execute('UPDATE research_context SET payload=? WHERE task_id=?',(encoded(context),request.task_id))
            journal.update(status='committed',receipt=receipt)
            c.execute('UPDATE archive_layout_transactions SET payload=? WHERE plan_id=?',(encoded(journal),request.plan_id))
        return receipt
