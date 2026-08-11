"""arXiv Atom API metadata provider."""

from __future__ import annotations

import time
from typing import Any, Dict, List
from xml.etree import ElementTree

import httpx

from .models import AcademicSearchRequest, AcademicWork, normalize_doi


class ArxivError(Exception):
    pass


class ArxivRateLimitError(ArxivError):
    pass


class ArxivProvider:
    name = "arxiv"

    def __init__(self, endpoint: str = "https://export.arxiv.org/api/query", timeout: float = 15.0, retries: int = 2) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self.retries = max(0, int(retries))

    def status(self) -> Dict[str, Any]:
        return {"configured": True, "available": True, "endpoint": self.endpoint, "requires_api_key": False}

    def search(self, request: AcademicSearchRequest) -> List[AcademicWork]:
        limit = max(1, min(int(request.limit or 5), 20))
        params = {"search_query": f"all:{request.query}", "start": 0, "max_results": limit}
        with httpx.Client(timeout=self.timeout, headers={"User-Agent": "KnowledgeRadar/academic-pilot"}) as client:
            for attempt in range(self.retries + 1):
                try:
                    response = client.get(self.endpoint, params=params)
                    if response.status_code == 429:
                        if attempt < self.retries:
                            time.sleep(min(8.0, 1.5 * (2**attempt)))
                            continue
                        raise ArxivRateLimitError("arXiv API rate limited (HTTP 429)")
                    response.raise_for_status()
                    text = response.text
                    break
                except ArxivRateLimitError:
                    raise
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if attempt < self.retries:
                        time.sleep(min(8.0, 1.0 * (2**attempt)))
                        continue
                    raise ArxivError(str(exc)) from exc
                except Exception as exc:
                    raise ArxivError(str(exc)) from exc
        return [_work_from_entry(entry) for entry in _entries(text)]


def _entries(text: str) -> List[ElementTree.Element]:
    root = ElementTree.fromstring(text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    return list(root.findall("atom:entry", ns))


def _work_from_entry(entry: ElementTree.Element) -> AcademicWork:
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    title = _text(entry.find("atom:title", ns))
    url = _text(entry.find("atom:id", ns))
    authors = [_text(author.find("atom:name", ns)) for author in entry.findall("atom:author", ns)]
    published = _text(entry.find("atom:published", ns))
    doi = normalize_doi(_text(entry.find("arxiv:doi", ns)))
    pdf_url = ""
    for link in entry.findall("atom:link", ns):
        if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
            pdf_url = link.attrib.get("href", "")
            break
    return AcademicWork(
        title=" ".join(title.split()),
        url=url or pdf_url,
        authors=[a for a in authors if a][:12],
        year=_year(published),
        doi=doi,
        abstract=" ".join(_text(entry.find("atom:summary", ns)).split())[:2000],
        source="arXiv",
        oa_status="green",
        license=_text(entry.find("arxiv:license", ns)),
        source_database="arxiv",
        access_mode="public_api",
        full_text_status="oa_available",
        provider_confidence=0.9,
        verification_status="doi_matched" if doi else "unverified",
        citation_export_formats=["bibtex"],
        license_scope="open",
        raw={"published": published, "pdf_url": pdf_url},
    )


def _text(node: ElementTree.Element | None) -> str:
    return node.text.strip() if node is not None and node.text else ""


def _year(value: str) -> int | None:
    try:
        return int(value[:4])
    except Exception:
        return None
