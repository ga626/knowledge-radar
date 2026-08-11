"""Unpaywall DOI lookup provider."""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List

import httpx

from .models import AcademicSearchRequest, AcademicWork, normalize_doi


class UnpaywallError(Exception):
    pass


class UnpaywallAuthError(UnpaywallError):
    pass


class UnpaywallProvider:
    name = "unpaywall"

    def __init__(self, endpoint: str = "https://api.unpaywall.org/v2", timeout: float = 15.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.email = os.environ.get("KR_UNPAYWALL_EMAIL", "").strip() or os.environ.get("UNPAYWALL_EMAIL", "").strip()

    def status(self) -> Dict[str, Any]:
        configured = bool(self.email)
        return {
            "configured": configured,
            "available": configured,
            "endpoint": self.endpoint,
            "requires_api_key": False,
            "requires_email": True,
            "auto_enabled": False,
            "access_mode": "public_doi_api",
            "status": "available" if configured else "degraded",
            "degraded_reason": "" if configured else "Set KR_UNPAYWALL_EMAIL or pass options.mailto for explicit DOI lookup.",
        }

    def search(self, request: AcademicSearchRequest) -> List[AcademicWork]:
        doi = _extract_doi(request.query)
        if not doi:
            return []
        email = str(request.options.get("mailto") or self.email).strip()
        if not email:
            raise UnpaywallAuthError("Unpaywall requires an email address via options.mailto, KR_UNPAYWALL_EMAIL, or UNPAYWALL_EMAIL.")
        try:
            with httpx.Client(timeout=self.timeout, headers={"User-Agent": "KnowledgeRadar/academic-unpaywall"}) as client:
                response = client.get(f"{self.endpoint}/{doi}", params={"email": email})
                if response.status_code == 404:
                    return []
                response.raise_for_status()
                data = response.json()
        except UnpaywallError:
            raise
        except Exception as exc:
            raise UnpaywallError(str(exc)) from exc
        if not isinstance(data, dict):
            return []
        work = self._work_from_unpaywall(data)
        return [work] if work.title or work.url else []

    def _work_from_unpaywall(self, item: Dict[str, Any]) -> AcademicWork:
        location = _best_location(item)
        pdf_url = str(location.get("url_for_pdf") or "").strip()
        landing_url = str(location.get("url") or item.get("doi_url") or "").strip()
        is_oa = bool(item.get("is_oa"))
        authors = [str(author.get("given", "") + " " + author.get("family", "")).strip() for author in item.get("z_authors") or [] if isinstance(author, dict)]
        authors = [author for author in authors if author]
        doi = normalize_doi(str(item.get("doi") or ""))
        return AcademicWork(
            title=str(item.get("title") or ""),
            url=pdf_url or landing_url or (f"https://doi.org/{doi}" if doi else ""),
            authors=authors[:12],
            year=_safe_int(item.get("year")),
            doi=doi,
            source=str(item.get("journal_name") or "Unpaywall"),
            oa_status=str(item.get("oa_status") or ""),
            license=str(location.get("license") or ""),
            source_database=self.name,
            access_mode="public_doi_api",
            full_text_status="pdf_text_extractable" if pdf_url else ("oa_available" if is_oa and landing_url else "metadata_only"),
            provider_confidence=0.82 if is_oa else 0.72,
            verification_status="doi_matched" if doi else "unverified",
            license_scope="open" if is_oa else "unknown",
            raw={
                "genre": item.get("genre"),
                "host_type": location.get("host_type"),
                "version": location.get("version"),
                "url_for_pdf": pdf_url,
                "url": landing_url,
            },
        )


def _extract_doi(value: str) -> str:
    match = re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", str(value or ""), flags=re.IGNORECASE)
    if not match:
        return ""
    return normalize_doi(match.group(0).rstrip(".,;:)"))


def _best_location(item: Dict[str, Any]) -> Dict[str, Any]:
    best = item.get("best_oa_location")
    if isinstance(best, dict):
        return best
    locations = item.get("oa_locations")
    if isinstance(locations, list):
        for location in locations:
            if isinstance(location, dict):
                return location
    return {}


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None
