from types import SimpleNamespace
import pytest
import requests
import youtube_transcript_api
from collectors.platform import youtube


@pytest.mark.parametrize('failure', [None, requests.ReadTimeout('read stalled')])
def test_transcript_http_has_timeouts_and_redacted_stage(monkeypatch, failure):
    seen = {}
    def request(self, method, url, **kwargs):
        seen.update(kwargs)
        assert self.trust_env is False
        if failure:
            raise failure
        return SimpleNamespace(status_code=200)
    monkeypatch.setattr(requests.Session, 'request', request)
    monkeypatch.setattr(youtube, 'get_httpx_proxy', lambda: None)
    class Api:
        def __init__(self, http_client): self.client = http_client
        def list(self, video_id):
            return self
        def find_transcript(self, languages):
            return self
        def fetch(self):
            self.client.get('https://www.youtube.com/api/timedtext?token=never-persist')
            return [SimpleNamespace(text='真实字幕接口返回的文字', start=1.5, duration=2.0)]
    monkeypatch.setattr(youtube_transcript_api, 'YouTubeTranscriptApi', Api)
    result = youtube.fetch_youtube_transcript('video')
    assert all(0 < t <= 10 for t in seen['timeout'])
    assert result['requests'][0]['stage'] == '/api/timedtext'
    assert 'never-persist' not in str(result)
    if failure:
        assert result['failure_type'] == 'network_timeout'
        assert result['status'] == 'timeout'
    else:
        assert result['status'] == 'ok'
        assert result['segments'] == 1


def test_transcript_block_is_distinct_from_timeout(monkeypatch):
    class IpBlocked(Exception): pass
    class Api:
        def __init__(self, **kwargs): pass
        def list(self, *a, **kw): raise IpBlocked('provider refused')
    monkeypatch.setattr(youtube_transcript_api, 'YouTubeTranscriptApi', Api)
    monkeypatch.setattr(youtube, 'get_httpx_proxy', lambda: None)
    assert youtube.fetch_youtube_transcript('video')['failure_type'] == 'rate_limited_or_ip_blocked'
