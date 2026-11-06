"""Semantic Scholar metadata provider."""

from __future__ import annotations

import os
import time
import sqlite3
from contextlib import closing
from pathlib import Path
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List

import httpx

from .models import AcademicSearchRequest, AcademicWork, normalize_doi
from .chinese_open_access import OpenAccessResults


class SemanticScholarError(Exception):
    pass


class SemanticScholarRateLimitError(SemanticScholarError):
    pass


class SemanticScholarAuthError(SemanticScholarError):
    pass


class SemanticScholarProvider:
    name = "semanticscholar"

    def __init__(self, endpoint: str = "https://api.semanticscholar.org/graph/v1/paper/search", timeout: float = 15.0, retries: int = 2) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()

    @property
    def auto_available(self) -> bool:
        return bool(self.api_key) and self._next_allowed() <= time.time()

    def _connect(self):
        from runtime.paths import runtime_state_dir
        path = Path(os.environ.get("KR_SEMANTIC_RATE_DB_PATH") or runtime_state_dir() / "academic_semantic_rate.sqlite3")
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=3)
        conn.execute("CREATE TABLE IF NOT EXISTS throttle (id INTEGER PRIMARY KEY, next_at REAL NOT NULL)")
        return conn

    def _next_allowed(self):
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT next_at FROM throttle WHERE id=1").fetchone()
            return row[0] if row else 0.0

    def _reserve(self):
        # 所有本机 MCP 进程共享同一节流位；不在数据库事务里等待。
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT next_at FROM throttle WHERE id=1").fetchone()
            now = time.time()
            if row and row[0] > now:
                raise SemanticScholarRateLimitError(f"Semantic Scholar cooldown; retry after {row[0] - now:.1f} seconds")
            conn.execute("INSERT OR REPLACE INTO throttle VALUES (1, ?)", (now + 1.1,))

    def _cooldown(self, seconds):
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT INTO throttle VALUES(1, ?) ON CONFLICT(id) DO UPDATE SET next_at=MAX(next_at, excluded.next_at)",
                         (time.time() + seconds,))

    def status(self) -> Dict[str, Any]:
        remaining = max(0.0, self._next_allowed() - time.time())
        return {"configured": True, "available": remaining <= 0, "endpoint": self.endpoint,
                "requires_api_key": False, "api_key_configured": bool(self.api_key),
                "auto_enabled": self.auto_available, "cooldown_remaining_s": round(remaining, 1),
                "rate_policy": "cross_process_1_request_per_1.1_seconds_with_server_cooldown",
                "manual_action": "自动补查需要 SEMANTIC_SCHOLAR_API_KEY；官网申请后核对账号配额。匿名显式调用可用性由公共限流决定。"}

    def search(self, request: AcademicSearchRequest) -> List[AcademicWork]:
        limit = max(1, min(int(request.limit or 5), 20))
        params = {
            "query": request.query,
            "limit": limit,
            "fields": "title,authors,year,externalIds,url,abstract,venue,isOpenAccess,openAccessPdf,publicationTypes",
        }
        headers = {"User-Agent": "KnowledgeRadar/academic-pilot"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        from runtime.proxy_config import get_httpx_proxy
        with httpx.Client(timeout=self.timeout, headers=headers, proxy=get_httpx_proxy(), trust_env=False) as client:
            for attempt in range(self.retries + 1):
                try:
                    self._reserve()
                    response = client.get(self.endpoint, params=params)
                    if response.status_code == 429:
                        retry_after = _retry_after_seconds(response) or 2.0 * (2**attempt)
                        self._cooldown(retry_after)
                        if attempt < self.retries:
                            if retry_after > 12.0:
                                raise SemanticScholarRateLimitError(f"HTTP 429; retry after {retry_after:g} seconds; no early retry")
                            time.sleep(max(retry_after, self._next_allowed() - time.time(), 1.1))
                            continue
                        raise SemanticScholarRateLimitError("Semantic Scholar API rate limited (HTTP 429)")
                    if response.status_code in {401, 403}:
                        raise SemanticScholarAuthError(f"Semantic Scholar HTTP {response.status_code}; 核对 Key 与接口权限")
                    response.raise_for_status()
                    data = response.json()
                    break
                except (SemanticScholarRateLimitError, SemanticScholarAuthError):
                    raise
                except ValueError:
                    return OpenAccessResults([], [{"type": "search_response_unparsed", "retryable": False,
                        "message": "Semantic Scholar 响应不是有效 JSON"}])
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if attempt < self.retries:
                        time.sleep(min(8.0, 1.1 * (2**attempt)))
                        continue
                    raise SemanticScholarError(str(exc)) from exc
                except Exception as exc:
                    raise SemanticScholarError(str(exc)) from exc
        records = data.get("data") if isinstance(data, dict) else None
        if not isinstance(records, list):
            return OpenAccessResults([], [{"type": "search_response_unparsed", "retryable": False,
                "message": "Semantic Scholar 响应缺少论文列表，不能判为无匹配"}])
        works = [self._work_from_semantic_scholar(item) for item in records if isinstance(item, dict)]
        works = [item for item in works if item.title and item.url]
        if records and not works:
            return OpenAccessResults([], [{"type": "search_response_unparsed", "retryable": False,
                "message": "Semantic Scholar 返回记录，但缺少有效题名或链接"}])
        return OpenAccessResults(works[:limit], [] if works else [{"type": "no_match", "retryable": False,
            "message": "已执行论文检索，接口返回空列表"}])

    def _work_from_semantic_scholar(self, item: Dict[str, Any]) -> AcademicWork:
        external = item.get("externalIds") if isinstance(item.get("externalIds"), dict) else {}
        doi = normalize_doi(str(external.get("DOI") or ""))
        authors = [str(author.get("name") or "") for author in item.get("authors") or [] if isinstance(author, dict) and author.get("name")]
        open_pdf = item.get("openAccessPdf") if isinstance(item.get("openAccessPdf"), dict) else {}
        return AcademicWork(
            title=str(item.get("title") or ""),
            url=str(item.get("url") or open_pdf.get("url") or (f"https://doi.org/{doi}" if doi else "")),
            authors=authors[:12],
            year=item.get("year"),
            doi=doi,
            abstract=str(item.get("abstract") or "")[:2000],
            source=str(item.get("venue") or "Semantic Scholar"),
            oa_status="open" if item.get("isOpenAccess") else "unknown",
            license=str(open_pdf.get('license') or ''),
            source_database=self.name,
            document_type=str(next(iter(item.get('publicationTypes') or []), 'unknown')),
            access_mode="public_api",
            full_text_status="oa_available" if item.get("isOpenAccess") or open_pdf.get("url") else "metadata_only",
            provider_confidence=0.84,
            verification_status="doi_present" if doi else "unverified",
            license_scope="license_declared" if open_pdf.get("license") else "unknown",
            raw={"externalIds": external, "paperId": item.get("paperId"),
                 "open_access_pdf_url": str(open_pdf.get("url") or ""), "fulltext_verified": False},
        )


def _retry_after_seconds(response: httpx.Response) -> float:
    try:
        return max(0.0, float(response.headers.get("retry-after") or 0))
    except Exception:
        try:
            return max(0.0, parsedate_to_datetime(response.headers.get("retry-after", "")).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return 0.0
