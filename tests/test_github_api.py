import json
import time

import pytest

from collectors.platform import github_api as api
from runtime import leases
from runtime.execution_context import ExecutionStopped


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('KR_LOG_DIR', str(tmp_path / 'logs'))
    monkeypatch.setenv('KR_RUNTIME_LEASE_DB_PATH', str(tmp_path / 'leases.sqlite3'))
    monkeypatch.setenv('KR_GITHUB_ENABLED', '1')
    monkeypatch.delenv('KR_DURABLE_WORKER', raising=False)
    monkeypatch.delenv('GH_TOKEN', raising=False)
    monkeypatch.delenv('GITHUB_TOKEN', raising=False)
    monkeypatch.setattr(leases, '_COORDINATOR', None)
    api._AUTH_CACHE.clear()


def test_token_precedence_and_credential_never_in_result_or_repr(monkeypatch):
    monkeypatch.setenv('GH_TOKEN', 'first-secret')
    monkeypatch.setenv('GITHUB_TOKEN', 'second-secret')
    client = api.GitHubClient.configured()
    assert client.transport == 'rest' and client.token == 'first-secret'
    assert 'secret' not in repr(client)
    monkeypatch.setenv('GH_TOKEN', 'changed-secret')
    assert api.GitHubClient.configured().identity != client.identity
    assert client.token == 'first-secret'


def test_gh_disabled_is_not_source_disabled(monkeypatch):
    monkeypatch.setenv('KR_GH_CLI_ENABLED', '0')
    monkeypatch.setenv('KR_GITHUB_TRANSPORT', 'auto')
    assert api.GitHubClient.configured().transport == 'rest'
    monkeypatch.setenv('KR_GITHUB_ENABLED', '0')
    with pytest.raises(api.GitHubError, match='disabled'):
        api.GitHubClient.configured()


def test_bad_keyring_identity_cannot_downgrade_to_anonymous(monkeypatch):
    monkeypatch.setenv('KR_GITHUB_TRANSPORT', 'auto')
    monkeypatch.setenv('KR_GH_CLI_ENABLED', '1')
    monkeypatch.setattr(api, '_gh_exe', lambda: 'exists')
    monkeypatch.setattr(api, '_run_gh', lambda *a, **k: (0,
        json.dumps({'hosts':{'github.com':[{'active':True, 'state':'error'}]}}).encode(), b''))
    with pytest.raises(api.GitHubError) as failure:
        api.GitHubClient.configured()
    assert failure.value.code == 'AUTH_INVALID'


@pytest.mark.parametrize('status,headers,message,expected', [
    (401, {}, 'Bad credentials', 'AUTH_INVALID'),
    (403, {}, 'Resource not accessible', 'PERMISSION_DENIED'),
    (403, {'x-ratelimit-remaining':'0', 'x-ratelimit-reset':str(time.time()+60)}, 'rate limit', 'RATE_LIMITED'),
    (403, {'retry-after':'60'}, 'secondary rate limit', 'RATE_LIMITED'),
    (404, {}, 'Not Found', 'NOT_FOUND_OR_INACCESSIBLE'),
    (422, {}, 'Validation failed', 'INVALID_QUERY_OR_OBJECT'),
    (429, {}, '', 'RATE_LIMITED'),
    (451, {}, '', 'LEGAL_RESTRICTION'),
])
def test_http_errors_keep_identity_and_are_actionable(monkeypatch, status, headers, message, expected):
    client = api.GitHubClient('rest', 'identity')
    calls = []
    def reply(*args):
        calls.append(args)
        return status, headers, json.dumps({'message':message}).encode()
    monkeypatch.setattr(client, '_rest_get', reply)
    with pytest.raises(api.GitHubError) as failure:
        client.json('repos/o/r')
    assert failure.value.code == expected
    assert len(calls) == 1 and len(client.attempts) == 1
    assert 'next_action' in failure.value.details
    if expected == 'RATE_LIMITED':
        assert failure.value.details['retry_after_s'] >= 0
        assert not api.resource_ready('github_api:identity:core')


