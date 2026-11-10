"""研究数据一致快照；只恢复到新目录，绝不激活外部执行或旧授权。"""
from __future__ import annotations
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from typing import Literal
from runtime.knowledge import Contract, guarded_path, encoded
from runtime.research_storage import storage_guard, admit_bytes
from runtime.research_archive import _hash_file, _save_file, _parents


class SnapshotResearch(Contract):
    action: Literal['snapshot_research']
    backup_grant_id: str
    project_id: str
    backup_name: str
    include_delivered_user_versions: bool = False


class RestoreResearch(Contract):
    action: Literal['restore_research']
    approved_backup_root: str
    snapshot_path: str
    approved_restore_root: str
    restore_path: str


def _database_copy(source,target):
    target.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True)) as src, closing(sqlite3.connect(target)) as out, out:
        src.backup(out)
        # 快照是独立只读制品，不携带WAL模式，防止校验时新建-shm/-wal污染清单。
        out.execute('PRAGMA journal_mode=DELETE')
        if out.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('snapshot_database_corrupt')


def snapshot(store, request):
    from runtime.knowledge_delivery import publish_directory
    from types import SimpleNamespace
    root=store._grant(SimpleNamespace(grant_id=request.backup_grant_id,project_id=request.project_id))
    if Path(request.backup_name).name!=request.backup_name or request.backup_name in {'.','..'}:
        raise ValueError('backup_name_must_be_one_component')
    target=guarded_path(root,root/request.backup_name)
    if target.exists():raise ValueError('backup_destination_exists')
    state=store.root.parent.resolve()
    from runtime.tasks import default_task_db_path
    from runtime.execution_owner import owner_root
    from runtime.paid_execution import default_usage_path
    task_database = Path(default_task_db_path()).resolve()
    task_artifacts = task_database.parent / 'task_artifacts'
    requests_root = owner_root().resolve()
    paid_usage = default_usage_path().resolve()
    if any(target.is_relative_to(base) for base in (state, store.assets.root.resolve(), task_artifacts, requests_root)):
        raise ValueError('backup_requires_separate_destination')
    stage=Path(tempfile.mkdtemp(prefix='.kr-backup-',dir=root))
    manifest={'schema':'kr-research-backup/v1','created_at':time.time(),'source_state':str(state),'files':{},
        'scope':'research_evidence_ledger_tasks_delivery_idempotency','excluded':['credentials','browser_profiles','models','raw_media','private_vault'],
        'restore_policy':'new_directory_reauthorization_no_automatic_execution',
        'source_layout': {'task_database': str(task_database), 'task_artifacts': str(task_artifacts),
                          'owner_root': str(requests_root), 'paid_usage': str(paid_usage)},
        'database_inventory': []}
    try:
        # 仅短写事务被冻结；快照采用SQLite backup读取WAL，原文为不可变文件。
        with storage_guard():
            databases=[(store.assets.root/'index.sqlite3','evidence_assets/index.sqlite3'),(store.db,'knowledge_delivery/delivery.sqlite3'),
                (state/'research_index.sqlite3','research_index.sqlite3'),(task_database,'knowledgeradar-tasks.sqlite3'),
                (requests_root/'requests.sqlite3','execution_owner/requests.sqlite3')]
            for source,relative in databases:
                manifest['database_inventory'].append({'snapshot_path': relative, 'present': source.is_file()})
                if source.is_file():
                    admit_bytes(stage,source.stat().st_size*2)
                    _database_copy(source,stage/relative)
            for base, folder in ((state/'research_tasks', 'research_tasks'),
                                 (state/'research_submissions', 'research_submissions'),
                                 (task_artifacts, 'task_artifacts')):
                if base.exists():
                    for path in base.rglob('*.json'):
                        guarded_path(base,path,require_file=True)
                        if folder=='task_artifacts' and not path.name.startswith(('result-','collection-')):continue
                        dest=stage/folder/path.relative_to(base);dest.parent.mkdir(parents=True,exist_ok=True)
                        admit_bytes(stage,path.stat().st_size);shutil.copyfile(path,dest)
            if paid_usage.is_file():shutil.copyfile(paid_usage,stage/'paid_usage.json')
        evidence_db=stage/'evidence_assets/index.sqlite3'
        if evidence_db.is_file():
            with closing(sqlite3.connect(evidence_db)) as c:
                for digest, in c.execute('SELECT DISTINCT raw_sha256 FROM assets'):
                    source=store.assets._file(digest);dest=stage/'evidence_assets'/source.name
                    admit_bytes(stage,source.stat().st_size);shutil.copyfile(source,dest)
                    if _hash_file(dest)!=digest:raise ValueError('snapshot_asset_hash_mismatch')
                # 校验固定证据引用闭包，不以FTS可重建为由遗漏原始元数据。
                if c.execute('SELECT 1 FROM asset_refs r LEFT JOIN assets a USING(version_id) WHERE a.version_id IS NULL LIMIT 1').fetchone():
                    raise ValueError('snapshot_reference_missing')
                for metadata, in c.execute('SELECT metadata FROM assets'):
                    for parent in json.loads(metadata).get('derived_from',[]):
                        if not c.execute('SELECT 1 FROM assets WHERE version_id=?',(parent,)).fetchone():raise ValueError('snapshot_provenance_missing')
        if request.include_delivered_user_versions:
            _capture_user_versions(store,stage,request.project_id)
        for path in stage.rglob('*'):
            if path.is_file():
                with path.open('r+b') as stream:os.fsync(stream.fileno())
                manifest['files'][path.relative_to(stage).as_posix()]=_hash_file(path)
        _save_file(stage/'snapshot.json',encoded(manifest).encode())
        _validate_snapshot(stage)
        with _parents(root,target.parent):publish_directory(stage,target)
        return {'status':'snapshot_committed','path':str(target),'manifest_sha256':_hash_file(target/'snapshot.json'),
            'file_count':len(manifest['files']),'scope':manifest['scope'],'includes_user_versions':request.include_delivered_user_versions}
    finally:
        if stage.exists():shutil.rmtree(stage)


