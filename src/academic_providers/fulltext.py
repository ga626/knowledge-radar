"""Unified direct-read academic full-text resolution helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping
import time

from .profile import AcademicProviderProfile, load_academic_provider_profiles
from .registry import instantiate_academic_providers


_DIRECT_READ_PROVIDER_MODES = {
    "pubscholar": "pdf_viewer_text",
    "sciengine": "pdf_viewer_text",
    "vip_oa": "pdf_viewer_text",
}


@dataclass(frozen=True)
class AcademicFullTextResult:
    provider_id: str
    status: str
    source_url: str
    mode: str
    text_extractable: bool
    text_length: int = 0
    sample: str = ""
    page_count: int = 0
    degraded_reason: str = ""
    provenance: Dict[str, Any] = field(default_factory=dict)
    raw_status: Dict[str, Any] = field(default_factory=dict)
    text: str = ""
    coverage: str = "unknown"
    locators: list[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "status": self.status,
            "source_url": self.source_url,
            "mode": self.mode,
            "text_extractable": self.text_extractable,
            "text_length": self.text_length,
            "sample": self.sample,
            "page_count": self.page_count,
            "degraded_reason": self.degraded_reason,
            "provenance": dict(self.provenance),
            "raw_status": dict(self.raw_status),
            "text": self.text,
            "coverage": self.coverage,
            "locators": list(self.locators),
        }


def provider_supports_direct_read(
    provider_id: str,
    profiles: Mapping[str, AcademicProviderProfile] | None = None,
    *, work: Any = None,
) -> bool:
    profile = (profiles or load_academic_provider_profiles()).get(str(provider_id or ""))
    if not profile or not profile.enabled:
        return False
    raw = getattr(work, 'raw', {}) or {}
    actual_content = bool(raw.get('fulltext_text') or raw.get('fulltext_inline_ref') or raw.get('fulltext_candidate_urls') or raw.get('open_access_pdf_url') or raw.get('fulltext_link') or
                          (raw.get('primary_location') or {}).get('is_oa'))
    if not (actual_content or profile.content.direct_read_preferred or profile.content.html_fulltext or profile.content.pdf_fulltext):
        return False
    return True


def extract_academic_fulltext(
    provider_id: str,
    url: str,
    *,
    timeout_s: float | None = None,
    profiles: Mapping[str, AcademicProviderProfile] | None = None,
    work: Any = None,
    include_ocr: bool | None = None,
) -> AcademicFullTextResult:
    """来源只定位，PDF统一公共处理；OCR遵守既有组件授权和显式禁止。"""

    provider_key = str(provider_id or "").strip()
    source_url = str(url or "").strip()
    permissions = getattr(work, 'raw', {}) or {}
    if permissions.get('read_allowed') is False or permissions.get('download_allowed') is False:
        return _degraded(provider_key, source_url, 'academic_fulltext_permission_denied')
    if permissions.get('evidence_storage_allowed') is False:
        return _degraded(provider_key, source_url, 'academic_persistent_fulltext_not_authorized')
    if permissions.get('cloud_processing_allowed') is False:
        include_ocr = False
    profile_map = profiles or load_academic_provider_profiles()
    if not provider_supports_direct_read(provider_key, profile_map, work=work):
        return _degraded(provider_key, source_url, "unsupported_direct_read_provider")

    providers = instantiate_academic_providers({provider_key: profile_map[provider_key]})
    provider = providers.get(provider_key)
    if provider is None or not hasattr(provider, "verify_article_fulltext"):
        profile = profile_map[provider_key]
        if profile.access.login_required or profile.access.payment_required:
            return _degraded(provider_key, source_url, "authorized_reader_required")
        return _extract_public_work(provider_key, source_url, work, timeout_s=timeout_s or 600, include_ocr=include_ocr)

    if timeout_s is not None and hasattr(provider, "timeout_s"):
        try:
            setattr(provider, "timeout_s", float(timeout_s))
        except Exception:
            pass

    try:
        reader = provider.verify_article_fulltext
        if source_url == (getattr(work, 'raw', {}) or {}).get('pdf_url') and callable(getattr(provider, 'verify_pdf_text_url', None)):
            reader = provider.verify_pdf_text_url
        from runtime.execution_context import check_execution
        check_execution()
        import inspect
        kwargs = {'include_ocr':include_ocr} if 'include_ocr' in inspect.signature(reader).parameters else {}
        if 'document_options' in inspect.signature(reader).parameters:
            kwargs['document_options'] = permissions.get('document_options', {})
        raw = reader(source_url, **kwargs)
    except Exception as exc:
        from runtime.execution_context import ExecutionStopped
        if isinstance(exc, ExecutionStopped):
            raise
        return _degraded(provider_key, source_url, type(exc).__name__)
    if not isinstance(raw, dict):
        return _degraded(provider_key, source_url, "provider_returned_non_dict_status")
    return _normalize_direct_read_status(provider_key, source_url, raw)


def _normalize_direct_read_status(provider_id: str, source_url: str, raw: Dict[str, Any]) -> AcademicFullTextResult:
    text_probe = raw.get("text_probe") or raw.get("text_extraction") or {}
    if not isinstance(text_probe, dict):
        text_probe = {}
    text_length = _int_value(text_probe.get("text_length"), text_probe.get("sample_text_len"))
    page_count = _int_value(text_probe.get("page_count"))
    sample = str(text_probe.get("sample") or text_probe.get("sample_text") or "")
    text = str(text_probe.get("text") or "")
    if text:
        text_length = len(text)
    text_extractable = bool(raw.get("text_extractable") or text_probe.get("extractable"))
    status = str(raw.get("status") or ("PASS" if text_extractable else "EXPECTED_DEGRADED"))
    if status == "PASS" and not text_extractable:
        status = "EXPECTED_DEGRADED"
    if status == "PASS" and not text:
        status = "PROBE_ONLY"
    degraded_reason = "" if status == "PASS" else str(raw.get("reason") or text_probe.get("error") or "text_not_extractable")
    mode = _DIRECT_READ_PROVIDER_MODES.get(provider_id, "unknown")
    resolved_url = str(raw.get("file_url") or raw.get("pdf_url") or raw.get("viewer_url") or raw.get("reading_url") or source_url)
    provenance = {
        "provider_id": provider_id,
        "source_url": source_url,
        "resolved_url": resolved_url,
        "mode": mode,
        "pdf_bytes_confirmed": bool(raw.get("pdf_bytes_confirmed")),
        "text_extractable": text_extractable,
        "page_count": page_count,
        "disk_persisted": False,
        "download_button_used": False,
        "input_sha256": raw.get('input_sha256', ''),
        "pdf_processing": raw.get('pdf_processing') or {k:v for k,v in text_probe.items() if k not in {'text','sample','sample_text','locators','document_blocks'}},
    }
    return AcademicFullTextResult(
        provider_id=provider_id,
        status=status,
        source_url=source_url,
        mode=mode,
        text_extractable=text_extractable,
        text_length=text_length,
        sample=sample,
        page_count=page_count,
        degraded_reason=degraded_reason,
        provenance=provenance,
        raw_status=_without_body(raw),
        text=text,
        coverage=str(text_probe.get("coverage") or "partial") if text else "unknown",
        locators=list(text_probe.get('locators') or []),
    )


def _without_body(value):
    if isinstance(value, dict):
        return {key: _without_body(item) for key, item in value.items()
                if key not in {'text', 'fulltext_text', 'document_blocks', 'locators'}}
    if isinstance(value, list):
        return [_without_body(item) for item in value]
    return value


def _extract_public_work(provider_id, source_url, work, *, timeout_s, include_ocr=None):
    """复用公开 URL 防护与公共 HTML/PDF 处理，不携带站点账号。"""
    from generic_web.url_policy import guarded_fetch, redact_url
    from generic_web.collector import _collect_from_html
    from generic_web.models import GenericWebRequest
    from runtime.pdf_text import process_pdf
    from runtime.document_contract import response_cache_allowed
    from kr_core.evidence_locators import document_text, paragraph_locators
    raw = dict(getattr(work, 'raw', {}) or {})
    inline = str(raw.get('fulltext_text') or '')
    if raw.get('fulltext_inline_ref'):
        from kr_core.evidence_assets import EvidenceAssets
        assets = EvidenceAssets()
        part = assets.fetch(raw['fulltext_inline_ref'], max_chars=24000)
        inline = part['text']
        while part['next_start'] is not None:
            part = assets.fetch(raw['fulltext_inline_ref'], start=part['next_start'], max_chars=24000)
            inline += part['text']
    if inline:
        return AcademicFullTextResult(provider_id, 'PASS', source_url, 'api_text', True,
            text_length=len(inline), text=inline, sample=inline[:240], coverage='partial',
            locators=paragraph_locators(inline),
            provenance={'provider_id': provider_id, 'source_url': source_url, 'mode': 'api_text'})
    urls = []
    for key in ('ar5iv_html_url', 'html_url', 'reader_url'):
        if raw.get(key):
            urls.append(raw[key])
    for entry in raw.get('full_text_urls', []):
        if isinstance(entry, dict) and entry.get('availabilityCode') == 'OA' and entry.get('documentStyle') in {'html', 'xml'}:
            urls.append(entry.get('url'))
    primary = raw.get('primary_location') or {}
    for key in ('pdf_url', 'url_for_pdf', 'download_url', 'fulltext_link', 'open_access_pdf_url'):
        if raw.get(key):
            urls.append(raw[key])
    urls.append(primary.get('pdf_url'))
    urls.extend(raw.get('fulltext_candidate_urls') or [])
    urls.extend(raw.get('source_fulltext_urls') or [])
    # 下载位置先于普通落地页；位置的版本/许可与作品书目分别保留。
    selected_version = str(getattr(work, 'version', 'unknown') or 'unknown')
    for location in raw.get('locations') or raw.get('oa_locations') or []:
        if not isinstance(location, dict) or location.get('is_oa') is False:
            continue
        declared = location.get('version')
        if selected_version != 'unknown' and declared and declared != selected_version:
            continue
        urls.extend([location.get('pdf_url') or location.get('url_for_pdf'), location.get('landing_page_url') or location.get('url')])
    for entry in raw.get('full_text_urls', []):
        if isinstance(entry, dict) and entry.get('availabilityCode') == 'OA':
            urls.append(entry.get('url'))
    urls.extend([primary.get('landing_page_url'), source_url])
    # arXiv HTML 与 PDF 是同一已标识论文的两种官方阅读入口。
    if provider_id in {'arxiv', 'ar5iv'}:
        from .ar5iv import _arxiv_id_from_text
        identifier = _arxiv_id_from_text(source_url)
        if identifier:
            urls = [f'https://arxiv.org/html/{identifier}', f'https://ar5iv.labs.arxiv.org/html/{identifier}',
                    f'https://arxiv.org/pdf/{identifier}'] + urls
    urls = list(dict.fromkeys(u for u in urls if isinstance(u, str) and u.startswith(('http://', 'https://'))))
    deadline = time.monotonic() + max(0.1, timeout_s)
    failures = []
    for url in urls[:12]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            pdf_processing = {}
            response = guarded_fetch(url, timeout=min(remaining, 30.0), max_bytes=32 * 1024 * 1024)
            response.raise_for_status()
            if provider_id in {'arxiv','ar5iv'}:
                from .identity import arxiv_identifier
                requested = arxiv_identifier(source_url)
                received = arxiv_identifier(str(response.url))
                if requested and received and requested != received and __import__('re').search(r'v\d+$', requested):
                    failures.append('academic_version_redirect_mismatch')
                    continue
            if response.content.startswith(b'%PDF-'):
                extracted = process_pdf(response.content, timeout_s=max(.01, deadline-time.monotonic()),
                    allow_ocr=include_ocr is not False, enhance_suspicious=include_ocr is True,
                    cache_allowed=raw.get('evidence_storage_allowed') is not False and response_cache_allowed(response),
                    page_numbers=raw.get('requested_pdf_pages'),
                    storage_allowed=(raw.get('document_options') or {}).get('storage_allowed', True))
                text = str(extracted['text'])
                if not text.strip():
                    reason = (extracted.get('ocr_errors') or [{}])[0].get('type', 'pdf_text_unavailable')
                    # A PDF has reached the common OCR processor. Do not replay
                    # model calls by probing alternate copies of the same work.
                    return _degraded(provider_id, source_url, reason)
                mode = 'public_pdf_text_ocr' if extracted.get('ocr_pages') else 'public_pdf_text'
                count, coverage = extracted['page_count'], extracted.get('coverage', 'partial')
                locators = extracted.get('locators') or paragraph_locators(text)
                pdf_processing = {k: extracted.get(k) for k in
                    ('processing_method', 'text_layer_empty_pages', 'ocr_pages', 'ocr_status',
                     'ocr_errors', 'missing_text_pages', 'truncated', 'ocr_model', 'page_count', 'pages_read',
                     'suspicious_text_pages', 'quality_warnings', 'page_ledger','request_satisfied','requested_pages',
                     'processing_identity','resume','source_pages_exhausted','original_asset','input_sha256')}
            else:
                from bs4 import BeautifulSoup
                is_xml = 'xml' in response.headers.get('content-type', '').lower()
                soup = BeautifulSoup(response.text, 'xml' if is_xml else 'html.parser')
                if is_xml and getattr(work, 'doi', ''):
                    from .models import normalize_doi
                    declared = soup.find('article-id', attrs={'pub-id-type':'doi'})
                    if declared and normalize_doi(declared.get_text(strip=True)) != normalize_doi(work.doi):
                        failures.append('academic_document_doi_mismatch')
                        continue
                body = (soup.find('body') if is_xml and soup.find('article') else
                        soup.select_one('.ltx_document, .jats-body, #article-body, .article-body, .fulltext-body, .article-content'))
                if not body or len(body.get_text(' ', strip=True)) < 500:
                    failures.append('landing_or_abstract_only')
                    continue
                for unwanted in body.select('script, style, nav, form, .abstract'):
                    unwanted.decompose()
                from .structured_text import academic_blocks
                blocks = academic_blocks(soup, body, is_xml=is_xml)
                if blocks:
                    text, locators = document_text(blocks)
                else:
                    parsed = _collect_from_html(GenericWebRequest(url=url, use_jina=False), html=str(body),
                        final_url=str(response.url), status_code=200, content_type='text/html', collector='academic_html')
                    text, locators = parsed.content, paragraph_locators(parsed.content)
                mode, count, coverage = 'public_xml_text' if is_xml else 'public_html_text', 0, 'partial'
                if len(text.strip()) < 100:
                    failures.append('html_parse_empty')
                    continue
            return AcademicFullTextResult(provider_id, 'PASS', source_url, mode, True,
                text_length=len(text), text=text, sample=text[:240], page_count=count, coverage=coverage,
                locators=locators,
                provenance={'provider_id': provider_id, 'source_url': source_url,
                    'resolved_url': redact_url(str(response.url)), 'mode': mode, 'coverage': coverage,
                    'input_sha256':__import__('hashlib').sha256(response.content).hexdigest(),
                    'location':next((location for location in raw.get('locations') or raw.get('oa_locations') or []
                        if isinstance(location, dict) and url in {location.get('pdf_url'),location.get('url_for_pdf'),location.get('landing_page_url'),location.get('url')}), {}),
                    'structure':{'scope':'text_layer_or_public_article_blocks', 'layout_math_accuracy':'not_asserted'},
                    'disk_persisted': False, 'max_pdf_pages': 500, 'pdf_processing': pdf_processing})
        except Exception as exc:
            from runtime.execution_context import ExecutionStopped
            if isinstance(exc, ExecutionStopped):
                raise
            # 不把可能含签名、邮箱和 token 的请求异常原文写进报告。
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            failures.append(f'http_{status}' if status else type(exc).__name__)
    result = _degraded(provider_id, source_url, ';'.join(dict.fromkeys(failures)) or 'no_public_text_url')
    return result


def _degraded(provider_id: str, source_url: str, reason: str) -> AcademicFullTextResult:
    return AcademicFullTextResult(
        provider_id=provider_id,
        status="EXPECTED_DEGRADED",
        source_url=source_url,
        mode="unsupported",
        text_extractable=False,
        degraded_reason=reason,
        provenance={
            "provider_id": provider_id,
            "source_url": source_url,
            "mode": "unsupported",
            "disk_persisted": False,
            "download_button_used": False,
        },
    )


def _int_value(*values: Any) -> int:
    for value in values:
        try:
            if value is not None and value != "":
                return int(value)
        except (TypeError, ValueError):
            continue
    return 0
