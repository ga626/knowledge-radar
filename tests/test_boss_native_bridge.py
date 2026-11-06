from __future__ import annotations
import json
from pathlib import Path
import httpx
import pytest
from runtime.browser_link import BrowserLink

@pytest.fixture(autouse=True)
def isolated_transport(monkeypatch,tmp_path):
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path/'state'))
    monkeypatch.delenv('KR_DURABLE_TASK_ID',raising=False)
    monkeypatch.setattr('runtime.browser_channel.ensure_borrowed_browser',
        lambda *a,**kw:{'ok':False,'code':'browser_channel_not_connected','request_executed':False})

class FakeClient:

    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(('get', url, kwargs))
        body=self.response.json()
        if body.get('connected') is False:return self.response
        return httpx.Response(200,json={'connected':True,'protocol':1,'generation':'test-generation'})

    def post(self, url, **kwargs):
        self.calls.append(('post', url, kwargs))
        if url.endswith('/command'):
            body=self.response.json();body.setdefault('request_id',kwargs['json']['request_id']);body.setdefault('generation',kwargs['json']['generation'])
            return httpx.Response(self.response.status_code,json=body)
        return self.response

def write_state(path: Path, **values):
    path.write_text(json.dumps({'port': 19890, 'token': 't', 'generation':'test-generation', **values}), encoding='utf-8')

def test_missing_state_is_classified(tmp_path):
    assert BrowserLink(state_file=tmp_path / 'missing.json').health()['code'] == 'native_host_unavailable'

def test_http_health_and_command_contract(tmp_path):
    state = tmp_path / 'host.json'
    write_state(state, connected=True)
    client = FakeClient(httpx.Response(200, json={'ok': True, 'connected': True, 'protocol': 1, 'generation':'test-generation','data': {'items': []}}))
    bridge = BrowserLink(state_file=state, client=client)
    assert bridge.health()['ok']
    result = bridge.request('search', {'url': 'https://www.zhipin.com/web/geek/jobs?query=Python'}, platform='boss')
    assert result['ok']
    assert next(call for call in client.calls if call[0]=='post')[2]['json']['operation'] == 'search'

def test_closed_browser_is_not_retried(tmp_path):
    state = tmp_path / 'host.json'
    write_state(state, connected=False)
    client = FakeClient(httpx.Response(409, json={'ok': False, 'connected': False}))
    result = BrowserLink(state_file=state, client=client).request('detail', {'url': 'https://www.zhipin.com/job_detail/x.html'}, platform='boss')
    assert result['failure_type'] == 'browser_channel_not_connected'
    assert result['request_executed'] is False
    assert not any(call[0]=='post' for call in client.calls)

def test_timeout_requests_cancel(tmp_path):
    state = tmp_path / 'host.json'
    write_state(state, connected=True)
    client = FakeClient(httpx.Response(200, json={'ok': True, 'code': 'cancel_requested'}))

    class TimeoutClient(FakeClient):

        def post(self, url, **kwargs):
            self.calls.append(('post', url, kwargs))
            if url.endswith('/command'):
                raise httpx.ReadTimeout('slow')
            return self.response
    timeout_client = TimeoutClient(client.response)
    result = BrowserLink(state_file=state, client=timeout_client).request('search', {'url': 'https://www.zhipin.com/web/geek/jobs?query=Python'}, timeout_s=0.01, platform='boss')
    assert result['failure_type'] == 'native_command_timeout'
    assert any((call[1].endswith('/cancel') for call in timeout_client.calls))

def test_cancel_without_host_is_clear(tmp_path):
    result = BrowserLink(state_file=tmp_path / 'missing.json').cancel('abc')
    assert result['failure_type'] == 'native_host_unavailable'

def test_extension_failure_body_is_preserved_on_gateway_status(tmp_path):
    state = tmp_path / 'host.json'
    write_state(state, connected=True)
    client = FakeClient(httpx.Response(502, json={'ok': False, 'code': 'content_unavailable: Receiving end does not exist.', 'data': {}}))
    result = BrowserLink(state_file=state, client=client).request('search', {'url': 'https://www.zhipin.com/web/geek/jobs'}, platform='boss')
    assert result['code'] == 'content_unavailable: Receiving end does not exist.'
    assert result['failure_type'] == result['code']
    assert result['status'] == 502

def test_failed_gateway_cannot_claim_success_from_body(tmp_path):
    state = tmp_path / 'host.json'
    write_state(state)
    client = FakeClient(httpx.Response(502, json={'ok': True, 'code': 'invalid_boss_url'}))
    result = BrowserLink(state_file=state, client=client).request('search', {}, platform='boss')
    assert result['ok'] is False

@pytest.mark.parametrize('task,expected', [({'status': 'cancelled'}, 'cancelled'), ({'status': 'running', 'metadata': {'deadline_at': 1}}, 'native_command_timeout')])
def test_stopped_durable_task_never_sends(monkeypatch, tmp_path, task, expected):
    from runtime import tasks
    from types import SimpleNamespace
    monkeypatch.setenv('KR_DURABLE_TASK_ID', 'stopped-task')
    monkeypatch.setattr(tasks, 'get_task_store', lambda: SimpleNamespace(get_task=lambda _: task))
    client = FakeClient(httpx.Response(200, json={'ok': True}))
    result = BrowserLink(state_file=tmp_path / 'not-needed.json', client=client).request('search', {}, platform='boss')
    assert result['code'] == expected and result['request_executed'] is False
    assert client.calls == []