def _capture_user_versions(store,stage,project):
    """只捕获本项目已有回执定位的文件，不读取私人库其他笔记。"""
    from runtime.research_archive import ResearchArchive, _read_safe
    archive=ResearchArchive(store)
    if not archive._has_contexts():return
    mapping=[]
    with store.connection() as c:
        from runtime.project_identity import canonical_project_id
        c.create_function('kr_project_id',1,canonical_project_id,deterministic=True)
        contexts=[json.loads(row[0]) for row in c.execute('SELECT payload FROM research_context WHERE kr_project_id(json_extract(payload,\'$.project_id\'))=kr_project_id(?)',(project,))]
    seen=set()
    for item in contexts:
        # 补交前的人改版本也在回执中，不能因换目标而从备份范围消失。
        for receipts in [item,*item.get('report_history',[]),*item.get('previous_report_targets',[])]:
            for kind in ('local','vault'):
                receipt=receipts.get(kind,{})
                if not receipt.get('identity'):continue
                grant=item['local_grant_id' if kind=='local' else 'vault_grant_id']
                root=archive._grant(grant,project)
                path=archive._find_committed(root,receipt)
                if str(path) in seen:continue
                seen.add(str(path))
                name='user_versions/'+str(len(mapping))+'.md'
                (stage/'user_versions').mkdir(exist_ok=True)
                _copy_user_file(root,path,stage/name)
                mapping.append({'project_id':project,'task_id':item['task_id'],'original_path':str(path),'snapshot_file':name,'sha256':_hash_file(stage/name)})
        with store.connection() as c:
            rounds=[json.loads(row[0]) for row in c.execute('SELECT payload FROM archive_rounds WHERE task_id=?',(item['task_id'],))]
        for record in rounds:
            if not record.get('identity') or not item.get('vault_grant_id'):continue
            root=archive._grant(item['vault_grant_id'],project);folder=archive._find_committed(root,record)
            for name in record['file_hashes']:
                file=archive._find_committed(root,record['file_records'][name]) if record.get('file_records') else guarded_path(root,folder/name,require_file=True)
                relative='user_versions/'+str(len(mapping))+file.suffix
                (stage/'user_versions').mkdir(exist_ok=True);_copy_user_file(root,file,stage/relative)
                mapping.append({'project_id':project,'task_id':item['task_id'],'original_path':str(file),'snapshot_file':relative,'sha256':_hash_file(stage/relative)})
    _save_file(stage/'user-version-map.json',encoded(mapping).encode())


