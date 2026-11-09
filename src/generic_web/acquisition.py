"""有界匿名 GET 复用；固定证据寿命与 HTTP 缓存寿命分别管理。"""
from collections import OrderedDict
import copy
import hashlib
import json
import threading
import time
from urllib.parse import urlsplit

import httpx

_LOCK = threading.RLock()
_RESPONSES = OrderedDict()
_PARSED = OrderedDict()
MAX_CACHE_BYTES = 16 * 1024 * 1024
MAX_CACHE_ENTRIES = 32
PARSER_CONTRACT = 'local-html-pdf-page-ocr-wechat-v7-body-observation'


def _eligible(url, headers):
    from .url_policy import SENSITIVE
    parsed = urlsplit(url)
    return (not parsed.query and not parsed.fragment and not SENSITIVE.search(parsed.path)
            and not any(k.lower() in {'authorization','cookie'} for k in headers))


def _key(url, headers):
    return hashlib.sha256(json.dumps([url,sorted((k.lower(),v) for k,v in headers.items())]).encode()).hexdigest()


def _prune(rows):
    while rows and (len(rows) > MAX_CACHE_ENTRIES or sum(r['size'] for r in rows.values()) > MAX_CACHE_BYTES):
        rows.popitem(last=False)


def fetch(request, headers, fetcher):
    """Representation key includes all request headers, conservatively covering Vary."""
    from runtime.execution_context import check_execution
    check_execution()
    eligible = _eligible(request.url, headers)
    key = _key(request.url, headers)
    with _LOCK:
        row = copy.deepcopy(_RESPONSES.get(key)) if eligible else None
    if row and row['response'].headers.get('cache-control','').lower().find('no-cache') < 0 and row['until'] > time.monotonic():
        if len(row['response'].content) > request.max_bytes:
            from .url_policy import URLPolicyError
            raise URLPolicyError('response exceeds bounded extraction size')
        response = row['response']
        response.extensions.update(acquisition_cache='fresh', acquisition_fetch_count=0)
        return response
    conditional = dict(headers)
    if row:
        for source, target in [('etag','If-None-Match'),('last-modified','If-Modified-Since')]:
            if row['response'].headers.get(source):
                conditional[target] = row['response'].headers[source]
    for attempt in range(2):
        check_execution()
        remaining = min(request.timeout,request.deadline_at-time.monotonic()) if request.deadline_at else request.timeout
        if remaining <= 0:
            raise httpx.TimeoutException('acquisition deadline exceeded')
        try:
            response = fetcher(request.url, timeout=remaining, max_bytes=request.max_bytes, headers=conditional)
            break
        except httpx.ConnectError:
            # Anonymous GET only: one bounded retry for a connection/TLS setup
            # failure, never for a refusal/challenge or a POST's unknown result.
            if attempt:
                raise
    check_execution()
    if len(response.content) > request.max_bytes:
        from .url_policy import URLPolicyError
        raise URLPolicyError('response exceeds bounded extraction size')
    cache_status = 'miss'
    if response.status_code == 304 and row:
        merged = dict(row['response'].headers)
        merged.update(response.headers)
        response = httpx.Response(200,headers=merged,content=row['response'].content,request=row['response'].request,extensions=dict(row['response'].extensions))
        cache_status = 'revalidated'
        if len(response.content) > request.max_bytes:
            from .url_policy import URLPolicyError
            raise URLPolicyError('response exceeds bounded extraction size')
    response.extensions.update(acquisition_cache=cache_status, acquisition_fetch_count=attempt+1)
    control = response.headers.get('cache-control','').lower()
    import re
    age = re.search(r'(?:^|,)\s*max-age\s*=\s*"?(\d+)', control)
    ttl = min(300,int(age[1])) if age else 0
    try:
        ttl = max(0, ttl-int(response.headers.get('age','0')))
    except ValueError:
        ttl = 0
    storable = (eligible and response.status_code == 200 and 'no-store' not in control and 'private' not in control
                and not response.headers.get('set-cookie') and response.headers.get('vary','').strip() != '*')
    if storable and (ttl or response.headers.get('etag') or response.headers.get('last-modified')):
        with _LOCK:
            _RESPONSES[key] = {'response':copy.deepcopy(response),'until':time.monotonic()+ttl,'size':len(response.content)}
            _prune(_RESPONSES)
    elif eligible:
        with _LOCK:
            _RESPONSES.pop(key,None)
    return response


def parsed_key(request, response):
    control = response.headers.get('cache-control','').lower()
    if not _eligible(request.url,{}) or 'no-store' in control or 'private' in control or response.headers.get('set-cookie'):
        return ''
    return hashlib.sha256(json.dumps([PARSER_CONTRACT,str(response.url),hashlib.sha256(response.content).hexdigest(),
                                      request.preferred_format,request.max_bytes,request.document_options]).encode()).hexdigest()


def get_parsed(key):
    with _LOCK:
        row = _PARSED.get(key)
        return copy.deepcopy(row['response']) if row else None


def put_parsed(key, response):
    # Retry after the user supplies authorization/key or a provider recovers.
    # An incomplete OCR result must not become a sticky parse-cache hit.
    if key and response.metadata.get('ocr_status', 'not_needed') in {'not_needed', 'complete'}:
        with _LOCK:
            _PARSED[key] = {'response':copy.deepcopy(response),'size':len(response.content.encode())}
            _prune(_PARSED)
