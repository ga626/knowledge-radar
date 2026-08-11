"""Lightweight generic web collector.

The first implementation intentionally avoids heavy browser frameworks:
1. Jina Reader URL-to-Markdown for clean article/document extraction.
2. Trafilatura/readability local extraction for normal HTML pages.
3. Static HTTP + BeautifulSoup fallback for simple HTML pages.
"""

from __future__ import annotations

import re
import time
from typing import Dict, Optional
from urllib.parse import quote, urlparse

import httpx
from bs4 import BeautifulSoup

from kr_core.strategy import generic_web_strategy_tree

from .models import GenericWebRequest, GenericWebResponse, utc_now_iso


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
        return data


def collect_url(request: GenericWebRequest) -> GenericWebResponse:
    started = time.time()
    errors = []
    strategy = generic_web_strategy_tree(use_jina=request.use_jina)

    if not _valid_http_url(request.url):
        return _error_response(
            request,
            started,
            GenericWebError("invalid_url", "Only http/https URLs are supported", collector="generic_web"),
            strategy=strategy,
        )

    if request.use_jina:
        try:
            return _with_elapsed(_collect_via_jina(request), started, errors=errors, strategy=strategy)
        except GenericWebError as exc:
            errors.append(exc.to_dict())
        except Exception as exc:
            errors.append(GenericWebError("unknown", str(exc), collector="jina_reader").to_dict())

    try:
        return _with_elapsed(_collect_via_trafilatura(request), started, errors=errors, strategy=strategy)
    except GenericWebError as exc:
        errors.append(exc.to_dict())
    except Exception as exc:
        errors.append(GenericWebError("unknown", str(exc), collector="trafilatura").to_dict())

    try:
        return _with_elapsed(_collect_via_readability(request), started, errors=errors, strategy=strategy)
    except GenericWebError as exc:
        errors.append(exc.to_dict())
    except Exception as exc:
        errors.append(GenericWebError("unknown", str(exc), collector="readability").to_dict())

    try:
        return _with_elapsed(_collect_static_html(request), started, errors=errors, strategy=strategy)
    except GenericWebError as exc:
        errors.append(exc.to_dict())
        return _error_response(request, started, exc, errors=errors, strategy=strategy)
    except Exception as exc:
        error = GenericWebError("unknown", str(exc), collector="static_html")
        errors.append(error.to_dict())
        return _error_response(request, started, error, errors=errors, strategy=strategy)


def _valid_http_url(url: str) -> bool:
    parsed = urlparse(url or "")
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _collect_via_jina(request: GenericWebRequest) -> GenericWebResponse:
    parsed = urlparse(request.url)
    target = f"{parsed.scheme}://{parsed.netloc}{parsed.path or ''}"
    if parsed.query:
        target += f"?{parsed.query}"
    reader_url = "https://r.jina.ai/http://" + quote(target.removeprefix("http://").removeprefix("https://"), safe="/:?=&%#.-_~")
    if parsed.scheme == "https":
        reader_url = "https://r.jina.ai/http://" + quote(target.removeprefix("https://"), safe="/:?=&%#.-_~")

    headers = {"Accept": "text/plain, text/markdown;q=0.9, */*;q=0.8"}
    try:
        jina_timeout = min(float(request.timeout or 20.0), 8.0)
        with httpx.Client(timeout=jina_timeout, follow_redirects=True, headers=headers) as client:
            response = client.get(reader_url)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise GenericWebError("network_error", str(exc), collector="jina_reader") from exc
    except httpx.RequestError as exc:
        raise GenericWebError("network_error", str(exc), collector="jina_reader") from exc

    if response.status_code in {401, 403, 429, 451}:
        raise GenericWebError("anti_bot_or_blocked", f"Jina Reader HTTP {response.status_code}", collector="jina_reader", status_code=response.status_code)
    if response.status_code >= 400:
        raise GenericWebError("network_error", f"Jina Reader HTTP {response.status_code}", collector="jina_reader", status_code=response.status_code)

    text = _normalize_text(response.text)
    if _looks_blocked(text):
        raise GenericWebError("anti_bot_or_blocked", "Jina Reader returned a blocked/captcha-like page", collector="jina_reader", status_code=response.status_code)
    if len(text) < 200:
        raise GenericWebError("parse_failed", "Jina Reader returned too little content", collector="jina_reader", status_code=response.status_code)

    title = _extract_markdown_title(text)
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
        with httpx.Client(timeout=request.timeout, follow_redirects=True, headers=headers) as client:
            response = client.get(request.url)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise GenericWebError("network_error", str(exc), collector=collector) from exc
    except httpx.RequestError as exc:
        raise GenericWebError("network_error", str(exc), collector=collector) from exc

    if response.status_code in {401, 403, 429, 451}:
        raise GenericWebError("anti_bot_or_blocked", f"HTTP {response.status_code}", collector=collector, status_code=response.status_code)
    if response.status_code >= 400:
        raise GenericWebError("network_error", f"HTTP {response.status_code}", collector=collector, status_code=response.status_code)

    content_type = response.headers.get("content-type", "")
    if "html" not in content_type.lower() and "xml" not in content_type.lower():
        raise GenericWebError("unsupported_content_type", f"Unsupported content-type: {content_type}", collector=collector, status_code=response.status_code)
    return response, content_type


