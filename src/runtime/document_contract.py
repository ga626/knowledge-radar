"""公共文档的请求范围、加工身份和页账本；不包含站点取件逻辑。"""

from __future__ import annotations

from importlib.metadata import version, PackageNotFoundError


def selected_pages(count, *, max_pages=None, page_numbers=None):
    if max_pages is not None and (type(max_pages) is not int or not 1 <= max_pages <= 500):
        raise ValueError("invalid_document_page_limit")
    if page_numbers is None:
        return list(range(1, min(count, max_pages or count) + 1))
    if (
        not isinstance(page_numbers, (list, tuple))
        or not page_numbers
        or len(page_numbers) > 500
        or any(type(n) is not int or not 1 <= n <= count for n in page_numbers)
        or len(set(page_numbers)) != len(page_numbers)
    ):
        raise ValueError("invalid_document_page_numbers")
    return sorted(page_numbers)[:max_pages]


def processing_identity(input_sha, options):
    from runtime.pdf_text import PDF_PROCESSING_CONTRACT
    from runtime.cloud_ocr import OCR_REQUEST_PROFILE, MAX_IMAGE_SIDE
    from runtime.model_roles import CLOUD_OCR_MODEL
    import os

    packages = {}
    for name in ("pypdf", "pypdfium2", "Pillow"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = "not_installed"
    return {
        "input_sha256": input_sha,
        "contract": PDF_PROCESSING_CONTRACT,
        "packages": packages,
        "ocr_model": CLOUD_OCR_MODEL,
        "ocr_profile": OCR_REQUEST_PROFILE,
        "image_max_side": MAX_IMAGE_SIDE,
        "ocr_configured": bool(os.environ.get("SILICONFLOW_API_KEY", "").strip()),
        "options": options,
    }


def response_cache_allowed(response):
    """HTTP缓存政策独立于用户是否许可保存证据；保守限制持久加工快照。"""
    control = response.headers.get("cache-control", "").lower()
    return not (
        "no-store" in control
        or "private" in control
        or response.headers.get("set-cookie")
        or response.headers.get("vary") == "*"
    )


def finish_document(parsed, *, input_sha="", identity=None, reused=False):
    import copy

    result = copy.deepcopy(parsed)
    requested = result.get("requested_pages") or list(range(1, result.get("pages_read", 0) + 1))
    processed = set(result.get("processed_pages", requested[: result.get("pages_read", 0)]))
    ocr = set(result.get("ocr_pages", []))
    empty = set(result.get("text_layer_empty_pages", result.get("empty_pages", [])))
    suspicious = set(result.get("suspicious_text_pages", []))
    unknown = set(result.get("ocr_unknown_pages", []))
    errors = {e["page"]: e["type"] for e in result.get("ocr_errors", []) if e.get("page") is not None}
    missing = sorted((empty | suspicious | (set(requested) - processed)) - ocr)
    ledger = []
    for page in requested:
        state = (
            "ocr_complete"
            if page in ocr
            else "result_unknown"
            if page in unknown
            else "not_processed"
            if page not in processed
            else "text_quality_pending"
            if page in suspicious
            else "blank_or_scan_unresolved"
            if page in empty
            else "text_layer_read"
        )
        ledger.append(
            {
                "page": page,
                "state": state,
                "error": errors.get(page, ""),
                "physical_page": True,
                "semantic_quality": "not_asserted",
            }
        )
    satisfied = not missing and not result.get("truncated", False)
    result.update(
        schema="knowledgeradar-document/v1",
        input_sha256=input_sha,
        processing_identity=identity or result.get("processing_identity", {}),
        processing_reused=reused,
        requested_pages=requested,
        page_ledger=ledger,
        missing_text_pages=missing,
        request_satisfied=satisfied,
        source_pages_exhausted=len(requested) == result.get("page_count"),
        layout_quality="not_asserted",
        coverage=("complete_text_and_ocr" if ocr else "complete_selectable_text") if satisfied else "partial",
    )
    result["resume"] = {
        "pending_pages": [p for p in missing if p not in unknown],
        "unknown_pages": sorted(unknown),
        "unknown_automatic_replay": False,
    }
    return result
