"""Dynamic web page extraction using Playwright as a high-cost fallback."""

from __future__ import annotations

import asyncio
import json
import os
from runtime.process import silent_subprocess_run
import sys
import time
from typing import Dict
from urllib.parse import urlparse

from .collector import collect_rendered_html
from .models import GenericWebRequest, GenericWebResponse, utc_now_iso
from runtime.executables import resolve_managed_chrome
from runtime.tool_trace import record_trace_child


def collect_dynamic_url(request: GenericWebRequest, *, wait_ms: int = 3000) -> GenericWebResponse:
    if not os.environ.get("KR_DYNAMIC_CHILD"):
        return _collect_dynamic_url_subprocess(request, wait_ms=wait_ms)
    return _collect_dynamic_url_current_process(request, wait_ms=wait_ms)


def _collect_dynamic_url_subprocess(request: GenericWebRequest, *, wait_ms: int = 3000) -> GenericWebResponse:
    started = time.time()
    payload = {
        "url": request.url,
        "preferred_format": request.preferred_format,
        "timeout": request.timeout,
        "use_jina": request.use_jina,
        "wait_ms": wait_ms,
    }
    script = (
        "import json, os, sys; "
        "sys.path.insert(0, r'%s'); "
        "os.environ['KR_DYNAMIC_CHILD']='1'; "
        "from generic_web import GenericWebRequest, collect_dynamic_url; "
        "p=json.loads(sys.stdin.read()); "
        "r=collect_dynamic_url(GenericWebRequest(url=p['url'], preferred_format=p.get('preferred_format','markdown'), timeout=p.get('timeout',25.0), use_jina=False), wait_ms=int(p.get('wait_ms',3000))); "
        "print(json.dumps(r.to_mcp_dict(), ensure_ascii=False))"
    ) % os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        proc = silent_subprocess_run(
            [sys.executable, "-X", "utf8", "-c", script],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=max(10, int(float(request.timeout or 25.0) + (wait_ms / 1000) + 20)),
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "dynamic subprocess failed")[-1000:])
        data = json.loads((proc.stdout or "{}").strip())
        return GenericWebResponse(
            url=data.get("url") or request.url,
            final_url=data.get("final_url") or request.url,
            title=data.get("title") or "",
            content=data.get("content") or "",
            content_format=data.get("content_format") or request.preferred_format,
            collector=data.get("collector") or "dynamic_playwright",
            fetched_at=data.get("fetched_at") or utc_now_iso(),
            elapsed_s=round(time.time() - started, 3),
            metadata=data.get("metadata") or {},
            error=data.get("error"),
        )
    except Exception as exc:
        return GenericWebResponse(
            url=request.url,
            final_url=request.url,
            content_format=request.preferred_format,
            collector="dynamic_playwright",
            fetched_at=utc_now_iso(),
            elapsed_s=round(time.time() - started, 3),
            error={"type": "dynamic_failed", "message": str(exc), "collector": "dynamic_playwright"},
        )


def _collect_dynamic_url_current_process(request: GenericWebRequest, *, wait_ms: int = 3000) -> GenericWebResponse:
    started = time.time()
    if not _valid_http_url(request.url):
        return GenericWebResponse(
            url=request.url,
            final_url=request.url,
            content_format=request.preferred_format,
            collector="dynamic_playwright",
            fetched_at=utc_now_iso(),
            elapsed_s=round(time.time() - started, 3),
            error={"type": "invalid_url", "message": "Only http/https URLs are supported", "collector": "dynamic_playwright"},
        )
    try:
        return asyncio.run(_collect_dynamic_url_async(request, wait_ms=wait_ms, started=started))
    except Exception as exc:
        return GenericWebResponse(
            url=request.url,
            final_url=request.url,
            content_format=request.preferred_format,
            collector="dynamic_playwright",
            fetched_at=utc_now_iso(),
            elapsed_s=round(time.time() - started, 3),
            error={"type": "dynamic_failed", "message": str(exc), "collector": "dynamic_playwright"},
        )


async def _collect_dynamic_url_async(request: GenericWebRequest, *, wait_ms: int, started: float) -> GenericWebResponse:
    from playwright.async_api import async_playwright

    timeout_ms = max(1000, int(float(request.timeout or 20.0) * 1000))
    async with async_playwright() as p:
        launch_options = {"headless": True}
        chrome_exe = _find_chrome_exe()
        if chrome_exe:
            launch_options["executable_path"] = chrome_exe
        browser = await p.chromium.launch(**launch_options)
        try:
            page = await browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                locale="zh-CN",
            )
            response = await page.goto(request.url, wait_until="domcontentloaded", timeout=timeout_ms)
            networkidle_status = "ready"
            try:
                await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 8000))
            except Exception as exc:
                networkidle_status = f"not_reached:{type(exc).__name__}"
            if wait_ms > 0:
                await page.wait_for_timeout(min(wait_ms, 10000))
            html = await page.content()
            final_url = page.url
            status_code = response.status if response else 200
        finally:
            await browser.close()

    cleaned = collect_rendered_html(
        request,
        html=html,
        final_url=final_url,
        status_code=status_code,
        content_type="text/html",
        render_metadata={
            "navigation_wait_until": "domcontentloaded",
            "networkidle": networkidle_status,
            "additional_wait_ms": min(wait_ms, 10000),
            "html_chars": len(html),
        },
    )
    record_trace_child(
        "collector_phase",
        metadata={
            "status": "ok" if not cleaned.error else "failed",
            "collector": "dynamic_playwright",
            "phase": "rendered_dom_cleanup",
            "outcome": str((cleaned.error or {}).get("type") or "usable_result"),
            "content_chars": int((cleaned.metadata or {}).get("content_chars") or 0),
            "content_selector": str((cleaned.metadata or {}).get("content_selector") or ""),
            "networkidle": networkidle_status,
        },
    )
    metadata: Dict = dict(cleaned.metadata)
    metadata["render_elapsed_s"] = round(time.time() - started, 3)
    return GenericWebResponse(
        url=cleaned.url,
        final_url=cleaned.final_url,
        title=cleaned.title,
        content=cleaned.content,
        content_format=cleaned.content_format,
        collector=cleaned.collector,
        fetched_at=cleaned.fetched_at,
        elapsed_s=round(time.time() - started, 3),
        metadata=metadata,
        error=cleaned.error,
    )


def _valid_http_url(url: str) -> bool:
    parsed = urlparse(url or "")
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _find_chrome_exe() -> str:
    selection = resolve_managed_chrome()
    return selection.path if selection else ""