def _copy_user_file(root,source,target):
    guarded_path(root,source,require_file=True)
    before=_hash_file(source)
    admit_bytes(target.parent,source.stat().st_size)
    shutil.copyfile(source,target)
    if _hash_file(target)!=before or _hash_file(source)!=before:raise ValueError('user_file_changed_during_snapshot_retry')


def _task_result_relative(manifest, original):
    """v1旧快照以state定位；新快照显式记录外置任务产物根。"""
    path = Path(original)
    artifacts = manifest.get('source_layout', {}).get('task_artifacts')
    base = Path(artifacts or manifest['source_state'])
    try:
        relative = path.relative_to(base)
    except ValueError as exc:
        raise ValueError('snapshot_task_result_outside_artifacts') from exc
    if not path.is_absolute() or '..' in relative.parts:
        raise ValueError('snapshot_task_result_outside_artifacts')
    return Path('task_artifacts') / relative if artifacts else relative


def _validate_snapshot(root):
    manifest=json.loads((root/'snapshot.json').read_text(encoding='utf-8'))
    if manifest.get('schema')!='kr-research-backup/v1':raise ValueError('unsupported_backup_schema')
    for relative,digest in manifest['files'].items():
        path=guarded_path(root,root/relative,require_file=True)
        if _hash_file(path)!=digest:raise ValueError('snapshot_hash_mismatch')
    expected=set(manifest['files'])|{'snapshot.json'}
    if {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}!=expected:raise ValueError('unexpected_snapshot_file')
    db=root/'evidence_assets/index.sqlite3'
    versions=set()
    if db.is_file():
        with closing(sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True)) as c:
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'assets','sources','asset_refs','asset_scopes'}<=tables:raise ValueError('backup_schema_incomplete')
            versions={r[0] for r in c.execute('SELECT version_id FROM assets')}
            for version,raw_hash,metadata in c.execute('SELECT version_id,raw_sha256,metadata FROM assets'):
                file=guarded_path(root,root/'evidence_assets'/f'{raw_hash}.utf8',require_file=True)
                if _hash_file(file)!=raw_hash:raise ValueError('snapshot_asset_hash_mismatch')
                if not set(json.loads(metadata).get('derived_from',[]))<=versions:raise ValueError('snapshot_provenance_missing')
            for table in ('asset_refs','asset_scopes'):
                if c.execute(f'SELECT 1 FROM {table} r LEFT JOIN assets a USING(version_id) WHERE a.version_id IS NULL LIMIT 1').fetchone():
                    raise ValueError('snapshot_reference_missing')
    delivery=root/'knowledge_delivery/delivery.sqlite3'
    if delivery.is_file():
        with closing(sqlite3.connect(delivery.resolve().as_uri()+'?mode=ro',uri=True)) as c:
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'grants','deliveries'}<=tables:raise ValueError('backup_schema_incomplete')
            if 'archive_members' in tables:
                if any(version not in versions for version, in c.execute('SELECT DISTINCT version_id FROM archive_members')):
                    raise ValueError('snapshot_archive_reference_missing')
            if 'archive_parts' in tables:
                for payload, in c.execute('SELECT payload FROM archive_parts'):
                    part=json.loads(payload)
                    if part['version_id'] not in versions or part.get('text_version_id',part['version_id']) not in versions:raise ValueError('snapshot_archive_reference_missing')
            if 'research_context' in tables:
                for payload, in c.execute('SELECT payload FROM research_context'):
                    context=json.loads(payload)
                    for item in [context,*context.get('report_history',[])]:
                        if item.get('report') and item['report']['version_id'] not in versions:raise ValueError('snapshot_report_reference_missing')
    tasks = root/'knowledgeradar-tasks.sqlite3'
    if tasks.is_file():
        with closing(sqlite3.connect(tasks.resolve().as_uri()+'?mode=ro&immutable=1', uri=True)) as c:
            tables = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'runtime_tasks' not in tables:
                raise ValueError('snapshot_task_schema_incomplete')
            for original, in c.execute("SELECT result_path FROM runtime_tasks WHERE result_path!=''"):
                relative = _task_result_relative(manifest, original)
                if relative.as_posix() not in manifest['files'] or not (root/relative).is_file():
                    raise ValueError('snapshot_task_result_missing')
    return manifest


