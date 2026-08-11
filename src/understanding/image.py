"""Image understanding helpers."""

from __future__ import annotations

import json
import logging
import hashlib
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import os
import re
import time
from typing import Any, Dict, List
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from runtime.cost_latency import TTLCache, attach_runtime_metadata, budget_envelope, stable_key
from runtime.tasks import compact_task_ref, get_task_store

from .siliconflow import call_multimodal_models, configured_models, image_bytes_to_base64

log = logging.getLogger("mcp-server")

_OCR_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kr-xhs-ocr")
_OCR_ARTIFACT_CACHE = TTLCache("xhs.ocr.artifact", ttl_s=float(os.environ.get("KR_XHS_OCR_ARTIFACT_TTL_S", "86400")), max_items=256)
_NOISE_IMAGE_TOKENS = (
    "avatar",
    "profile",
    "usericon",
    "user_icon",
    "head",
    "icon",
    "logo",
    "emoji",
    "sticker",
    "placeholder",
    "default",
    "sprite",
    "qrcode",
)


def _extract_json(text: str) -> Dict:
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    for marker in ("```json", "```"):
        if marker in text:
            for part in text.split(marker):
                candidate = part.strip()
                if candidate.startswith("{") and candidate.endswith("}"):
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return {"text": text}


def _image_url_from_item(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("url", "src", "image_url", "imageUrl", "origin", "original", "master_url"):
            value = item.get(key)
            if value:
                return str(value).strip()
    return str(item or "").strip()


def _image_noise_reason(url: str) -> str:
    lower = url.lower()
    if not lower.startswith(("http://", "https://")):
        return "non_http_url"
    if lower.startswith("data:"):
        return "inline_data_url"
    if "fe-platform.xhscdn.com/platform/" in lower:
        return "platform_asset"
    if "sns-avatar" in lower:
        return "avatar_asset"
    if lower.endswith(".svg") or ".svg?" in lower:
        return "svg_asset"
    for token in _NOISE_IMAGE_TOKENS:
        if token in lower:
            return f"likely_{token}"
    return ""


def _select_xhs_ocr_images(images: List[Any], max_images: int) -> tuple[List[str], Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    fallback: List[Dict[str, Any]] = []
    for index, raw in enumerate(images or []):
        url = _image_url_from_item(raw)
        if not url:
            continue
        reason = _image_noise_reason(url)
        row: Dict[str, Any] = {"index": index, "url": url[:240], "selected": False}
        if reason:
            row["rejected_reason"] = reason
            fallback.append(row)
            continue
        score = 0
        lower = url.lower()
        if "sns-webpic" in lower:
            score += 5
        elif "sns-img" in lower:
            score += 4
        elif "xhscdn" in lower or "xiaohongshu" in lower or "xhs" in lower:
            score += 2
        if "spectrum" in lower or "nd_dft" in lower:
            score += 2
        if any(suffix in lower for suffix in (".jpg", ".jpeg", ".png", ".webp", "image")):
            score += 1
        row["score"] = score
        candidates.append(row)
    ranked = sorted(candidates, key=lambda item: (-int(item.get("score") or 0), int(item.get("index") or 0)))
    if ranked:
        selected = ranked[:max_images]
        fallback_used = False
    else:
        selected = fallback[:max_images]
        fallback_used = bool(selected)
    selected_urls = [str(item.get("url") or "") for item in selected if item.get("url")]
    selected_indexes = {int(item.get("index") or 0) for item in selected}
    preview = []
    for row in [*ranked, *fallback]:
        item = dict(row)
        item["selected"] = int(item.get("index") or 0) in selected_indexes
        preview.append(item)
        if len(preview) >= 10:
            break
    return selected_urls, {
        "schema": "xhs-ocr-image-selection/v1",
        "input_count": len([item for item in images or [] if _image_url_from_item(item)]),
        "selected_count": len(selected_urls),
        "selected_indexes": sorted(selected_indexes),
        "fallback_used": fallback_used,
        "candidates_preview": preview,
    }


def _normalized_image_url(url: str) -> str:
    try:
        parsed = urlsplit(str(url or "").strip())
    except Exception:
        return str(url or "").strip()
    ignored_prefixes = ("xsec", "sign", "token", "expires", "expire", "timestamp", "ts", "t")
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not any(key.lower().startswith(prefix) for prefix in ignored_prefixes)
    ]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), urlencode(query), ""))


