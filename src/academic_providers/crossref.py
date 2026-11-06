"""Crossref metadata provider."""

from __future__ import annotations

from typing import Any, Dict, List
from urllib.parse import quote
from dataclasses import replace

import httpx

from .models import AcademicSearchRequest, AcademicWork, normalize_doi
from .relevance import exact_doi_query
from .chinese_open_access import OpenAccessResults


class CrossrefError(Exception):
    pass


class CrossrefProvider:
    name = "crossref"

    def __init__(self, endpoint: str = "https://api.crossref.org/works", timeout: float = 15.0) -> None:
        self.endpoint = endpoint
        self.timeout = timeout

    def status(self) -> Dict[str, Any]:
        return {"configured": True, "available": True, "endpoint": self.endpoint, "requires_api_key": False}

    def search(self, request: AcademicSearchRequest) -> List[AcademicWork]:
        limit = max(1, min(int(request.limit or 5), 20))
        doi = exact_doi_query(request.query)
        params = {} if doi else {"query": request.query, "rows": limit, "offset":int(request.options.get("source_offset",0))}
        mailto = request.options.get("mailto")
        if mailto:
            params["mailto"] = mailto
        try:
            with httpx.Client(timeout=self.timeout, headers={"User-Agent": "KnowledgeRadar/academic-pilot"}) as client:
                response = client.get(self.endpoint + ("/" + quote(doi, safe="/") if doi else ""), params=params)
                if doi and response.status_code == 404:
                    # DOI注册机构不同不等于DOI不存在；仅精确读取公开DataCite元数据。
                    from runtime.execution_context import check_execution
                    check_execution()
                    alternative = client.get('https://api.datacite.org/dois/' + quote(doi, safe='/'))
                    if alternative.status_code == 200:
                        attributes = (alternative.json().get('data') or {}).get('attributes') or {}
                        if normalize_doi(attributes.get('doi')) != doi:
                            return OpenAccessResults([], [{'type':'identity_mismatch', 'retryable':False}])
                        return [_datacite_work(attributes)]
                    if alternative.status_code != 404:
                        alternative.raise_for_status()
                    return OpenAccessResults([], [{"type": "identifier_not_found", "http_status": 404, "retryable": False}])
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            from runtime.execution_context import ExecutionStopped
            if isinstance(exc, ExecutionStopped):
                raise
            raise CrossrefError(type(exc).__name__) from exc
        if doi:
            work = self._work_from_crossref(data.get("message") or {})
            if normalize_doi(work.doi) != doi:
                return OpenAccessResults([], [{"type": "identity_mismatch", "retryable": False}])
            return [replace(work, verification_status="doi_matched", raw={**work.raw, "identity_match": True})]
        items = ((data.get("message") or {}).get("items") or [])
        rows = OpenAccessResults([self._work_from_crossref(item) for item in items if isinstance(item, dict)], [])
        rows.continuation = {'source_offset':int(request.options.get('source_offset',0))+len(rows)} if len(rows)==limit else None
        return rows

    def _work_from_crossref(self, item: Dict[str, Any]) -> AcademicWork:
        authors = []
        for author in item.get("author") or []:
            if not isinstance(author, dict):
                continue
            name = " ".join(part for part in [author.get("given"), author.get("family")] if part).strip()
            if name:
                authors.append(name)
        title = ""
        if isinstance(item.get("title"), list) and item["title"]:
            title = str(item["title"][0] or "")
        year = _first_year(item)
        doi = normalize_doi(str(item.get("DOI") or ""))
        return AcademicWork(
            title=title,
            url=str(item.get("URL") or (f"https://doi.org/{doi}" if doi else "")),
            authors=authors[:12],
            year=year,
            doi=doi,
            abstract=str(item.get("abstract") or "")[:2000],
            source=str((item.get("container-title") or ["Crossref"])[0] or "Crossref"),
            oa_status="unknown",
            license=_first_license(item),
            source_database=self.name,
            access_mode="public_api",
            full_text_status="metadata_only",
            provider_confidence=0.82,
            verification_status="doi_present" if doi else "unverified",
            license_scope="license_declared" if _first_license(item) else "unknown",
            document_type=str(item.get("type") or "unknown"),
            raw={"type": item.get("type"), "publisher": item.get("publisher"), "score": item.get("score"), "relation":item.get("relation") or {}, "update_to":item.get("update-to") or [], "license_records":item.get("license") or [], "links":item.get("link") or []},
        )


def _datacite_work(item):
    titles = item.get('titles') or []
    rights = item.get('rightsList') or []
    return AcademicWork(title=str(titles[0].get('title') or '') if titles else '',
        url=str(item.get('url') or 'https://doi.org/'+item['doi']), doi=normalize_doi(item['doi']),
        authors=[str(author.get('name') or '') for author in item.get('creators') or []],
        year=item.get('publicationYear'), source='DataCite', source_database='crossref',
        document_type=str((item.get('types') or {}).get('resourceTypeGeneral') or 'unknown'),
        version=str(item.get('version') or 'unknown'), verification_status='doi_matched',
        license=str(rights[0].get('rightsUri') or rights[0].get('rights') or '') if rights else '',
        raw={'metadata_agency':'datacite', 'related_identifiers':item.get('relatedIdentifiers') or [],
             'identity_match':True})


def _first_year(item: Dict[str, Any]) -> int | None:
    for key in ("published-print", "published-online", "issued", "created"):
        parts = ((item.get(key) or {}).get("date-parts") or [])
        if parts and parts[0]:
            try:
                return int(parts[0][0])
            except Exception:
                continue
    return None


def _first_license(item: Dict[str, Any]) -> str:
    licenses = item.get("license") or []
    if licenses and isinstance(licenses[0], dict):
        return str(licenses[0].get("URL") or "")
    return ""