def restore(request):
    from runtime.knowledge_delivery import publish_directory
    root=Path(request.approved_backup_root);source=guarded_path(root,request.snapshot_path)
    manifest=_validate_snapshot(source)
    destination_root=Path(request.approved_restore_root)
    target=guarded_path(destination_root,request.restore_path)
    if target.exists():raise ValueError('restore_requires_new_directory')
    if target.is_relative_to(source):raise ValueError('restore_inside_snapshot_rejected')
    stage=Path(tempfile.mkdtemp(prefix='.kr-restore-',dir=destination_root))
    try:
        for relative,digest in manifest['files'].items():
            source_file=guarded_path(source,source/relative,require_file=True)
            dest=stage/relative;dest.parent.mkdir(parents=True,exist_ok=True)
            admit_bytes(stage,source_file.stat().st_size);shutil.copyfile(source_file,dest)
            if _hash_file(dest)!=digest:raise ValueError('snapshot_changed_during_restore')
        for path in stage.rglob('*.sqlite3'):
            with closing(sqlite3.connect(path)) as c, c:
                if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('restore_database_corrupt')
                tables={x[0] for x in c.execute('SELECT name FROM sqlite_master WHERE type=\'table\'')}
                if 'grants' in tables:c.execute('UPDATE grants SET active=0')
                if 'research_context' in tables:
                    for task,payload in c.execute('SELECT task_id,payload FROM research_context').fetchall():
                        item=json.loads(payload);item.update(restored_requires_reauthorization=True,retry_at=0)
                        c.execute('UPDATE research_context SET payload=? WHERE task_id=?',(encoded(item),task))
                if 'requests' in tables:c.execute("UPDATE requests SET status='interrupted' WHERE status='running'")
                if 'runtime_tasks' in tables:
                    c.execute("UPDATE runtime_tasks SET status='interrupted' WHERE status IN ('running','queued','waiting_for_user')")
                    for tid,old in c.execute("SELECT task_id,result_path FROM runtime_tasks WHERE result_path!=''").fetchall():
                        relative = _task_result_relative(manifest, old)
                        if not (stage/relative).is_file():raise ValueError('restore_task_result_missing')
                        c.execute('UPDATE runtime_tasks SET result_path=? WHERE task_id=?',(str(target/relative),tid))
        _save_file(stage/'RESTORE-RECEIPT.json',encoded({'source_manifest_sha256':_hash_file(source/'snapshot.json'),'external_authorization':'disabled',
            'execution':'not_replayed','reindex':'existing_fts_preserved','user_versions':'separate_recovery_copies_never_auto_overwrite',
            'runtime_paths': {'KR_STATE_DIR': str(target), 'KR_LOG_DIR': str(target/'logs'),
                              'KR_TASK_DB_PATH': str(target/'knowledgeradar-tasks.sqlite3'),
                              'KR_EVIDENCE_ROOT': str(target/'evidence_assets'),
                              'KR_OWNER_ROOT': str(target/'execution_owner'),
                              'KR_PAID_USAGE_PATH': str(target/'paid_usage.json')}}).encode())
        with _parents(destination_root,target.parent):publish_directory(stage,target)
        return {'status':'restored_inactive','path':str(target),'external_authorization':'disabled','execution':'not_replayed'}
    finally:
        if stage.exists():shutil.rmtree(stage)
