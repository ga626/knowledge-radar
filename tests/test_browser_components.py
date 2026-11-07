import json
from pathlib import Path

import pytest

from runtime.browser_components import verify_component, prepare_component, component_status


def test_bundled_extension_provenance_and_stable_paths(tmp_path):
    program=Path(__file__).resolve().parents[1]
    for component in ('browser_link','opencli'):
        item=verify_component(component,program=program)
        assert len(item['extension_id'])==32
        result=prepare_component(component,program=program,install=tmp_path/'install',state=tmp_path/'state')
        assert result['status']=='prepared' and result['browser_confirmation_required']
        assert prepare_component(component,program=program,install=tmp_path/'install',state=tmp_path/'state')==result


def test_user_modified_extension_is_preserved(tmp_path):
    result=prepare_component('browser_link',install=tmp_path/'install',state=tmp_path/'state')
    manifest=Path(result['path'])/'manifest.json'
    manifest.write_text('user modification',encoding='utf-8')
    with pytest.raises(ValueError,match='modified_preserved'):
        prepare_component('browser_link',install=tmp_path/'install',state=tmp_path/'state')
    assert manifest.read_text()=='user modification'


def test_unowned_existing_directory_is_preserved(tmp_path):
    folder=tmp_path/'install/components/extensions/opencli/loaded'
    folder.mkdir(parents=True);(folder/'mine').write_text('mine')
    with pytest.raises((ValueError,FileExistsError)):
        prepare_component('opencli',install=tmp_path/'install',state=tmp_path/'state')
    assert (folder/'mine').read_text()=='mine'


def _changed_bundle(tmp_path, revision):
    import hashlib
    import shutil
    from runtime.browser_components import _files
    program=tmp_path/'program'
    if not program.exists():
        root=Path(__file__).resolve().parents[1]
        shutil.copytree(root/'browser_link/extension',program/'browser_link/extension')
        (program/'config').mkdir()
        shutil.copyfile(root/'config/browser-components.json',program/'config/browser-components.json')
    (program/'browser_link/extension/revision.txt').write_text(str(revision))
    path=program/'config/browser-components.json';catalog=json.loads(path.read_text())
    item=catalog['components']['browser_link'];item['files']=_files(program/item['source'])
    item['tree_sha256']=hashlib.sha256(json.dumps(item['files'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
    path.write_text(json.dumps(catalog))
    return program


def test_repeated_upgrades_keep_stable_path_and_previous_payloads(tmp_path):
    paths=[]
    for revision in range(3):
        program=_changed_bundle(tmp_path,revision)
        result=prepare_component('browser_link',program=program,install=tmp_path/'install',state=tmp_path/'state')
        paths.append(result['path'])
        assert (Path(result['path'])/'revision.txt').read_text()==str(revision)
    assert len(set(paths))==1
    assert len(list(Path(paths[0]).parent.glob('previous-*')))==2


def test_component_switch_recovers_after_old_directory_rename(tmp_path,monkeypatch):
    program=_changed_bundle(tmp_path,1)
    result=prepare_component('browser_link',program=program,install=tmp_path/'install',state=tmp_path/'state')
    _changed_bundle(tmp_path,2);rename=Path.rename
    def rename_then_crash(path,target):
        outcome=rename(path,target)
        if path==Path(result['path']):raise OSError('power loss after old rename')
        return outcome
    monkeypatch.setattr(Path,'rename',rename_then_crash)
    with pytest.raises(OSError):prepare_component('browser_link',program=program,install=tmp_path/'install',state=tmp_path/'state')
    assert component_status('browser_link',program=program,install=tmp_path/'install',state=tmp_path/'state')['status']=='recovery_required'
    monkeypatch.setattr(Path,'rename',rename)
    recovered=prepare_component('browser_link',program=program,install=tmp_path/'install',state=tmp_path/'state')
    assert recovered['status']=='prepared'
    assert (Path(recovered['path'])/'revision.txt').read_text()=='2'
