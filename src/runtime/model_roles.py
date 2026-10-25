"""产品模型职责的唯一来源；读取此表不会下载或加载模型。"""

from __future__ import annotations

ASR_COMPONENT = "qwen3-asr-0.6b"
ASR_MODEL_REF = "local:qwen3-asr/0.6b-hf"
OCR_COMPONENT = "paddleocr-vl-1.6"
CLOUD_MODEL = "qwen3.8-omni-flash"
CLOUD_MODEL_REF = "bailian:" + CLOUD_MODEL

MODEL_ENVIRONMENT = {
    "KR_ASR_MODELS": ASR_MODEL_REF,
    "KR_OCR_MODELS": OCR_COMPONENT,
    "KR_FRAME_VISION_MODELS": CLOUD_MODEL_REF,
    "KR_NATIVE_VIDEO_MODELS": CLOUD_MODEL_REF,
    "KR_NATIVE_AUDIO_VIDEO_MODELS": CLOUD_MODEL_REF,
    "KR_PROVIDER_CONCURRENCY_BAILIAN": "1",
}
RETIRED_MODEL_CONTROLS = {
    "KR_COMMENT_FILTER_MODELS", "KR_ENABLE_SILICONFLOW_FALLBACK", "KR_ENABLE_MIMO_FALLBACK",
    "KR_WHISPER_MODEL_DIR", "KR_ASR_BATCH_SIZE", "KR_ASR_BEAM_SIZE", "KR_ASR_VAD",
    "KR_ASR_COMPUTE_TYPE", "KR_ASR_LIFECYCLE", "KR_ASR_PREWARM_TRIGGER",
    "KR_ASR_GPU_HOT_TTL_SECONDS", "KR_ASR_WORKER_KEEPALIVE_SECONDS",
    "KR_ASR_GPU_UNLOAD_POLICY", "KR_ASR_ENABLE_CTRANSLATE2_UNLOAD", "KR_ASR_GPU_MIN_FREE_MB",
}

COMPONENTS = {
    ASR_COMPONENT: {
        "label": "本地语音转写（Qwen3-ASR 0.6B）",
        "package": "transformers",
        "model": "Qwen/Qwen3-ASR-0.6B-hf",
        "kind": "asr",
        "default": True,
        "source": "https://github.com/QwenLM/Qwen3-ASR",
        "weight_source": "https://huggingface.co/Qwen/Qwen3-ASR-0.6B-hf",
        "weight_revision": "7f1569a48a89f3e3f4dc3a5c9d28bddd903bc76c",
        "requirements": ["transformers==5.20.2", "torch==2.9.1", "soundfile==0.13.1", "librosa==0.11.0"],
        "requirements_lock": "config/optional-asr-win-py312.lock",
        "requirements_sha256": "696fbe9beb60197acd6fc6823faad60a29dc30d468839f5f61ca2e726680c9e5",
        "code_license": "Apache-2.0",
        "weight_license": "Apache-2.0",
        "model_required": True,
    },
    "docling": {
        "label": "Office 文档解析",
        "package": "docling",
        "model": "none",
        "kind": "document",
        "default": True,
        "source": "https://github.com/docling-project/docling",
        "requirements": ["docling==2.142.1"],
        "requirements_lock": "config/optional-docling-win-py312.lock",
        "requirements_sha256": "b60c5a0e837bf2a5aedae27a12139099d933a97cec5135b544ebe7f9a2af14f0",
        "code_license": "MIT",
        "weight_license": "not_applicable",
        "model_required": False,
        "weight_revision": "not_applicable_no_models",
    },
    OCR_COMPONENT: {
        "label": "扫描件与复杂文档识别（PaddleOCR-VL 1.6）",
        "package": "paddleocr",
        "model": "PaddlePaddle/PaddleOCR-VL-1.6",
        "kind": "document",
        "default": True,
        "source": "https://github.com/PaddlePaddle/PaddleOCR",
        "weight_source": "https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6",
        "weight_revision": "c5630abae1d940eafe0697512a0325494b02ab42",
        "layout_model": "PaddlePaddle/PP-DocLayoutV3",
        "layout_revision": "241f8bdfc77a7c7bee915a5057aaee58c235a8d3",
        "requirements": ["paddleocr[doc-parser]==3.7.0", "paddlepaddle==3.3.1"],
        "requirements_lock": "config/optional-ocr-win-py312.lock",
        "requirements_sha256": "2bf2720a39decf321fd411aea7a87ccb91e189bea64d2cd193b38d1368f6b9f4",
        "code_license": "Apache-2.0",
        "weight_license": "Apache-2.0",
        "model_required": True,
    },
}


def role_summary():
    return {
        "asr": {"component": ASR_COMPONENT, "model_ref": ASR_MODEL_REF},
        "office": {"component": "docling", "downloads_models": False},
        "pdf_text": {"component": "pypdf", "downloads_models": False},
        "ocr": {"component": OCR_COMPONENT, "model_ref": COMPONENTS[OCR_COMPONENT]["model"]},
        "cloud_media": {"provider": "bailian", "model_ref": CLOUD_MODEL_REF},
        "local_heavy_concurrency": 1,
    }
