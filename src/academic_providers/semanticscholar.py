"""Semantic Scholar metadata provider."""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List

import httpx

from .models import AcademicSearchRequest, AcademicWork, normalize_doi


class SemanticScholarError(Exception):
    pass


class SemanticScholarRateLimitError(SemanticScholarError):
    pass


class SemanticScholarProvider:
    name = "semanticscholar"

    def __init__(self, endpoint: str = "https://api.semanticscholar.org/graph/v1/paper/search", timeout: float = 15.0, retries: int = 2) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")

    def status(self) -> Dict[str, Any]:
        return {"configured": True, "available": True, "endpoint": self.endpoint, "requires_api_key": False, "api_key_configured": bool(self.api_key)}

    def search(self, request: AcademicSearchRequest) -> List[AcademicWork]:
        limit = max(1, min(int(request.limit or 5), 20))
        params = {
            "query": request.query,
            "limit": limit,
            "fields": "title,authors,year,externalIds,url,abstract,venue,isOpenAccess,openAccessPdf",
        }
        headers = {"User-Agent": "KnowledgeRadar/academic-pilot"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        with httpx.Client(timeout=self.timeout, headers=headers) as client:
            for attempt in range(self.retries + 1):
                try:
                    response = client.get(self.endpoint, params=params)
                    if response.status_code == 429:
                        if attempt < self.retries:
                            retry_after = _retry_after_seconds(response)
                            time.sleep(min(12.0, retry_after or 2.0 * (2**attempt)))
                            continue
                        raise SemanticScholarRateLimitError("Semantic Scholar API rate limited (HTTP 429)")
                    response.raise_for_status()
                    data = response.json()
                    break
                except SemanticScholarRateLimitError:
                    raise
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if attempt < self.retries:
                        time.sleep(min(8.0, 1.0 * (2**attempt)))
                        continue
                    raise SemanticScholarError(str(exc)) from exc
                except Exception as exc:
                    raise SemanticScholarError(str(exc)) from exc
        return [self._work_from_semantic_scholar(item) for item in data.get("data") or [] if isinstance(item, dict)]

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
            license="",
            source_database=self.name,
            access_mode="public_api",
            full_text_status="oa_available" if item.get("isOpenAccess") or open_pdf.get("url") else "metadata_only",
            provider_confidence=0.84,
            verification_status="doi_matched" if doi else "unverified",
            license_scope="open" if item.get("isOpenAccess") or open_pdf.get("url") else "unknown",
            raw={"externalIds": external, "paperId": item.get("paperId")},
        )


def _retry_after_seconds(response: httpx.Response) -> float:
    try:
        return max(0.0, float(response.headers.get("retry-after") or 0))
    except Exception:
        return 0.0
