"""Provider/model preflight semantics for media routing.

This is intentionally a classifier, not a fake network probe.  A model name
or a screenshot of quota cannot prove that the configured endpoint is usable;
the caller must record a real canary separately.
"""

from __future__ import annotations

from typing import Any


SCHEMA = "knowledgeradar-media-provider-preflight/v1"


def classify_provider_error(error: Any) -> dict[str, Any]:
    text = str(error or "")
    upper = text.upper()
    status_code = None
    for code in (400, 401, 403, 404, 408, 409, 429, 500, 502, 503, 504):
        if str(code) in upper:
            status_code = code
            break
    if status_code in {401, 403} or any(token in upper for token in ("ACCESS DENIED", "UNAUTHORIZED", "PERMISSION")):
        kind = "AUTH_OR_PERMISSION"
    elif status_code == 429 or "RATE LIMIT" in upper or "QUOTA" in upper:
        kind = "QUOTA_OR_RATE_LIMIT"
    elif status_code == 400 or "BAD REQUEST" in upper or "UNSUPPORTED" in upper:
        kind = "MODEL_OR_REQUEST_CONTRACT"
    elif any(token in upper for token in ("PROVIDER_UNAVAILABLE", "ENDPOINT", "CONNECTION", "TIMEOUT", "503", "502")):
        kind = "PROVIDER_UNAVAILABLE"
    else:
        kind = "UNKNOWN_PROVIDER_FAILURE"
    return {"schema": SCHEMA, "failure_class": kind, "status_code": status_code, "raw_label": kind.lower()}


def preflight_contract(model_ref: str, *, capability: str = "native_video", canary_passed: bool = False) -> dict[str, Any]:
    ref = str(model_ref or "").strip()
    provider, _, model = ref.partition(":")
    from .model_roles import ASR_MODEL_REF, OCR_MODEL_REF, CLOUD_MODEL_REF
    known_model = ref in {ASR_MODEL_REF, OCR_MODEL_REF, CLOUD_MODEL_REF}
    canary_required = ref == CLOUD_MODEL_REF
    return {
        "schema": SCHEMA,
        "model_ref": ref,
        "provider": provider or "unknown",
        "model": model or ref,
        "capability": capability,
        "known_contract": known_model,
        "canary_required": canary_required,
        "canary_passed": bool(canary_passed),
        "status": "ready" if known_model and (not canary_required or canary_passed) else ("canary_required" if known_model else "blocked_model"),
        "does_not_infer_quota": True,
    }

