"""User-supplied citation import provider.

This provider parses citation exports that a user obtained from authorized
database sessions, Zotero, NoteExpress, EndNote, or journal sites. It does not
fetch remote pages or paper full text.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from .models import AcademicSearchRequest, AcademicWork, normalize_doi


class CitationImportError(RuntimeError):
    """Raised when a user citation import cannot be parsed."""


class CitationImportProvider:
    name = "citation_import"
    supported_formats = ["ris", "bibtex", "endnote", "noteexpress", "plain_text"]

    def __init__(self, max_bytes: int | None = None) -> None:
        self.max_bytes = max_bytes or int(os.getenv("KR_ACADEMIC_IMPORT_MAX_BYTES", "1048576"))

    def status(self) -> Dict[str, object]:
        return {
            "configured": True,
            "available": True,
            "requires_api_key": False,
            "network": False,
            "supported_formats": list(self.supported_formats),
            "max_bytes": self.max_bytes,
        }

    def search(self, request: AcademicSearchRequest) -> List[AcademicWork]:
        text, import_source = self._load_input(request)
        items = parse_citation_text(text, import_source=import_source)
        return items[: max(1, min(int(request.limit or 5), 20))]

    def _load_input(self, request: AcademicSearchRequest) -> Tuple[str, str]:
        raw = str(request.query or "").strip()
        source_path = str(request.options.get("source_path") or "").strip()
        if raw.lower().startswith("file:"):
            source_path = raw[5:].strip()
        elif source_path:
            source_path = source_path
        elif _looks_like_existing_file(raw):
            source_path = raw

        if source_path:
            path = Path(source_path).expanduser()
            if not path.exists() or not path.is_file():
                raise CitationImportError(f"Citation import file not found: {source_path}")
            if path.stat().st_size > self.max_bytes:
                raise CitationImportError(f"Citation import file exceeds max bytes: {self.max_bytes}")
            if path.suffix.lower() not in {".ris", ".bib", ".enw", ".txt"}:
                raise CitationImportError("Citation import supports .ris, .bib, .enw, and .txt files")
            return path.read_text(encoding="utf-8-sig", errors="replace"), str(path)

        if not raw:
            raise CitationImportError("Citation import requires a file path or citation text")
        if len(raw.encode("utf-8", errors="ignore")) > self.max_bytes:
            raise CitationImportError(f"Citation import text exceeds max bytes: {self.max_bytes}")
        return raw, "inline"


def parse_citation_text(text: str, import_source: str = "inline") -> List[AcademicWork]:
    content = str(text or "").strip()
    if not content:
        return []
    if re.search(r"(?m)^TY\s*-\s*", content):
        return _parse_ris(content, import_source)
    if re.search(r"(?m)^%[0A-Z]\s+", content):
        return _parse_endnote(content, import_source)
    if re.search(r"@\w+\s*\{", content):
        return _parse_bibtex(content, import_source)
    return _parse_plain_text(content, import_source)


def _parse_ris(text: str, import_source: str) -> List[AcademicWork]:
    records: List[Dict[str, List[str]]] = []
    current: Dict[str, List[str]] = {}
    for line in text.splitlines():
        match = re.match(r"^([A-Z0-9]{2})\s*-\s*(.*)$", line.strip())
        if not match:
            continue
        tag, value = match.group(1), match.group(2).strip()
        if tag == "TY" and current:
            records.append(current)
            current = {}
        current.setdefault(tag, []).append(value)
        if tag == "ER":
            records.append(current)
            current = {}
    if current:
        records.append(current)

    return [_work_from_fields(_ris_fields(record), "ris", import_source) for record in records if _ris_fields(record).get("title")]


def _ris_fields(record: Dict[str, List[str]]) -> Dict[str, object]:
    return {
        "title": _first(record, "TI", "T1", "CT"),
        "authors": record.get("AU") or record.get("A1") or [],
        "year": _year(_first(record, "PY", "Y1", "DA")),
        "doi": _first(record, "DO"),
        "url": _first(record, "UR", "LK"),
        "source": _first(record, "JO", "JF", "T2", "JA"),
        "abstract": _first(record, "AB", "N2"),
        "raw": dict(record),
    }


def _parse_endnote(text: str, import_source: str) -> List[AcademicWork]:
    records: List[Dict[str, List[str]]] = []
    current: Dict[str, List[str]] = {}
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        match = re.match(r"^%([0A-Z])\s+(.*)$", line)
        if not match:
            continue
        tag, value = match.group(1), match.group(2).strip()
        if tag == "0" and current:
            records.append(current)
            current = {}
        current.setdefault(tag, []).append(value)
    if current:
        records.append(current)

    return [_work_from_fields(_endnote_fields(record), "endnote", import_source) for record in records if _endnote_fields(record).get("title")]


def _endnote_fields(record: Dict[str, List[str]]) -> Dict[str, object]:
    return {
        "title": _first(record, "T"),
        "authors": record.get("A") or [],
        "year": _year(_first(record, "D")),
        "doi": _first(record, "R"),
        "url": _first(record, "U"),
        "source": _first(record, "J", "B"),
        "abstract": _first(record, "X"),
        "raw": dict(record),
    }


def _parse_bibtex(text: str, import_source: str) -> List[AcademicWork]:
    works: List[AcademicWork] = []
    for entry_type, key, body in _iter_bib_entries(text):
        fields = _parse_bib_fields(body)
        authors = _split_bib_authors(fields.get("author", ""))
        work = _work_from_fields(
            {
                "title": fields.get("title", ""),
                "authors": authors,
                "year": _year(fields.get("year", "")),
                "doi": fields.get("doi", ""),
                "url": fields.get("url", ""),
                "source": fields.get("journal") or fields.get("booktitle") or fields.get("publisher", ""),
                "abstract": fields.get("abstract", ""),
                "raw": {"entry_type": entry_type, "key": key, "fields": fields},
            },
            "bibtex",
            import_source,
        )
        if work.title:
            works.append(work)
    return works


def _iter_bib_entries(text: str) -> Iterable[Tuple[str, str, str]]:
    index = 0
    while True:
        match = re.search(r"@(\w+)\s*\{\s*([^,\s]+)\s*,", text[index:], flags=re.DOTALL)
        if not match:
            break
        start = index + match.end()
        absolute_start = index + match.start()
        depth = 1
        pos = start
        while pos < len(text) and depth:
            char = text[pos]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            pos += 1
        body = text[start : pos - 1]
        yield match.group(1).lower(), match.group(2), body
        index = max(pos, absolute_start + 1)


def _parse_bib_fields(body: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    pattern = re.compile(r"(\w+)\s*=\s*([{\"])", flags=re.DOTALL)
    pos = 0
    while True:
        match = pattern.search(body, pos)
        if not match:
            break
        name = match.group(1).lower()
        opener = match.group(2)
        value_start = match.end()
        if opener == '"':
            end = body.find('"', value_start)
            if end == -1:
                break
            value = body[value_start:end]
            pos = end + 1
        else:
            depth = 1
            cursor = value_start
            while cursor < len(body) and depth:
                if body[cursor] == "{":
                    depth += 1
                elif body[cursor] == "}":
                    depth -= 1
                cursor += 1
            value = body[value_start : cursor - 1]
            pos = cursor
        fields[name] = re.sub(r"\s+", " ", value).strip()
    return fields


def _parse_plain_text(text: str, import_source: str) -> List[AcademicWork]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    title = lines[0]
    year = _year(" ".join(lines[:3]))
    doi_match = re.search(r"(10\.\d{4,9}/\S+)", text, flags=re.IGNORECASE)
    return [
        _work_from_fields(
            {
                "title": title,
                "authors": [],
                "year": year,
                "doi": doi_match.group(1).rstrip(".,;") if doi_match else "",
                "url": "",
                "source": "",
                "abstract": "\n".join(lines[1:]),
                "raw": {"text": text},
            },
            "plain_text",
            import_source,
        )
    ]


def _work_from_fields(fields: Dict[str, object], fmt: str, import_source: str) -> AcademicWork:
    doi = normalize_doi(str(fields.get("doi") or ""))
    raw = fields.get("raw") if isinstance(fields.get("raw"), dict) else {}
    return AcademicWork(
        title=str(fields.get("title") or "").strip(),
        url=str(fields.get("url") or "").strip() or (f"doi:{doi}" if doi else ""),
        authors=[str(author).strip() for author in fields.get("authors") or [] if str(author).strip()],
        year=fields.get("year") if isinstance(fields.get("year"), int) else None,
        doi=doi,
        abstract=str(fields.get("abstract") or "").strip(),
        source=str(fields.get("source") or "").strip(),
        oa_status="",
        source_database=_source_database_from_import(import_source),
        access_mode="user_import",
        full_text_status="metadata_only",
        provider_confidence=0.85,
        verification_status="user_supplied",
        citation_export_formats=[fmt],
        license_scope="user_supplied",
        raw={"import_source": import_source, "format": fmt, **raw},
    )


def _source_database_from_import(import_source: str) -> str:
    lowered = import_source.lower()
    if "cnki" in lowered or "知网" in lowered:
        return "cnki"
    if "wanfang" in lowered or "万方" in lowered:
        return "wanfang"
    if "vip" in lowered or "维普" in lowered:
        return "vip"
    return "user_citation_import"


def _split_bib_authors(value: str) -> List[str]:
    if not value:
        return []
    return [part.strip() for part in re.split(r"\s+and\s+", value) if part.strip()]


def _first(record: Dict[str, List[str]], *keys: str) -> str:
    for key in keys:
        values = record.get(key) or []
        if values:
            return str(values[0] or "").strip()
    return ""


def _year(value: str) -> int | None:
    match = re.search(r"(19|20)\d{2}", str(value or ""))
    return int(match.group(0)) if match else None


def _looks_like_existing_file(value: str) -> bool:
    if not value or "\n" in value:
        return False
    suffix = Path(value).suffix.lower()
    if suffix not in {".ris", ".bib", ".enw", ".txt"}:
        return False
    return Path(value).expanduser().exists()
