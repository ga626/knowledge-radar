"""SiliconFlow OpenAI-compatible model client."""

from __future__ import annotations

import base64
import logging
import os
from typing import Dict, Iterable, List, Optional, Tuple

import httpx

from runtime.monitor import get_monitor_tracker
from runtime.degradation import get_degradation_policy
from runtime.usage_tracker import get_usage_tracker

log = logging.getLogger("mcp-server")

DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_IMAGE_MODELS = ("PaddlePaddle/PaddleOCR-VL-1.5", "Qwen/Qwen3-VL-8B-Instruct")
DEFAULT_VIDEO_MODELS = ("Qwen/Qwen3-VL-8B-Instruct", "Qwen/Qwen3-VL-30B-A3B-Instruct")
MODEL_ALIASES = {
    "paddleocr-vl-1.5": "PaddlePaddle/PaddleOCR-VL-1.5",
    "qwen3-vl-8b-instruct": "Qwen/Qwen3-VL-8B-Instruct",
    "qwen3-vl-30b-a3b-instruct": "Qwen/Qwen3-VL-30B-A3B-Instruct",
    "qwen3-vl-30b-a3b-thinking": "Qwen/Qwen3-VL-30B-A3B-Thinking",
    "qwen3.5-122b-a10b": "Qwen/Qwen3.5-122B-A10B",
    "qwen3.5-397b-a17b": "Qwen/Qwen3.5-397B-A17B",
    "qwen3.6-35b-a3b": "Qwen/Qwen3.6-35B-A3B",
    "qwen3.6-27b": "Qwen/Qwen3.6-27B",
}

def siliconflow_base_url() -> str:
    return (os.environ.get("SILICONFLOW_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def siliconflow_api_key() -> str:
    return os.environ.get("SILICONFLOW_API_KEY", "")


def configured_models(capability: str) -> List[str]:
    if capability == "image":
        env_values = [os.environ.get("KR_OCR_MODELS"), os.environ.get("KR_IMAGE_MODELS")]
        defaults = list(DEFAULT_IMAGE_MODELS)
    elif capability == "video":
        env_values = [os.environ.get("KR_FRAME_VISION_MODELS"), os.environ.get("KR_VIDEO_MODELS")]
        defaults = list(DEFAULT_VIDEO_MODELS)
    elif capability == "native_video":
        env_values = [os.environ.get("KR_NATIVE_VIDEO_MODELS"), os.environ.get("KR_VIDEO_MODELS")]
        defaults = ["Qwen/Qwen3-VL-30B-A3B-Instruct", "Qwen/Qwen3-VL-8B-Instruct"]
    elif capability == "native_audio_video":
        env_values = [os.environ.get("KR_NATIVE_AUDIO_VIDEO_MODELS")]
        defaults = ["Qwen/Qwen3-Omni-30B-A3B-Instruct"]
    elif capability == "asr":
        env_values = [os.environ.get("KR_ASR_MODELS")]
        defaults = ["local:faster-whisper/base", "local:faster-whisper/tiny"]
    elif capability == "basic_text":
        env_values = [os.environ.get("KR_BASIC_TEXT_MODELS")]
        defaults = []
    else:
        env_values = []
        defaults = []

    for env_value in env_values:
        configured = _filter_siliconflow_model_ids(env_value)
        if configured:
            return configured

    return defaults


def _filter_siliconflow_model_ids(value: str | None) -> List[str]:
    models: List[str] = []
    for raw in (value or "").split(","):
        item = raw.strip()
        if not item:
            continue
        lowered = item.lower()
        if lowered.startswith("siliconflow:"):
            model_id = item.split(":", 1)[1].strip()
            if model_id:
                models.append(model_id)
            continue
        if ":" in item:
            continue
        models.append(item)
    return models


def normalize_model_id(model: str) -> str:
    return MODEL_ALIASES.get(model, MODEL_ALIASES.get(model.lower(), model))


def _image_part_from_base64(image_base64: str, mime_type: str = "image/jpeg") -> Dict:
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime_type};base64,{image_base64}"},
    }


