"""GitHub 只读传输：请求前冻结身份，不因空结果、权限或限流切换账号。"""
from __future__ import annotations

from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlencode, urlsplit

import httpx
from runtime.proxy_config import http_client

from runtime.execution_context import check_execution, current_execution, execution_value
from runtime.file_write_lock import file_write_lock
from runtime.paths import runtime_state_dir
from runtime.process import silent_subprocess_popen, silent_subprocess_run

CONTRACT = 'github-readonly/v2'
_AUTH_CACHE = {}
_AUTH_LOCK = threading.RLock()


def enabled(name, default='1'):
    return os.environ.get(name, default).strip().lower() in {'1', 'true', 'yes', 'on'}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class GitHubError(RuntimeError):
    def __init__(self, code, message, *, retryable=False, **details):
        self.code, self.error_code = code, code.lower()
        self.retryable, self.details = retryable, details
        super().__init__(message)

    def result(self):
        return {'status': 'failed', 'platform': 'GitHub', 'failure_type': self.code.lower(),
                'error': {'type': self.code, 'message': str(self), 'retryable': self.retryable,
                          **self.details}}

    @property
    def metadata(self):
        return {'failure_code': self.code, **self.details}


def _deadline(seconds):
    current = current_execution()
    if current:
        seconds = min(seconds, max(0, current['deadline_at'] - time.time()))
    return time.monotonic() + seconds


def _remaining(deadline):
    check_execution()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GitHubError('TIMEOUT', 'GitHub request deadline exceeded.', retryable=True)
    return remaining


def _gh_exe():
    explicit = os.environ.get('KR_GH_CLI_EXE', '').strip()
    return explicit or shutil.which('gh') or ''


def _run_gh(args, *, timeout=20, max_bytes=4 * 1024 * 1024, auth_env=None):
    """固定参数子进程；输出有界，取消只终止本次创建的进程。"""
    exe = _gh_exe()
    if not exe or not Path(exe).is_file():
        raise GitHubError('DEPENDENCY_MISSING', 'gh is unavailable; select REST or configure KR_GH_CLI_EXE.')
    env = dict(os.environ)
    if auth_env is not None:
        for key, value in auth_env.items():
            if value is None: env.pop(key, None)
            else: env[key] = value
    env.update(GH_PROMPT_DISABLED='1', GH_PAGER='cat', GH_NO_UPDATE_NOTIFIER='1', NO_COLOR='1')
    env.pop('GH_DEBUG', None)
    from runtime.proxy_config import httpx_options
    route = httpx_options('https://api.github.com', adapter='gh-child')
    # 仅本次子进程继承同一路线；不改全局环境/gh账号或代理配置。
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        env.pop(key, None)
    if route['proxy']:
        env['HTTPS_PROXY'] = route['proxy']
        env['HTTP_PROXY'] = route['proxy']
    deadline = _deadline(timeout)
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = silent_subprocess_popen([exe, *args], stdin=subprocess.DEVNULL,
                                             stdout=stdout, stderr=stderr, env=env)
        except OSError:
            raise GitHubError('DEPENDENCY_UNAVAILABLE', 'gh could not start; check its executable and local permissions.') from None
        try:
            while process.poll() is None:
                _remaining(deadline)
                if os.fstat(stdout.fileno()).st_size > max_bytes or os.fstat(stderr.fileno()).st_size > 256 * 1024:
                    raise GitHubError('CONTENT_TOO_LARGE', 'GitHub response exceeded the bounded read budget.')
                time.sleep(.05)
            _remaining(deadline)
            stdout.seek(0); stderr.seek(0)
            data, error = stdout.read(max_bytes + 1), stderr.read(256 * 1024)
            if len(data) > max_bytes:
                raise GitHubError('CONTENT_TOO_LARGE', 'GitHub response exceeded the bounded read budget.')
            return process.returncode, data, error
        finally:
            if process.poll() is None:
                if os.name == 'nt':
                    taskkill = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/taskkill.exe'
                    silent_subprocess_run([str(taskkill), '/PID', str(process.pid), '/T', '/F'],
                                          capture_output=True, timeout=5)
                else:
                    process.kill()
                process.wait(timeout=5)


