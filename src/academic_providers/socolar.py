"""Socolar OA discovery provider."""

from __future__ import annotations

import html
import json
import re
import time
from typing import Any, Dict, List
from urllib.parse import quote
from urllib.request import urlopen

import websocket

from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig
from .models import AcademicSearchRequest, AcademicWork, normalize_doi
from runtime.chrome_manager import _chrome_debug_port, _ensure_chrome_debugging, finish_chrome_automation


SOCOLAR_SEARCH_ENDPOINT = "/api/cepiec-elasticsearch/manager/search/query"
SOCOLAR_DETAIL_ENDPOINT = "/api/socolar-article/scholar/articleinfo/normal/detail"
SOCOLAR_DETAIL_URL = "https://www.socolar.com/articleDetails?articleId={article_id}"
SOCOLAR_MIN_USEFUL_ABSTRACT_CHARS = 80


class SocolarProvider(ChineseOpenAccessProvider):
    name = "socolar"
    config = OpenAccessPlatformConfig(
        name="socolar",
        display_name="Socolar",
        homepage="https://www.socolar.com/",
        status="available",
        available=True,
        auto_enabled=False,
        access_mode="logged_abstract_discovery",
        full_text_access="structured_abstract_with_external_fulltext_link",
        coverage="OA resource discovery, article-level metadata, structured abstracts, and external full-text links",
        stable=True,
        degraded_reason="",
        failure_category="",
        requires_login=True,
        login_url="https://www.socolar.com/",
        manual_action="Use managed browser login if the token-backed detail API is missing; do not treat external PDF/full-text links as stable by default.",
    )
    timeout_s = 18.0

    def status(self) -> dict:
        status = super().status()
        status.update(
            {
                "network": "managed_browser_json_api",
                "notes": (
                    "Socolar is admitted as a logged abstract/discovery provider. "
                    "Its own detail API is stable for metadata and abstracts; full text remains an external landing URL."
                ),
                "requires_login": True,
                "text_extraction": {
                    "method": "socolar_detail_api_abstract",
                    "minimum_chars": SOCOLAR_MIN_USEFUL_ABSTRACT_CHARS,
                    "default_fulltext_behavior": "do_not_follow_external_url",
                },
                "fulltext_limitation": {
                    "status": "EXPECTED_DEGRADED",
                    "reason": "Socolar aggregates OA external landing URLs; /openAccess returned null in probes and no universal PDF route is guaranteed.",
                },
            }
        )
        return status

    def search(self, request: AcademicSearchRequest) -> List[AcademicWork]:
        query = str(request.query or "").strip()
        if not query:
            return []
        limit = max(1, min(int(request.limit or 5), 20))
        data = self._search_with_managed_browser(query=query, limit=limit)
        records = data.get("details") if isinstance(data, dict) else []
        if not isinstance(records, list):
            return []
        works = [self._work_from_detail(record, request) for record in records if isinstance(record, dict)]
        useful = [work for work in works if work.abstract or work.raw.get("external_url")]
        return useful[:limit]

    def _search_with_managed_browser(self, *, query: str, limit: int) -> Dict[str, Any]:
        if not _ensure_chrome_debugging("socolar", visible=False, detach=False):
            return {"status": "cdp_unavailable", "details": []}
        page = _CdpPage(_target_ws_url("socolar"))
        try:
            page.call("Runtime.enable")
            expression = _socolar_search_expression(query=query, limit=limit)
            value = page.eval(expression, timeout=self.timeout_s + 25)
            return value if isinstance(value, dict) else {"status": "invalid_response", "details": []}
        finally:
            try:
                page.close()
            finally:
                finish_chrome_automation("socolar", reason="socolar_search")

    def search_urls(self, request: AcademicSearchRequest):
        query = quote(str(request.query or "").strip())
        if query:
            return (f"https://www.socolar.com/academicSpaceSearch?korder=title&kw={query}", self.config.homepage)
        return (self.config.homepage,)

    def _work_from_detail(self, item: Dict[str, Any], request: AcademicSearchRequest) -> AcademicWork:
        article_id = str(item.get("id") or item.get("articleId") or item.get("objectId") or item.get("uuid") or "").strip()
        title = _clean_text(item.get("title") or item.get("name") or "")
        external_url = str(item.get("fullTextUrl") or item.get("fulltextUrl") or item.get("url") or "").strip()
        article_url = SOCOLAR_DETAIL_URL.format(article_id=quote(article_id)) if article_id else external_url or self.config.homepage
        abstract = _clean_text(item.get("abstracts") or item.get("abstract") or "")
        is_oa = _boolish(item.get("isOa") or item.get("isOA") or item.get("oa"))
        source = _clean_text(item.get("journal") or item.get("source") or item.get("publisher") or "Socolar")
        raw = {
            "id": article_id,
            "query": request.query,
            "external_url": external_url,
            "is_oa": is_oa,
            "abstract_chars": len(abstract),
            "external_fulltext_default": "not_followed",
        }
        return AcademicWork(
            title=title or "Socolar article",
            url=article_url,
            authors=_authors(item),
            year=_year(item),
            doi=normalize_doi(str(item.get("doi") or item.get("articleDoi") or "")),
            abstract=abstract,
            source=source,
            oa_status="open" if is_oa else "",
            source_database="socolar",
            access_mode="logged_abstract_discovery",
            full_text_status="abstract_with_external_landing_url" if external_url else "metadata_only",
            provider_confidence=0.82 if abstract else 0.68,
            verification_status="socolar_detail_abstract_confirmed" if abstract else "socolar_detail_metadata_confirmed",
            license_scope="open_or_external_platform_terms",
            degraded_reason=(
                "" if abstract else "Socolar detail record has no abstract; external landing URL was not followed by default."
            ),
            raw=raw,
        )