def _post_chat_completion(
    model: str,
    messages: List[Dict],
    *,
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> Tuple[str, Dict]:
    api_key = siliconflow_api_key()
    if not api_key:
        raise RuntimeError("SiliconFlow API key is not configured")

    resp = httpx.post(
        f"{siliconflow_base_url()}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"SiliconFlow {model} HTTP {resp.status_code}: {resp.text[:240]}")
    data = resp.json()
    content = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return content, usage


def call_multimodal_models(
    *,
    models: Iterable[str],
    system_prompt: str,
    user_text: str,
    images_base64: List[str],
    temperature: float = 0.2,
    max_tokens: int = 4096,
    timeout: float = 300,
) -> Tuple[str, str]:
    last_error: Optional[Exception] = None
    tracker = get_usage_tracker()
    monitor = get_monitor_tracker()
    policy = get_degradation_policy()
    content_parts = [{"type": "text", "text": user_text}]
    content_parts.extend(_image_part_from_base64(image) for image in images_base64)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content_parts},
    ]

    models_list = list(models)
    for index, model in enumerate(models_list):
        breaker_key = f"model:image:{model}"
        breaker = policy.is_open(breaker_key)
        if breaker.get("open"):
            policy.record_degradation("model", breaker_key, breaker.get("last_reason") or "circuit breaker open", {"model": model, "capability": "image"})
            continue
        try:
            request_model = normalize_model_id(model)
            log.info(f"[siliconflow] Calling {request_model} ({len(images_base64)} image/frame input)")
            content, usage = policy.retry_with_jitter(
                breaker_key,
                "model",
                lambda: _post_chat_completion(
                    request_model,
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=timeout,
                ),
                retryable_exceptions=(httpx.HTTPError, RuntimeError),
                metadata={"model": model, "capability": "image"},
            )
            if content.strip():
                tracker.record(
                    model=model,
                    capability="image",
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                    total_tokens=int(usage.get("total_tokens") or 0) or None,
                    metadata={"provider": "siliconflow", "input_images": len(images_base64)},
                )
                if index > 0:
                    monitor.record(
                        scope="model_fallback",
                        name="image",
                        success=True,
                        fallback_count=1,
                        metadata={"model": request_model, "provider": "siliconflow", "configured_model": model},
                    )
                return content, f"SiliconFlow {request_model}"
            last_error = RuntimeError(f"SiliconFlow {model} returned empty content")
        except Exception as exc:
            last_error = exc
            log.warning(f"[siliconflow] {model} failed: {exc}")
            policy.record_degradation("model", breaker_key, str(exc), {"model": model, "capability": "image"})
    policy.record_dead_letter(
        "model_call",
        f"image:{models_list[0] if models_list else 'unknown'}",
        "all siliconflow multimodal models failed",
        payload={"models": models_list, "system_prompt": system_prompt[:240], "user_text": user_text[:240]},
        metadata={"last_error": str(last_error) if last_error else ""},
    )
    raise RuntimeError(f"All SiliconFlow models failed. Last: {last_error}")


def call_text_models(
    *,
    models: Iterable[str],
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    timeout: float = 120,
) -> Tuple[str, str]:
    last_error: Optional[Exception] = None
    tracker = get_usage_tracker()
    monitor = get_monitor_tracker()
    policy = get_degradation_policy()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    models_list = list(models)
    for index, model in enumerate(models_list):
        breaker_key = f"model:text:{model}"
        breaker = policy.is_open(breaker_key)
        if breaker.get("open"):
            policy.record_degradation("model", breaker_key, breaker.get("last_reason") or "circuit breaker open", {"model": model, "capability": "text"})
            continue
        try:
            request_model = normalize_model_id(model)
            log.info(f"[siliconflow] Calling text model {request_model}")
            content, usage = policy.retry_with_jitter(
                breaker_key,
                "model",
                lambda: _post_chat_completion(
                    request_model,
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=timeout,
                ),
                retryable_exceptions=(httpx.HTTPError, RuntimeError),
                metadata={"model": model, "capability": "text"},
            )
            if content.strip():
                tracker.record(
                    model=model,
                    capability="text",
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                    total_tokens=int(usage.get("total_tokens") or 0) or None,
                    metadata={"provider": "siliconflow"},
                )
                if index > 0:
                    monitor.record(
                        scope="model_fallback",
                        name="text",
                        success=True,
                        fallback_count=1,
                        metadata={"model": request_model, "provider": "siliconflow", "configured_model": model},
                    )
                return content, f"SiliconFlow {request_model}"
            last_error = RuntimeError(f"SiliconFlow {model} returned empty content")
        except Exception as exc:
            last_error = exc
            log.warning(f"[siliconflow] {model} text call failed: {exc}")
            policy.record_degradation("model", breaker_key, str(exc), {"model": model, "capability": "text"})
    policy.record_dead_letter(
        "model_call",
        f"text:{models_list[0] if models_list else 'unknown'}",
        "all siliconflow text models failed",
        payload={"models": models_list, "system_prompt": system_prompt[:240], "user_prompt": user_prompt[:240]},
        metadata={"last_error": str(last_error) if last_error else ""},
    )
    raise RuntimeError(f"All SiliconFlow text models failed. Last: {last_error}")


def _direct_media_part(media_type: str, media_url: str, params: Dict | None = None) -> Dict:
    media_type = str(media_type or "").strip().lower()
    if media_type not in {"video", "audio"}:
        raise ValueError(f"unsupported direct media type: {media_type}")
    key = f"{media_type}_url"
    payload = {"url": media_url}
    payload.update(params or {})
    return {"type": key, key: payload}


