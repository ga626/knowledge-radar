"""通过正常 Browser Link 执行入口读取一次文字记录，保留失败阶段。"""

from __future__ import annotations

from typing import Any

from runtime.browser_link import BrowserLink


def _url(video_id_or_url: str) -> str:
    from collectors.platform.youtube import canonical_youtube_url, extract_youtube_video_id

    video_id = extract_youtube_video_id(video_id_or_url)
    return canonical_youtube_url(video_id) if video_id else str(video_id_or_url or '')


def fetch_browser_transcript(video_id_or_url: str, *, timeout_s: float = 25.0,
                             language: str | None = None) -> dict[str, Any]:
    """借用日常 Chrome；由公共执行入口按需启动，不在外层做 health 准入。"""
    url = _url(video_id_or_url)
    if not url.startswith('https://www.youtube.com/watch?v='):
        return {'status': 'unavailable', 'failure_type': 'invalid_youtube_url', 'text': '', 'source': 'browser_link'}
    bridge = BrowserLink()
    response = bridge.request('transcript', {'url': url, 'video_id': url.rsplit('=', 1)[-1],
                                              'language': language or ''},
                              platform='youtube', timeout_s=max(1.0, min(float(timeout_s), 30.0)))
    if not isinstance(response, dict):
        response = {'ok': False, 'code': 'native_host_invalid_result', 'request_executed': None}
    data = response.get('data')
    data = data if isinstance(data, dict) else {}
    diagnostics = {**(response.get('browser_diagnostics') or {}), **(data.get('browser_diagnostics') or {})}
    common = {'request_id': response.get('request_id', ''),
              'request_executed': response.get('request_executed'),
              'browser_readiness': response.get('browser_readiness') or {},
              'browser_diagnostics': diagnostics,
              'transcript_diagnostics': data.get('transcript_diagnostics') or data.get('diagnostics') or {}}
    text = str(data.get('transcript') or data.get('text') or '').strip()
    if response.get('ok') and text and not data.get('failure_type'):
        return {**common, 'status': 'ok', 'source': 'browser_link_youtube_dom', 'text': text,
                'processing_method': '日常 Chrome 页面文字记录（完整性未核实）',
                'segments': data.get('segments') or [], 'language': data.get('language') or '',
                'completeness': data.get('completeness') or 'unverified'}
    code = data.get('failure_type') or response.get('failure_type') or response.get('code') or 'browser_transcript_unavailable'
    return {**common, 'status': 'unavailable', 'failure_type': code, 'text': '', 'source': 'browser_link',
            'failure_phase': diagnostics.get('last_stage') or diagnostics.get('last_confirmed_stage') or 'browser_readiness',
            'automatic_retry': False, 'next_action': 'continue_other_features_or_use_authorized_local_asr',
            'retryable': False}
