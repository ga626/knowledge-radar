"""Crossref metadata provider."""

from __future__ import annotations

from typing import Any, Dict, List

import httpx

from .models import AcademicSearchRequest, AcademicWork, normalize_doi


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
        params = {"query": request.query, "rows": limit}
        mailto = request.options.get("mailto")
        if mailto:
            params["mailto"] = mailto
        try:
            with httpx.Client(timeout=self.timeout, headers={"User-Agent": "KnowledgeRadar/academic-pilot"}) as client:
                response = client.get(self.endpoint, params=params)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            raise CrossrefError(str(exc)) from exc
        items = ((data.get("message") or {}).get("items") or [])
        return [self._work_from_crossref(item) for item in items if isinstance(item, dict)]

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
            verification_status="doi_matched" if doi else "unverified",
            license_scope="open" if _first_license(item) else "unknown",
            raw={"type": item.get("type"), "publisher": item.get("publisher"), "score": item.get("score")},
        )


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