def call_direct_media_url_model(
    *,
    media_url: str,
    media_type: str,
    models: Iterable[str] | None = None,
    prompt: str,
    system_prompt: str = "你是一个严谨的多模态资料分析助手。只根据输入媒体和文字回答。",
    temperature: float = 0.1,
    max_tokens: int = 1024,
    timeout: float = 120,
    media_params: Dict | None = None,
) -> Tuple[str, str, Dict]:
    """Call a SiliconFlow OpenAI-compatible model with video_url/audio_url input."""
    if not media_url:
        raise RuntimeError(f"SiliconFlow {media_type}_url is empty")
    capability = "native_audio_video" if media_type == "audio" else "native_video"
    models_list = list(models or configured_models(capability))
    if not models_list:
        raise RuntimeError(f"No SiliconFlow models configured for {capability}")

    tracker = get_usage_tracker()
    monitor = get_monitor_tracker()
    policy = get_degradation_policy()
    last_error: Optional[Exception] = None
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                _direct_media_part(media_type, media_url, media_params),
            ],
        },
    ]

    for index, model in enumerate(models_list):
        breaker_key = f"model:{media_type}_url:{model}"
        breaker = policy.is_open(breaker_key)
        if breaker.get("open"):
            policy.record_degradation(
                "model",
                breaker_key,
                breaker.get("last_reason") or "circuit breaker open",
                {"model": model, "capability": capability},
            )
            continue
        try:
            request_model = normalize_model_id(model)
            log.info(f"[siliconflow] Calling {request_model} ({media_type}_url input)")
            content, usage = policy.retry_with_jitter(
                breaker_key,
                "model",
                lambda: _post_chat_completion(
                    request_model,
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=timeout,
                ),
                retryable_exceptions=(httpx.HTTPError, RuntimeError),
                metadata={"model": model, "capability": capability, "media_type": media_type},
            )
            if content.strip():
                tracker.record(
                    model=model,
                    capability=capability,
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                    total_tokens=int(usage.get("total_tokens") or 0) or None,
                    metadata={
                        "provider": "siliconflow",
                        "media_type": media_type,
                        "input_url": True,
                        "media_params": media_params or {},
                    },
                )
                if index > 0:
                    monitor.record(
                        scope="model_fallback",
                        name=capability,
                        success=True,
                        fallback_count=1,
                        metadata={"model": request_model, "provider": "siliconflow", "configured_model": model},
                    )
                return content, f"SiliconFlow {request_model}", usage
            last_error = RuntimeError(f"SiliconFlow {model} returned empty content")
        except Exception as exc:
            last_error = exc
            log.warning(f"[siliconflow] {model} {media_type}_url call failed: {exc}")
            policy.record_degradation("model", breaker_key, str(exc), {"model": model, "capability": capability})

    policy.record_dead_letter(
        "model_call",
        f"{media_type}_url:{models_list[0] if models_list else 'unknown'}",
        "all siliconflow direct media models failed",
        payload={"models": models_list, "prompt": prompt[:240], "media_type": media_type},
        metadata={"last_error": str(last_error) if last_error else ""},
    )
    raise RuntimeError(f"All SiliconFlow direct media models failed. Last: {last_error}")


def call_video_url_model(
    video_url: str,
    *,
    models: Iterable[str] | None = None,
    prompt: str,
    max_frames: int | None = None,
    fps: float | None = None,
    max_pixels: int | None = None,
    timeout: float = 120,
    max_tokens: int = 1024,
) -> Tuple[str, str, Dict]:
    media_params: Dict[str, object] = {}
    if max_frames is not None:
        media_params["num_frames"] = int(max_frames)
    if fps is not None:
        media_params["fps"] = float(fps)
    if max_pixels is not None:
        media_params["max_pixels"] = int(max_pixels)
    return call_direct_media_url_model(
        media_url=video_url,
        media_type="video",
        models=models,
        prompt=prompt,
        max_tokens=max_tokens,
        timeout=timeout,
        media_params=media_params,
    )


def call_audio_url_model(
    audio_url: str,
    *,
    models: Iterable[str] | None = None,
    prompt: str,
    timeout: float = 120,
    max_tokens: int = 1024,
) -> Tuple[str, str, Dict]:
    return call_direct_media_url_model(
        media_url=audio_url,
        media_type="audio",
        models=models,
        prompt=prompt,
        max_tokens=max_tokens,
        timeout=timeout,
    )


def smoke_video_url(
    video_url: str,
    *,
    model: str | None = None,
    prompt: str = "请用一句话说明这个视频画面是否可以被访问和理解。",
    timeout: float = 90,
    max_tokens: int = 128,
) -> Dict:
    """Probe OpenAI-compatible ``video_url`` support without entering the main chain."""
    if not video_url:
        return {"status": "bad_request", "reason": "video_url is empty"}
    if not siliconflow_api_key():
        return {"status": "skipped", "reason": "SiliconFlow API key is not configured"}
    selected_model = normalize_model_id(model or (configured_models("native_video") or ["Qwen/Qwen3-VL-30B-A3B-Instruct"])[0])
    try:
        content, provider, usage = call_video_url_model(
            video_url,
            models=[selected_model],
            prompt=prompt,
            timeout=timeout,
            max_tokens=max_tokens,
        )
        return {
            "status": "ok" if content.strip() else "empty",
            "model": provider.replace("SiliconFlow ", ""),
            "content_preview": content.strip()[:200],
            "usage": usage,
        }
    except Exception as exc:
        return {
            "status": "failed",
            "model": selected_model,
            "reason": str(exc)[:240],
        }


def image_bytes_to_base64(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode("ascii")
