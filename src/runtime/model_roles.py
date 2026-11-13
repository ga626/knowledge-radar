"""产品模型职责的唯一来源；读取此表不会下载或加载模型。"""

from __future__ import annotations

ASR_COMPONENT = "qwen3-asr-0.6b"
ASR_MODEL_REF = "local:qwen3-asr/0.6b-onnx-int8"
ASR_ENGINE = "sherpa_onnx_int8"
ASR_MODEL_FILES = {
    "conv_frontend.onnx": "d22dc4423e0940e49884e903d2ea2f7e5567c14fc1aed97e4e26d6b8f208ef9e",
    "encoder.int8.onnx": "60748d3e6744a57c9c91e1b17424a6c2990567e8adceb0783940c03ed98fa9d9",
    "decoder.int8.onnx": "4f6885be5959ae26af3089d38ee7972c5fafbeeb1cf8d5e76eab6d8b61ca5771",
    "tokenizer/vocab.json": "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910",
    "tokenizer/merges.txt": "8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5",
    "tokenizer/tokenizer_config.json": "4942d005604266809309cabc9f4e9cb89ce855d59b14681fdc0e1cc62ea26c4c",
}
CLOUD_OCR_COMPONENT = "siliconflow-ocr"
CLOUD_OCR_MODEL = "PaddlePaddle/PaddleOCR-VL-1.5"
OCR_MODEL_REF = "siliconflow:" + CLOUD_OCR_MODEL
CLOUD_MODEL = "qwen3.8-omni-flash"
CLOUD_MODEL_REF = "bailian:" + CLOUD_MODEL

MODEL_ENVIRONMENT = {
    "KR_ASR_MODELS": ASR_MODEL_REF,
    "KR_OCR_MODELS": OCR_MODEL_REF,
    "KR_FRAME_VISION_MODELS": CLOUD_MODEL_REF,
    "KR_NATIVE_VIDEO_MODELS": CLOUD_MODEL_REF,
    "KR_NATIVE_AUDIO_VIDEO_MODELS": CLOUD_MODEL_REF,
    "KR_PROVIDER_CONCURRENCY_BAILIAN": "1",
}


COMPONENTS = {
    ASR_COMPONENT: {
        "label": "本地语音转写（Qwen3-ASR 0.6B · sherpa INT8）",
        "package": "sherpa-onnx",
        "model": "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25",
        "kind": "asr",
        "engine": ASR_ENGINE,
        "languages": ["zh", "en", "zh-en"],
        "language_detection": "qwen_generated_prefix/v1",
        "device": "cpu",
        "compute_type": "int8",
        "default": True,
        "source": "https://github.com/QwenLM/Qwen3-ASR",
        "weight_source": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25.tar.bz2",
        "weight_revision": "sha256:393f8a14e2f5fb96746aaab342997a40641001fbd5bf9592a080a8329178ee96",
        "model_files": ASR_MODEL_FILES,
        "requirements": ["sherpa-onnx==2.0.1", "sherpa-onnx-core==2.0.1"],
        "requirements_lock": "config/optional-asr-win-py312.lock",
        "requirements_sha256": "1109a12243807b78c65636d2319eac7f4bbfdadfd5c03c02345dc7187d58b09e",
        "code_license": "Apache-2.0",
        "weight_license": "Apache-2.0",
        "model_required": True,
    },
    "docling": {
        "label": "Office 文档解析",
        "package": "docling-slim",
        "model": "none",
        "kind": "document",
        "default": True,
        "source": "https://github.com/docling-project/docling",
        "requirements": ["docling-slim==2.148.0"],
        "extras": ["format-office", "format-web", "convert-core"],
        "requirements_lock": "config/optional-docling-win-py312.lock",
        "requirements_sha256": "de1c7699e95b274b78d97273daa597e7df031e588ae138135831cf7cc7091212",
        "code_license": "MIT",
        "weight_license": "not_applicable",
        "model_required": False,
        "weight_revision": "not_applicable_no_models",
    },

}


def role_summary():
    return {
        "asr": {"component": ASR_COMPONENT, "model_ref": ASR_MODEL_REF},
        "office": {"component": "docling", "downloads_models": False},
        "pdf_text": {"component": "pypdf", "downloads_models": False},
        "ocr": {"component": CLOUD_OCR_COMPONENT, "provider": "siliconflow", "model_ref": OCR_MODEL_REF,
                "automatic_cpu_fallback": False},
        "cloud_media": {"provider": "bailian", "model_ref": CLOUD_MODEL_REF},
        "local_heavy_concurrency": 1,
    }
