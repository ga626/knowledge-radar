"""只验证超时分型与已下载字节复用，不访问公网。"""
import time

import httpx
import pytest

from generic_web.models import GenericWebRequest


def test_local_deadline_is_not_network_failure(monkeypatch):
    from generic_web import collector, acquisition
    def expired(*args):
        raise acquisition.AcquisitionDeadlineExceeded('local deadline')
    monkeypatch.setattr(acquisition, 'fetch', expired)
    with pytest.raises(collector.GenericWebError) as caught:
        collector._fetch_html(GenericWebRequest('https://example.org'), collector='http_acquisition')
    assert caught.value.to_dict()['type'] == 'deadline_exceeded'
    assert caught.value.to_dict()['retryable'] is False


@pytest.mark.parametrize('kind,phase', [(httpx.ConnectTimeout,'connect'),(httpx.ReadTimeout,'read'),
    (httpx.PoolTimeout,'pool'),(httpx.WriteTimeout,'write')])
def test_httpx_failure_preserves_phase_without_guessing_root(monkeypatch, kind, phase):
    from generic_web import collector
    monkeypatch.setattr(collector, 'guarded_fetch', lambda *a, **kw: (_ for _ in ()).throw(kind('timeout')))
    with pytest.raises(collector.GenericWebError) as caught:
        collector._fetch_html(GenericWebRequest('https://example.org'), collector='http_acquisition')
    data = caught.value.to_dict()
    assert data['type'] == 'network_error'
    assert data['diagnostics']['phase'] == phase
    assert data['diagnostics']['attempts'] == 1
    assert data['diagnostics']['root_cause'] == 'unconfirmed'
    assert data['automatic_retry'] is False


def test_retry_cannot_disguise_exhausted_total_deadline(monkeypatch):
    from generic_web import acquisition
    calls = []
    request = GenericWebRequest('https://example.org', deadline_at=time.monotonic()+10)
    def fail(*args, **kwargs):
        calls.append(1)
        monkeypatch.setattr(acquisition.time, 'monotonic', lambda: request.deadline_at+1)
        raise httpx.ConnectError('connect failed')
    with pytest.raises(acquisition.AcquisitionDeadlineExceeded) as caught:
        acquisition.fetch(request, {}, fail)
    assert caught.value.acquisition_diagnostics['attempts'] == 1
    assert len(calls) == 1


def test_wechat_success_keeps_acquisition_observation(monkeypatch):
    from generic_web import collector
    from collectors.platform import wechat
    response = httpx.Response(200, text='<html>article</html>',
        request=httpx.Request('GET', 'https://mp.weixin.qq.com/s/fixture'),
        extensions={'acquisition_fetch_count':1,'acquisition_elapsed_s':0.12})
    monkeypatch.setattr(collector, 'public_addresses', lambda *a: None)
    monkeypatch.setattr(collector, '_fetch_html', lambda *a, **kw: (response,'text/html'))
    monkeypatch.setattr(wechat, 'parse_html', lambda *a: {
        'title':'title','content':'body','source_presentation':'article'})
    result = collector.collect_url(GenericWebRequest('https://mp.weixin.qq.com/s/fixture'))
    assert result.metadata['acquisition']['fetch_count'] == 1
    assert result.metadata['acquisition']['elapsed_s'] == 0.12
    assert result.elapsed_s >= 0
