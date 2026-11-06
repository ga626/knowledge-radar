"""安装级唯一执行所有者及薄客户端；只传小请求和本机文件引用。"""
from __future__ import annotations
from runtime.research_storage import guarded_transaction

from contextlib import ExitStack, contextmanager
from functools import lru_cache
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

import httpx
from pydantic_core import to_jsonable_python

from runtime.file_write_lock import file_write_lock
from runtime.paths import runtime_state_dir
from runtime.process_identity import process_identity

PROTOCOL = 1
MAX_REQUEST = 2 * 1024 * 1024


def private_owner_directory(root):
    """IPC凭据只授权本机当前用户与SYSTEM；不改变其他运行数据权限。"""
    if any(p.is_symlink() or p.is_junction() for p in (root, *root.parents)):
        raise ValueError('owner_directory_link_rejected')
    root.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        root.chmod(0o700)
        return
    import ctypes
    from ctypes import wintypes
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    adv.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    token = wintypes.HANDLE()
    sid_text = wintypes.LPWSTR()
    descriptor = ctypes.c_void_p()
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        length = wintypes.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        buffer = ctypes.create_string_buffer(length.value)
        if not adv.GetTokenInformation(token, 1, buffer, length, ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        if not adv.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        sddl = f'D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{sid_text.value})'
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if not adv.SetFileSecurityW(str(root), 0x80000004, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)
        if sid_text:
            kernel.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        kernel.CloseHandle(token)


@lru_cache(maxsize=1)
def source_identity():
    # 身份查询不能导入任务库或创建运行态。
    root = Path(__file__).resolve().parents[1]
    hashes = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in root.rglob('*') if path.is_file() and path.suffix in {'.py', '.js', '.mjs'}
              and not any(part in path.parts for part in {'__pycache__', 'data'})}
    return hashlib.sha256(json.dumps(hashes, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def owner_root():
    return Path(os.environ.get('KR_OWNER_ROOT') or runtime_state_dir().resolve() / 'execution_owner')


def active_client():
    root = os.environ.get('KR_ACTIVE_INSTALL_ROOT')
    if not root:
        return True
    try:
        active = json.loads((Path(root)/'active.json').read_text(encoding='utf-8'))
        return Path(active['program_root']).resolve() == Path(__file__).resolve().parents[2]
    except (OSError, ValueError, KeyError, TypeError):
        return False


def failure(code, *, started=False, request_id='', layer='runtime_transport'):
    from runtime.agent_contracts import error_result
    result = error_result(ValueError(code), '', not_started=not started, layer=layer)
    result['error']['request_id'] = request_id
    result['error']['next_action'] = (
        'Inspect this request receipt and task before retry; repeat only the same request_id.' if started
        else 'Inspect runtime status and component versions; explicitly start the matching runtime if absent.')
    return result


class OwnerClient:
    def __init__(self, *, root=None, timeout=900):
        self.root = Path(root) if root else owner_root()
        self.timeout = timeout
        self.last_receipt = None

    def descriptor(self):
        try:
            value = json.loads((self.root / 'owner.json').read_text(encoding='utf-8'))
            if not isinstance(value, dict) or type(value.get('pid')) is not int or value['pid'] <= 0 or not value.get('process_identity'):
                return None
            if process_identity(value.get('pid')) != value['process_identity']:
                return None
            return value
        except (OSError, ValueError, TypeError):
            return None

    def _request(self, descriptor, path, payload=None, *, timeout=None, management=False):
        port = descriptor.get('port')
        if type(port) is not int or not 0 < port < 65536:
            raise ValueError('invalid_owner_descriptor')
        with httpx.Client(trust_env=False, timeout=timeout or self.timeout) as client:
            response = client.post(f'http://127.0.0.1:{port}/{path}',
                headers={'Authorization': 'Bearer ' + str(descriptor.get('token', ''))},
                json={**(payload or {}), 'generation': descriptor.get('generation'),
                      'source_identity': descriptor.get('source_identity') if management else source_identity(), 'protocol': PROTOCOL})
            response.raise_for_status()
            return response.json()

    def status(self):
        """纯查询：不创建目录，不启动、恢复或替换运行时。"""
        descriptor = self.descriptor()
        if not descriptor:
            return {'status': 'unavailable', 'layer': 'execution_owner', 'next_action': 'runtime start'}
        if descriptor.get('source_identity') != source_identity() or descriptor.get('protocol') != PROTOCOL:
            return {'status': 'incompatible', 'layer': 'execution_owner',
                    'pid': descriptor['pid'], 'protocol': descriptor.get('protocol'),
                    'source_identity': descriptor.get('source_identity'),
                    'next_action': 'Keep the active owner; install or select a matching client.'}
        try:
            return self._request(descriptor, 'status', timeout=3)
        except (httpx.HTTPError, ValueError):
            return {'status': 'unreachable', 'layer': 'runtime_transport', 'pid': descriptor['pid']}

    def start(self, *, timeout=30):
        """显式启动；竞争客户端只选主，不杀死或覆盖不同身份的有效owner。"""
        private_owner_directory(self.root)
        with file_write_lock(self.root / 'launch'):
            if not active_client():
                return {'status':'incompatible','layer':'product_identity','next_action':'Reconnect using the active installation.'}
            current = self.status()
            if current['status'] != 'unavailable':
                return current
            env = dict(os.environ, KR_RUNTIME_ROLE='owner')
            source = Path(__file__).resolve().parents[1]
            command = [sys.executable, '-X', 'utf8', '-c',
                       'import sys,runpy; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module("runtime.execution_owner",run_name="__main__")', str(source)]
            env['PYTHONPATH'] = str(source)
            env['KR_OWNER_ROOT'] = str(self.root.resolve())
            kwargs = {'cwd': str(source.parent), 'env': env, 'stdin': subprocess.DEVNULL,
                      'stdout': subprocess.DEVNULL, 'close_fds': True}
            if os.name == 'nt':
                # DETACHED_PROCESS只脱离控制台，仍会继承宿主的KILL_ON_JOB_CLOSE。
                # 显式启动的安装owner独立于单次宿主/命令；禁止失败后退回短命子进程。
                kwargs['creationflags'] = (subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
                                           | subprocess.CREATE_BREAKAWAY_FROM_JOB)
            else:
                kwargs['start_new_session'] = True
            with (self.root / 'startup.log').open('ab') as log:
                try:
                    child = subprocess.Popen(command, stderr=log, **kwargs)
                except OSError as exc:
                    if os.name == 'nt' and exc.winerror == 5:
                        raise RuntimeError('execution_owner_job_breakaway_denied_start_from_user_terminal') from exc
                    raise
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                current = self.status()
                if current['status'] == 'ready':
                    return current
                if child.poll() is not None:
                    raise RuntimeError('execution_owner_start_failed')
                time.sleep(.05)
            # 不杀死已开始恢复的owner，也不启动第二个；调用方可显式再查询。
            raise TimeoutError('execution_owner_start_timeout_inspect_status')

    def call(self, name, arguments, *, request_id=None):
        request_id = request_id or uuid.uuid4().hex
        self.last_receipt = {'request_id': request_id, 'operation': name, 'execution_state': 'not_started'}
        if not active_client():
            return failure('inactive_installation_client', request_id=request_id, layer='product_identity')
        descriptor = self.descriptor()
        if not descriptor:
            return failure('execution_owner_unavailable', request_id=request_id)
        if descriptor.get('source_identity') != source_identity() or descriptor.get('protocol') != PROTOCOL:
            return failure('execution_owner_identity_mismatch', request_id=request_id)
        try:
            reply = self._request(descriptor, 'call', {'name': name,
                'arguments': to_jsonable_python(arguments), 'request_id': request_id, 'include_receipt': True})
            if isinstance(reply, dict) and reply.get('schema') == 'kr-operation-reply/v1':
                self.last_receipt = reply['execution_receipt']
                return reply['result']
            return reply
        except (httpx.HTTPError, ValueError):
            self.last_receipt['execution_state'] = 'unknown'
            # 不知道对端是否已执行，绝不自动改走另一入口重发。
            return failure('execution_outcome_unknown', started=True, request_id=request_id)

    def receipt(self, request_id):
        descriptor = self.descriptor()
        if not descriptor:
            return failure('execution_owner_unavailable', request_id=request_id)
        try:
            return self._request(descriptor, 'receipt', {'request_id': request_id}, timeout=3)
        except (httpx.HTTPError, ValueError):
            return failure('execution_outcome_unknown', started=True, request_id=request_id)

    def receipts(self, limit=20):
        descriptor = self.descriptor()
        if not descriptor:
            return failure('execution_owner_unavailable')
        try:
            return self._request(descriptor, 'receipts', {'limit': limit}, timeout=3)
        except (httpx.HTTPError, ValueError):
            return failure('execution_owner_receipts_unavailable')

    def stop(self):
        """安装维护动作：只停空闲owner；不同源码客户端也可请求旧版本安全排空。"""
        descriptor = self.descriptor()
        if not descriptor:
            return {'status': 'unavailable'}
        if descriptor.get('protocol') != PROTOCOL:
            return failure('execution_owner_protocol_mismatch')
        try:
            return self._request(descriptor, 'stop', timeout=5, management=True)
        except (httpx.HTTPError, ValueError):
            return failure('execution_outcome_unknown', started=True)


class RequestReceipts:
    """只存请求哈希与结果，不存原始入参；未知结果跨owner代际不自动重发。"""
    def __init__(self, root, generation):
        self.path = root / 'requests.sqlite3'
        self.generation = generation
        with self.connection() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, fingerprint TEXT, generation TEXT, status TEXT, result TEXT)')
            columns = {row[1] for row in conn.execute('PRAGMA table_info(requests)')}
            if 'name' not in columns:
                conn.execute('ALTER TABLE requests ADD COLUMN name TEXT')
            if 'created_at' not in columns:
                conn.execute('ALTER TABLE requests ADD COLUMN created_at REAL')
            conn.execute("UPDATE requests SET status='interrupted' WHERE status='running'")

    @guarded_transaction
    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def reserve(self, request_id, name, arguments):
        digest = hashlib.sha256(json.dumps([name, arguments], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT fingerprint,status,result FROM requests WHERE id=?', (request_id,)).fetchone()
            if row:
                if row[0] != digest:
                    return failure('request_identity_conflict', request_id=request_id)
                if row[1] == 'completed':
                    return json.loads(row[2])
                return failure('request_' + row[1], started=True, request_id=request_id)
            if conn.execute('SELECT count(*) FROM requests').fetchone()[0] >= 100000:
                return failure('request_receipt_capacity_reached', request_id=request_id)
            conn.execute('INSERT INTO requests(id,fingerprint,generation,status,result,name,created_at) VALUES(?,?,?,?,?,?,?)',
                         (request_id, digest, self.generation, 'running', None, name, time.time()))
        return None

    def finish(self, request_id, value):
        from kr_core.source_urls import public_result_payload
        value = public_result_payload(value)
        with self.connection() as conn:
            if isinstance(value, dict) and value.get('status') == 'error' and isinstance(value.get('error'), dict) and value['error'].get('execution_state') == 'not_started':
                conn.execute('DELETE FROM requests WHERE id=?', (request_id,))
            else:
                conn.execute("UPDATE requests SET status='completed',result=? WHERE id=?",
                             (json.dumps(value, ensure_ascii=False), request_id))
        return value

    def read(self, request_id):
        with self.connection() as conn:
            row = conn.execute('SELECT status,result FROM requests WHERE id=?', (request_id,)).fetchone()
        return {'status': row[0] if row else 'not_found', 'request_id': request_id,
                'result': json.loads(row[1]) if row and row[1] else None}

    def active(self):
        with self.connection() as conn:
            return conn.execute("SELECT count(*) FROM requests WHERE status='running'").fetchone()[0]

    def recent(self, limit):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('bounded_receipt_limit_required')
        with self.connection() as conn:
            rows = conn.execute('SELECT id,name,status,created_at FROM requests ORDER BY rowid DESC LIMIT ?', (limit,)).fetchall()
        return {'status':'ok','requests':[dict(zip(('request_id','operation','status','created_at'), row)) for row in rows]}


def serve(*, root=None, bootstrap=None):
    root = Path(root or os.environ.get('KR_OWNER_ROOT') or owner_root())
    private_owner_directory(root)
    root = root.resolve()
    os.environ['KR_RUNTIME_ROLE'] = 'owner'
    # 整个生命周期持有同一锁文件，进程退出由OS释放；不得删除或替换锁文件。
    with file_write_lock(root / 'singleton'), ExitStack() as cleanup:
        if bootstrap is None:
            from runtime import composition
            bootstrap = composition._bootstrap_persistent_runtime
            from runtime.desktop_notification import start_notification_outbox_worker
            cleanup.callback(start_notification_outbox_worker().set)
        stop = bootstrap()
        if stop is not None:
            cleanup.callback(stop.set)
        from runtime.application import catalog
        generation = uuid.uuid4().hex
        descriptor = {'schema': 'kr-execution-owner/v1', 'protocol': PROTOCOL,
                      'pid': os.getpid(), 'process_identity': process_identity(),
                      'generation': generation, 'source_identity': source_identity(),
                      'contract_hash': catalog()['contract_hash'], 'token': secrets.token_urlsafe(32)}
        receipts = RequestReceipts(root, generation)
        admission = threading.RLock()
        draining = False
        active_calls = 0

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # 不把token、请求参数或正文写到HTTP访问日志。

            def send_json(self, value, status=200):
                raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # 调用方断连不取消已经接受的工作。

            def do_POST(self):
                self.connection.settimeout(15)
                if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + descriptor['token']):
                    self.send_json(failure('owner_authorization_required'), 403)
                    return
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= MAX_REQUEST:
                        raise ValueError('bounded_request_required')
                    body = json.loads(self.rfile.read(size))
                    if not isinstance(body, dict):
                        raise ValueError('object_required')
                    if (body.get('protocol') != PROTOCOL or body.get('generation') != generation
                            or body.get('source_identity') != descriptor['source_identity']):
                        self.send_json(failure('execution_owner_identity_mismatch'))
                        return
                    self.handle_request(body)
                except (ValueError, TypeError, TimeoutError):
                    self.send_json(failure('invalid_owner_request'), 400)

            def handle_request(self, body):
                nonlocal draining, active_calls
                if self.path == '/status':
                    self.send_json({k: v for k, v in descriptor.items() if k != 'token'} | {'status': 'draining' if draining else 'ready'})
                    return
                if self.path == '/stop':
                    from runtime.tasks import get_task_store
                    from runtime.leases import get_runtime_lease_coordinator
                    with admission:
                        store = get_task_store()
                        tasks = store.managed_tasks(adapter='durable_process', statuses=('queued', 'running'))
                        from runtime.research_ledger import has_pending_submissions
                        pending = store.managed_tasks(adapter='durable_process', statuses=('completed', 'failed', 'cancelled', 'interrupted'), recording_pending=True)
                        if active_calls or receipts.active() or tasks or pending or has_pending_submissions() or get_runtime_lease_coordinator().active_leases(limit=1):
                            self.send_json({'status': 'busy', 'layer': 'execution_owner', 'next_action': 'Wait for accepted work; no task was cancelled.'})
                            return
                        draining = True
                    self.send_json({'status': 'stopping', 'pid': os.getpid()})
                    threading.Thread(target=httpd.shutdown, daemon=True).start()
                    return
                if self.path == '/receipts':
                    self.send_json(receipts.recent(body.get('limit', 20)))
                    return
                request_id = body.get('request_id', '')
                if not isinstance(request_id, str) or not request_id.isascii() or not 1 <= len(request_id) <= 128 or not all(c.isalnum() or c in '-_' for c in request_id):
                    raise ValueError('invalid_request_id')
                if self.path == '/receipt':
                    self.send_json(receipts.read(request_id))
                    return
                if self.path != '/call':
                    self.send_json(failure('unknown_owner_operation'), 404)
                    return
                from runtime.application import OPERATIONS, submit_application
                from runtime.agent_contracts import error_result
                name, arguments = body.get('name'), body.get('arguments')
                if isinstance(arguments, dict):
                    arguments = {key: value for key, value in arguments.items() if key != 'request_id'}
                operation = OPERATIONS.get(name) if isinstance(name, str) else None
                try:
                    if operation is None:
                        raise ValueError('unknown_tool_use_catalog')
                    arguments = to_jsonable_python(operation.validate(arguments))
                    arguments.pop('request_id', None)
                except Exception as exc:
                    self.send_json(error_result(exc, str(name), not_started=True))
                    return
                with admission:
                    if draining:
                        self.send_json(failure('execution_owner_draining', request_id=request_id))
                        return
                    value = None if operation.read_only else receipts.reserve(request_id, name, arguments)
                    if value is None:
                        active_calls += 1
                if value is not None:
                    self.send_reply(body, value, request_id, name, reused=True)
                    return
                try:
                    try:
                        from runtime.application import request_scope
                        with request_scope(request_id):
                            value = submit_application(name, arguments).result()
                    except Exception as exc:
                        value = error_result(exc, name)
                    if not operation.read_only:
                        value = receipts.finish(request_id, value)
                finally:
                    with admission:
                        active_calls -= 1
                self.send_reply(body, value, request_id, name)

            def send_reply(self, body, value, request_id, name, *, reused=False):
                if body.get('include_receipt'):
                    error = value.get('error') if isinstance(value, dict) else None
                    state = error.get('execution_state', 'unknown') if isinstance(error, dict) else 'completed'
                    value = {'schema': 'kr-operation-reply/v1', 'result': value,
                        'execution_receipt': {'request_id': request_id, 'operation': name,
                            'generation': generation, 'source_identity': descriptor['source_identity'],
                            'execution_state': state, 'reused': reused,
                            'lookup': {'tool': 'get_task_status', 'receipt_request_id': request_id}}}
                self.send_json(value)

        httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        httpd.daemon_threads = True
        cleanup.callback(httpd.server_close)
        descriptor['port'] = httpd.server_address[1]
        path = root / 'owner.json'
        temporary = root / ('owner-' + generation + '.tmp')
        temporary.write_text(json.dumps(descriptor), encoding='utf-8')
        temporary.replace(path)
        print(json.dumps({'event': 'execution_owner_ready', 'pid': descriptor['pid'],
                          'generation': generation, 'source_identity': descriptor['source_identity']}),
              file=sys.stderr, flush=True)
        try:
            httpd.serve_forever(poll_interval=.2)
        finally:
            print(json.dumps({'event': 'execution_owner_stopped', 'pid': descriptor['pid'],
                              'generation': generation}), file=sys.stderr, flush=True)
            # 锁内清理本代描述；不得误删后继代际状态。
            if path.exists() and json.loads(path.read_text(encoding='utf-8')).get('generation') == generation:
                path.unlink()


def main():
    serve()


if __name__ == '__main__':
    main()