def _xhs_ocr_task_identity(selected_urls: List[str], base_metadata: Dict[str, Any]) -> tuple[str, str]:
    normalized_urls = sorted({_normalized_image_url(url) for url in selected_urls if str(url or "").strip()})
    content_id = str(base_metadata.get("content_id") or "").strip()
    policy = str(os.environ.get("KR_XHS_OCR_TRIGGER_POLICY") or "image_presence").strip().lower()
    model_family = ",".join(configured_models("image"))
    identity = json.dumps(
        {
            "content_id": content_id,
            "image_urls": normalized_urls,
            "policy": policy,
            "model_family": model_family,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    stable_part = content_id or "\n".join(normalized_urls)
    task_id = f"xhs_ocr_{hashlib.sha1(f'{stable_part}\n{policy}\n{model_family}'.encode('utf-8')).hexdigest()[:16]}"
    return task_id, hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]


def _ocr_signal_strength(text: str, items: List[Any], visual_summary: Any) -> tuple[str, str]:
    if str(text or "").strip() or items:
        return "text", ""
    if str(visual_summary or "").strip():
        return "visual_only", "text_empty_visual_summary_present"
    return "none_or_weak", "no_text_or_visual_summary_extracted"


def _existing_xhs_ocr_task(task_store: Any, *, content_id: str, identity_hash: str) -> Dict[str, Any]:
    if not content_id:
        return {}
    try:
        tasks = task_store.tasks_for_source(content_id=content_id, blocking_only=False, include_terminal=True, limit=20)
    except Exception:
        return {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if str(task.get("task_type") or "") != "xhs_image_ocr":
            continue
        status = str(task.get("status") or "")
        if status not in {"queued", "running", "completed"}:
            continue
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        if identity_hash and metadata.get("ocr_identity_hash") and metadata.get("ocr_identity_hash") != identity_hash:
            continue
        return task
    return {}


def ocr_first_xhs_image(images: List[str], task_metadata: Dict[str, Any] | None = None) -> Dict:
    """Run OCR/visual text extraction for the first few Xiaohongshu images."""
    started = time.time()
    task_store = get_task_store()
    base_metadata = dict(task_metadata or {})
    if not images:
        return {"status": "skipped", "reason": "no_images", "elapsed_s": 0}

    try:
        max_images = max(1, int(os.environ.get("KR_XHS_OCR_MAX_IMAGES", "3")))
    except Exception:
        max_images = 3
    selected_urls, image_selection = _select_xhs_ocr_images(images, max_images)
    if not selected_urls:
        return {"status": "skipped", "reason": "empty_image_url", "elapsed_s": 0}
    image_url = selected_urls[0]

    task_id, identity_hash = _xhs_ocr_task_identity(selected_urls, base_metadata)
    artifact_key = stable_key("xhs.ocr.artifact", selected_urls)
    cached = _OCR_ARTIFACT_CACHE.get(artifact_key, allow_stale=True)
    if cached:
        cached["task_id"] = task_id
        cached.setdefault("metadata", {})
        cached["metadata"].setdefault("artifact_cache_key", artifact_key)
        cached["metadata"].setdefault("ocr_identity_hash", identity_hash)
        signal, reason = _ocr_signal_strength(
            str(cached.get("text") or ""),
            list(cached.get("items") or []),
            cached.get("visual_summary"),
        )
        cached.setdefault("ocr_signal_strength", signal)
        if reason:
            cached.setdefault("ocr_empty_reason", reason)
        return cached
    existing_task = _existing_xhs_ocr_task(
        task_store,
        content_id=str(base_metadata.get("content_id") or ""),
        identity_hash=identity_hash,
    )
    if existing_task:
        status = str(existing_task.get("status") or "")
        return {
            "status": "degraded" if status in {"queued", "running"} else "ok",
            "task_id": str(existing_task.get("task_id") or task_id),
            "engine": str((existing_task.get("metadata") or {}).get("engine") or base_metadata.get("engine") or "configured_image_model"),
            "image_url": image_url,
            "image_urls": selected_urls,
            "image_selection": image_selection,
            "images_processed": len(selected_urls),
            "reason": "existing_ocr_task_reused",
            "text": "",
            "items": [],
            "ocr_signal_strength": "pending" if status in {"queued", "running"} else "result_external",
            "ocr_empty_reason": "existing_task_not_completed" if status in {"queued", "running"} else "completed_task_reused_without_inline_text",
            "elapsed_s": round(time.time() - started, 3),
            "metadata": {
                "artifact_cache_key": artifact_key,
                "ocr_identity_hash": identity_hash,
                "existing_task_status": status,
                "task_ref": compact_task_ref(existing_task),
                "recommended_next_action": "poll_get_task_status" if status in {"queued", "running"} else "reuse_completed_task",
            },
        }
    metadata = {
        **base_metadata,
        "image_url": image_url,
        "image_urls": selected_urls,
        "image_count": len(selected_urls),
        "image_selection": image_selection,
        "blocks_final_report": bool(base_metadata.get("blocks_final_report", True)),
        "result_reread_tool": base_metadata.get("result_reread_tool", "get_content_detail"),
        "approach": base_metadata.get("approach", "derived_text"),
        "media_operation": base_metadata.get("media_operation", "xhs_image_ocr"),
        "artifact_cache_key": artifact_key,
        "ocr_identity_hash": identity_hash,
    }
    task_store.upsert_task(
        task_id=task_id,
        task_type="xhs_image_ocr",
        platform="小红书",
        target=str(base_metadata.get("source_url") or image_url),
        content_id=str(base_metadata.get("content_id") or ""),
        status="queued",
        metadata=metadata,
    )

    max_wait_s = 12
    try:
        max_wait_s = int(os.environ.get("KR_XHS_OCR_SYNC_TIMEOUT_SECONDS", "8"))
    except Exception:
        max_wait_s = 8

    def _run_ocr() -> Dict:
        inner_started = time.time()
        task_store.mark_running(task_id)
        task_store.heartbeat(task_id, metadata={"phase": "download_image"})
        images_b64: List[str] = []
        for current_url in selected_urls:
            resp = httpx.get(
                current_url,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.xiaohongshu.com/"},
                timeout=20,
                follow_redirects=True,
            )
            resp.raise_for_status()
            images_b64.append(image_bytes_to_base64(resp.content))
        task_store.heartbeat(task_id, metadata={"phase": "model_call"})

        system_prompt = "你是严谨的图片OCR与视觉信息提取助手。只提取图片中可见文字和关键视觉信息，不要编造。"
        user_text = """请按图片顺序识别这些小红书图片中的文字与关键视觉信息，并输出 JSON：
{
  "text": "合并后的可读文字",
  "items": [{"text": "最重要的单条文字或结论", "score": 0.0}],
  "visual_summary": "图片中与内容理解有关的关键信息"
}
只保留不超过 12 条 items；如果没有文字，text 置空，items 为空数组；如果图片包含图表、清单、流程、截图或商品信息，请在 visual_summary 中概括。"""
        content, model_label = call_multimodal_models(
            models=configured_models("image"),
            system_prompt=system_prompt,
            user_text=user_text,
            images_base64=images_b64,
            temperature=0.1,
            max_tokens=1800,
            timeout=120,
        )
        parsed = _extract_json(content)
        items = parsed.get("items") if isinstance(parsed.get("items"), list) else []
        text = str(parsed.get("text") or "").strip()
        if not text and items:
            text = "\n".join(str(item.get("text") or "") for item in items if isinstance(item, dict)).strip()
        visual_summary = parsed.get("visual_summary", "")
        signal_strength, empty_reason = _ocr_signal_strength(text, items, visual_summary)
        elapsed = time.time() - inner_started
        log.info(f"小红书首图 OCR 完成: model={model_label}, chars={len(text)}, elapsed={elapsed:.2f}s")
        task_store.mark_completed(
            task_id,
            metadata={
                **base_metadata,
                "image_url": image_url,
                "image_urls": selected_urls,
                "image_count": len(selected_urls),
                "image_selection": image_selection,
                "engine": model_label,
                "text_chars": len(text),
                "ocr_signal_strength": signal_strength,
                "ocr_empty_reason": empty_reason,
                "elapsed_s": round(elapsed, 2),
                "artifact_cache_key": artifact_key,
                "ocr_identity_hash": identity_hash,
            },
        )
        result = {
            "status": "ok",
            "task_id": task_id,
            "engine": model_label,
            "image_url": image_url,
            "image_urls": selected_urls,
            "image_selection": image_selection,
            "images_processed": len(selected_urls),
            "text": text,
            "items": items,
            "visual_summary": visual_summary,
            "ocr_signal_strength": signal_strength,
            "elapsed_s": round(elapsed, 2),
            "metadata": {"artifact_cache_key": artifact_key, "ocr_identity_hash": identity_hash, "cache_policy": "artifact_cache_by_media_url"},
        }
        if empty_reason:
            result["ocr_empty_reason"] = empty_reason
        cache_meta = _OCR_ARTIFACT_CACHE.set(artifact_key, result)
        return attach_runtime_metadata(
            result,
            tool_name="get_content_detail",
            capability_id="xhs.detail.image_ocr",
            started=inner_started,
            budget=budget_envelope("balanced", max_sync_wait_s=max_wait_s),
            cache=cache_meta,
        )

    try:
        future = _OCR_EXECUTOR.submit(_run_ocr)
        return future.result(timeout=max_wait_s)
    except FutureTimeoutError:
        elapsed = time.time() - started
        log.warning(f"小红书首图 OCR 超过同步等待窗口，转为后台任务: task_id={task_id}, elapsed={elapsed:.2f}s")
        task_store.heartbeat(task_id, metadata={"phase": "background_model_call", "sync_timeout_s": max_wait_s})
        return {
            "status": "degraded",
            "task_id": task_id,
            "engine": str(base_metadata.get("engine") or "configured_image_model"),
            "image_url": image_url,
            "image_urls": selected_urls,
            "image_selection": image_selection,
            "images_processed": len(selected_urls),
            "reason": "ocr_running_in_background",
            "text": "",
            "items": [],
            "ocr_signal_strength": "pending",
            "ocr_empty_reason": "sync_timeout_background_task_running",
            "elapsed_s": round(elapsed, 2),
            "metadata": {
                "artifact_cache_key": artifact_key,
                "background_after_s": max_wait_s,
                "recommended_next_action": "poll_get_task_status",
            },
        }
    except Exception as e:
        elapsed = time.time() - started
        log.warning(f"小红书首图 OCR 失败: {e}")
        task_store.mark_failed(
            task_id,
            error=str(e),
            metadata={
                **base_metadata,
                "image_url": image_url,
                "image_urls": selected_urls,
                "image_count": len(selected_urls),
                "elapsed_s": round(elapsed, 2),
                "artifact_cache_key": artifact_key,
                "ocr_identity_hash": identity_hash,
            },
        )
        return {
            "status": "degraded",
            "task_id": task_id,
            "engine": str(base_metadata.get("engine") or "configured_image_model"),
            "image_url": image_url,
            "image_urls": selected_urls,
            "image_selection": image_selection,
            "error": str(e),
            "text": "",
            "items": [],
            "ocr_signal_strength": "failed",
            "ocr_empty_reason": "ocr_exception",
            "elapsed_s": round(elapsed, 2),
        }


__all__ = ["ocr_first_xhs_image"]
