from types import SimpleNamespace
import pytest


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
    result = youtube.get_youtube_detail('https://www.youtube.com/watch?v=abc12345678', browser_fallback=False)
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


def test_content_detail_exposes_explicit_browser_fallback_switch():
    import inspect
    from runtime.composition import get_content_detail

    parameter = inspect.signature(get_content_detail).parameters['youtube_browser_fallback']
    assert parameter.default is True


@pytest.mark.parametrize('api_key', ['', 'fixture'])
def test_default_fallback_preserves_both_attempts_and_429_notice(monkeypatch, api_key):
    from collectors.platform import youtube
    from runtime import youtube_browser
    monkeypatch.setenv('YOUTUBE_API_KEY', api_key)
    monkeypatch.setattr(youtube, '_youtube_get', lambda *a, **kw: {'items': [{
        'snippet': {'title': '标题'}, 'contentDetails': {}, 'statistics': {}}]})
    monkeypatch.setattr(youtube, 'fetch_youtube_transcript', lambda *a, **kw: {
        'status': 'failed', 'failure_type': 'rate_limited_or_ip_blocked', 'text': '',
        'observed_http_status': 429, 'failure_phase': 'caption_download',
        'requests': [{'stage': '/api/timedtext', 'http_status': 429, 'retry_after': '60'}]})
    calls = []
    monkeypatch.setattr(youtube_browser, 'fetch_browser_transcript', lambda *a, **kw: (
        calls.append(kw) or {'status': 'ok', 'text': '备用正文', 'source': 'browser_link_youtube_dom',
                             'language': 'en', 'completeness': 'unverified'}))
    result = youtube.get_youtube_detail('abc12345678')
    assert len(calls) == 1
    assert result['transcript'] == '备用正文'
    meta = result['transcript_metadata']
    assert meta['attempts'][0]['observed_http_status'] == 429
    assert meta['attempts'][0]['requests'][0]['retry_after'] == '60'
    assert meta['attempts'][1]['status'] == 'ok'
    assert any('429' in n['message'] and '切换节点' in n['message'] for n in meta['notices'])


def test_failed_fallback_keeps_deep_diagnostics(monkeypatch):
    from collectors.platform import youtube
    from runtime import youtube_browser
    monkeypatch.setenv('YOUTUBE_API_KEY', '')
    monkeypatch.setattr(youtube, 'fetch_youtube_transcript', lambda *a, **kw: {
        'status': 'failed', 'text': '', 'failure_type': 'network_timeout', 'failure_phase': 'track_discovery'})
    monkeypatch.setattr(youtube_browser, 'fetch_browser_transcript', lambda *a, **kw: {
        'status': 'unavailable', 'text': '', 'failure_type': 'transcript_not_exposed',
        'failure_phase': 'transcript_panel_requested', 'transcript_diagnostics': {'button_present': False}})
    result = youtube.get_youtube_detail('abc12345678')
    meta = result['transcript_metadata']
    assert meta['status'] != 'ok'
    assert meta['attempts'][0]['failure_phase'] == 'track_discovery'
    assert meta['attempts'][1]['transcript_diagnostics']['button_present'] is False
    assert meta['failure_type'] == 'network_timeout'


@pytest.mark.parametrize('include,fallback,status', [(False, True, 'skipped'), (True, False, 'failed'), (True, True, 'ok')])
def test_no_browser_when_not_needed_or_disabled(monkeypatch, include, fallback, status):
    from collectors.platform import youtube
    from runtime import youtube_browser
    monkeypatch.setenv('YOUTUBE_API_KEY', '')
    monkeypatch.setattr(youtube, 'fetch_youtube_transcript', lambda *a, **kw: {'status': status, 'text': '正文' if status == 'ok' else ''})
    monkeypatch.setattr(youtube_browser, 'fetch_browser_transcript', lambda *a, **kw: pytest.fail('unexpected browser'))
    result = youtube.get_youtube_detail('abc12345678', include_transcript=include, browser_fallback=fallback)
    assert result['transcript_metadata']['status'] == status


