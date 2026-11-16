from types import SimpleNamespace


def test_media_error_classification_keeps_independent_download_boundaries():
    from runtime.media_acquisition import classify_media_error

    assert classify_media_error(RuntimeError('No supported JavaScript runtime could be found')) == 'javascript_runtime_required'
    assert classify_media_error(RuntimeError('PO Token required for this client')) == 'proof_of_origin_required'
    assert classify_media_error(RuntimeError('ffmpeg not found')) == 'ffmpeg_required'
    assert classify_media_error(RuntimeError('HTTP Error 429: Too Many Requests')) == 'rate_limited'
    assert classify_media_error(RuntimeError('Sign in to confirm you are not a bot')) == 'verification_required'


def test_youtube_transcript_429_exposes_stage_without_claiming_permanent_ban(monkeypatch):
    import youtube_transcript_api
    from collectors.platform import youtube

    class IpBlocked(Exception):
        pass

    class Api:
        def __init__(self, **kwargs):
            pass

        def list(self, *args, **kwargs):
            raise IpBlocked('provider refused')

    monkeypatch.setattr(youtube_transcript_api, 'YouTubeTranscriptApi', Api)
    monkeypatch.setattr(youtube, 'get_httpx_proxy', lambda: 'http://proxy.test:7890')
    result = youtube.fetch_youtube_transcript('video')
    assert result['failure_type'] == 'rate_limited_or_ip_blocked'
    assert result['failure_class'] == 'http_429_or_provider_block'
    assert result['retryable'] is False
    assert result['next_action'] == 'use_independent_transcript_path_or_continue_other_features'
    assert result['proxy_configured'] is True


def test_youtube_detail_keeps_metadata_when_transcript_path_is_blocked(monkeypatch):
    from collectors.platform import youtube

    monkeypatch.setenv('YOUTUBE_API_KEY', 'fixture')
    monkeypatch.setattr(youtube, '_youtube_get', lambda *args, **kwargs: {
        'items': [{'snippet': {'title': '标题', 'description': '描述', 'channelTitle': '频道',
                               'channelId': 'c', 'publishedAt': '2026-01-01T00:00:00Z', 'thumbnails': {}},
                    'contentDetails': {'duration': 'PT1M'}, 'statistics': {}}]
    })
    monkeypatch.setattr(youtube, 'list_youtube_captions', lambda video_id: {'status': 'authorization_required'})
    monkeypatch.setattr(youtube, 'fetch_youtube_transcript', lambda *args, **kwargs: {
        'status': 'failed', 'failure_type': 'rate_limited_or_ip_blocked', 'text': '',
        'automatic_retry': False, 'failure_phase': 'track_discovery'
    })
    result = youtube.get_youtube_detail('https://www.youtube.com/watch?v=abc12345678')
    assert result['title'] == '标题'
    assert result['feature_status']['metadata'] == 'available'
    assert result['feature_status']['transcript'] == 'blocked'
    assert result['feature_status']['comments'] == 'available_via_data_api'
    assert result['transcript_metadata']['next_action'] == 'use_independent_transcript_path_or_continue_other_features'


def test_browser_transcript_fallback_is_explicit_and_bounded(monkeypatch):
    from runtime import youtube_browser

    calls = []
    monkeypatch.setattr(youtube_browser.BrowserLink, 'health', lambda self: {'ok': True, 'connected': True})
    monkeypatch.setattr(youtube_browser.BrowserLink, 'request', lambda self, operation, payload, **kwargs: (
        calls.append((operation, payload, kwargs)) or {'ok': True, 'data': {
            'transcript': '浏览器字幕', 'segments': [{'text': '浏览器字幕'}], 'language': 'zh-Hans'
        }}
    ))
    result = youtube_browser.fetch_browser_transcript('abc12345678', timeout_s=12, language='zh-Hans')
    assert result['status'] == 'ok'
    assert result['text'] == '浏览器字幕'
    assert calls[0][0] == 'transcript'
    assert calls[0][2]['timeout_s'] == 12


def test_youtube_search_uses_provider_page_capacity_without_raising_local_cap(monkeypatch):
    from collectors.platform import youtube

    monkeypatch.setenv('YOUTUBE_API_KEY', 'fixture')
    seen = {}
    def run(fn, *, timeout_s):
        return fn()
    def get(resource, params):
        seen.update(params)
        return {'items': [], 'nextPageToken': ''}
    monkeypatch.setattr(youtube, '_run_with_timeout', run)
    monkeypatch.setattr(youtube, '_youtube_get', get)
    youtube.search_youtube('agent', limit=50)
    assert seen['maxResults'] == 50