def test_cooldown_is_shared_between_clients_without_resending(monkeypatch):
    first = api.GitHubClient('rest', 'same')
    monkeypatch.setattr(first, '_rest_get', lambda *a: (429, {'retry-after':'100'}, b'{}'))
    with pytest.raises(api.GitHubError): first.json('search/repositories')
    second = api.GitHubClient('gh', 'same')
    monkeypatch.setattr(second, '_gh_get', lambda *a: pytest.fail('cooldown must stop before transport'))
    with pytest.raises(api.GitHubError) as failure:
        second.json('repos/o/r')
    assert failure.value.code == 'RATE_LIMITED' and not second.attempts


def test_durable_throttle_yields_existing_scheduler(monkeypatch):
    from runtime.resource_concurrency import ResourceDeferred
    monkeypatch.setenv('KR_DURABLE_WORKER', '1')
    client = api.GitHubClient('rest', 'defer')
    monkeypatch.setattr(client, '_rest_get', lambda *a: (429, {'retry-after':'30'}, b'{}'))
    with pytest.raises(ResourceDeferred) as failure:
        client.json('repos/o/r')
    assert failure.value.resource_kind == 'github_api:defer:core'
    assert not api.resource_ready(failure.value.resource_kind)
    assert not leases.get_runtime_lease_coordinator().resource_leases('github_api', 'defer')


def test_cancellation_stops_before_http(monkeypatch):
    client = api.GitHubClient('rest', 'cancel')
    monkeypatch.setattr(api, 'check_execution', lambda: (_ for _ in ()).throw(ExecutionStopped('cancelled')))
    monkeypatch.setattr(client, '_rest_get', lambda *a: pytest.fail('must not send'))
    with pytest.raises(ExecutionStopped): client.json('repos/o/r')


def test_gh_transport_is_get_with_explicit_host_and_no_parameter_post(monkeypatch):
    calls=[]
    def run(args, **kwargs):
        calls.append(args)
        return 0, b'HTTP/2.0 200 OK\nContent-Type: application/json\nX-GitHub-Request-Id: TEST\n\n{"ok":true}', b''
    monkeypatch.setattr(api, '_run_gh', run)
    data, headers = api.GitHubClient('gh', 'read').json('repos/o/r', params={'ref':'release/v1'})
    assert data == {'ok':True} and headers['x-github-request-id'] == 'TEST'
    assert calls[0][0:2] == ['api', 'repos/o/r?ref=release%2Fv1']
    assert calls[0][calls[0].index('--method')+1] == 'GET'
    assert calls[0][calls[0].index('--hostname')+1] == 'github.com'
    assert '-f' not in calls[0] and '-F' not in calls[0]


def test_gh_account_change_fails_instead_of_silent_switch(monkeypatch):
    client = api.GitHubClient('gh', 'first', auth_stamp='original')
    monkeypatch.setattr(api, '_auth_stamp', lambda: 'changed')
    with pytest.raises(api.GitHubError) as failure:
        client.json('repos/o/r')
    assert failure.value.code == 'AUTH_CONTEXT_CHANGED'


def test_rest_redirect_and_body_budget_are_enforced(monkeypatch):
    client = api.GitHubClient('rest', 'public')
    def handler(request):
        return httpx.Response(302, headers={'location':'https://outside.invalid/secret'})
    import httpx
    original = httpx.Client
    monkeypatch.setattr(api, 'http_client', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(api.GitHubError) as failure: client.json('repos/o/r')
    assert failure.value.code == 'UNSAFE_REDIRECT'
    monkeypatch.setattr(api, 'http_client', lambda **kw: original(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'123456789')), **kw))
    with pytest.raises(api.GitHubError) as failure: client.get('repos/o/r', max_bytes=4)
    assert failure.value.code == 'CONTENT_TOO_LARGE'