class _CdpPage:
    def __init__(self, ws_url: str) -> None:
        self.ws = websocket.create_connection(ws_url, timeout=20, suppress_origin=True)
        self.seq = 1

    def close(self) -> None:
        self.ws.close()

    def call(self, method: str, params: Dict[str, Any] | None = None, *, timeout: float = 45) -> Dict[str, Any]:
        msg_id = self.seq
        self.seq += 1
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = json.loads(self.ws.recv())
            if data.get("id") == msg_id:
                if data.get("error"):
                    raise RuntimeError(json.dumps(data["error"], ensure_ascii=False))
                return data.get("result") or {}
        raise TimeoutError(f"CDP call timed out: {method}")

    def eval(self, expression: str, *, timeout: float = 60) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "timeout": int(timeout * 1000),
            },
            timeout=timeout + 5,
        )
        return (result.get("result") or {}).get("value")


def _target_ws_url(platform: str) -> str:
    port = _chrome_debug_port(platform)
    with urlopen(f"http://127.0.0.1:{port}/json", timeout=8) as response:
        tabs = json.loads(response.read().decode("utf-8"))
    pages = [tab for tab in tabs if tab.get("type") == "page" and tab.get("webSocketDebuggerUrl")]
    for tab in pages:
        if "socolar.com" in str(tab.get("url") or ""):
            return str(tab["webSocketDebuggerUrl"])
    if pages:
        return str(pages[0]["webSocketDebuggerUrl"])
    raise RuntimeError(f"No page target on CDP port {port}")


def _socolar_search_expression(*, query: str, limit: int) -> str:
    query_json = json.dumps(query, ensure_ascii=False)
    limit_int = max(1, min(int(limit or 5), 20))
    return rf"""
new Promise(async (resolve) => {{
  const query = {query_json};
  const limit = {limit_int};
  const token = localStorage.getItem('token') || sessionStorage.getItem('token') || '';
  const headers = {{
    'Content-Type': 'application/json',
    'Cepiec-Auth': token,
    'Tenant-Id': '000000'
  }};
  const out = {{status: 'ok', has_token: Boolean(token), record_count: 0, details: []}};
  if (!token) {{
    out.status = 'needs_interaction';
    out.reason = 'missing_socolar_token';
    resolve(out);
    return;
  }}
  try {{
    const searchResponse = await fetch('{SOCOLAR_SEARCH_ENDPOINT}', {{
      method: 'POST',
      credentials: 'include',
      headers,
      body: JSON.stringify({{
        current: 1,
        size: Math.max(limit * 2, limit),
        searchTypes: 'article',
        conditionList: [{{field: 'title', mateType: 'like', relationType: 'and', value: query}}],
        sortCondition: {{sortField: 'publishDate', sortType: ''}}
      }})
    }});
    out.search_status = searchResponse.status;
    const searchJson = await searchResponse.json();
    const records = (searchJson && (searchJson.records || searchJson.data?.records)) || [];
    out.record_count = records.length;
    const selected = records.slice(0, limit);
    for (const record of selected) {{
      const articleId = record.id || record.articleId || record.objectId || record.uuid || '';
      let detail = record;
      if (articleId) {{
        try {{
          const detailResponse = await fetch('{SOCOLAR_DETAIL_ENDPOINT}?articleId=' + encodeURIComponent(articleId), {{
            method: 'GET',
            credentials: 'include',
            headers
          }});
          const detailJson = await detailResponse.json();
          if (detailJson && detailJson.data) {{
            detail = Object.assign({{}}, record, detailJson.data);
          }}
        }} catch (e) {{
          detail = Object.assign({{}}, record, {{detail_error: String(e)}});
        }}
      }}
      out.details.push(detail);
    }}
  }} catch (e) {{
    out.status = 'error';
    out.error = String(e);
  }}
  resolve(out);
}})
"""


def _clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _authors(item: Dict[str, Any]) -> List[str]:
    raw = item.get("authors") or item.get("author") or item.get("authorList") or []
    if isinstance(raw, str):
        return [_clean_text(part) for part in re.split(r"[;,，；]", raw) if _clean_text(part)][:20]
    authors: List[str] = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                name = _clean_text(entry.get("name") or entry.get("authorName") or entry.get("realName") or "")
            else:
                name = _clean_text(entry)
            if name:
                authors.append(name)
    return authors[:20]


def _year(item: Dict[str, Any]) -> int | None:
    for key in ("year", "publishYear", "date", "publishDate", "onlineDate"):
        match = re.search(r"(19|20)\d{2}", str(item.get(key) or ""))
        if match:
            return int(match.group(0))
    return None


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "open", "oa"}
