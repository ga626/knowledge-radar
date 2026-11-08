"""Lightweight generic web collector.

The first implementation intentionally avoids heavy browser frameworks:
1. Jina Reader URL-to-Markdown for clean article/document extraction.
2. Trafilatura/readability local extraction for normal HTML pages.
3. Static HTTP + BeautifulSoup fallback for simple HTML pages.
"""

from __future__ import annotations

import re
import time
from dataclasses import replace
from typing import Dict, Optional
from urllib.parse import quote, urlparse

import httpx
from bs4 import BeautifulSoup

from kr_core.strategy import generic_web_strategy_tree

from .models import GenericWebRequest, GenericWebResponse, utc_now_iso
from .url_policy import URLPolicyError, guarded_fetch, public_addresses, reader_allowed


class GenericWebError(Exception):
    def __init__(self, error_type: str, message: str, *, collector: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.collector = collector
        self.status_code = status_code

    def to_dict(self) -> Dict[str, object]:
        data: Dict[str, object] = {
            "type": self.error_type,
            "message": self.message,
            "collector": self.collector,
        }
        if self.status_code is not None:
            data["status_code"] = self.status_code
        data['retryable'] = self.error_type in {'rate_limited', 'upstream_error', 'network_error'}
        data['next_action'] = {'authentication_required': 'Use an explicitly authorized account route.',
            'access_forbidden': 'Check access policy; do not assume login or bypass restrictions.',
            'not_found': 'Check URL or choose another source.', 'rate_limited': 'Respect retry_after before a new job.',
            'legal_restriction': 'Choose a permitted source.'}.get(self.error_type, 'Inspect the recorded failure before retrying.')
        if getattr(self, 'retry_after', ''):
            data['retry_after'] = self.retry_after
        return data


def collect_url(request: GenericWebRequest) -> GenericWebResponse:
    from runtime.execution_context import check_execution
    started = time.time()
    request = replace(request, deadline_at=request.deadline_at or time.monotonic() + request.timeout)
    errors = []
    strategy = generic_web_strategy_tree(use_jina=request.use_jina)
    try:
        check_execution()
        public_addresses(request.url)
        artifact = _fetch_html(request, collector='http_acquisition')
    except URLPolicyError as exc:
        kind = 'document_too_large' if 'exceeds bounded' in str(exc) else 'restricted_url'
        return _error_response(request, started, GenericWebError(kind, str(exc), collector='http_acquisition'), strategy=strategy)
    except GenericWebError as exc:
        return _error_response(request, started, exc, strategy=strategy)
    # 微信状态在任何通用解析/远端Reader之前判定；所有入口共用同一已下载字节。
    from collectors.platform.wechat import article_url, parse_html
    if article_url(request.url) or article_url(str(artifact[0].url)):
        data = parse_html(artifact[0].text, str(artifact[0].url))
        return _wechat_response(request, data, str(artifact[0].url))
    from .acquisition import parsed_key, get_parsed, put_parsed
    parse_key = parsed_key(request, artifact[0])
    parsed = get_parsed(parse_key)
    if parsed is not None:
        parsed.metadata.update(parse_cache='content_hash_and_contract', acquisition={
            'fetch_count':artifact[0].extensions.get('acquisition_fetch_count',1),
            'cache':artifact[0].extensions.get('acquisition_cache','miss'), 'bytes':len(artifact[0].content)})
        return _with_elapsed(parsed, started, errors=[], strategy=strategy)
    for parse in (_collect_via_trafilatura, _collect_via_readability, _collect_static_html):
        check_execution()
        try:
            _remaining(request)
            response = parse(request, artifact=artifact)
            _remaining(request)
            response.metadata.update(acquisition={'fetch_count': artifact[0].extensions.get('acquisition_fetch_count',1),
                'cache':artifact[0].extensions.get('acquisition_cache','miss'), 'bytes': len(artifact[0].content),
                'status_code': artifact[0].status_code, 'kind': 'original_bytes'},
                processing_method=response.metadata.get('processing_method', response.collector))
            put_parsed(parse_key, response)
            return _with_elapsed(response, started, errors=errors, strategy=strategy)
        except GenericWebError as exc:
            errors.append(exc.to_dict())
            if exc.error_type not in {'parse_failed', 'requires_dynamic_render'}:
                return _error_response(request, started, exc, errors=errors, strategy=strategy)
        except Exception as exc:
            from runtime.execution_context import ExecutionStopped
            if isinstance(exc, ExecutionStopped):
                raise
            errors.append(GenericWebError('parse_failed', type(exc).__name__, collector=parse.__name__).to_dict())
    if request.use_jina and 'pdf' not in artifact[1].lower() and reader_allowed(request.url):
        try:
            return _with_elapsed(_collect_via_jina(request), started, errors=errors, strategy=strategy)
        except GenericWebError as exc:
            errors.append(exc.to_dict())
    error = errors[-1] if errors else {'type': 'parse_failed', 'message': 'No readable content', 'collector': 'generic_web'}
    return _error_response(request, started, GenericWebError(error['type'], error['message'], collector=error['collector']), errors=errors, strategy=strategy)


def _check_status(status, *, collector, retry_after=''):
    if status < 400:
        return
    kinds = {401: 'authentication_required', 403: 'access_forbidden', 404: 'not_found',
             429: 'rate_limited', 451: 'legal_restriction'}
    error = GenericWebError(kinds.get(status, 'upstream_error' if status >= 500 else 'http_error'),
        f'HTTP {status}', collector=collector, status_code=status)
    error.retry_after = retry_after
    raise error


def _remaining(request):
    from runtime.execution_context import check_execution
    check_execution()
    value = min(request.timeout, request.deadline_at-time.monotonic()) if request.deadline_at else request.timeout
    if value <= 0:
        raise GenericWebError('deadline_exceeded', 'Acquisition deadline exceeded', collector='http_acquisition')
    return value


def _valid_http_url(url: str) -> bool:
    try:
        public_addresses(url, resolve=False)
        return True
    except URLPolicyError:
        return False


def _collect_via_jina(request: GenericWebRequest) -> GenericWebResponse:
    parsed = urlparse(request.url)
    target = f"{parsed.scheme}://{parsed.netloc}{parsed.path or ''}"
    if parsed.query:
        target += f"?{parsed.query}"
    reader_url = 'https://r.jina.ai/' + quote(target, safe='/:?=&%#.-_~')

    headers = {"Accept": "text/plain, text/markdown;q=0.9, */*;q=0.8"}
    try:
        jina_timeout = min(_remaining(request), 8.0)
        response = guarded_fetch(reader_url, timeout=jina_timeout, max_bytes=request.max_bytes, headers=headers)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise GenericWebError("network_error", str(exc), collector="jina_reader") from exc
    except httpx.RequestError as exc:
        raise GenericWebError("network_error", str(exc), collector="jina_reader") from exc

    _check_status(response.status_code, collector='jina_reader', retry_after=response.headers.get('retry-after', ''))

    text = _normalize_text(response.text)
    if _looks_blocked(text):
        raise GenericWebError("anti_bot_or_blocked", "Jina Reader returned a blocked/captcha-like page", collector="jina_reader", status_code=response.status_code)
    if not text.strip():
        raise GenericWebError("parse_failed", "Jina Reader returned too little content", collector="jina_reader", status_code=response.status_code)

    title = _extract_markdown_title(text)
    from kr_core.evidence_coverage import body_observation
    observation = body_observation({'title': title, 'content': re.split(r'^Markdown Content:\s*',text,flags=re.M)[-1]})
    if observation['missing_reason']:
        raise GenericWebError(observation['missing_reason'], 'Reader returned an access or navigation shell', collector='jina_reader', status_code=response.status_code)
    return GenericWebResponse(
        url=request.url,
        final_url=request.url,
        title=title,
        content=text,
        content_format="markdown",
        collector="jina_reader",
        fetched_at=utc_now_iso(),
        metadata={
            "reader_url": reader_url,
            "acquisition_kind": "remote_converted_text",
            "processing_method": "jina_reader",
            "status_code": response.status_code,
            "content_chars": len(text),
        },
    )


def _fetch_html(request: GenericWebRequest, *, collector: str):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        from .acquisition import fetch
        _remaining(request)
        response = fetch(request, headers, guarded_fetch)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise GenericWebError("network_error", str(exc), collector=collector) from exc
    except httpx.RequestError as exc:
        raise GenericWebError("network_error", str(exc), collector=collector) from exc

    _check_status(response.status_code, collector=collector, retry_after=response.headers.get('retry-after', ''))

    content_type = response.headers.get("content-type", "")
    if response.content.startswith(b'%PDF-'):
        content_type = 'application/pdf'  # Public file hosts may use octet-stream.
    if not any(kind in content_type.lower() for kind in ["html", "xml", "text/plain", "text/markdown", "application/pdf"]):
        raise GenericWebError("unsupported_content_type", f"Unsupported content-type: {content_type}", collector=collector, status_code=response.status_code)
    if 'html' in content_type.lower():
        soup = BeautifulSoup(response.text, 'lxml')
        try:
            _check_page_body(soup, collector=collector, status_code=response.status_code)
        except GenericWebError as exc:
            from .url_policy import redact_url
            exc.final_url = redact_url(str(response.url))
            exc.redirect_chain = response.extensions.get('redirect_chain', [])
            raise
        if soup.select('script[src], #app, #root, #__next'):
            for node in soup(['script','style','noscript','head']):
                node.decompose()
            text = _normalize_inline(soup.get_text(' ')).strip(' .…!。！')
            if not text or text.lower() in {'loading','please wait','正在加载','加载中','enable javascript','please enable javascript'}:
                raise GenericWebError('requires_dynamic_render', 'Application shell has no readable content', collector=collector)
    return response, content_type


def _check_page_body(soup, *, collector, status_code):
    """强访问壳信号先于解析器检查；正文中的普通登录/验证码讨论不阻断。"""
    from kr_core.evidence_coverage import body_observation
    body = soup.body or soup
    observation = body_observation({'title': soup.title.get_text(' ', strip=True) if soup.title else '',
                                    'content': body.get_text(' ', strip=True)})
    reason = observation['missing_reason']
    article = soup.select_one('article,main')
    if reason and article and article.get_text(' ',strip=True):
        # 旁栏的JS/登录提示不应截断已经存在的正文；强挑战标题仍会被拒绝。
        reason = body_observation({'title':soup.title.get_text(' ',strip=True) if soup.title else '',
                                   'content':article.get_text(' ',strip=True)})['missing_reason']
    if not reason and soup.select_one('meta[http-equiv="refresh" i]') and not soup.select_one('article,main'):
        reason = 'redirect_shell'
    if not reason and not body.get_text(' ', strip=True) and any(re.search(r'(?:location\.(?:replace|assign)\s*\(|location(?:\.href)?\s*=)', node.get_text()) for node in soup.find_all('script')):
        reason = 'redirect_shell'
    if not reason and soup.select_one('input[type="password"]') and not soup.select_one('article,main'):
        reason = 'authentication_required'
    if reason:
        raise GenericWebError('requires_dynamic_render' if reason == 'navigation_shell' else reason, 'Page contains an access or navigation shell, not the requested body',
                              collector=collector, status_code=status_code)


def _collect_via_trafilatura(request: GenericWebRequest, *, artifact=None) -> GenericWebResponse:
    response, content_type = artifact or _fetch_html(request, collector="trafilatura")
    if 'application/pdf' in content_type.lower():
        from runtime.pdf_text import process_pdf
        try:
            from runtime.document_contract import response_cache_allowed
            opts = request.document_options
            parsed = process_pdf(response.content, max_bytes=min(request.max_bytes,32*1024*1024), timeout_s=_remaining(request),
                allow_ocr=opts.get('include_ocr') is not False, enhance_suspicious=opts.get('include_ocr') is True,
                page_numbers=opts.get('page_numbers'),cache_allowed=response_cache_allowed(response),
                storage_allowed=opts.get('storage_allowed',True),read_allowed=opts.get('read_allowed',True))
        except Exception as exc:
            from runtime.execution_context import ExecutionStopped
            if isinstance(exc, ExecutionStopped):
                raise
            reason = str(exc) if str(exc) in {'document_too_large', 'document_page_budget_exceeded', 'document_expansion_budget_exceeded'} else 'pdf_parse_failed'
            raise GenericWebError(reason, type(exc).__name__, collector='pypdf') from exc
        if not parsed['text'].strip():
            reason = (parsed.get('ocr_errors') or [{}])[0].get('type', 'pdf_text_unavailable')
            raise GenericWebError(reason, 'PDF OCR could not complete; inspect OCR configuration or provider status.', collector='pypdf')
        return GenericWebResponse(url=request.url, final_url=str(response.url), title=request.url.rsplit('/', 1)[-1],
            content=parsed['text'], content_format='text', collector='pypdf', fetched_at=utc_now_iso(),
            metadata={**{k:v for k,v in parsed.items() if k != 'text'}, 'images':'not_requested',
                      'tables':'text_only_no_cell_geometry', 'content_type':content_type})
    if content_type.lower().startswith(("text/plain", "text/markdown")):
        original = response.text
        text = original[:1_000_000].strip()
        if not text:
            raise GenericWebError("empty_detail", "Plain text body is empty", collector="plain_text")
        return GenericWebResponse(url=request.url, final_url=str(response.url), title=request.url.rsplit("/", 1)[-1],
                                  content=text, content_format="text", collector="plain_text", fetched_at=utc_now_iso(),
                                  metadata={"status_code": response.status_code, "content_type": content_type,
                                            "content_chars": len(text), "truncated": len(original) > 1_000_000})
    try:
        import trafilatura

        text = trafilatura.extract(
            response.text,
            url=str(response.url),
            output_format="markdown",
            include_comments=False,
            include_tables=True,
            include_formatting=True,
            favor_precision=False,
            favor_recall=True,
        )
    except Exception as exc:
        raise GenericWebError("parse_failed", str(exc), collector="trafilatura", status_code=response.status_code) from exc

    markdown = _normalize_markdown(text or "")
    if _looks_blocked(markdown):
        raise GenericWebError("anti_bot_or_blocked", "Trafilatura returned a blocked/captcha-like page", collector="trafilatura", status_code=response.status_code)
    if not markdown.strip():
        raise GenericWebError("parse_failed", "Trafilatura extraction returned too little content", collector="trafilatura", status_code=response.status_code)

    return GenericWebResponse(
        url=request.url,
        final_url=str(response.url),
        title=_extract_html_title(response.text) or _extract_markdown_title(markdown),
        content=markdown,
        content_format="markdown",
        collector="trafilatura",
        fetched_at=utc_now_iso(),
        metadata={
            "status_code": response.status_code,
            "content_type": content_type,
            "content_chars": len(markdown),
            "extraction_policy": "favor_recall_preserve_body_structures",
        },
    )


def _collect_via_readability(request: GenericWebRequest, *, artifact=None) -> GenericWebResponse:
    response, content_type = artifact or _fetch_html(request, collector="readability")
    try:
        from readability import Document

        doc = Document(response.text)
        title = _normalize_inline(doc.short_title() or "")
        soup = BeautifulSoup(doc.summary(html_partial=True), "lxml")
        markdown = _normalize_markdown(_html_to_markdown(soup))
    except Exception as exc:
        raise GenericWebError("parse_failed", str(exc), collector="readability", status_code=response.status_code) from exc

    if _looks_blocked(markdown):
        raise GenericWebError("anti_bot_or_blocked", "Readability returned a blocked/captcha-like page", collector="readability", status_code=response.status_code)
    if not markdown.strip():
        raise GenericWebError("parse_failed", "Readability extraction returned too little content", collector="readability", status_code=response.status_code)

    return GenericWebResponse(
        url=request.url,
        final_url=str(response.url),
        title=title or _extract_html_title(response.text) or _extract_markdown_title(markdown),
        content=markdown,
        content_format="markdown",
        collector="readability",
        fetched_at=utc_now_iso(),
        metadata={
            "status_code": response.status_code,
            "content_type": content_type,
            "content_chars": len(markdown),
        },
    )


def _collect_static_html(request: GenericWebRequest, *, artifact=None) -> GenericWebResponse:
    response, content_type = artifact or _fetch_html(request, collector="static_html")
    return _collect_from_html(
        request,
        html=response.text,
        final_url=str(response.url),
        status_code=response.status_code,
        content_type=content_type,
        collector="static_html",
    )


def collect_rendered_html(
    request: GenericWebRequest,
    *,
    html: str,
    final_url: str = "",
    status_code: int = 200,
    content_type: str = "text/html",
    render_metadata: Dict[str, object] | None = None,
) -> GenericWebResponse:
    """Clean already-rendered HTML from a dynamic browser collector."""
    started = time.time()
    strategy = generic_web_strategy_tree(use_jina=False, include_dynamic_hint=True)
    try:
        response = _with_elapsed(
            _collect_from_html(
                request,
                html=html,
                final_url=final_url or request.url,
                status_code=status_code,
                content_type=content_type,
                collector="dynamic_playwright",
            ),
            started,
            errors=[],
            strategy=strategy,
        )
        if render_metadata:
            response.metadata.update({"render": dict(render_metadata)})
        return response
    except GenericWebError as exc:
        response = _error_response(request, started, exc, errors=[exc.to_dict()], strategy=strategy)
        if render_metadata:
            response.metadata['render'] = dict(render_metadata)
        return response


def _collect_from_html(
    request: GenericWebRequest,
    *,
    html: str,
    final_url: str,
    status_code: int,
    content_type: str,
    collector: str,
) -> GenericWebResponse:
    _check_status(status_code, collector=collector)
    from collectors.platform.wechat import article_url, parse_html
    if article_url(request.url) or article_url(final_url):
        return _wechat_response(request, parse_html(html, final_url, transport=collector), final_url)
    soup = BeautifulSoup(html or "", "lxml")
    _check_page_body(soup, collector=collector, status_code=status_code)
    has_client_scripts = bool(soup.select('script[src], #app, #root, #__next'))
    for node in soup(["script", "style", "noscript", "svg", "canvas", "form", "nav", "footer", "aside"]):
        node.decompose()
    title = _normalize_inline(soup.title.get_text(" ")) if soup.title else ""
    main, selector, candidate_count = _select_content_root(soup)
    markdown = _html_to_markdown(main)
    markdown = _normalize_markdown(markdown)
    if _looks_blocked(markdown):
        raise GenericWebError("anti_bot_or_blocked", "HTML extraction returned a blocked/captcha-like page", collector=collector, status_code=status_code)
    if not markdown.strip():
        if has_client_scripts:
            rendered = collector == 'dynamic_playwright'
            raise GenericWebError('rendered_content_unavailable' if rendered else 'requires_dynamic_render',
                '渲染后仍无可读正文；需要站点公开数据接口或其他官方正文来源。' if rendered else '页面只有脚本或应用壳；需要显式动态渲染。',
                collector=collector, status_code=status_code)
        raise GenericWebError("parse_failed", "HTML extraction returned too little content", collector=collector, status_code=status_code)

    return GenericWebResponse(
        url=request.url,
        final_url=final_url,
        title=title or _extract_markdown_title(markdown),
        content=markdown,
        content_format="markdown",
        collector=collector,
        fetched_at=utc_now_iso(),
        metadata={
            "status_code": status_code,
            "content_type": content_type,
            "content_chars": len(markdown),
            "content_selector": selector,
            "content_candidate_count": candidate_count,
        },
    )


def _wechat_response(request, data, final_url):
    return GenericWebResponse(url=request.url, final_url=final_url, title=data.get('title',''),
        content=data.get('content',''), collector=data.get('collector','wechat_http'), fetched_at=utc_now_iso(),
        metadata={'wechat':{k:v for k,v in data.items() if k!='content'},'source_presentation':data['source_presentation'],
                  'processing_method':'wechat_dom_to_markdown'}, error=data.get('error'))


def _select_content_root(soup: BeautifulSoup):
    """Choose a rendered content container without treating body as the only fallback."""
    candidates = []
    seen = set()
    selector_specs = [
        ("article", lambda: soup.find_all("article")),
        ("main", lambda: soup.find_all("main")),
        ("role=main", lambda: soup.select('[role="main"]')),
        ("app_root", lambda: soup.select('#main, #app, #root, #__next')),
        ("semantic_class_or_id", lambda: soup.find_all(_semantic_content_candidate)),
    ]
    for label, finder in selector_specs:
        for node in finder():
            identity = id(node)
            if identity in seen:
                continue
            seen.add(identity)
            text_length = len(_normalize_inline(node.get_text(" ")))
            if not text_length:
                continue
            # Structural elements are preferred when comparable, while a long
            # rendered article body can beat an empty layout <main>.
            structural_bonus = 500 if label in {"article", "main", "role=main"} else 250
            candidates.append((text_length + structural_bonus, label, node))
    if candidates:
        _, label, node = max(candidates, key=lambda item: item[0])
        return node, label, len(candidates)
    return soup.body or soup, "body_fallback", 0


def _semantic_content_candidate(node) -> bool:
    if not getattr(node, "name", None):
        return False
    value = " ".join(
        [str(node.get("id") or ""), *[str(item) for item in (node.get("class") or [])]]
    ).lower()
    return bool(re.search(r"(?:article|content|post|entry|story|markdown|prose|正文|内容)", value))


def html_fragment_to_markdown(html: str) -> str:
    """转换已采集正文；不再次下载、挑选实体或执行页面脚本。"""
    from bs4 import BeautifulSoup, NavigableString
    root = BeautifulSoup(html, 'html.parser')
    for image in root.find_all('img'):
        url = str(image.get('src') or '')
        alt = str(image.get('alt') or '').replace('[', '').replace(']', '')
        image.replace_with(NavigableString(f'![{alt}]({url.replace(")", "%29")})') if url.startswith(('https://', 'http://')) else NavigableString(alt))
    return _normalize_markdown(_html_to_markdown(root))


def _html_to_markdown(node) -> str:
    """按树渲染一次；保留示例、缩进、表格，不把嵌套元素再抄一遍。"""
    from bs4 import NavigableString

    def render(element):
        if isinstance(element, NavigableString):
            return re.sub(r"\s+", " ", str(element))
        name = (element.name or '').lower()
        if name in {'script', 'style', 'noscript', 'svg', 'canvas', 'nav', 'footer', 'aside', 'form'}:
            return ''
        if name == 'pre':
            def code_text(item):
                if isinstance(item, NavigableString):
                    return str(item)
                if item.name == 'br':
                    return '\n'
                if 'code-snippet__line-index' in (item.get('class') or []):
                    return ''
                text = ''.join(code_text(child) for child in item.children)
                if item.name in {'div', 'li'} or 'code-snippet__line' in (item.get('class') or []):
                    return text + ('\n' if not text.endswith('\n') else '')
                return text
            code = code_text(element).replace('\r\n', '\n').replace('\r', '\n').strip('\n')
            runs = [len(x) for x in re.findall(r'`+', code)]
            fence = '`' * max(3, max(runs, default=0) + 1)
            return '\n\n' + fence + '\n' + code + '\n' + fence + '\n\n'
        if name == 'table':
            rows = []
            for tr in element.find_all('tr'):
                if tr.find_parent('table') is not element:
                    continue
                cells = [render(cell).strip().replace('\n', '<br>').replace('|', r'\|')
                         for cell in tr.find_all(['th', 'td'], recursive=False)]
                if cells:
                    rows.append(cells)
            if not rows:
                return ''
            width = max(map(len, rows))
            rows = [row + [''] * (width-len(row)) for row in rows]
            table = ['| ' + ' | '.join(row) + ' |' for row in rows]
            table.insert(1, '| ' + ' | '.join(['---']*width) + ' |')
            return '\n\n' + '\n'.join(table) + '\n\n'
        text = ''.join(render(child) for child in element.children)
        if name == 'code':
            fence = '`' * (max([len(x) for x in re.findall(r'`+', text)], default=0)+1)
            return fence + text + fence
        if name in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
            return '\n\n' + '#'*int(name[1]) + ' ' + text.strip() + '\n\n'
        if name == 'br':
            return '\n'
        if name == 'li':
            return '\n- ' + text.strip() + '\n'
        if name == 'blockquote':
            return '\n\n' + '\n'.join('> '+line for line in text.strip().splitlines()) + '\n\n'
        if name == 'a':
            href = str(element.get('href') or '')
            if href.startswith(('https://', 'http://', '#', '/')):
                return '['+text.strip()+']('+href.replace(' ', '%20').replace(')', '%29')+')'
        if name in {'p', 'div', 'section', 'article', 'main', 'ul', 'ol', 'header'}:
            return '\n\n'+text.strip()+'\n\n'
        return text

    return render(node).strip()


def _normalize_inline(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _normalize_markdown(text: str) -> str:
    # 正文空白可规整，代码围栏内的空行、尾空格和缩进属于原内容。
    result, fence, blanks = [], '', 0
    for line in (text or '').splitlines():
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if fence:
            result.append(line)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = ''
            continue
        if marker:
            fence = marker[1]
            result.append(line)
            blanks = 0
        elif line.strip():
            result.append(line.rstrip())
            blanks = 0
        else:
            blanks += 1
            if blanks <= 1:
                result.append('')
    return '\n'.join(result).strip()


def _normalize_text(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def _extract_markdown_title(text: str) -> str:
    for line in (text or "").splitlines():
        clean = line.strip()
        if clean.startswith("#"):
            return clean.lstrip("#").strip()
        if clean and len(clean) < 120:
            return clean
    return ""


def _extract_html_title(html: str) -> str:
    try:
        soup = BeautifulSoup(html or "", "lxml")
        if soup.title:
            return _normalize_inline(soup.title.get_text(" "))
    except Exception:
        return ""
    return ""


def _looks_blocked(text: str) -> bool:
    # A technical article mentioning Cloudflare, captcha or rate limits is not a challenge page.
    sample = _normalize_text(text).lower()
    if len(sample) > 800:
        return False
    return bool(re.match(r'^(?:access denied|verify you are human|just a moment|too many requests|'
                         r'请完成验证|安全验证|访问过于频繁)(?:[.!。！？\s]|$)', sample))


def _with_elapsed(response: GenericWebResponse, started: float, *, errors: list, strategy: Optional[Dict[str, object]] = None) -> GenericWebResponse:
    metadata = dict(response.metadata)
    if errors:
        metadata["fallback_errors"] = errors
    if strategy:
        metadata["strategy"] = strategy
        metadata["selected_node"] = response.collector
        metadata["selected_strategy"] = response.collector
        metadata["fallback_count"] = len(errors or [])
        metadata["attempts"] = [
            {
                "name": str(error.get("collector") or error.get("type") or ""),
                "status": "failed",
                "error_type": str(error.get("type") or "unknown"),
                "detail": str(error.get("message") or error.get("error") or ""),
            }
            for error in errors or []
        ] + ([{"name": response.collector, "status": "ok"}] if response.collector else [])
        metadata["failure_taxonomy"] = strategy.get("error_taxonomy", {})
    return GenericWebResponse(
        url=response.url,
        final_url=response.final_url,
        title=response.title,
        content=response.content,
        content_format=response.content_format,
        collector=response.collector,
        fetched_at=response.fetched_at,
        elapsed_s=round(time.time() - started, 3),
        metadata=metadata,
        error=response.error,
    )


def _error_response(
    request: GenericWebRequest,
    started: float,
    error: GenericWebError,
    *,
    errors: Optional[list] = None,
    strategy: Optional[Dict[str, object]] = None,
) -> GenericWebResponse:
    metadata = {"fallback_errors": errors or [error.to_dict()], 'redirect_chain':getattr(error, 'redirect_chain', [])}
    if strategy:
        metadata["strategy"] = strategy
        metadata["selected_node"] = ""
        metadata["selected_strategy"] = ""
        metadata["fallback_count"] = len(errors or [error.to_dict()])
        metadata["attempts"] = [
            {
                "name": str(item.get("collector") or item.get("type") or ""),
                "status": "failed",
                "error_type": str(item.get("type") or "unknown"),
                "detail": str(item.get("message") or item.get("error") or ""),
            }
            for item in errors or [error.to_dict()]
        ]
        metadata["failure_taxonomy"] = strategy.get("error_taxonomy", {})
    return GenericWebResponse(
        url=request.url,
        final_url=getattr(error, 'final_url', request.url),
        content_format=request.preferred_format,
        collector=error.collector,
        fetched_at=utc_now_iso(),
        elapsed_s=round(time.time() - started, 3),
        metadata=metadata,
        error=error.to_dict(),
    )
