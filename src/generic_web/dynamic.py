"""Dynamic web page extraction using Playwright as a high-cost fallback."""

from __future__ import annotations

import asyncio
import time
from typing import Dict
from urllib.parse import urlparse

from .collector import collect_rendered_html
from .models import GenericWebRequest, GenericWebResponse, utc_now_iso
from runtime.tool_trace import record_trace_child
from .url_policy import public_addresses, guarded_fetch, URLPolicyError


def collect_dynamic_url(request: GenericWebRequest, *, wait_ms: int = 3000) -> GenericWebResponse:
    try:
        public_addresses(request.url)
    except URLPolicyError as exc:
        return GenericWebResponse(url=request.url, collector="dynamic_playwright", fetched_at=utc_now_iso(),
                                  error={"type": "restricted_url", "message": str(exc), "collector": "dynamic_playwright"})
    from .browser_runtime import run
    from runtime.execution_context import ExecutionStopped
    try:
        return run(_collect_dynamic_url_async(request,wait_ms=wait_ms,started=time.time()), timeout=request.timeout)
    except ExecutionStopped:
        raise
    except Exception as exc:
        return GenericWebResponse(url=request.url,final_url=request.url,collector='dynamic_playwright',fetched_at=utc_now_iso(),
            error={'type':'dynamic_failed','message':type(exc).__name__,'collector':'dynamic_playwright',
                   'next_action':'Inspect acquisition failure; no shared or user browser was closed.'})


async def _collect_dynamic_url_async(request: GenericWebRequest, *, wait_ms: int, started: float) -> GenericWebResponse:

    timeout_ms = max(1000, int(float(request.timeout or 20.0) * 1000))
    deadline = time.monotonic() + timeout_ms / 1000
    resource_failures = []
    resource_bytes, resource_count = 0, 0
    fetch_slots = asyncio.Semaphore(6)
    from .browser_runtime import context as owned_context
    async with owned_context() as (context, browser_reused):
        navigation_redirects = {}
        async def guarded_route(route):
            nonlocal resource_bytes, resource_count
            req = route.request
            # Text extraction does not need large galleries, video or fonts.
            # Loading them kept the anonymous OCR landing page busy until
            # the outer subprocess deadline, even after its text rendered.
            if req.resource_type in {"image", "media", "font"}:
                await route.abort("blockedbyclient")
                return
            declaration, form = None, None
            if req.method == 'POST':
                from urllib.parse import parse_qsl
                declaration = next((d for d in request.readonly_posts if d.endpoint == req.url), None)
                if declaration is None or not req.headers.get('content-type','').lower().startswith('application/x-www-form-urlencoded'):
                    await route.abort('blockedbyclient')
                    return
                raw_body = req.post_data or ''
                if len(raw_body.encode('utf-8')) > declaration.max_body_bytes:
                    await route.abort('blockedbyclient')
                    return
                try:
                    pairs = parse_qsl(raw_body, keep_blank_values=True, max_num_fields=50)
                except ValueError:
                    await route.abort('blockedbyclient')
                    return
                form = dict(pairs)
                if len(form) != len(pairs):
                    await route.abort('blockedbyclient')
                    return
            elif req.method != "GET":
                await route.abort("blockedbyclient")
                return
            resource_count += 1
            if resource_count > 80 or resource_bytes >= 32 * 1024 * 1024:
                resource_failures.append({'type': 'resource_budget_exceeded'})
                await route.abort('blockedbyclient')
                return
            try:
                navigation = req.is_navigation_request() and req.frame == page.main_frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    await route.abort("timedout")
                    return
                for attempt in range(1 if declaration else 2):
                    try:
                        async with fetch_slots:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise httpx.TimeoutException('dynamic resource deadline exceeded')
                            post_options = {'readonly_post':declaration, 'form':form} if declaration else {}
                            fetched = await asyncio.to_thread(guarded_fetch, req.url, timeout=min(12.0, remaining), follow_redirects=not navigation,
                                max_bytes=min(4 * 1024 * 1024, max(1,32 * 1024 * 1024-resource_bytes)), **post_options)
                            resource_bytes += len(fetched.content)
                            if resource_bytes > 32 * 1024 * 1024:
                                raise URLPolicyError('dynamic resource budget exceeded')
                        break
                    except httpx.TransportError:
                        remaining = deadline - time.monotonic()
                        if declaration or attempt or remaining <= 1:
                            raise
                        await asyncio.sleep(0.2)
                if navigation and fetched.status_code in {301, 302, 303, 307, 308}:
                    # Playwright不会再次拦截HTTP重定向的下一跳。以空白成功页截住，
                    # 再由显式goto逐跳导航，避免浏览器自行访问未验证目标。
                    navigation_redirects[req.url] = fetched.headers["location"]
                    await route.fulfill(status=200, headers={"content-type": "text/html"}, body="")
                    return
                await route.fulfill(status=fetched.status_code, headers=dict(fetched.headers), body=fetched.content)
            except (URLPolicyError, httpx.RequestError) as exc:
                from .url_policy import redact_url
                if len(resource_failures) < 15:
                    resource_failures.append({"url": redact_url(req.url), "type": type(exc).__name__})
                await route.abort("blockedbyclient")
        import httpx
        await context.route("**/*", guarded_route)
        if hasattr(context, "route_web_socket"):
            async def close_socket(ws):
                await ws.close()
            await context.route_web_socket("**/*", close_socket)
        await context.add_init_script("window.WebSocket = class { constructor() { throw new Error('URL_POLICY_BLOCKED'); } }; window.RTCPeerConnection = undefined; window.WebTransport = undefined;")
        page = await context.new_page()
        target = request.url
        for _ in range(6):
            response = await page.goto(target, wait_until="domcontentloaded", timeout=timeout_ms)
            redirected = navigation_redirects.pop(target, None)
            if not redirected:
                break
            target = redirected
        else:
            raise URLPolicyError("dynamic navigation redirect limit exceeded")
        networkidle_status = "text_ready"
        try:
            await page.wait_for_function("document.body && document.body.innerText.trim().length >= 1", timeout=max(1, int((deadline-time.monotonic())*1000)))
        except Exception as exc:
            networkidle_status = f"text_not_ready:{type(exc).__name__}"
        if wait_ms > 0:
            await page.wait_for_timeout(min(wait_ms, 1000, max(0, (deadline-time.monotonic())*1000)))
        html = await page.content()
        final_url = page.url
        status_code = response.status if response else 200
    cleaned = collect_rendered_html(
        request,
        html=html,
        final_url=final_url,
        status_code=status_code,
        content_type="text/html",
        render_metadata={
            "navigation_wait_until": "domcontentloaded",
            "networkidle": networkidle_status,
            "additional_wait_ms": min(wait_ms, 1000),
            "html_chars": len(html),
            "readiness": networkidle_status,
            "resource_failures": resource_failures,
            "resource_bytes":resource_bytes, "resource_count":resource_count,
            "browser_reused":browser_reused, "ownership":"owned", "identity":"anonymous_per_request",
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
    try:
        public_addresses(url, resolve=False)
        return True
    except URLPolicyError:
        return False
