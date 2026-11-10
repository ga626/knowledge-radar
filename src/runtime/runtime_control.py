"""内核外的运行控制面：只读观察不启动，恢复与研究请求分别对账。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time

from runtime.file_write_lock import file_write_lock, read_observed



def read_record(path):
    """缺失、损坏与不可读不同；限制控制记录大小，不扫描日志或任务库。"""
    try:
        raw = read_observed(Path(path), max_bytes=65537)
        if len(raw) > 65536:
            return {}, 'invalid'
        value = json.loads(raw.decode('utf-8'))
        return (value, 'ok') if isinstance(value, dict) else ({}, 'invalid')
    except FileNotFoundError:
        return {}, 'missing'
    except (ValueError, UnicodeError):
        return {}, 'invalid'
    except OSError:
        return {}, 'unreadable'


def _identity():
    from runtime.execution_owner import source_identity
    return source_identity()


def _maintenance(client):
    root = os.environ.get('KR_ACTIVE_INSTALL_ROOT') or os.environ.get('KR_INSTALL_ROOT')
    if root and (Path(root)/'backup/data-move-pending.json').exists():
        return True
    request, _ = read_record(client.root.parent/'console_supervisor/stable/shutdown.request.json')
    return request.get('reason') == 'installation_handoff'


def _runtime_snapshot(client):
    from runtime.execution_owner import active_client, process_identity
    raw, observation = read_record(client.root/'supervision.json')
    descriptor, descriptor_observation = read_record(client.root/'owner.json')
    status = client.status()
    supervision = status.get('supervision') or raw
    source_matches = not raw or raw.get('source_identity') == _identity()
    supervisor_available = bool(source_matches and raw.get('supervisor_pid') and raw.get('process_identity') and
        process_identity(raw['supervisor_pid']) == raw['process_identity'])
    supervisor = {'status': 'available' if supervisor_available else 'unavailable' if raw else 'unknown',
                  'state': supervision.get('state'), 'event_at': raw.get('time')}
    state = 'unknown'
    owner_status = status.get('status')
    if owner_status == 'incompatible' or not active_client() or not source_matches:
        state = 'blocked'
    elif owner_status == 'ready':
        state = 'ready'
    elif owner_status == 'unreachable':
        state = 'unresponsive'
    elif supervisor_available and observation == 'ok':
        state = {'starting': 'recovering', 'retrying': 'recovering',
                 'circuit_open': 'circuit_open', 'stopped': 'stopped', 'blocked': 'blocked',
                 'unresponsive': 'unresponsive'}.get(raw.get('state'), 'unknown')
    if state == 'unknown' and not supervisor_available and raw.get('source_identity') == _identity() and raw.get('state') in {'circuit_open', 'stopped', 'blocked'} and owner_status == 'unavailable':
        state = raw['state']
    if observation in {'invalid', 'unreadable'} or descriptor_observation in {'invalid', 'unreadable'}:
        state = 'unknown'
    if _maintenance(client) or owner_status == 'draining':
        state = 'maintenance'
    next_action = 'continue' if state == 'ready' else 'wait' if state == 'recovering' else 'await_user_decision'
    last_failure = raw.get('last_failure')
    if isinstance(last_failure, dict):
        last_failure = {key: last_failure[key] for key in (
            'code', 'cause', 'stage', 'time', 'exit_code', 'os_error', 'error_type', 'retry_policy',
            'evidence', 'unknown', 'event_id') if key in last_failure}
    else:
        last_failure = None
    maximum = raw.get('max_restarts', 3)
    failures = raw.get('restart_count', 0)
    if type(maximum) is not int or type(failures) is not int:
        maximum, failures, state, next_action = 3, 0, 'unknown', 'await_user_decision'
    result = {'schema': 'kr-runtime-state/v1', 'state': state, 'observed_at': time.time(),
              'source_identity': _identity(), 'generation': status.get('generation') or raw.get('generation'),
              'supervisor': supervisor, 'observations': {'supervision': observation, 'owner': descriptor_observation},
              'recovery': {'automatic_failures': failures, 'maximum_restarts': maximum,
                           'remaining_restarts': max(0, maximum-failures+int(raw.get('state') == 'retrying')),
                           'next_retry_at': raw.get('next_retry_at') if state == 'recovering' else None,
                           'startup_deadline_at': raw.get('startup_deadline_at'),
                           'request_id': raw.get('recovery_request_id'), 'result': raw.get('recovery_result')},
              'last_failure': last_failure, 'incident_id': raw.get('incident_id'),
              'next_action': next_action, 'automatic_replay': False,
              'manual_recovery_allowed': state in {'circuit_open', 'stopped', 'blocked'} and active_client() and source_matches and owner_status != 'incompatible' and raw.get('recovery_result') != 'unknown' or
                  (state == 'unknown' and observation == 'missing' and descriptor_observation == 'missing'),
              'warnings': ['supervisor_unavailable'] if state == 'ready' and not supervisor_available else []}
    return result


def runtime_snapshot(client=None, *, wait_seconds=0):
    from runtime.execution_owner import OwnerClient
    if not isinstance(wait_seconds, (int, float)) or not 0 <= wait_seconds <= 30:
        raise ValueError('runtime_wait_must_be_between_0_and_30_seconds')
    client = client or OwnerClient()
    deadline = time.monotonic()+wait_seconds
    result = _runtime_snapshot(client)
    while result['state'] == 'recovering' and time.monotonic() < deadline:
        time.sleep(min(.2, max(0, deadline-time.monotonic())))
        result = _runtime_snapshot(client)
    return {'status': 'ok', 'runtime': result}


def attach_runtime_failure(client, value):
    if isinstance(value, dict) and value.get('status') == 'error' and 'error' in value:
        value = dict(value)
        value['runtime'] = runtime_snapshot(client)['runtime']
        value['error'] = dict(value['error'])
        value['error']['next_action'] = ('reconcile_request' if value['error'].get('execution_state') != 'not_started'
                                         else value['runtime']['next_action'])
        value['error']['automatic_investigation'] = False
    return value


def classify_failure(exc=None, *, stage='startup', exit_code=None):
    """仅分类自身操作可观测的事实，绝不暴露异常消息或猜安全软件。"""
    code, policy = 'unexpected_exit' if exit_code is not None else 'startup_failed', 'bounded_retry'
    os_error = getattr(exc, 'winerror', None) or getattr(exc, 'errno', None)
    if isinstance(exc, FileNotFoundError):
        code, policy = 'required_file_missing', 'stop'
    elif isinstance(exc, PermissionError):
        code, policy = 'access_denied', 'stop'
    elif isinstance(exc, (ModuleNotFoundError, ImportError)):
        code, policy = 'required_dependency_missing', 'stop'
    elif isinstance(exc, sqlite3.DatabaseError):
        code, policy = 'data_open_failed', 'stop'
    elif isinstance(exc, (json.JSONDecodeError, UnicodeError)):
        code, policy = 'configuration_invalid', 'stop'
    elif isinstance(exc, OSError) and os_error in {28, 39, 112}:
        code, policy = 'storage_unavailable', 'stop'
    elif isinstance(exc, OSError) and os_error in {98, 10048}:
        code, policy = 'address_in_use', 'stop'
    elif isinstance(exc, TimeoutError):
        code = 'startup_timeout'
    return {'code': code, 'cause': 'unknown', 'stage': stage, 'time': time.time(),
            'exit_code': exit_code, 'os_error': os_error,
            'error_type': type(exc).__name__ if exc else None, 'retry_policy': policy,
            'unknown': 'underlying_cause_not_investigated'}


def _request_path(client, request_id):
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', request_id):
        raise ValueError('valid_recovery_request_id_required')
    return client.root/'recovery_requests'/(request_id+'.json')


def recovery_receipt(client, request_id):
    value, observation = read_record(_request_path(client, request_id))
    return value if observation == 'ok' else {'status': 'unknown', 'recovery_request_id': request_id,
                                              'observation': observation, 'automatic_replay': False}


def _wake_manager(client):
    """只唤醒既有正式用户任务；源码/预览不注册或接管正式入口。"""
    from runtime import execution_owner as owner
    if not os.environ.get('KR_ACTIVE_INSTALL_ROOT'):
        return False
    if os.name != 'nt':
        return False
    result = owner.silent_subprocess_run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
        "$ErrorActionPreference='Stop'; Enable-ScheduledTask -TaskName 'KnowledgeRadar Stable Local Console' | Out-Null; Start-ScheduledTask -TaskName 'KnowledgeRadar Stable Local Console'"],
        capture_output=True, timeout=10)
    return result.returncode == 0


def request_recovery(client=None, *, request_id, expected_source, expected_incident=None, action='recover_once'):
    from runtime import execution_owner as owner
    client = client or owner.OwnerClient()
    path = _request_path(client, request_id)
    if action not in {'recover_once', 'rearm_auto_recovery'}:
        raise ValueError('unknown_runtime_recovery_action')
    fingerprint = {'action': action, 'source_identity': expected_source, 'incident_id': expected_incident}
    if expected_source != _identity() or not owner.active_client() or _maintenance(client):
        return {'status': 'rejected', 'reason': 'installation_changed_or_maintenance', 'automatic_replay': False}
    if os.environ.get('KR_CONSOLE_READ_ONLY') == '1' or os.environ.get('KR_CONSOLE_ROLE') == 'dev':
        return {'status': 'rejected', 'reason': 'preview_is_read_only', 'automatic_replay': False}
    # 不接受损坏记录为新的恢复请求，也不覆写旧收据。
    old, observation = read_record(path)
    if observation == 'ok':
        if old.get('fingerprint') != fingerprint:
            return {'status': 'rejected', 'reason': 'recovery_request_identity_conflict'}
        return old
    if observation != 'missing':
        return {'status': 'unknown', 'reason': 'recovery_receipt_unreadable', 'automatic_replay': False}
    state = runtime_snapshot(client)['runtime']
    if state['incident_id'] != expected_incident:
        return {'status': 'rejected', 'reason': 'incident_changed_refresh_status'}
    if state['state'] not in {'ready', 'recovering'} and not state['manual_recovery_allowed']:
        return {'status': 'rejected', 'reason': 'runtime_state_requires_user_investigation', 'runtime': state}
    if state['state'] == 'ready':
        return {'status': 'no_op', 'runtime': state, 'automatic_replay': False}
    if state['state'] == 'recovering':
        return {'status': 'pending', 'recovery_request_id': state['recovery']['request_id'],
                'coalesced': True, 'runtime': state}
    owner.private_owner_directory(client.root)
    with file_write_lock(client.root/'recovery-control'):
        if not owner.active_client() or _maintenance(client):
            return {'status': 'rejected', 'reason': 'installation_changed_or_maintenance'}
        old, observation = read_record(path)
        if observation == 'ok':
            return old if old.get('fingerprint') == fingerprint else {'status': 'rejected', 'reason': 'recovery_request_identity_conflict'}
        if observation != 'missing':
            return {'status': 'unknown', 'reason': 'recovery_receipt_unreadable'}
        pending, pending_observation = read_record(client.root/'start.request.json')
        if pending_observation == 'ok' and pending.get('request_id'):
            prior = recovery_receipt(client, pending['request_id'])
            return {**prior, 'recovery_request_id': pending['request_id'], 'coalesced': True}
        if pending_observation != 'missing':
            return {'status': 'unknown', 'reason': 'pending_control_record_unreadable'}
        state = runtime_snapshot(client)['runtime']
        if state['state'] == 'recovering':
            return {'status': 'pending', 'recovery_request_id': state['recovery']['request_id'], 'coalesced': True}
        if state['state'] == 'ready':
            return {'status': 'no_op', 'runtime': state}
        if state['incident_id'] != expected_incident or not state['manual_recovery_allowed']:
            return {'status': 'rejected', 'reason': 'runtime_changed_refresh_status'}
        # 限制控制收据数量，满时拒绝新意图而不删除既有幂等证据。
        directory = path.parent
        if directory.exists() and sum(1 for _ in directory.glob('*.json')) >= 200:
            return {'status': 'rejected', 'reason': 'recovery_receipt_capacity_reached'}
        value = {'schema': 'kr-runtime-recovery/v1', 'status': 'accepted', 'fingerprint': fingerprint,
                 'recovery_request_id': request_id, 'accepted_at': time.time(), 'automatic_replay': False}
        owner._write_json(path, value)
        owner._write_json(client.root/'start.request.json', {'request_id': request_id, **fingerprint})
    if state['supervisor']['status'] != 'available':
        try:
            woke = _wake_manager(client)
        except (OSError, RuntimeError, TimeoutError, subprocess.TimeoutExpired):
            woke = False
        if not woke:
            # 请求保留，不能因唤醒超时推断未执行而重发。
            return {**value, 'status': 'unknown', 'reason': 'manager_wakeup_unconfirmed'}
    return value


def update_recovery(client, request_id, status, **details):
    """监督者更新自身已接受的操作，不触发其他请求。"""
    from runtime.execution_owner import _write_json
    path = _request_path(client, request_id)
    value, observation = read_record(path)
    if observation != 'ok':
        return
    _write_json(path, {**value, 'status': status, 'updated_at': time.time(), **details})


def console_runtime_action(action, payload):
    """复用控制台权限；网页只允许单次恢复，不接收维护命令或路径。"""
    from runtime.execution_owner import OwnerClient
    if os.environ.get('KR_CONSOLE_ROLE') == 'dev' or os.environ.get('KR_CONSOLE_READ_ONLY') == '1':
        return {'status': 'rejected', 'reason': 'preview_is_read_only'}
    allowed = {'recover': {'request_id', 'expected_source', 'expected_incident'},
               'receipt': {'request_id'}, 'wait': {'wait_seconds'}}
    if action not in allowed or set(payload)-allowed[action]:
        raise ValueError('invalid_runtime_control_fields')
    if action == 'wait':
        seconds = payload.get('wait_seconds', 0)
        if type(seconds) not in (int, float) or not 0 <= seconds <= 30:
            raise ValueError('invalid_runtime_wait')
        return runtime_snapshot(wait_seconds=seconds)
    _request_path(OwnerClient(), payload.get('request_id'))
    if action == 'receipt':
        return recovery_receipt(OwnerClient(), payload['request_id'])
    if not isinstance(payload.get('expected_source'), str) or not payload['expected_source']:
        raise ValueError('expected_source_required')
    if payload.get('expected_incident') is not None and not isinstance(payload['expected_incident'], str):
        raise ValueError('invalid_expected_incident')
    return request_recovery(request_id=payload['request_id'], expected_source=payload['expected_source'],
                            expected_incident=payload.get('expected_incident'))


def static_catalog(name=''):
    """从构建时生成的同源合同读取；不依赖客户端已装载业务注册表。"""
    path = Path(__file__).resolve().parents[2]/'config/codex-product/tool-contracts.json'
    with path.open('rb') as stream:
        raw = stream.read(2*1024*1024+1)
    if len(raw) > 2*1024*1024:
        raise ValueError('bounded_tool_contract_required')
    data = json.loads(raw)
    cards = data['tools']
    if data.get('schema') != 'kr-tool-catalog/v1' or data.get('tool_count') != len(cards):
        raise ValueError('invalid_packaged_tool_contract')
    if name:
        card = next((item for item in cards if item['name'] == name), None)
        if card is None:
            raise ValueError('unknown_tool_use_catalog')
        return {'schema': 'kr-tool-contract/v1', 'contract_hash': data['contract_hash'], 'tool': card}
    from runtime.application import research_workflow_identity
    return {**data, 'tools': [{k: v for k, v in item.items()
                             if k not in {'input_schema', 'output_schema', 'error_schema', 'usage'}} for item in cards],
            'research_workflow': research_workflow_identity()}
