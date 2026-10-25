"""本地账号会话交接与重启后的读写边界。"""
import json
import os
import socket
import struct
import sys
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from onboarding.console_access import ConsoleAccess, _matching_tcp_pid
from onboarding.setup_wizard import WizardServer


@pytest.mark.skipif(os.name != 'nt', reason='Windows local connection identity')
def test_same_windows_user_opens_fixed_url_without_cookie_or_launch_ticket(tmp_path):
    access = ConsoleAccess(tmp_path, 'dev')
    server = WizardServer(('127.0.0.1', 0), access=access)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
    try:
        connection.request('GET', '/')
        response = connection.getresponse()
        page = response.read()
        assert response.status == 200
        assert access.csrf_token.encode() in page
        assert '请从本机 KnowledgeRadar 启动入口打开。'.encode() not in page
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_ticket_is_single_use_role_scoped_and_expiring(tmp_path):
    access = ConsoleAccess(tmp_path, 'dev')
    ticket = access.issue_ticket(access.challenge, now=100)
    assert access.redeem(ticket, now=100)
    assert not access.redeem(ticket, now=100)
    assert not access.redeem(access.issue_ticket(access.challenge, now=100), now=162)
    assert not access.redeem(access.issue_ticket('old-server', now=100), now=100)
    other = ConsoleAccess(tmp_path, 'stable')
    assert not other.redeem(access.issue_ticket(other.challenge, now=100), now=100)


def test_persistent_key_is_sealed_and_sessions_survive_restart(tmp_path):
    access = ConsoleAccess(tmp_path, 'dev')
    cookie = access.session_cookie(now=100)
    restarted = ConsoleAccess(tmp_path, 'dev')
    assert restarted.valid_cookie(cookie, now=101)
    assert restarted.csrf_token == access.csrf_token
    assert not restarted.valid_cookie(cookie, now=100 + 31 * 86400)
    assert not ConsoleAccess(tmp_path, 'stable').valid_cookie(cookie, now=101)
    assert access.key not in (tmp_path / 'dev.key').read_bytes()


def test_http_gate_requires_session_even_with_page_token(tmp_path, monkeypatch):
    monkeypatch.setenv('KR_CONSOLE_ROLE', 'dev')
    access = ConsoleAccess(tmp_path, 'dev')
    monkeypatch.setattr(access, 'connection_identity', lambda connection: None)
    server = WizardServer(('127.0.0.1', 0), access=access)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    origin = f'http://127.0.0.1:{port}'

    def request(method, path, *, body=None, headers=None):
        conn = HTTPConnection('127.0.0.1', port, timeout=3)
        conn.request(method, path, body=body, headers=headers or {})
        res = conn.getresponse()
        data, response_headers = res.read(), dict(res.getheaders())
        conn.close()
        return res.status, data, response_headers

    try:
        code, page, _ = request('GET', '/')
        assert code == 200
        assert access.csrf_token.encode() not in page
        assert '请从本机'.encode() in page
        assert request('GET', '/api/configuration', headers={'X-KR-Setup-Token': access.csrf_token})[0] == 403
        ticket = access.issue_ticket(access.challenge)
        code, _, headers = request('POST', '/api/session', body=json.dumps({'ticket': ticket}), headers={'Origin': origin})
        assert code == 200
        cookie = headers['Set-Cookie'].split(';')[0]
        assert 'HttpOnly' in headers['Set-Cookie'] and 'SameSite=Strict' in headers['Set-Cookie']
        assert request('POST', '/api/session', body=json.dumps({'ticket': ticket}), headers={'Origin': origin})[0] == 403
        code, page, _ = request('GET', '/', headers={'Cookie': cookie})
        assert code == 200 and access.csrf_token.encode() in page
        server.setup_token_created_at -= 3600
        assert request('GET', '/api/configuration', headers={'Cookie': cookie, 'X-KR-Setup-Token': access.csrf_token})[0] == 200
        assert request('POST', '/api/config', body='{}', headers={'Origin': origin, 'X-KR-Setup-Token': access.csrf_token})[0] == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


@pytest.mark.parametrize('identity', [True, False])
def test_identity_gate_preserves_origin_csrf_and_never_accepts_foreign_cookie(tmp_path, monkeypatch, identity):
    access = ConsoleAccess(tmp_path, 'dev')
    monkeypatch.setattr(access, 'connection_identity', lambda connection: identity)
    server = WizardServer(('127.0.0.1', 0), access=access)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'

    def request(method, path, headers=None, body=None):
        conn = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        conn.request(method, path, headers=headers or {}, body=body)
        response = conn.getresponse()
        result = response.status, response.read(), str(response.getheaders())
        conn.close()
        return result

    try:
        credentials = {'Cookie': access.session_cookie().split(';')[0], 'X-KR-Setup-Token': access.csrf_token, 'Origin': origin}
        status, page, headers = request('GET', '/', credentials)
        assert status == (200 if identity else 403)
        if not identity:
            assert access.csrf_token not in headers and access.csrf_token.encode() not in page
        assert request('GET', '/api/configuration', credentials)[0] == (200 if identity else 403)
        assert request('GET', '/api/configuration')[0] == 403
        code, health, health_headers = request('GET', '/api/health')
        assert code == 200 and access.csrf_token not in health_headers and access.csrf_token.encode() not in health
        for invalid in ({'Host': 'evil.example'}, {'Origin': 'https://evil.example'}, {'Sec-Fetch-Site': 'cross-site'}, {'Sec-Fetch-Site': 'same-site'}):
            assert request('GET', '/', {**credentials, **invalid})[0] == 403
            assert request('POST', '/api/config', {**credentials, **invalid}, '{}')[0] == 403
        assert request('POST', '/api/config', {'X-KR-Setup-Token': access.csrf_token}, '{}')[0] == 403
        if not identity:
            ticket = access.issue_ticket(access.challenge)
            assert request('POST', '/api/session', credentials, json.dumps({'ticket': ticket}))[0] == 403
            assert request('POST', '/api/console/stop', {**credentials, 'X-KR-Control': access.control_proof(access.challenge)}, '{}')[0] == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_tcp_owner_matches_only_unique_established_client_tuple():
    address = struct.unpack('<I', socket.inet_aton('127.0.0.1'))[0]
    client, server = ('127.0.0.1', 51234), ('127.0.0.1', 18883)
    def row(local, remote, pid, state=5):
        return struct.pack('<6I', state, address, socket.htons(local), address, socket.htons(remote), pid)
    client_row = row(client[1], server[1], 1234)
    others = row(server[1], client[1], 4321) + row(client[1] + 1, server[1], 9000) + row(client[1], server[1], 7000, state=2)
    assert _matching_tcp_pid(struct.pack('<I', 4) + others + client_row, client, server) == 1234
    assert _matching_tcp_pid(struct.pack('<I', 3) + others, client, server) == 0
    assert _matching_tcp_pid(struct.pack('<I', 2) + client_row * 2, client, server) == 0
    with pytest.raises((ValueError, struct.error)):
        _matching_tcp_pid(struct.pack('<I', 2) + client_row, client, server)
    with pytest.raises(struct.error):
        _matching_tcp_pid(b'', client, server)


@pytest.mark.skipif(os.name != 'nt', reason='Windows local connection identity')
def test_windows_identity_lookup_failure_denies_access(tmp_path, monkeypatch):
    import onboarding.console_access as module
    def unavailable():
        raise OSError('Windows identity lookup failed')
    monkeypatch.setattr(module, '_windows_connection_identity', unavailable)
    assert ConsoleAccess(tmp_path, 'dev').connection_identity(None) is False
