"""显式迁移已归属的Vault任务；目录移动和路径登记由持久事务协调。"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import quote

from runtime.file_write_lock import file_write_lock
from runtime.knowledge import encoded, guarded_path
from runtime.research_archive import calendar_layout, sha, _hash_file
from runtime.knowledge_delivery import identity
from runtime.project_identity import canonical_project_id


def _binding(engine, root, owner):
    owners=[owner]
    if owner[0] in {'project','year','task'}:
        # 只查同一稳定项目的既存owner，计划不分配目录、不改历史键。
        with engine._connection() as c:
            aliases={json.loads(raw).get('project_id','') for (raw,) in c.execute('SELECT payload FROM research_context')}
        aliases.add(canonical_project_id(owner[1]))
        owners += [[owner[0],alias,*owner[2:]] for alias in sorted(aliases)
                   if alias!=owner[1] and canonical_project_id(alias)==canonical_project_id(owner[1])]
    with engine._connection() as c:
        matches=[(key,json.loads(row[0])) for value in owners
                 for key in [engine._owner_key(root,value)]
                 for row in c.execute('SELECT payload FROM archive_display_names WHERE owner=?',(key,))]
    if not matches:raise ValueError('archive_directory_ownership_required')
    if len({str(Path(record['path'])).casefold() for _,record in matches})>1:
        raise ValueError('archive_project_alias_conflict_requires_explicit_resolution')
    key,record=matches[0]
    return key, engine._find_committed(root,record)


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
    if not context or canonical_project_id(context['project_id'])!=request.project_id:raise ValueError('research_project_mismatch_use_new_task')
    if not context.get('vault_grant_id'):raise ValueError('vault_grant_required')
    root=engine._grant(context['vault_grant_id'],request.project_id)
    task_key,folder=_binding(engine,root,['task',request.project_id,request.task_id])
    _,project=_binding(engine,root,['project',request.project_id])
    metadata=engine._ledger(request.task_id)
    if not metadata.get('created_at') and context.get('directory_layout')!='project-year-task-v1':
        raise ValueError('archive_original_creation_time_required')
    layout=calendar_layout(metadata,context['task_title'])
    # 命名迁移保持已经绑定的本地日历值，不随机器时区变化改写历史时间。
    if context.get('directory_layout') in {'project-year-task-v1','project-year-task-v2'}:
        stamp=re.fullmatch(r'(\d{2})[月.\-](\d{2})日? (\d{2})[：\-](\d{2}) (.+)',context['directory_title'])
        if not stamp:raise ValueError('archive_original_creation_time_required')
        month,day,hour,minute,subject=stamp.groups()
        layout.update(directory_year=context['directory_year'],
            directory_title=f'{month}.{day} {hour}：{minute} {subject}')
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


def _source_link_plan(engine, request):
    """只预览完整单文件来源的基址修复；原文、报告、分卷及人改不覆盖。"""
    from runtime.research_archive import _source_markdown_links
    context=engine._context(request.task_id)
    if not context or canonical_project_id(context['project_id'])!=request.project_id:
        raise ValueError('research_project_mismatch_use_new_task')
    root=engine._grant(context['vault_grant_id'],request.project_id)
    _,folder=_binding(engine,root,['task',request.project_id,request.task_id])
    grouped={};conflicts=[]
    with engine._connection() as c:
        rows=c.execute("SELECT p.round_id,p.ordinal,p.payload,r.payload FROM archive_parts p JOIN archive_rounds r ON r.id=p.round_id WHERE r.task_id=? AND json_extract(r.payload,'$.status')='local_committed'",(request.task_id,)).fetchall()
    for round_id,ordinal,payload,raw_round in rows:
        part=json.loads(payload);record=json.loads(raw_round);name=part['file']
        grouped.setdefault(name,[]).append((round_id,ordinal,part,record))
    updates=[]
    for name,entries in sorted(grouped.items()):
        first=entries[0][2]
        with engine.assets.connection() as c:
            row=c.execute('SELECT * FROM assets WHERE version_id=?',(first['text_version_id'],)).fetchone()
        if row is None:
            conflicts.append({'file':name,'reason':'archive_source_missing'});continue
        row=dict(row);meta=json.loads(row['metadata'])
        if row['kind']=='report' or meta.get('original_kind')=='report':continue
        text=engine.assets._verified_text(row)
        projected=_source_markdown_links(text,first['source_url'])
        if projected==text:continue
        if any(part['version_id']!=first['version_id'] or part['text_version_id']!=first['text_version_id']
               or part['raw_start']!=0 or part['raw_end']!=part['raw_bytes'] for _,_,part,_ in entries):
            conflicts.append({'file':name,'reason':'source_volume_requires_separate_projection'});continue
        try:
            actual=engine._find_committed(root,dict(entries[0][3]['file_records'][name]))
            if actual.parent!=folder:raise ValueError('source_relocated_preserved')
            original=actual.read_bytes();digest=sha(original)
            if any(record['file_hashes'][name]!=digest for _,_,_,record in entries):
                raise ValueError('destination_modified_preserved')
            start,end=first['export_start'],first['export_end']
            if original[start:end]!=text.encode('utf-8'):
                raise ValueError('archive_source_slice_mismatch')
        except (ValueError,FileNotFoundError,KeyError) as exc:
            conflicts.append({'file':name,'reason':str(exc) if isinstance(exc,ValueError) else type(exc).__name__});continue
        replacement=original[:start]+projected.encode('utf-8')+original[end:]
        updates.append({'file':name,'path':str(actual),'identity':identity(actual),'old_sha256':digest,
            'new_sha256':sha(replacement),'original_text':original.decode('utf-8'),
            'replacement_text':replacement.decode('utf-8'),'projected_text':projected,
            'source_url':first['source_url'],'title':row['title'],'parent_version_id':first['version_id'],
            'source_metadata':{k:v for k,v in meta.items() if k!='locators'},
            'previous_text_version_id':first['text_version_id'],
            'parts':[[rid,ordinal] for rid,ordinal,_,_ in entries]})
    payload={'task_id':request.task_id,'project_id':request.project_id,'root':str(root),'root_identity':identity(root),
        'folder':str(folder),'folder_identity':identity(folder),'context_revision':context['context_revision'],
        'updates':updates,'conflicts':conflicts,'scope':'owned_complete_source_files_only'}
    return {'status':'planned','plan_id':sha(encoded(payload).encode()),**payload}


def plan_source_links(engine, request):
    with file_write_lock(engine.root/('archive-'+sha(request.task_id.encode()))):
        return _source_link_plan(engine,request)


def apply_source_links(engine, request):
    """同一计划恢复，先存原件与替换身份，最后原子提交归档映射。"""
    from runtime.research_archive import _read_safe
    with file_write_lock(engine.root/('archive-'+sha(request.task_id.encode()))):
        context=engine._context(request.task_id)
        if not context or canonical_project_id(context['project_id'])!=request.project_id:raise ValueError('research_project_mismatch_use_new_task')
        root=engine._grant(context['vault_grant_id'],request.project_id)
        with engine._connection() as c:
            c.execute('CREATE TABLE IF NOT EXISTS archive_source_link_repairs(plan_id TEXT PRIMARY KEY,task_id TEXT,payload TEXT)')
            saved=c.execute('SELECT payload FROM archive_source_link_repairs WHERE plan_id=? AND task_id=?',(request.plan_id,request.task_id)).fetchone()
        if saved:
            journal=json.loads(saved[0]);candidate=journal['plan']
        else:
            candidate=_source_link_plan(engine,request)
            if candidate['plan_id']!=request.plan_id:raise ValueError('archive_source_link_plan_stale')
            journal={'status':'applying','plan':candidate,'files':{}}
            with engine._connection() as c:c.execute('INSERT INTO archive_source_link_repairs VALUES(?,?,?)',(request.plan_id,request.task_id,encoded(journal)))
        if identity(root)!=candidate['root_identity'] or identity(Path(candidate['folder']))!=candidate['folder_identity']:
            raise ValueError('archive_source_link_identity_changed')
        if context['context_revision']!=candidate['context_revision']:raise ValueError('archive_source_link_context_changed')
        for number,change in enumerate(candidate['updates']):
            engine._grant(context['vault_grant_id'],request.project_id)
            target=guarded_path(root,change['path'],require_file=True)
            observed={'identity':identity(target),'sha256':sha(_read_safe(root,target))}
            expected=journal['files'].get(change['file'])
            if expected and observed==expected:continue
            if journal['status']=='committed' or observed!={'identity':change['identity'],'sha256':change['old_sha256']}:
                raise ValueError('destination_modified_preserved')
            staged=guarded_path(root,target.parent/('.kr-source-links-'+request.plan_id+'-'+str(number)+'.tmp'))
            intent=journal.get('intents',{}).get(change['file'])
            if staged.exists():
                if intent!=str(staged) or sha(_read_safe(root,staged))!=change['new_sha256']:
                    raise ValueError('archive_source_link_staging_changed')
                if expected and identity(staged)!=expected['identity']:
                    raise ValueError('archive_source_link_staging_changed')
            else:
                if expected:raise ValueError('archive_source_link_staging_missing')
                journal.setdefault('intents',{})[change['file']]=str(staged)
                with engine._connection() as c:c.execute('UPDATE archive_source_link_repairs SET payload=? WHERE plan_id=?',(encoded(journal),request.plan_id))
                with staged.open('xb') as stream:
                    stream.write(change['replacement_text'].encode('utf-8'));stream.flush();os.fsync(stream.fileno())
            journal['files'][change['file']]={'identity':identity(staged),'sha256':change['new_sha256']}
            with engine._connection() as c:c.execute('UPDATE archive_source_link_repairs SET payload=? WHERE plan_id=?',(encoded(journal),request.plan_id))
            if {'identity':identity(target),'sha256':sha(_read_safe(root,target))}!=observed:
                raise ValueError('destination_modified_preserved')
            os.replace(staged,target)
        if journal['status']=='committed':return journal['receipt']
        # 派生资产另存版本，固定原始证据和作品/任务身份完全保留。
        versions={}
        for change in candidate['updates']:
            versions[change['file']]=engine.assets.put(change['source_url'],change['projected_text'],kind='readable_text',title=change['title'],metadata={
                **change['source_metadata'],
                'extractor':'archive-relative-links/v1','content_format':'markdown',
                'parent_version_id':change['parent_version_id'],'derived_from':[change['previous_text_version_id']],
                'projection_transforms':['source_relative_links_resolved']})['version_id']
        with engine._connection() as c:
            changes={change['file']:change for change in candidate['updates']}
            for rid,raw in c.execute('SELECT id,payload FROM archive_rounds WHERE task_id=?',(request.task_id,)).fetchall():
                record=json.loads(raw)
                for name,file in journal['files'].items():
                    if name in record.get('file_hashes',{}):
                        record['file_hashes'][name]=file['sha256']
                        record['file_records'][name]={'path':changes[name]['path'],'identity':file['identity']}
                c.execute('UPDATE archive_rounds SET payload=? WHERE id=?',(encoded(record),rid))
            for change in candidate['updates']:
                for rid,ordinal in change['parts']:
                    part=json.loads(c.execute('SELECT payload FROM archive_parts WHERE round_id=? AND ordinal=?',(rid,ordinal)).fetchone()[0])
                    size=len(change['projected_text'].encode('utf-8'))
                    part.update(text_version_id=versions[change['file']],raw_sha256=sha(change['projected_text'].encode('utf-8')),
                        raw_bytes=size,raw_end=size,export_end=part['export_start']+size,part_sha256=sha(change['projected_text'].encode('utf-8')))
                    c.execute('UPDATE archive_parts SET payload=? WHERE round_id=? AND ordinal=?',(encoded(part),rid,ordinal))
                for owner,raw in c.execute('SELECT owner,payload FROM archive_display_names WHERE parent=?',(str(Path(change['path']).parent).casefold(),)).fetchall():
                    item=json.loads(raw)
                    if item.get('path')==change['path']:
                        item['identity']=journal['files'][change['file']]['identity']
                        c.execute('UPDATE archive_display_names SET payload=? WHERE owner=?',(encoded(item),owner))
            receipt={'status':'committed','plan_id':request.plan_id,'task_id':request.task_id,
                     'repaired_count':len(candidate['updates']),'conflicts':candidate['conflicts'],
                     'originals_preserved_in':'archive_source_link_repairs','fixed_versions_unchanged':True}
            journal.update(status='committed',receipt=receipt)
            c.execute('UPDATE archive_source_link_repairs SET payload=? WHERE plan_id=?',(encoded(journal),request.plan_id))
        return receipt


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
        if not context or canonical_project_id(context['project_id'])!=request.project_id:raise ValueError('research_project_mismatch_use_new_task')
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
