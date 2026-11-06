"""OpenAlex metadata provider."""

from __future__ import annotations

import os
from typing import Any, Dict, List

import httpx

from .models import AcademicSearchRequest, AcademicWork, normalize_doi


class OpenAlexError(Exception):
    pass


class OpenAlexProvider:
    name = "openalex"

    def __init__(self, endpoint: str = "https://api.openalex.org/works", timeout: float = 15.0) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self.api_key = os.environ.get("OPENALEX_API_KEY", "").strip()

    def status(self) -> Dict[str, Any]:
        return {
            "configured": True,
            "available": True,
            "endpoint": self.endpoint,
            "requires_api_key": False,
            "api_key_configured": bool(self.api_key),
            "quota": {"free_credit_per_day_usd": 1.0 if self.api_key else 0.10, "source": "OpenAlex API key policy"},
            "degraded_reason": "" if self.api_key else "OPENALEX_API_KEY not configured; unauthenticated compatibility mode may still work but is not the productized stable path",
        }

    def search(self, request: AcademicSearchRequest) -> List[AcademicWork]:
        limit = max(1, min(int(request.limit or 5), 20))
        if int(request.options.get('source_offset',0)) % limit:
            raise ValueError('source_offset_must_be_multiple_of_limit; use source_cursor or keep limit unchanged')
        params = {
            "search": request.query,
            "per-page": limit,
            "select": "id,doi,display_name,publication_year,authorships,primary_location,open_access,abstract_inverted_index,type,locations,is_retracted",
        }
        params['page'] = max(1, int(request.options.get('source_offset', 0)) // limit + 1)
        cursor = request.options.get('source_cursor')
        if cursor:
            params.pop('page', None)
            params['cursor'] = cursor
        mailto = request.options.get("mailto")
        if mailto:
            params["mailto"] = mailto
        if self.api_key:
            params["api_key"] = self.api_key
        try:
            with httpx.Client(timeout=self.timeout, headers={"User-Agent": "KnowledgeRadar/academic-pilot"}) as client:
                from runtime.paid_execution import paid_send
                import json
                safe_params = {k: v for k, v in params.items() if k != "api_key"}
                with paid_send("openalex", safe_params, bound=len(json.dumps(safe_params).encode("utf-8")), endpoint=self.endpoint, authenticated=bool(self.api_key)):
                    response = client.get(self.endpoint, params=params)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            raise OpenAlexError(str(exc)) from exc
        from .chinese_open_access import OpenAccessResults
        rows = OpenAccessResults([self._work_from_openalex(item) for item in data.get('results') or [] if isinstance(item, dict)], [])
        following = (data.get('meta') or {}).get('next_cursor')
        rows.continuation = {'source_cursor':following} if following else (
            {'source_offset':int(request.options.get('source_offset',0))+len(rows)} if len(rows)==limit else None)
        return rows

    def _work_from_openalex(self, item: Dict[str, Any]) -> AcademicWork:
        primary_location = item.get("primary_location") if isinstance(item.get("primary_location"), dict) else {}
        source = primary_location.get("source") if isinstance(primary_location.get("source"), dict) else {}
        open_access = item.get("open_access") if isinstance(item.get("open_access"), dict) else {}
        authors = []
        for authorship in item.get("authorships") or []:
            if not isinstance(authorship, dict):
                continue
            author = authorship.get("author") if isinstance(authorship.get("author"), dict) else {}
            name = str(author.get("display_name") or "").strip()
            if name:
                authors.append(name)
        doi = normalize_doi(str(item.get("doi") or ""))
        landing_url = str(primary_location.get("landing_page_url") or primary_location.get("pdf_url") or item.get("id") or doi)
        return AcademicWork(
            title=str(item.get("display_name") or ""),
            url=landing_url,
            authors=authors[:12],
            year=item.get("publication_year"),
            doi=doi,
            abstract=_abstract_from_inverted_index(item.get("abstract_inverted_index")),
            source=str(source.get("display_name") or "OpenAlex"),
            oa_status=str(open_access.get("oa_status") or ""),
            license=str(primary_location.get("license") or ""),
            source_database=self.name,
            document_type=str(item.get("type") or "unknown"),
            version=str(primary_location.get("version") or "unknown"),
            access_mode="public_api",
            full_text_status="oa_available" if open_access.get("is_oa") else "metadata_only",
            provider_confidence=0.86,
            verification_status="doi_present" if doi else "unverified",
            license_scope="license_declared" if primary_location.get("license") else "unknown",
            raw={
                "openalex_id": item.get("id"),
                "primary_location": primary_location,
                "open_access": open_access,
                "locations": item.get("locations") or [],
                "is_retracted":item.get("is_retracted", "unknown"),
            },
        )


def _abstract_from_inverted_index(index: Any) -> str:
    if not isinstance(index, dict):
        return ""
    positions: Dict[int, str] = {}
    for word, offsets in index.items():
        if not isinstance(offsets, list):
            continue
        for offset in offsets:
            try:
                positions[int(offset)] = str(word)
            except Exception:
                continue
    if not positions:
        return ""
    return " ".join(positions[i] for i in sorted(positions))[:2000]
