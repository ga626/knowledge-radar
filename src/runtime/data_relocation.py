"""Relocate owned runtime pointers after a verified, offline data-root copy.

Evidence bodies and provenance metadata remain immutable. External Vaults and
browser profiles retain their original paths and file identities.
"""
from __future__ import annotations
import json
from pathlib import Path
import sqlite3
import os
import tempfile


def _write_atomic(path,text):
    with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=path.parent,delete=False,suffix='.tmp') as stream:
        temporary=Path(stream.name);stream.write(text);stream.flush();os.fsync(stream.fileno())
    try:os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


def relocate_owned_state(source, target):
    from runtime.knowledge_delivery import identity
    source=Path(source).resolve();target=Path(target).resolve()
    def mapped(value):
        if not isinstance(value,str):return value
        try:
            path=Path(value)
            if path.is_absolute() and path.is_relative_to(source):return str(target/path.relative_to(source))
        except (ValueError,OSError):pass
        return value
    path_keys={'path','root','result_path','local_report_path','vault_report_path','state_file','task_root','artifact_path','input_path','dependency_root','approved_root','output_path','db_path','script','source','destination','project_path','staging_path'}
    def rewrite(value):
        if isinstance(value,list):return [rewrite(item) for item in value]
        if not isinstance(value,dict):return value
        result={key:mapped(item) if key in path_keys else rewrite(item) for key,item in value.items()}
        if 'identity' in value and value.get('path') and mapped(value['path'])!=value['path']:
            path=Path(result['path'])
            if not path.exists():raise ValueError('relocation_registered_file_missing')
            result['identity']=identity(path)
        return result
    job_inputs=set(target.glob('logs/task_artifacts/*/input.json'))
    for database in target.rglob('*.sqlite3'):
        if database.is_symlink():raise ValueError('relocation_database_link_rejected')
        with sqlite3.connect(database) as connection:
            if connection.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('relocation_database_corrupt')
            tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'archive_layout_transactions' in tables and connection.execute(
                "SELECT 1 FROM archive_layout_transactions WHERE json_extract(payload,'$.status')!='committed' LIMIT 1").fetchone():
                raise ValueError('finish_archive_layout_transaction_before_data_migration')
            if 'grants' in tables:
                connection.execute('CREATE TABLE IF NOT EXISTS archive_root_aliases(root TEXT PRIMARY KEY,identity TEXT NOT NULL,canonical_path TEXT NOT NULL,canonical_identity TEXT NOT NULL)')
                for grant,root,original_identity in connection.execute('SELECT id,root,identity FROM grants').fetchall():
                    next_root=mapped(root)
                    if next_root==root:continue
                    alias=connection.execute('SELECT canonical_path,canonical_identity FROM archive_root_aliases WHERE root=?',(root,)).fetchone()
                    canonical_path,canonical_identity=alias or (root,original_identity)
                    new_identity=json.dumps(identity(Path(next_root)),ensure_ascii=False,sort_keys=True,separators=(',',':'))
                    connection.execute('INSERT OR REPLACE INTO archive_root_aliases VALUES(?,?,?,?)',(next_root,new_identity,canonical_path,canonical_identity))
                    connection.execute('UPDATE grants SET root=?,identity=? WHERE id=?',(next_root,new_identity,grant))
            for table,key in [('research_context','task_id'),('archive_rounds','id'),('deliveries','id'),('archive_display_names','owner')]:
                if table not in tables:continue
                for identifier,raw in connection.execute('SELECT '+key+',payload FROM '+table).fetchall():
                    value=rewrite(json.loads(raw))
                    connection.execute('UPDATE '+table+' SET payload=? WHERE '+key+'=?',(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')),identifier))
                    if table=='archive_display_names':
                        path=Path(value['path'])
                        connection.execute('UPDATE archive_display_names SET parent=?,name_key=? WHERE owner=?',(str(path.parent).casefold(),path.name.casefold(),identifier))
            if 'archive_parts' in tables:
                for rid,ordinal,raw in connection.execute('SELECT round_id,ordinal,payload FROM archive_parts').fetchall():
                    connection.execute('UPDATE archive_parts SET payload=? WHERE round_id=? AND ordinal=?',(json.dumps(rewrite(json.loads(raw)),ensure_ascii=False),rid,ordinal))
            if 'runtime_tasks' in tables:
                # TaskService locates inputs beside its selected task database,
                # which may be a custom directory inside the user's data root.
                job_inputs.update(database.parent.glob('task_artifacts/*/input.json'))
                for task,path,raw in connection.execute('SELECT task_id,result_path,metadata_json FROM runtime_tasks').fetchall():
                    connection.execute('UPDATE runtime_tasks SET result_path=?,metadata_json=? WHERE task_id=?',(mapped(path),json.dumps(rewrite(json.loads(raw)),ensure_ascii=False),task))
            if 'archive_layout_transactions' in tables:
                for plan_id,raw in connection.execute('SELECT plan_id,payload FROM archive_layout_transactions').fetchall():
                    original=json.loads(raw);value=rewrite(original)
                    candidate=value['plan'];old_plan=original['plan']
                    if candidate['destination']!=old_plan['destination']:
                        destination=Path(candidate['destination']);candidate['folder_identity']=identity(destination)
                        for name,record in candidate['files'].items():
                            record['identity']=identity(destination/candidate['renames'].get(name,name))
                        for name,record in value.get('navigation_files',{}).items():record['identity']=identity(destination/name)
                    connection.execute('UPDATE archive_layout_transactions SET payload=? WHERE plan_id=?',
                        (json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')),plan_id))
    # Only mutable, owned configuration is rewritten. Source documents are excluded.
    candidates=[target/'config/profile_registry.json',*target.glob('state/xhs_opencli/*.json'),*target.glob('state/browser_components/*.json'),
                *job_inputs]
    for path in candidates:
        if not path.is_file():continue
        value=json.loads(path.read_text(encoding='utf-8-sig'))
        _write_atomic(path,json.dumps(rewrite(value),ensure_ascii=False,indent=2)+'\n')
    env=target/'config/runtime.env'
    if env.is_file():
        lines=[]
        for line in env.read_text(encoding='utf-8-sig').splitlines():
            key,separator,value=line.partition('=')
            if key in {'KR_XHS_OPENCLI_ROOT','KR_XHS_OPENCLI_NODE','KR_BROWSER_DATA_DIR','KR_MEDIA_CACHE_DIR','KR_PROFILE_REGISTRY_PATH','PLAYWRIGHT_BROWSERS_PATH'}:
                quote=value[0] if len(value)>1 and value[0]==value[-1] and value[0] in {'"',"'"} else ''
                value=quote+mapped(value[1:-1])+quote if quote else mapped(value)
            lines.append(key+separator+value)
        _write_atomic(env,'\n'.join(lines)+'\n')
