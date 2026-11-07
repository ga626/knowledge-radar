"""Verified bundled extensions, with stable paths and user-owned state isolation."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

from runtime.file_write_lock import file_write_lock
from runtime.paths import project_root, runtime_state_dir


def _files(folder):
    result={}
    if folder.is_symlink() or (hasattr(folder,'is_junction') and folder.is_junction()):
        raise ValueError('component_links_not_allowed')
    for item in sorted(folder.rglob('*')):
        if item.is_symlink() or (hasattr(item,'is_junction') and item.is_junction()):
            raise ValueError('component_links_not_allowed')
        if item.is_file():result[item.relative_to(folder).as_posix()]=hashlib.sha256(item.read_bytes()).hexdigest()
    return result


def verify_component(component, *, program=None):
    program=Path(program or project_root()).resolve()
    catalog=json.loads((program/'config/browser-components.json').read_text(encoding='utf-8'))
    if component not in catalog['components']:raise ValueError('unknown_browser_component')
    item=catalog['components'][component]
    tree=hashlib.sha256(json.dumps(item['files'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if tree!=item['tree_sha256']:raise ValueError('component_catalog_tree_mismatch')
    source=program/item['source']
    if not source.resolve().is_relative_to(program):raise ValueError('component_source_outside_package')
    if _files(source)!=item['files']:raise ValueError('component_bundle_hash_mismatch')
    manifest=json.loads((source/'manifest.json').read_text(encoding='utf-8'))
    digest=hashlib.sha256(base64.b64decode(manifest['key'],validate=True)).hexdigest()[:32]
    identifier=''.join(chr(ord('a')+int(char,16)) for char in digest)
    if identifier!=item['extension_id'] or manifest['version']!=item['version']:
        raise ValueError('component_bundle_identity_mismatch')
    return item


def component_status(component, *, program=None, install=None, state=None):
    item=verify_component(component,program=program)
    install=Path(install or os.environ.get('KR_INSTALL_ROOT') or '')
    if not install.is_absolute():return {**item,'status':'bundled','path':None,'browser_confirmation_required':True}
    folder=install/'components/extensions'/component/'loaded'
    receipt=Path(state or runtime_state_dir())/'browser_components'/f'{component}.json'
    if receipt.with_suffix('.pending.json').is_file():return {**item,'status':'recovery_required','path':str(folder),'browser_confirmation_required':True}
    if not receipt.is_file():return {**item,'status':'conflict' if folder.exists() else 'not_prepared','path':str(folder),'browser_confirmation_required':True}
    previous=json.loads(receipt.read_text(encoding='utf-8'))
    owned=previous.get('path')==str(folder) and folder.is_dir() and _files(folder)==previous.get('files')
    return {**item,'status':('prepared' if previous.get('files')==item['files'] else 'update_available') if owned else 'conflict',
        'path':str(folder),'browser_confirmation_required':True}


def prepare_component(component, *, program=None, install=None, state=None):
    program=Path(program or project_root());install=Path(install or os.environ.get('KR_INSTALL_ROOT') or '')
    state=Path(state or runtime_state_dir())
    if not install.is_absolute():raise ValueError('installed_product_required')
    with file_write_lock(state/'browser_components'/component):
        receipt=state/'browser_components'/f'{component}.json'
        journal_path=receipt.with_suffix('.pending.json')
        if journal_path.is_file():
            _finish_component_switch(json.loads(journal_path.read_text(encoding='utf-8')),receipt,journal_path,install)
        item=component_status(component,program=program,install=install,state=state)
        if item['status']=='conflict':raise ValueError('component_modified_preserved')
        if item['status']=='prepared':return item
        folder=Path(item['path']);parent=folder.parent
        parent.mkdir(parents=True,exist_ok=True)
        snapshot=parent/item['tree_sha256']
        if snapshot.exists():
            if _files(snapshot)!=item['files']:raise ValueError('component_snapshot_modified_preserved')
        else:
            staging=Path(tempfile.mkdtemp(prefix='.prepare-',dir=parent))
            try:
                shutil.copytree(program/item['source'],staging/'payload')
                if _files(staging/'payload')!=item['files']:raise ValueError('component_copy_mismatch')
                (staging/'payload').rename(snapshot)
            finally:shutil.rmtree(staging)
        # Keep the browser's loaded path stable across upgrades; never overwrite unowned files.
        transition=uuid.uuid4().hex
        staged=parent/('pending-'+transition)
        shutil.copytree(snapshot,staged)
        previous=parent/('previous-'+transition)
        desired={key:item[key] for key in ('path','files','tree_sha256','version','extension_id')}
        journal={'desired':desired,'previous_files':_files(folder) if folder.exists() else None,
            'staged':str(staged),'previous':str(previous)}
        receipt.parent.mkdir(parents=True,exist_ok=True)
        _atomic_json(journal_path,journal)
        _finish_component_switch(journal,receipt,journal_path,install)
        return component_status(component,program=program,install=install,state=state)


def _atomic_json(path,value):
    temporary=path.with_suffix('.tmp')
    with temporary.open('w',encoding='utf-8') as stream:
        stream.write(json.dumps(value,ensure_ascii=False,sort_keys=True));stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


def _finish_component_switch(journal,receipt,journal_path,install):
    desired=journal['desired'];folder=Path(desired['path']);staged=Path(journal['staged']);previous=Path(journal['previous'])
    for path in (folder,staged,previous):
        if not path.resolve().is_relative_to((install/'components/extensions').resolve()):raise ValueError('component_journal_outside_installation')
    if folder.exists() and _files(folder)==desired['files']:
        _atomic_json(receipt,desired);journal_path.unlink();return
    if folder.exists():
        if _files(folder)!=journal['previous_files'] or previous.exists():raise ValueError('component_modified_preserved')
        folder.rename(previous)
    elif journal['previous_files'] is not None:
        if not previous.is_dir() or _files(previous)!=journal['previous_files']:raise ValueError('component_previous_missing_preserved')
    if not staged.is_dir() or _files(staged)!=desired['files']:raise ValueError('component_staged_modified_preserved')
    staged.rename(folder)
    _atomic_json(receipt,desired)
    journal_path.unlink()