def _auth_environment():
    return {key: os.environ.get(key) for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_CONFIG_DIR', 'GH_HOST')}


def _auth_stamp():
    dirs = [os.environ.get('GH_CONFIG_DIR', ''), str(Path.home() / '.config/gh'),
            str(Path(os.environ.get('APPDATA', str(Path.home()))) / 'GitHub CLI')]
    stamps = [(d, (Path(d) / 'hosts.yml').stat().st_mtime_ns) for d in dirs
              if d and (Path(d) / 'hosts.yml').is_file()]
    return fingerprint([_gh_exe(), stamps, _auth_environment(), os.environ.get('KR_GITHUB_AUTH_EPOCH', '')])


def _gh_identity():
    key = _auth_stamp()
    with _AUTH_LOCK:
        cached = _AUTH_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < 20:
            return cached[1]
    code, raw, error = _run_gh(['auth', 'status', '--active', '--hostname', 'github.com', '--json', 'hosts'],
                               timeout=8, max_bytes=128 * 1024)
    try:
        hosts = json.loads(raw).get('hosts', {}).get('github.com', [])
    except (ValueError, AttributeError):
        if 'not logged' in error.decode('utf-8', 'replace').lower():
            return None
        raise GitHubError('AUTH_CHECK_FAILED', 'Cannot determine the active gh identity; no anonymous fallback.') from None
    if not hosts:
        return None
    active = next((row for row in hosts if row.get('active')), None)
    if not active or active.get('state') != 'success':
        raise GitHubError('AUTH_INVALID', 'Active github.com authentication is invalid; repair gh auth before retrying.')
    identity = fingerprint([active.get('login'), active.get('scopes'), active.get('tokenSource'), key])
    with _AUTH_LOCK:
        _AUTH_CACHE.clear()
        _AUTH_CACHE[key] = (time.monotonic(), identity)
    return identity


def _state_path():
    return Path(runtime_state_dir()) / 'github-api-cooldowns.json'


def _read_state():
    try:
        value = json.loads(_state_path().read_text('utf-8'))
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise GitHubError('RATE_STATE_UNAVAILABLE', 'Cannot safely read GitHub cooldown state.') from None


def _cooldown(identity, bucket):
    path = _state_path()
    with file_write_lock(path):
        state = _read_state()
    return max(float(state.get(identity + ':' + name, 0)) for name in ('all', bucket))


def resource_ready(resource):
    """由平台注册钩子调用；公共调度器无需认识 GitHub。"""
    if not resource.startswith('github_api:'):
        return None
    _, identity, bucket = resource.split(':', 2)
    if _cooldown(identity, bucket) > time.time():
        return False
    from runtime.leases import get_runtime_lease_coordinator
    return len(get_runtime_lease_coordinator().resource_leases('github_api', identity)) < 2


def _save_cooldown(identity, bucket, until):
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_write_lock(path):
        state = {k: v for k, v in _read_state().items() if isinstance(v, (float, int)) and v > time.time()}
        key = identity + ':' + bucket
        state[key] = max(float(state.get(key, 0)), until)
        temporary = path.with_suffix('.pending')
        temporary.write_text(json.dumps(state), encoding='utf-8')
        os.replace(temporary, path)


def _retry_at(headers):
    value = headers.get('retry-after', '')
    try:
        return time.time() + max(0, float(value))
    except ValueError:
        try:
            return parsedate_to_datetime(value).timestamp()
        except (ValueError, TypeError, OverflowError):
            return time.time() + 60


@dataclass
class GitHubClient:
    transport: str = ''
    identity: str = ''
    token: str = field(default='', repr=False)
    timeout: float = 20
    attempts: list = field(default_factory=list)
    auth_env: dict = field(default_factory=dict, repr=False)
    auth_stamp: str = field(default='', repr=False)

    @classmethod
    def configured(cls, *, timeout=20):
        if not enabled('KR_GITHUB_ENABLED'):
            raise GitHubError('PROVIDER_DISABLED', 'GitHub source is disabled by KR_GITHUB_ENABLED.')
        if os.environ.get('KR_GITHUB_HOST', 'github.com') != 'github.com':
            raise GitHubError('UNSUPPORTED_HOST', 'This adapter supports github.com; enterprise hosts require an explicit adapter.')
        transport = os.environ.get('KR_GITHUB_TRANSPORT', 'auto').strip().lower()
        if transport not in {'auto', 'rest', 'gh'}:
            raise GitHubError('INVALID_CONFIGURATION', 'KR_GITHUB_TRANSPORT must be auto, rest or gh.')
        token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN') or ''
        epoch = os.environ.get('KR_GITHUB_AUTH_EPOCH', '')
        if transport != 'gh' and token:
            return cls('rest', fingerprint(['github.com', 'token', token, epoch]), token, timeout)
        if transport == 'gh' or (transport == 'auto' and enabled('KR_GH_CLI_ENABLED') and _gh_exe()):
            if not enabled('KR_GH_CLI_ENABLED'):
                raise GitHubError('PROVIDER_DISABLED', 'gh transport is disabled; select REST explicitly.')
            identity = _gh_identity()
            if identity:
                return cls('gh', fingerprint(['github.com', 'gh', identity, epoch]), '', timeout,
                           auth_env=_auth_environment(), auth_stamp=_auth_stamp())
            if transport == 'gh':
                raise GitHubError('AUTH_REQUIRED', 'gh has no active github.com login; authenticate or select public REST.')
        return cls('rest', fingerprint(['github.com', 'anonymous', epoch]), '', timeout)

    def _resource(self, bucket):
        return 'github_api:' + self.identity + ':' + bucket

    def _defer(self, bucket, code, message, **details):
        if execution_value('KR_DURABLE_WORKER') == '1':
            from runtime.resource_concurrency import ResourceDeferred
            raise ResourceDeferred(self._resource(bucket))
        raise GitHubError(code, message, retryable=True, **details)

    def get(self, endpoint, *, params=None, accept='application/vnd.github+json', max_bytes=4 * 1024 * 1024):
        check_execution()
        # Internal callers choose objects, never arbitrary URLs or mutation endpoints.
        if not re.fullmatch(r'(?:repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[^?#]*)?|search/(?:repositories|issues|code)|rate_limit)', endpoint):
            raise GitHubError('INVALID_ENDPOINT', 'Only the declared GitHub read endpoints are allowed.')
        if any(part in {'.', '..'} for part in endpoint.split('/')) or '\\' in endpoint:
            raise GitHubError('INVALID_ENDPOINT', 'Unsafe GitHub path.')
        bucket = 'code_search' if endpoint == 'search/code' else 'search' if endpoint.startswith('search/') else 'core'
        until = _cooldown(self.identity, bucket)
        if until > time.time():
            self._defer(bucket, 'RATE_LIMITED', 'GitHub cooldown is active; do not switch identities.',
                        retry_at=until, retry_after_s=round(until - time.time(), 2), next_action='Wait until retry_at, then resume the same task.')
        from runtime.leases import default_owner, get_runtime_lease_coordinator
        coordinator = get_runtime_lease_coordinator()
        lease = coordinator.acquire_bounded('github_api', self.identity, limit=2,
            owner=default_owner('github_api'), ttl_s=int(self.timeout) + 5)
        if not lease.acquired:
            self._defer(bucket, 'RESOURCE_BUSY', 'GitHub request slots are occupied.', retry_after_s=.2)
        path = endpoint + ('?' + urlencode(params) if params else '')
        started = time.monotonic()
        attempt = {'transport': self.transport, 'endpoint': endpoint.split('?')[0], 'method': 'GET', 'status': 'started'}
        self.attempts.append(attempt)
        try:
            status, headers, body = (self._gh_get(path, accept, max_bytes) if self.transport == 'gh'
                                     else self._rest_get(path, accept, max_bytes))
            headers = {k.lower(): v for k, v in headers.items()}
            attempt.update(status=status, elapsed_s=round(time.monotonic() - started, 3))
            if status >= 400:
                self._raise_http(status, headers, body, bucket)
            if headers.get('x-ratelimit-remaining') == '0':
                try:
                    _save_cooldown(self.identity, headers.get('x-ratelimit-resource', bucket), max(time.time() + 1, float(headers['x-ratelimit-reset'])))
                except (ValueError, KeyError):
                    pass
            check_execution()
            return body, headers
        except GitHubError as exc:
            attempt.update(status=exc.code, elapsed_s=round(time.monotonic() - started, 3))
            exc.details.setdefault('transport', self.transport)
            exc.details.setdefault('attempts', [dict(row) for row in self.attempts])
            raise
        finally:
            coordinator.release(lease.lease_id)

    def json(self, endpoint, *, params=None):
        body, headers = self.get(endpoint, params=params)
        try:
            return json.loads(body), headers
        except (ValueError, UnicodeError):
            raise GitHubError('SCHEMA_CHANGED', 'GitHub returned a non-JSON response.') from None

    def _raise_http(self, status, headers, body, bucket):
        try:
            message = str(json.loads(body).get('message', '')).lower()
        except (ValueError, AttributeError, UnicodeError):
            message = ''
        info = {'http_status': status, 'request_id': headers.get('x-github-request-id', '')}
        primary = headers.get('x-ratelimit-remaining') == '0'
        throttled = status == 429 or (status == 403 and (primary or 'retry-after' in headers or 'rate limit' in message or 'abuse' in message))
        if throttled:
            until = _retry_at(headers)
            if primary:
                try: until = max(time.time() + 1, float(headers['x-ratelimit-reset']))
                except (KeyError, ValueError): pass
            scope = headers.get('x-ratelimit-resource', bucket) if primary else 'all'
            _save_cooldown(self.identity, scope, until)
            self._defer(bucket, 'RATE_LIMITED', 'GitHub rate limit; preserve this identity and resume after cooldown.',
                        **info, limit_kind='primary' if primary else 'secondary', retry_at=until,
                        retry_after_s=max(0, round(until - time.time(), 2)), next_action='Wait until retry_at, then resume the same task.')
        codes = {401: ('AUTH_INVALID', 'Authentication rejected; repair the configured credential.'),
                 403: ('PERMISSION_DENIED', 'GitHub denied access; inspect repository/organization authorization, not browser login.'),
                 404: ('NOT_FOUND_OR_INACCESSIBLE', 'Object not found or not visible to this identity.'),
                 422: ('INVALID_QUERY_OR_OBJECT', 'GitHub rejected the query, ref or path; check qualifiers and object identity.'),
                 451: ('LEGAL_RESTRICTION', 'Content is legally restricted.')}
        code, text = codes.get(status, ('UPSTREAM_ERROR', 'GitHub request failed; inspect its status before retrying.'))
        raise GitHubError(code, text, retryable=status >= 500, **info,
                          next_action='Correct the indicated authorization or input; do not change identity automatically.')

    def _gh_get(self, path, accept, max_bytes):
        if self.auth_stamp and self.auth_stamp != _auth_stamp():
            raise GitHubError('AUTH_CONTEXT_CHANGED', 'gh identity/configuration changed during this request; start a new explicit request.')
        _, data, error = _run_gh(['api', path, '--method', 'GET', '--hostname', 'github.com', '--include',
            '--header', 'Accept: ' + accept, '--header', 'X-GitHub-Api-Version: 2022-11-28'],
            timeout=self.timeout, max_bytes=max_bytes + 32768, auth_env=self.auth_env or None)
        match = re.match(rb'HTTP/\S+\s+(\d+)[^\r\n]*\r?\n', data)
        separator = re.search(rb'\r?\n\r?\n', data)
        if not match or not separator:
            # Never expose stderr: it can contain authentication debug material.
            code = 'TIMEOUT' if b'timeout' in error.lower() else 'NETWORK_ERROR'
            raise GitHubError(code, 'gh API transport failed before a complete HTTP response.', retryable=True)
        headers = {}
        for line in data[match.end():separator.start()].splitlines():
            key, sep, value = line.partition(b':')
            if sep: headers[key.decode('ascii', 'replace').lower()] = value.strip().decode('latin1')
        body = data[separator.end():]
        if len(body) > max_bytes:
            raise GitHubError('CONTENT_TOO_LARGE', 'GitHub response exceeded the bounded read budget.')
        return int(match.group(1)), headers, body

    def _rest_get(self, path, accept, max_bytes):
        headers = {'Accept': accept, 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'KnowledgeRadar'}
        if self.token: headers['Authorization'] = 'Bearer ' + self.token
        deadline = _deadline(self.timeout)
        url = 'https://api.github.com/' + path
        try:
            with http_client(follow_redirects=False) as client:
                for _ in range(4):
                    remaining = _remaining(deadline)
                    with client.stream('GET', url, headers=headers, timeout=httpx.Timeout(min(remaining, 2), connect=min(remaining, 5))) as response:
                        if response.status_code in {301, 302, 307, 308}:
                            target = response.headers.get('location', '')
                            parsed = urlsplit(target)
                            if parsed.scheme != 'https' or parsed.netloc != 'api.github.com':
                                raise GitHubError('UNSAFE_REDIRECT', 'GitHub redirect left the approved API host.')
                            url = target
                            continue
                        data = bytearray()
                        for chunk in response.iter_bytes():
                            _remaining(deadline)
                            data.extend(chunk)
                            if len(data) > max_bytes:
                                raise GitHubError('CONTENT_TOO_LARGE', 'GitHub response exceeded the bounded read budget.')
                        return response.status_code, dict(response.headers), bytes(data)
        except httpx.TimeoutException:
            raise GitHubError('TIMEOUT', 'GitHub transport timed out; no identity fallback was attempted.', retryable=True) from None
        except httpx.TransportError:
            raise GitHubError('NETWORK_ERROR', 'GitHub transport failed; check the configured network route.', retryable=True) from None
        raise GitHubError('REDIRECT_LIMIT', 'GitHub redirected too many times.')