def test_browser_adapter_does_not_preflight_and_keeps_dom_failure(monkeypatch):
    from runtime import youtube_browser
    monkeypatch.setattr(youtube_browser.BrowserLink, 'health', lambda self: pytest.fail('preflight blocks startup'))
    monkeypatch.setattr(youtube_browser.BrowserLink, 'request', lambda *a, **kw: {
        'ok': True, 'request_id': 'req-1', 'request_executed': True,
        'data': {'failure_type': 'transcript_not_exposed', 'transcript_diagnostics': {'panel_present': False},
                 'browser_diagnostics': {'last_stage': 'transcript_panel_requested'}}})
    result = youtube_browser.fetch_browser_transcript('abc12345678', language='zh')
    assert result['failure_type'] == 'transcript_not_exposed'
    assert result['failure_phase'] == 'transcript_panel_requested'
    assert result['request_id'] == 'req-1'
    assert result['transcript_diagnostics']['panel_present'] is False


def test_browser_does_not_forge_actual_language(monkeypatch):
    from runtime import youtube_browser
    monkeypatch.setattr(youtube_browser.BrowserLink, 'health', lambda self: {'ok': True})
    monkeypatch.setattr(youtube_browser.BrowserLink, 'request', lambda *a, **kw: {
        'ok': True, 'data': {'transcript': 'English body', 'completeness': 'unverified'}})
    result = youtube_browser.fetch_browser_transcript('abc12345678', language='zh')
    assert result['language'] == ''
    assert result['completeness'] == 'unverified'


def test_metadata_api_failure_cannot_prevent_independent_transcript(monkeypatch):
    from collectors.platform import youtube
    monkeypatch.setenv('YOUTUBE_API_KEY', 'fixture')
    monkeypatch.setattr(youtube, '_youtube_get', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('quota exceeded')))
    monkeypatch.setattr(youtube, 'fetch_youtube_transcript', lambda *a, **kw: {
        'status': 'ok', 'text': '独立字幕', 'source': 'youtube_transcript_api'})
    result = youtube.get_youtube_detail('abc12345678')
    assert result['transcript'] == '独立字幕'
    assert result['feature_status']['transcript'] == 'available'


def test_library_block_without_http_status_does_not_invent_429(monkeypatch):
    from collectors.platform import youtube
    monkeypatch.setattr(youtube, 'fetch_youtube_transcript', lambda *a, **kw: {
        'status': 'failed', 'text': '', 'failure_type': 'rate_limited_or_ip_blocked'})
    result = youtube._resolve_youtube_transcript('abc12345678', include=True, language=None, browser_fallback=False)
    assert result['notices'][0]['code'] == 'youtube_access_restricted'
    assert 'observed_http_status' not in result


def test_unknown_primary_result_is_not_replayed_in_browser(monkeypatch):
    from collectors.platform import youtube
    from runtime import youtube_browser
    monkeypatch.setattr(youtube, 'fetch_youtube_transcript', lambda *a, **kw: {
        'status': 'timeout', 'text': '', 'result_known': False})
    monkeypatch.setattr(youtube_browser, 'fetch_browser_transcript', lambda *a, **kw: pytest.fail('unknown replay'))
    result = youtube._resolve_youtube_transcript('abc12345678', include=True, language=None, browser_fallback=True)
    assert result['browser_fallback_attempted'] is False


def test_browser_fallback_and_429_survive_media_read_and_evidence(monkeypatch):
    from detail_strategies.video import enhance_video_detail
    from kr_core import DetailRequest
    meta = {'source': 'browser_link_youtube_dom', 'language': 'en', 'completeness': 'unverified',
            'notices': [{'code': 'youtube_http_429', 'http_status': 429}],
            'attempts': [{'path': 'subtitle_api', 'observed_http_status': 429}, {'path': 'daily_chrome', 'status': 'ok'}]}
    request = DetailRequest(platform='YouTube', url='https://www.youtube.com/watch?v=abc12345678',
                            options={'include_transcript': True})
    response = enhance_video_detail(request, {'title': '标题', 'transcript': '备用正文',
        'content_id': 'abc12345678', 'transcript_metadata': meta})
    assert response.data['transcript_metadata'] == meta
    assert '429' in str(response.evidence)
