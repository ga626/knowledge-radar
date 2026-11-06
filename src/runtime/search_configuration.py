"""搜索配置快照：文件变更可见、在途身份固定，撤销在每次发送前重核。"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from search_providers.catalog import SEARCH_CATALOG

_SNAPSHOT = ContextVar('kr_search_configuration', default=None)
_PROVIDER = ContextVar('kr_sending_search_provider', default='')
_DEADLINE = ContextVar('kr_search_deadline', default=float('inf'))
CONFIG_KEYS = {row['key'] for row in SEARCH_CATALOG.values() if row['key']} | {
    'KR_WEB_SEARCH_PROVIDERS', 'KR_SEARCH_POLICY', 'DDGS_BACKENDS', 'DDGS_REGION',
    'ANYSEARCH_SEARCH_ENDPOINT', 'TAVILY_SEARCH_ENDPOINT', 'BRAVE_SEARCH_ENDPOINT', 'EXA_SEARCH_ENDPOINT'}


def config_path():
    return Path(os.environ.get('KR_RUNTIME_ENV_PATH') or Path(__file__).resolve().parents[2] / '.env')


def read_env(path):
    values = {}
    if Path(path).is_file():
        for raw in Path(path).read_text(encoding='utf-8-sig').splitlines():
            line = raw.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class SearchConfiguration:
    generation: str
    values: dict
    policy: dict

    def allowed(self, name):
        if name == 'searxng' or name.startswith('searxng:'):
            return False  # Retired config is read for compatibility, never executed.
        family = 'searxng' if name.startswith('searxng:') else name
        if family not in SEARCH_CATALOG:
            return True  # host capability contracts are checked by host.py
        if ':' in name:
            connection = self.connection(name)
            return bool(connection and connection.get('enabled') is True and connection.get('authorized') is True)
        legacy = [x.strip() for x in self.values.get('KR_WEB_SEARCH_PROVIDERS', '').split(',') if x.strip()]
        default = name in legacy or family in legacy if legacy else SEARCH_CATALOG[family]['default_enabled']
        row = self.policy.get('providers', {}).get(name, {})
        enabled = row.get('enabled', default)
        authorized = row.get('authorized', default)
        return enabled is True and authorized is True

    def connection(self, name):
        return next((row for row in self.policy.get('connections', [])
                     if name == 'searxng:' + row['id']), None)


def validate_policy(policy):
    """Malformed persisted policies fail closed, including optional connections."""
    import re
    if not isinstance(policy, dict) or set(policy) - {'providers', 'connections'}:
        raise ValueError('invalid_search_policy')
    providers = policy.get('providers', {})
    if not isinstance(providers, dict):
        raise ValueError('invalid_search_providers')
    for name, flags in providers.items():
        if (name not in {*SEARCH_CATALOG, 'searxng'} or not isinstance(flags, dict)
                or set(flags) - {'enabled', 'authorized'} or any(type(v) is not bool for v in flags.values())):
            raise ValueError('invalid_search_provider_flags')
    connections = policy.get('connections', [])
    if not isinstance(connections, list) or len(connections) > 8:
        raise ValueError('invalid_search_connections')
    seen = set()
    for row in connections:
        if (not isinstance(row, dict) or set(row) - {'id', 'url', 'mode', 'enabled', 'authorized'}
                or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', str(row.get('id', '')))
                or row['id'] in seen or any(type(row.get(k)) is not bool for k in ('enabled', 'authorized'))):
            raise ValueError('invalid_search_connection')
        seen.add(row['id'])
        validate_connection_url(row.get('url', ''), row.get('mode', 'public'))
    return policy


def current_configuration(*, fresh=False, path=None):
    active = _SNAPSHOT.get()
    if active is not None and not fresh and path is None:
        return active
    values = {key: os.environ.get(key, '') for key in CONFIG_KEYS}
    values.update({k: v for k, v in read_env(path or config_path()).items() if k in CONFIG_KEYS})
    raw = json.dumps(values, sort_keys=True, ensure_ascii=False)
    try:
        policy = json.loads(values.get('KR_SEARCH_POLICY') or '{}')
        validate_policy(policy)
    except (json.JSONDecodeError, ValueError):
        # A malformed policy must not restore legacy permissions.
        policy = {'providers': {name: {'enabled': False, 'authorized': False} for name in SEARCH_CATALOG},
                  'configuration_error': 'invalid_search_policy'}
    return SearchConfiguration(hashlib.sha256(raw.encode()).hexdigest()[:20], values, policy)


def config_value(key, default=''):
    snapshot = current_configuration()
    return snapshot.values.get(key, os.environ.get(key, default)) or default


@contextmanager
def configuration_scope(snapshot=None):
    token = _SNAPSHOT.set(snapshot or _SNAPSHOT.get() or current_configuration(fresh=True))
    try:
        yield _SNAPSHOT.get()
    finally:
        _SNAPSHOT.reset(token)


@contextmanager
def sending_provider(name, deadline=float('inf')):
    token = _PROVIDER.set(name)
    deadline_token = _DEADLINE.set(deadline)
    try:
        yield
    finally:
        _PROVIDER.reset(token)
        _DEADLINE.reset(deadline_token)


def check_search_send():
    from runtime.execution_context import check_execution
    check_execution()
    import time
    if time.monotonic() >= _DEADLINE.get():
        raise TimeoutError('search_deadline_exceeded_before_send')
    name = _PROVIDER.get()
    if name and not current_configuration(fresh=True).allowed(name):
        raise PermissionError('search_provider_revoked')
    if name and _SNAPSHOT.get() is not None:
        latest = current_configuration(fresh=True)
        active = _SNAPSHOT.get()
        # Changed credentials/endpoints require a new operation rather than sending stale credentials.
        family = 'searxng' if name.startswith('searxng:') else name
        if family in SEARCH_CATALOG:
            key = SEARCH_CATALOG[family]['key']
            keys = [key, family.upper() + '_SEARCH_ENDPOINT']
            if any(active.values.get(k) != latest.values.get(k) for k in keys if k):
                raise PermissionError('search_configuration_changed_before_send')
            if ':' in name and active.connection(name) != latest.connection(name):
                raise PermissionError('search_connection_changed_before_send')
            if family == 'ddgs' and any(active.values.get(k) != latest.values.get(k) for k in ('DDGS_BACKENDS', 'DDGS_REGION')):
                raise PermissionError('search_configuration_changed_before_send')


def validate_connection_url(url, mode='public'):
    parsed = urlsplit(url)
    if (parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname
            or parsed.scheme not in {'https', 'http'}):
        raise ValueError('实例地址必须是无凭据、无查询参数的 HTTP(S) 基础地址。')
    if mode == 'public':
        from generic_web.url_policy import public_addresses
        if parsed.scheme != 'https':
            raise ValueError('公共实例须使用 HTTPS。')
        public_addresses(url, resolve=False)
    elif mode == 'self_hosted':
        if parsed.scheme == 'http' and parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}:
            raise ValueError('明文自建连接仅允许回环地址。')
    else:
        raise ValueError('实例类型必须是 public 或 self_hosted。')
    return url.rstrip('/')
