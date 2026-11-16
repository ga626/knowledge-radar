"""Explicit, bounded YouTube transcript fallback through Browser Link.

The fallback is opt-in per detail request.  It never retries, reads cookies,
or silently opens a foreground page; Browser Link owns the borrowed Chrome
session and returns a typed failure when the extension does not expose the
YouTube adapter.
"""

from __future__ import annotations

from typing import Any

from runtime.browser_link import BrowserLink


def _url(video_id_or_url: str) -> str:
    from collectors.platform.youtube import canonical_youtube_url, extract_youtube_video_id

    video_id = extract_youtube_video_id(video_id_or_url)
    return canonical_youtube_url(video_id) if video_id else str(video_id_or_url or '')


def fetch_browser_transcript(video_id_or_url: str, *, timeout_s: float = 25.0,
                             language: str | None = None) -> dict[str, Any]:
    """Read an already visible transcript UI once and return a typed result."""
    url = _url(video_id_or_url)
    if not url.startswith('https://www.youtube.com/watch?v='):
        return {'status': 'unavailable', 'failure_type': 'invalid_youtube_url', 'text': '', 'source': 'browser_link'}
    bridge = BrowserLink()
    health = bridge.health()
    if not health.get('ok'):
        return {'status': 'unavailable', 'failure_type': health.get('code', 'browser_link_unavailable'),
                'text': '', 'source': 'browser_link', 'automatic_retry': False,
                'next_action': 'continue_other_features_or_request_explicit_browser_check'}
    response = bridge.request('transcript', {'url': url, 'video_id': url.rsplit('=', 1)[-1],
                                              'language': language or ''},
                              platform='youtube', timeout_s=max(1.0, min(float(timeout_s), 30.0)))
    data = response.get('data') if isinstance(response, dict) else None
    data = data if isinstance(data, dict) else {}
    text = str(data.get('transcript') or data.get('text') or '').strip()
    if response.get('ok') and text:
        return {'status': 'ok', 'source': 'browser_link_youtube_dom', 'text': text,
                'segments': data.get('segments') or [], 'language': data.get('language') or language or '',
                'browser_diagnostics': data.get('browser_diagnostics') or response.get('browser_diagnostics') or {}}
    code = response.get('failure_type') or response.get('code') or data.get('failure_type') or 'browser_transcript_unavailable'
    return {'status': 'unavailable', 'failure_type': code, 'text': '', 'source': 'browser_link',
            'automatic_retry': False, 'next_action': 'continue_other_features_or_use_authorized_local_asr',
            'browser_diagnostics': data.get('browser_diagnostics') or response.get('browser_diagnostics') or {}}
