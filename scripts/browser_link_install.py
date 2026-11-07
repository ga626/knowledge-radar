"""KR Browser Link 安装合同：仅 HKCU，版本化 Host，保留账号与配对。

由产品安装器调用；本模块导入/plan 均无副作用。不安装浏览器扩展，不伪造商店身份。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request
import urllib.error

HOST = 'com.knowledgeradar.browser_link'
REG_KEY = 'Software\\Google\\Chrome\\NativeMessagingHosts\\' + HOST


def plan(program_root: Path, install_root: Path, data_root: Path):
    metadata_path = program_root / 'config/browser-link.json'
    if not metadata_path.is_file():
        return {'status': 'not_in_package', 'registration': False}
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    identifiers = metadata.get('extension_ids', [])
    if not isinstance(identifiers, list) or any(not re.fullmatch('[a-p]{32}', str(x)) for x in identifiers):
        raise ValueError('invalid_browser_link_extension_ids')
    source = program_root / 'browser_link/host/BrowserLinkHost.cs'
    if not source.is_file():
        raise ValueError('browser_link_host_source_missing')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    component_digest=hashlib.sha256(source.read_bytes()+b'\x00winexe-v1').hexdigest()
    component = install_root / 'components/browser_link' / component_digest[:20]
    return {'status': 'ready' if identifiers else 'store_registration_pending', 'registration': bool(identifiers),
        'source': str(source), 'source_sha256': digest, 'component_root': str(component),
        'manifest': str(component / (HOST + '.json')), 'host': str(component / 'BrowserLinkHost.exe'),
        'state_file': str(data_root / 'runtime/browser_link/host.json'), 'extension_ids': identifiers,
        'store_url': metadata.get('store_url'), 'protocol': metadata.get('protocol')}


def read_registration():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY) as key:
            return winreg.QueryValueEx(key, '')[0]
    except FileNotFoundError:
        return None


def write_registration(value):
    import winreg
    if value is None:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REG_KEY)
        except FileNotFoundError:
            pass
    else:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_KEY) as key:
            winreg.SetValueEx(key, '', 0, winreg.REG_SZ, str(value))


def _assert_owned(value, install_root):
    if value and not Path(value).resolve().is_relative_to((install_root / 'components/browser_link').resolve()):
        raise RuntimeError('browser_link_registration_owned_by_another_installation')


def _registered_state(value, install_root):
    _assert_owned(value, install_root)
    if not value:
        return None
    manifest=json.loads(Path(value).read_text(encoding='utf-8'))
    host=Path(manifest['path']).resolve()
    if not host.is_relative_to((install_root/'components/browser_link').resolve()):
        raise RuntimeError('browser_link_registered_host_outside_installation')
    return Path(json.loads(host.with_name('host-settings.json').read_text(encoding='utf-8'))['state_file'])


def _control(state_file, operation):
    if not state_file.is_file():
        return {'ok': True, 'pending': 0, 'connected': False}
    state = json.loads(state_file.read_text(encoding='utf-8-sig'))
    port = int(state.get('port') or 0)
    if not 1024 <= port <= 65535:
        raise ValueError('invalid_native_port')
    request = urllib.request.Request(f'http://127.0.0.1:{port}/{operation}', data=b'{}',
        headers={'X-KR-Native-Token': state.get('token', ''), 'Content-Type': 'application/json'}, method='POST')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=3) as response:
            return json.load(response)
    except urllib.error.URLError as exc:
        # A refused local socket is a stopped host; timeouts/HTTP errors are not.
        import errno
        code = getattr(exc.reason, 'winerror', None) or getattr(exc.reason, 'errno', None)
        if code in {10061, errno.ECONNREFUSED}:
            return {'ok': True, 'pending': 0, 'connected': False}
        raise


def _snapshot(paths):
    return {str(path): path.read_text(encoding='utf-8') if path.is_file() else None for path in paths}


def _restore_files(snapshot):
    for name, content in snapshot.items():
        path = Path(name)
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(content, encoding='utf-8')


def _drain_idle(state_file, action):
    if state_file is None:
        return {'connected': False}
    drained = _control(state_file, 'drain')
    if drained.get('pending'):
        _control(state_file, 'resume')
        raise RuntimeError('browser_link_' + action + '_wait_for_active_tasks')
    return drained


def handoff_plan(install_root: Path):
    """An explicit, content-bound handoff preserves the previous host registration."""
    value=read_registration()
    if not value:return {'status':'not_needed','confirmation_token':None}
    payload=_registration_fingerprint(value)
    payload['destination_install_root']=str(install_root.resolve())
    return {'status':'PLAN','previous_registration':value,'confirmation_token':hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest(),
        'previous_identity':payload,'preserves_previous_files':True}


def _registration_fingerprint(value):
    path=Path(value)
    manifest=json.loads(path.read_text(encoding='utf-8-sig'))
    if manifest.get('name')!=HOST or manifest.get('type')!='stdio':
        raise RuntimeError('browser_link_unknown_registration_preserved')
    host=Path(manifest.get('path',''))
    if not host.is_file():raise RuntimeError('browser_link_previous_host_missing')
    settings=host.with_name('host-settings.json')
    payload={'registration':value,'manifest_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'host_sha256':hashlib.sha256(host.read_bytes()).hexdigest()}
    payload['settings_sha256']=hashlib.sha256(settings.read_bytes()).hexdigest() if settings.is_file() else None
    return payload


def apply(program_root: Path, install_root: Path, data_root: Path, *, handoff_confirmation=None):
    item = plan(program_root, install_root, data_root)
    # HTTP-only installs remain functional when the connector is unavailable.
    if not item['registration']:
        return item
    before = read_registration()
    external=False;previous_identity=None
    try:_assert_owned(before, install_root)
    except RuntimeError:
        external=True
        if handoff_confirmation is None:
            return {**item,'status':'registration_conflict','previous_registration':before,'next_action':'explicit_host_handoff_required'}
        candidate=handoff_plan(install_root)
        if handoff_confirmation!=candidate['confirmation_token']:
            raise RuntimeError('browser_link_handoff_plan_stale')
        previous_identity=candidate['previous_identity'].copy();previous_identity.pop('destination_install_root',None)
    previous_state = None if external else _registered_state(before, install_root)
    if external:
        previous_manifest=json.loads(Path(before).read_text(encoding='utf-8-sig'))
        previous_settings=Path(previous_manifest['path']).with_name('host-settings.json')
        if previous_settings.is_file():
            previous_state=Path(json.loads(previous_settings.read_text(encoding='utf-8-sig'))['state_file'])
            if not previous_state.is_absolute():raise RuntimeError('browser_link_previous_state_not_absolute')
    host = Path(item['host']); host.parent.mkdir(parents=True, exist_ok=True)
    if not host.is_file():
        compiler = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        if not compiler.is_file():
            raise RuntimeError('browser_link_framework_compiler_unavailable')
        subprocess.run([str(compiler), '/nologo', '/target:winexe', '/reference:System.Web.Extensions.dll',
                        '/out:' + str(host), item['source']], check=True, capture_output=True,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    manifest = {'name': HOST, 'description': 'KnowledgeRadar Browser Link', 'path': str(host), 'type': 'stdio',
        'allowed_origins': ['chrome-extension://' + identifier + '/' for identifier in item['extension_ids']]}
    # Chrome requires an executable Host path; use a version-local settings file,
    # read by the exe itself, rather than a cmd intermediary.
    state_file = previous_state or Path(item['state_file'])
    files = _snapshot([host.with_name('host-settings.json'), Path(item['manifest'])])
    drained = _drain_idle(state_file, 'upgrade')
    mutating=False
    try:
        if (read_registration()!=before or _snapshot([Path(name) for name in files])!=files
                or (external and _registration_fingerprint(before)!=previous_identity)):
            raise RuntimeError('browser_link_registration_changed_before_apply')
        mutating=True
        host.with_name('host-settings.json').write_text(json.dumps({'state_file': item['state_file']}), encoding='utf-8')
        Path(item['manifest']).write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        write_registration(item['manifest'])
        if drained.get('connected', True):
            _control(state_file, 'shutdown')
    except Exception:
        try:
            if mutating:
                current=read_registration()
                if current not in {before,item['manifest']}:
                    raise RuntimeError('browser_link_registration_changed_preserved')
                # A registry call may have committed before reporting an error.
                if current==item['manifest'] and current!=before:write_registration(before)
                _restore_files(files)
        finally:
            if drained.get('connected', True):
                _control(state_file,'resume')
        raise
    return {**item, 'previous_registration': before, 'previous_files': files, 'host_sha256': hashlib.sha256(host.read_bytes()).hexdigest(),
        'external_handoff':external,'previous_identity':previous_identity,
        'applied_identity':_registration_fingerprint(item['manifest']),
        'extension_install': 'user_unpacked_load_consent_required', 'status': 'registered'}


def restore(receipt, install_root):
    if receipt.get('status') == 'unregistered':
        if read_registration() is not None:
            raise RuntimeError('browser_link_registration_changed_since_uninstall')
        previous = receipt.get('previous_registration')
        _assert_owned(previous, install_root)
        write_registration(previous)
        return
    if receipt.get('status') == 'registered':
        current = read_registration()
        _assert_owned(current, install_root)
        if current != receipt['manifest']:
            raise RuntimeError('browser_link_registration_changed_since_apply')
        if receipt.get('applied_identity') and _registration_fingerprint(current)!=receipt['applied_identity']:
            raise RuntimeError('browser_link_current_registration_modified_preserved')
        state_file = _registered_state(current, install_root)
        files = receipt.get('previous_files', {})
        for name in files:
            if not Path(name).resolve().is_relative_to((install_root / 'components/browser_link').resolve()):
                raise RuntimeError('browser_link_restore_file_outside_installation')
        if not receipt.get('external_handoff'):
            _assert_owned(receipt.get('previous_registration'), install_root)
        elif _registration_fingerprint(receipt['previous_registration'])!=receipt.get('previous_identity'):
            raise RuntimeError('browser_link_previous_registration_modified_preserved')
        forward = _snapshot([Path(name) for name in files])
        drained = _drain_idle(state_file, 'rollback')
        mutating=False
        try:
            if read_registration()!=current or _snapshot([Path(name) for name in files])!=forward:
                raise RuntimeError('browser_link_registration_changed_before_restore')
            mutating=True
            _restore_files(files)
            write_registration(receipt.get('previous_registration'))
            if drained.get('connected', True):
                _control(state_file, 'shutdown')
        except Exception:
            try:
                if mutating:
                    if read_registration() not in {current,receipt.get('previous_registration')}:
                        raise RuntimeError('browser_link_registration_changed_preserved')
                    _restore_files(forward)
                    write_registration(current)
            finally:
                if drained.get('connected', True):
                    _control(state_file, 'resume')
            raise


def uninstall(install_root: Path):
    value = read_registration()
    _assert_owned(value, install_root)
    state_file = _registered_state(value, install_root)
    drained = _drain_idle(state_file, 'uninstall')
    try:
        write_registration(None)
        if state_file is not None and drained.get('connected',True):
            _control(state_file,'shutdown')
    except Exception:
        try:
            write_registration(value)
        finally:
            if state_file is not None and drained.get('connected',True):
                _control(state_file,'resume')
        raise
    return {'status': 'unregistered', 'previous_registration': value,
            'preserved': ['paired-profile', 'browser_accounts', 'data']}
