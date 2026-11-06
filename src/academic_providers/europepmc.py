"""Europe PMC metadata and open-access full-text link provider."""

from __future__ import annotations

import re
from typing import Any, Dict, List

import httpx

from .models import AcademicSearchRequest, AcademicWork, normalize_doi


class EuropePmcError(Exception):
    pass


class EuropePmcProvider:
    name = "europepmc"

    def __init__(self, endpoint: str = "https://www.ebi.ac.uk/europepmc/webservices/rest/search", timeout: float = 15.0) -> None:
        self.endpoint = endpoint
        self.timeout = timeout

    def status(self) -> Dict[str, Any]:
        return {
            "configured": True,
            "available": True,
            "endpoint": self.endpoint,
            "requires_api_key": False,
            "auto_enabled": True,
            "access_mode": "public_api",
        }

    def search(self, request: AcademicSearchRequest) -> List[AcademicWork]:
        limit = max(1, min(int(request.limit or 5), 20))
        params = {
            "query": request.query,
            "format": "json",
            "pageSize": limit,
            "resultType": "core",
        }
        try:
            with httpx.Client(timeout=self.timeout, headers={"User-Agent": "KnowledgeRadar/academic-europepmc"}) as client:
                response = client.get(self.endpoint, params=params)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            from runtime.execution_context import ExecutionStopped
            if isinstance(exc, ExecutionStopped):
                raise
            raise EuropePmcError(str(exc)) from exc
        items = ((data.get("resultList") or {}).get("result") or [])
        return [self._work_from_europepmc(item) for item in items if isinstance(item, dict)]

    def _work_from_europepmc(self, item: Dict[str, Any]) -> AcademicWork:
        full_text_urls = _full_text_urls(item)
        pdf_url = _first_url(full_text_urls, style="pdf", open_only=True)
        html_url = _first_url(full_text_urls, style="html", open_only=True)
        doi = normalize_doi(str(item.get("doi") or ""))
        pmcid = str(item.get("pmcid") or "").strip()
        pmid = str(item.get("pmid") or "").strip()
        is_oa = str(item.get("isOpenAccess") or "").upper() == "Y"
        # 官方 REST 提供 OA PMC 的 JATS 正文；网页落地页可能受 challenge 限制。
        # PMCID 本身不代表开放获取，也不能作为未校验的 URL 路径片段。
        if is_oa and re.fullmatch(r"PMC[0-9]+", pmcid):
            full_text_urls = [{"availabilityCode": "OA", "documentStyle": "xml", "site": "Europe_PMC",
                "url": f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"}, *full_text_urls]
        return AcademicWork(
            title=str(item.get("title") or ""),
            url=pdf_url or html_url or (f"https://europepmc.org/article/MED/{pmid}" if pmid else (f"https://doi.org/{doi}" if doi else "")),
            authors=_split_authors(str(item.get("authorString") or ""))[:12],
            year=_safe_int(item.get("pubYear")),
            doi=doi,
            abstract=_clean_abstract(str(item.get("abstractText") or "")),
            source=str(item.get("journalTitle") or item.get("bookOrReportDetails") or "Europe PMC"),
            oa_status="open" if is_oa else "unknown",
            license="",
            source_database=self.name,
            document_type=str(next(iter((item.get('pubTypeList') or {}).get('pubType') or []), 'unknown')),
            access_mode="public_api",
            full_text_status="pdf_link_found" if pdf_url else ("html_link_found" if html_url else ("oa_available" if is_oa else "metadata_only")),
            provider_confidence=0.87,
            verification_status="doi_present" if doi else ("pmcid_matched" if pmcid else "unverified"),
            license_scope="unknown",
            raw={
                "pmid": pmid,
                "pmcid": pmcid,
                "source": item.get("source"),
                "in_epmc": item.get("inEPMC"),
                "in_pmc": item.get("inPMC"),
                "full_text_urls": full_text_urls,
            },
        )


def _full_text_urls(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    urls = ((item.get("fullTextUrlList") or {}).get("fullTextUrl") or [])
    return [url for url in urls if isinstance(url, dict)]


def _first_url(urls: List[Dict[str, Any]], *, style: str, open_only: bool) -> str:
    for item in urls:
        if str(item.get("documentStyle") or "").lower() != style:
            continue
        if open_only and str(item.get("availabilityCode") or "").upper() != "OA":
            continue
        url = str(item.get("url") or "").strip()
        if url:
            return url
    return ""


def _split_authors(value: str) -> List[str]:
    return [part.strip() for part in value.rstrip(".").split(",") if part.strip()]


def _clean_abstract(value: str) -> str:
    text = re.sub(r"</h\d+>", ". ", value)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()[:4000]


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None
