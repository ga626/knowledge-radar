"""Unified direct-read academic full-text resolution helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping

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
        }


def provider_supports_direct_read(
    provider_id: str,
    profiles: Mapping[str, AcademicProviderProfile] | None = None,
) -> bool:
    profile = (profiles or load_academic_provider_profiles()).get(str(provider_id or ""))
    if not profile or not profile.enabled:
        return False
    if not (profile.content.direct_read_preferred or profile.content.html_fulltext or profile.content.pdf_fulltext):
        return False
    return True


def extract_academic_fulltext(
    provider_id: str,
    url: str,
    *,
    timeout_s: float | None = None,
    profiles: Mapping[str, AcademicProviderProfile] | None = None,
) -> AcademicFullTextResult:
    """Resolve article text through provider-owned preview/direct-read paths.

    This helper intentionally does not persist PDFs to disk and does not use download
    controls. It normalizes existing provider verification methods so the strategy
    layer can treat PubScholar, SciEngine, and VIP preview routes consistently.
    """

    provider_key = str(provider_id or "").strip()
    source_url = str(url or "").strip()
    profile_map = profiles or load_academic_provider_profiles()
    if not provider_supports_direct_read(provider_key, profile_map):
        return _degraded(provider_key, source_url, "unsupported_direct_read_provider")

    providers = instantiate_academic_providers(profile_map)
    provider = providers.get(provider_key)
    if provider is None or not hasattr(provider, "verify_article_fulltext"):
        return _degraded(provider_key, source_url, "provider_has_no_verify_article_fulltext")

    if timeout_s is not None and hasattr(provider, "timeout_s"):
        try:
            setattr(provider, "timeout_s", float(timeout_s))
        except Exception:
            pass

    try:
        raw = provider.verify_article_fulltext(source_url)
    except Exception as exc:
        return _degraded(provider_key, source_url, f"{type(exc).__name__}: {exc}")
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
    text_extractable = bool(raw.get("text_extractable") or text_probe.get("extractable"))
    status = str(raw.get("status") or ("PASS" if text_extractable else "EXPECTED_DEGRADED"))
    if status == "PASS" and not text_extractable:
        status = "EXPECTED_DEGRADED"
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
        raw_status=raw,
    )


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