def _collect_via_trafilatura(request: GenericWebRequest) -> GenericWebResponse:
    response, content_type = _fetch_html(request, collector="trafilatura")
    try:
        import trafilatura

        text = trafilatura.extract(
            response.text,
            url=str(response.url),
            output_format="markdown",
            include_comments=False,
            include_tables=True,
            favor_precision=False,
        )
    except Exception as exc:
        raise GenericWebError("parse_failed", str(exc), collector="trafilatura", status_code=response.status_code) from exc

    markdown = _normalize_markdown(text or "")
    if _looks_blocked(markdown):
        raise GenericWebError("anti_bot_or_blocked", "Trafilatura returned a blocked/captcha-like page", collector="trafilatura", status_code=response.status_code)
    if len(markdown) < 200:
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
        },
    )


def _collect_via_readability(request: GenericWebRequest) -> GenericWebResponse:
    response, content_type = _fetch_html(request, collector="readability")
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
    if len(markdown) < 200:
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


def _collect_static_html(request: GenericWebRequest) -> GenericWebResponse:
    response, content_type = _fetch_html(request, collector="static_html")
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
        return _error_response(request, started, exc, errors=[exc.to_dict()], strategy=strategy)


def _collect_from_html(
    request: GenericWebRequest,
    *,
    html: str,
    final_url: str,
    status_code: int,
    content_type: str,
    collector: str,
) -> GenericWebResponse:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript", "svg", "canvas", "form", "nav", "footer", "aside"]):
        node.decompose()
    title = _normalize_inline(soup.title.get_text(" ")) if soup.title else ""
    main, selector, candidate_count = _select_content_root(soup)
    markdown = _html_to_markdown(main)
    markdown = _normalize_markdown(markdown)
    if _looks_blocked(markdown):
        raise GenericWebError("anti_bot_or_blocked", "HTML extraction returned a blocked/captcha-like page", collector=collector, status_code=status_code)
    if len(markdown) < 200:
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


def _select_content_root(soup: BeautifulSoup):
    """Choose a rendered content container without treating body as the only fallback."""
    candidates = []
    seen = set()
    selector_specs = [
        ("article", lambda: soup.find_all("article")),
        ("main", lambda: soup.find_all("main")),
        ("role=main", lambda: soup.select('[role="main"]')),
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


def _html_to_markdown(node) -> str:
    parts = []
    for element in node.find_all(["h1", "h2", "h3", "p", "li", "pre", "code", "blockquote"], recursive=True):
        text = _normalize_inline(element.get_text(" "))
        if not text:
            continue
        name = element.name.lower()
        if name == "h1":
            parts.append(f"# {text}")
        elif name == "h2":
            parts.append(f"## {text}")
        elif name == "h3":
            parts.append(f"### {text}")
        elif name == "li":
            parts.append(f"- {text}")
        elif name == "blockquote":
            parts.append(f"> {text}")
        elif name in {"pre", "code"}:
            parts.append(f"```\n{text}\n```")
        else:
            parts.append(text)
    return "\n\n".join(parts)


def _normalize_inline(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _normalize_markdown(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text or "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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
    sample = (text or "").lower()
    markers = [
        "captcha",
        "access denied",
        "verify you are human",
        "enable javascript",
        "too many requests",
        "cloudflare",
        "访问过于频繁",
        "安全验证",
        "请完成验证",
    ]
    return any(marker in sample for marker in markers)


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
    metadata = {"fallback_errors": errors or [error.to_dict()]}
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
        final_url=request.url,
        content_format=request.preferred_format,
        collector=error.collector,
        fetched_at=utc_now_iso(),
        elapsed_s=round(time.time() - started, 3),
        metadata=metadata,
        error=error.to_dict(),
    )
