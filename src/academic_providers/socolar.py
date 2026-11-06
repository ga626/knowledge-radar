"""Socolar 历史文章记录的纯离线解析；独立关键词搜索已退役。"""
from __future__ import annotations
import html
import re
from typing import Any, Dict, List
from urllib.parse import quote
from .models import AcademicSearchRequest, AcademicWork, normalize_doi

SOCOLAR_DETAIL_URL = "https://www.socolar.com/articleDetails?articleId={article_id}"


def parse_article_record(item: Dict[str, Any], request: AcademicSearchRequest) -> AcademicWork | None:
    article_id = str(item.get("id") or item.get("articleId") or item.get("objectId") or item.get("uuid") or "").strip()
    title = _clean_text(item.get("title") or item.get("name") or "")
    external_url = str(item.get("fullTextUrl") or item.get("fulltextUrl") or item.get("url") or "").strip()
    if not title or not (article_id or external_url):
        return None
    article_url = SOCOLAR_DETAIL_URL.format(article_id=quote(article_id)) if article_id else external_url or "https://www.socolar.com/"
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
        access_mode="public_abstract_discovery",
        full_text_status="abstract_with_external_landing_url" if external_url else "metadata_only",
        provider_confidence=0.82 if abstract else 0.68,
        verification_status="socolar_detail_abstract_confirmed" if abstract else "socolar_detail_metadata_confirmed",
        license_scope="open_or_external_platform_terms",
        degraded_reason=(
            "" if abstract else "Socolar detail record has no abstract; external landing URL was not followed by default."
        ),
        raw=raw,
    )



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
