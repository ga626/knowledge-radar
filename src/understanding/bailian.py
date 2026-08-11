"""DashScope Bailian model client.

This module implements the official DashScope ``multimodal-generation``
schema. It is intentionally small and side-effect free until a call function is
invoked, so importing capabilities can describe the provider without probing
the network.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx

from runtime.degradation import get_degradation_policy
from runtime.monitor import get_monitor_tracker
from runtime.usage_tracker import get_usage_tracker

log = logging.getLogger("mcp-server")

DEFAULT_MULTIMODAL_ENDPOINT = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
)
DEFAULT_COMPATIBLE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_VL_MODEL = "qwen3-vl-flash"
DEFAULT_TEXT_MODEL = "qwen-turbo"

MODEL_ALIASES = {
    "bailian:qwen3-vl-flash": "qwen3-vl-flash",
    "bailian:qwen3.5-omni-flash": "qwen3.5-omni-flash",
    "bailian:qwen-turbo": "qwen-turbo",
    "bailian:qwen3.5-flash": "qwen3.5-flash",
}


@dataclass(frozen=True)
class BailianResponse:
    content: str
    provider: str
    usage: Dict[str, Any]
    elapsed_s: float
    raw: Dict[str, Any]


def bailian_api_key() -> str:
    return os.environ.get("DASHSCOPE_API_KEY") or ""


def bailian_multimodal_endpoint() -> str:
    return os.environ.get("DASHSCOPE_MULTIMODAL_ENDPOINT") or DEFAULT_MULTIMODAL_ENDPOINT


def bailian_compatible_base_url() -> str:
    return (os.environ.get("DASHSCOPE_COMPATIBLE_BASE_URL") or DEFAULT_COMPATIBLE_BASE_URL).rstrip("/")


def normalize_model_id(model: str | None, *, default: str = DEFAULT_VL_MODEL) -> str:
    value = (model or default).strip()
    return MODEL_ALIASES.get(value, MODEL_ALIASES.get(value.lower(), value))


def _headers() -> Dict[str, str]:
    api_key = bailian_api_key()
    if not api_key:
        raise RuntimeError("DashScope API key is not configured")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _extract_content(data: Dict[str, Any]) -> str:
    choices = ((data.get("output") or {}).get("choices") or [])
    texts: list[str] = []
    for choice in choices:
        message = (choice or {}).get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
            continue
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("text"):
                    texts.append(str(part.get("text")))
                elif isinstance(part, str):
                    texts.append(part)
    return "\n".join(text for text in texts if text).strip()


def _extract_chat_content(data: Dict[str, Any]) -> str:
    choices = data.get("choices") or []
    texts: list[str] = []
    for choice in choices:
        message = (choice or {}).get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("text"):
                    texts.append(str(part.get("text")))
    return "\n".join(text for text in texts if text).strip()


def _normalise_usage(data: Dict[str, Any]) -> Dict[str, Any]:
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    prompt_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    cached_tokens = 0
    prompt_details = usage.get("prompt_tokens_details")
    if isinstance(prompt_details, dict):
        cached_tokens = int(prompt_details.get("cached_tokens") or 0)
    return {
        **usage,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": int(usage.get("total_tokens") or prompt_tokens + completion_tokens),
        "cached_tokens": cached_tokens,
    }


def _media_part(media_type: str, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    media_type = media_type.strip().lower()
    if media_type not in {"image", "video", "audio"}:
        raise ValueError(f"unsupported Bailian media type: {media_type}")
    if not url:
        raise RuntimeError(f"Bailian {media_type} URL is empty")
    part = {media_type: url}
    part.update(params or {})
    return part


def build_multimodal_payload(
    *,
    model: str,
    prompt: str,
    system_prompt: str = "",
    image_urls: Iterable[str] = (),
    video_urls: Iterable[str] = (),
    audio_urls: Iterable[str] = (),
    media_params: Optional[Dict[str, Any]] = None,
    max_tokens: int = 1024,
    temperature: float | None = None,
) -> Dict[str, Any]:
    content: List[Dict[str, Any]] = []
    if system_prompt:
        content.append({"text": system_prompt})
    for url in image_urls:
        content.append(_media_part("image", url, media_params))
    for url in video_urls:
        content.append(_media_part("video", url, media_params))
    for url in audio_urls:
        content.append(_media_part("audio", url, media_params))
    content.append({"text": prompt})
    parameters: Dict[str, Any] = {"max_tokens": int(max_tokens)}
    if temperature is not None:
        parameters["temperature"] = float(temperature)
    return {
        "model": normalize_model_id(model),
        "input": {"messages": [{"role": "user", "content": content}]},
        "parameters": parameters,
    }


def _post_multimodal_generation(
    payload: Dict[str, Any],
    *,
    timeout: float,
) -> BailianResponse:
    started = time.perf_counter()
    resp = httpx.post(
        bailian_multimodal_endpoint(),
        headers=_headers(),
        json=payload,
        timeout=timeout,
    )
    elapsed_s = time.perf_counter() - started
    if resp.status_code != 200:
        raise RuntimeError(f"DashScope {payload.get('model')} HTTP {resp.status_code}: {resp.text[:240]}")
    data = resp.json()
    usage = _normalise_usage(data)
    return BailianResponse(
        content=_extract_content(data),
        provider=f"Bailian {payload.get('model')}",
        usage=usage,
        elapsed_s=elapsed_s,
        raw=data,
    )


def _post_chat_completion(
    payload: Dict[str, Any],
    *,
    timeout: float,
) -> BailianResponse:
    started = time.perf_counter()
    resp = httpx.post(
        f"{bailian_compatible_base_url()}/chat/completions",
        headers=_headers(),
        json=payload,
        timeout=timeout,
    )
    elapsed_s = time.perf_counter() - started
    if resp.status_code != 200:
        raise RuntimeError(f"DashScope {payload.get('model')} HTTP {resp.status_code}: {resp.text[:240]}")
    data = resp.json()
    usage = _normalise_usage(data)
    return BailianResponse(
        content=_extract_chat_content(data),
        provider=f"Bailian {payload.get('model')}",
        usage=usage,
        elapsed_s=elapsed_s,
        raw=data,
    )


def call_multimodal_generation(
    *,
    model: str = DEFAULT_VL_MODEL,
    prompt: str,
    system_prompt: str = "你是一个严谨的多模态资料分析助手。只根据输入媒体和文字回答。",
    image_urls: Iterable[str] = (),
    video_urls: Iterable[str] = (),
    audio_urls: Iterable[str] = (),
    media_params: Optional[Dict[str, Any]] = None,
    max_tokens: int = 1024,
    temperature: float | None = None,
    timeout: float = 120,
    capability: str = "native_video",
) -> Tuple[str, str, Dict[str, Any]]:
    """Call Bailian official multimodal-generation and track usage."""
    request_model = normalize_model_id(model)
    image_url_list = list(image_urls)
    video_url_list = list(video_urls)
    audio_url_list = list(audio_urls)
    payload = build_multimodal_payload(
        model=request_model,
        prompt=prompt,
        system_prompt=system_prompt,
        image_urls=image_url_list,
        video_urls=video_url_list,
        audio_urls=audio_url_list,
        media_params=media_params,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    breaker_key = f"model:bailian:{capability}:{request_model}"
    policy = get_degradation_policy()
    breaker = policy.is_open(breaker_key)
    if breaker.get("open"):
        raise RuntimeError(f"Bailian breaker is open for {request_model}: {breaker.get('last_reason') or ''}")
    try:
        log.info("[bailian] Calling %s via multimodal-generation", request_model)
        result = policy.retry_with_jitter(
            breaker_key,
            "model",
            lambda: _post_multimodal_generation(payload, timeout=timeout),
            retryable_exceptions=(httpx.HTTPError, RuntimeError),
            metadata={"model": request_model, "capability": capability, "provider": "bailian"},
        )
        if not result.content.strip():
            raise RuntimeError(f"Bailian {request_model} returned empty content")
        get_usage_tracker().record(
            model=request_model,
            capability=capability,
            prompt_tokens=int(result.usage.get("prompt_tokens") or 0),
            completion_tokens=int(result.usage.get("completion_tokens") or 0),
            total_tokens=int(result.usage.get("total_tokens") or 0) or None,
            metadata={
                "provider": "bailian",
                "schema": "multimodal-generation",
                "cached_tokens": int(result.usage.get("cached_tokens") or 0),
                "input_images": len(image_url_list),
                "input_videos": len(video_url_list),
                "input_audios": len(audio_url_list),
                "elapsed_s": round(result.elapsed_s, 3),
            },
        )
        get_monitor_tracker().record(
            scope="model_provider",
            name=f"bailian:{capability}",
            success=True,
            latency_ms=int(result.elapsed_s * 1000),
            metadata={"model": request_model, "schema": "multimodal-generation"},
        )
        return result.content, result.provider, result.usage
    except Exception as exc:
        log.warning("[bailian] %s failed: %s", request_model, exc)
        policy.record_degradation(
            "model",
            breaker_key,
            str(exc),
            {"model": request_model, "capability": capability, "provider": "bailian"},
        )
        raise


def call_text_model(
    *,
    system_prompt: str,
    user_prompt: str,
    model: str = DEFAULT_TEXT_MODEL,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    timeout: float = 120,
    capability: str = "text_analysis",
) -> Tuple[str, str, Dict[str, Any]]:
    """Call DashScope OpenAI-compatible chat completions for text-only work."""
    request_model = normalize_model_id(model, default=DEFAULT_TEXT_MODEL)
    payload = {
        "model": request_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
    }
    breaker_key = f"model:bailian:{capability}:{request_model}"
    policy = get_degradation_policy()
    breaker = policy.is_open(breaker_key)
    if breaker.get("open"):
        raise RuntimeError(f"Bailian breaker is open for {request_model}: {breaker.get('last_reason') or ''}")
    try:
        log.info("[bailian] Calling %s via chat/completions", request_model)
        result = policy.retry_with_jitter(
            breaker_key,
            "model",
            lambda: _post_chat_completion(payload, timeout=timeout),
            retryable_exceptions=(httpx.HTTPError, RuntimeError),
            metadata={"model": request_model, "capability": capability, "provider": "bailian"},
        )
        if not result.content.strip():
            raise RuntimeError(f"Bailian {request_model} returned empty content")
        get_usage_tracker().record(
            model=request_model,
            capability=capability,
            prompt_tokens=int(result.usage.get("prompt_tokens") or 0),
            completion_tokens=int(result.usage.get("completion_tokens") or 0),
            total_tokens=int(result.usage.get("total_tokens") or 0) or None,
            metadata={
                "provider": "bailian",
                "schema": "openai-compatible",
                "cached_tokens": int(result.usage.get("cached_tokens") or 0),
                "elapsed_s": round(result.elapsed_s, 3),
            },
        )
        get_monitor_tracker().record(
            scope="model_provider",
            name=f"bailian:{capability}",
            success=True,
            latency_ms=int(result.elapsed_s * 1000),
            metadata={"model": request_model, "schema": "openai-compatible"},
        )
        return result.content, result.provider, result.usage
    except Exception as exc:
        log.warning("[bailian] %s text call failed: %s", request_model, exc)
        policy.record_degradation(
            "model",
            breaker_key,
            str(exc),
            {"model": request_model, "capability": capability, "provider": "bailian"},
        )
        raise


def call_image_url_model(
    image_url: str,
    *,
    model: str = DEFAULT_VL_MODEL,
    prompt: str,
    max_tokens: int = 1024,
    timeout: float = 120,
) -> Tuple[str, str, Dict[str, Any]]:
    return call_multimodal_generation(
        model=model,
        prompt=prompt,
        image_urls=[image_url],
        max_tokens=max_tokens,
        timeout=timeout,
        capability="frame_vision",
    )


def call_frame_images_model(
    images_base64: Iterable[str],
    *,
    model: str = DEFAULT_VL_MODEL,
    prompt: str,
    system_prompt: str = "你是一个严谨的视频帧分析助手。只根据输入截图和文字回答。",
    max_tokens: int = 2048,
    timeout: float = 180,
) -> Tuple[str, str, Dict[str, Any]]:
    image_urls = [
        item if item.strip().startswith("data:") else f"data:image/jpeg;base64,{item.strip()}"
        for item in images_base64
        if item and item.strip()
    ]
    if not image_urls:
        raise RuntimeError("Bailian frame vision requires at least one image frame")
    return call_multimodal_generation(
        model=model,
        prompt=prompt,
        system_prompt=system_prompt,
        image_urls=image_urls,
        max_tokens=max_tokens,
        timeout=timeout,
        capability="frame_vision",
    )


def call_video_url_model(
    video_url: str,
    *,
    model: str = DEFAULT_VL_MODEL,
    prompt: str,
    max_frames: int | None = None,
    fps: float | None = None,
    max_pixels: int | None = None,
    timeout: float = 120,
    max_tokens: int = 1024,
) -> Tuple[str, str, Dict[str, Any]]:
    media_params: Dict[str, Any] = {}
    if max_frames is not None:
        media_params["num_frames"] = int(max_frames)
    if fps is not None:
        media_params["fps"] = float(fps)
    if max_pixels is not None:
        media_params["max_pixels"] = int(max_pixels)
    return call_multimodal_generation(
        model=model,
        prompt=prompt,
        video_urls=[video_url],
        media_params=media_params,
        max_tokens=max_tokens,
        timeout=timeout,
        capability="native_video",
    )


def smoke_video_url(
    video_url: str,
    *,
    model: str = DEFAULT_VL_MODEL,
    prompt: str = "请用一句话说明这个视频画面是否可以被访问和理解。",
    timeout: float = 90,
    max_tokens: int = 128,
) -> Dict[str, Any]:
    if not video_url:
        return {"status": "bad_request", "reason": "video_url is empty"}
    if not bailian_api_key():
        return {"status": "skipped", "reason": "DashScope API key is not configured"}
    try:
        content, provider, usage = call_video_url_model(
            video_url,
            model=model,
            prompt=prompt,
            timeout=timeout,
            max_tokens=max_tokens,
        )
        return {
            "status": "ok" if content.strip() else "empty",
            "model": provider.replace("Bailian ", ""),
            "content_preview": content.strip()[:200],
            "usage": usage,
        }
    except Exception as exc:
        return {
            "status": "failed",
            "model": normalize_model_id(model),
            "reason": str(exc)[:240],
        }
