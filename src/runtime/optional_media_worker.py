"""可选组件的库内处理实现；不启动模型子进程。"""

import importlib.metadata
from pathlib import Path
import time


class UnsupportedASRLanguage(ValueError):
    error_code = 'unsupported_language'

    def __init__(self, language):
        self.language = language
        super().__init__('不支持的语言：' + language + '；仅支持中文、英文及中英混合')


class ASRLanguageDetectionFailed(ValueError):
    error_code = 'asr_language_detection_failed'

    def __init__(self):
        super().__init__('asr_language_detection_failed:无法确认音频语言，未返回转写')


def _language_tokenizer(root):
    """v2.0.1专用解码视图，保留上游自动剥掉的语言前缀；不改权重或原分词器。

    只改ID 11528的显示拼写，固定auto提示不含该词、禁止语言预填和hotwords。
    模型生成ID不变；返回tokens按ID显示视图逐个还原，再解析模型语言标签。
    上游接口/资产变动须重新验证，绝不把缺失标签当中文。
    """
    import json
    import os
    import tempfile
    from runtime.media_cache import media_cache_subdir
    from runtime.optional_media import file_sha
    from runtime.file_write_lock import file_write_lock
    from runtime.model_roles import ASR_MODEL_FILES
    source = Path(root) / 'tokenizer'
    for name in ('vocab.json', 'merges.txt', 'tokenizer_config.json'):
        if file_sha(source / name) != ASR_MODEL_FILES['tokenizer/' + name]:
            raise ValueError('asr_tokenizer_identity_mismatch')
    folder = media_cache_subdir('asr_tokenizers') / ('qwen-language-view-v1-' + ASR_MODEL_FILES['tokenizer/vocab.json'][:16])
    folder.mkdir(parents=True, exist_ok=True)
    vocab = json.loads((source / 'vocab.json').read_text(encoding='utf-8'))
    if vocab.get('language') != 11528 or 'KRlanguage' in vocab:
        raise ValueError('asr_language_token_contract_mismatch')
    vocab['KRlanguage'] = vocab.pop('language')
    assets = {'vocab.json': json.dumps(vocab, ensure_ascii=False, separators=(',', ':')).encode('utf-8'),
              'merges.txt': (source / 'merges.txt').read_bytes(),
              'tokenizer_config.json': (source / 'tokenizer_config.json').read_bytes()}
    with file_write_lock(folder / 'identity'):
        for name, raw in assets.items():
            target = folder / name
            if target.is_file() and target.read_bytes() == raw:
                continue
            with tempfile.NamedTemporaryFile(dir=folder, suffix='.pending', delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(raw)
            try:
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
    return str(folder)


def _detected_transcript(result):
    """读取音频模型生成的语言，而非按正文字符或请求提示猜语言。"""
    import re
    if not result.text.strip():
        return '', ''
    # 原词恰为language时也逐token还原，避免改写其他token拼出的KRlanguage。
    raw = ''.join('language' if token == 'KRlanguage' else token for token in result.tokens)
    match = re.fullmatch(r'language ([^<>]+)<asr_text>(.*)', raw, re.DOTALL)
    if not match:
        raise ASRLanguageDetectionFailed()
    label, text = match.groups()
    # Qwen的无语音输出也是带语言头的非空字符串；None不代表一种外语。
    if label.strip() == 'None':
        if not text.strip():
            return '', ''
        raise ASRLanguageDetectionFailed()
    language = {'Chinese': 'zh', 'Cantonese': 'zh', 'English': 'en'}.get(label.strip())
    if language is None:
        raise UnsupportedASRLanguage(label.strip() or 'unknown')
    return text.strip(), language


def native_asr(request):
    """单实例CPU INT8、逐30秒块检测与转写；外语整次失败，不归档部分正文。"""
    from runtime.execution_context import check_execution, current_execution
    from runtime.media_decode import audio_chunks
    from runtime.operation_checkpoint import OperationCheckpoint
    from runtime.media_cache import media_cache_subdir
    from runtime.optional_media import file_sha
    from runtime.model_roles import ASR_ENGINE
    started = time.monotonic()
    check_execution()
    if request.get('device', 'cpu') != 'cpu':
        raise ValueError('unsupported_asr_device:CPU_INT8_required')
    model_started = time.monotonic()
    model, hit = _load_asr_model(request['model_root'], request['model_identity'], request.get('threads', 4))
    load_s = time.monotonic() - model_started
    identity = {key: request[key] for key in ('input_sha256', 'model_identity', 'language', 'device', 'threads')}
    identity.update(engine=ASR_ENGINE, chunk_seconds=30, max_tokens=1024, language_contract=3,
                    implementation=file_sha(__file__))
    checkpoint = OperationCheckpoint(media_cache_subdir('asr_checkpoints'), identity, identity)
    rows = checkpoint.read().get('chunks', [])
    reused = len(rows)
    inference_s, decode_s, count = 0., 0., 0
    decode_started = time.monotonic()
    for index, (start, samples, rate) in enumerate(audio_chunks(request['input_path'])):
        decode_s += time.monotonic() - decode_started
        end = start + len(samples) / rate
        check_execution()
        if index < reused:
            if rows[index]['start_s'] != start or rows[index]['end_s'] != end:
                raise ValueError('asr_checkpoint_chunk_identity_mismatch')
        else:
            stream = model.create_stream()
            # 始终auto；语言偏好仅用于字幕/缓存，不能预填覆盖真实音频语言。
            stream.accept_waveform(rate, samples)
            inference_started = time.monotonic()
            model.decode_stream(stream)
            inference_s += time.monotonic() - inference_started
            check_execution()  # native返回前保留租约；取消后不读回、不保存迟到块。
            result = stream.result
            if len(result.tokens) >= 1024:
                raise ValueError('asr_token_budget_exhausted_no_complete_transcript')
            text, detected = _detected_transcript(result)
            rows.append({'text': text, 'language': detected, 'start_s': start, 'end_s': end, 'kind': 'audio_chunk'})
            context = current_execution() or {}
            checkpoint.write({'chunks': rows, 'writer_attempt': context.get('attempt_id', ''),
                              'writer_execution': context.get('execution_id', '')})
        count += 1
        decode_started = time.monotonic()
    if count != len(rows):
        raise ValueError('asr_checkpoint_duration_mismatch')
    locators, cursor, parts = [], 0, []
    for row in rows:
        if not row['text']:
            continue
        parts.append(row['text'])
        locators.append({'start': cursor, 'end': cursor + len(row['text']), 'unit': 'unicode_code_point',
                         'start_s': row['start_s'], 'end_s': row['end_s'], 'kind': 'audio_chunk'})
        cursor += len(row['text']) + 1
    duration = rows[-1]['end_s'] if rows else 0
    languages = sorted({row['language'] for row in rows if row.get('language')})
    return {'status': 'acquired', 'text': '\n'.join(parts), 'source_kind': 'asr', 'component': request['component'],
        'speech_status': 'speech_detected' if parts else 'no_speech_detected' if count else 'no_decoded_audio',
        'content_locators': locators, 'timestamp_precision': 'input_chunk_only', 'word_timestamps': 'not_acquired',
        'device': 'cpu', 'dtype': 'int8', 'engine': ASR_ENGINE,
        'language_hint': request.get('language') or 'auto', 'decoding_language': 'auto',
        'detected_languages': languages, 'detected_language': languages[0] if len(languages) == 1 else 'zh-en' if languages else '',
        'language_detection': 'qwen_generated_prefix/v1',
        'asr_metrics': {'audio_duration_s': duration, 'chunks': count, 'resumed_chunks': reused,
            'model_load_s': round(load_s, 3), 'model_cache_hit': hit,
            'audio_decode_s': round(decode_s, 3), 'transcription_s': round(inference_s, 3),
            'worker_total_s': round(time.monotonic() - started, 3),
            'real_time_factor': round(inference_s / duration, 4) if duration else None}}


_ASR_MODEL = None


def _load_asr_model(root, identity, threads):
    global _ASR_MODEL
    key = (root, identity, threads)
    if _ASR_MODEL is not None and _ASR_MODEL[0] == key:
        return _ASR_MODEL[1], True
    _ASR_MODEL = None
    from sherpa_onnx import OfflineRecognizer
    root = Path(root)
    model = OfflineRecognizer.from_qwen3_asr(
        conv_frontend=str(root / 'conv_frontend.onnx'), encoder=str(root / 'encoder.int8.onnx'),
        decoder=str(root / 'decoder.int8.onnx'), tokenizer=_language_tokenizer(root),
        num_threads=threads, max_new_tokens=1024, max_total_len=4096, provider='cpu')
    _ASR_MODEL = (key, model)
    return model, False


def docling_blocks(document):
    blocks = []
    for item, _level in document.iterate_items():
        kind = str(getattr(item, "label", "text")).split(".")[-1].lower()
        text = getattr(item, "text", "")
        if kind == "table":
            text = item.export_to_markdown(doc=document)
        if not text and kind == "picture":
            text = "[图片：未提取图内文字]"
        if not text:
            continue
        provenance = list(getattr(item, "prov", []) or [])
        block = {"text": str(text), "kind": kind}
        if provenance:
            prov = provenance[0]
            block["page"] = int(prov.page_no)
            box = prov.bbox
            block["bbox"] = [float(box.l), float(box.t), float(box.r), float(box.b)]
            block["coordinate_system"] = str(getattr(box, "coord_origin", "parser_native"))
        blocks.append(block)
    return blocks


def execute(request):
    for package, version in request["expected_packages"].items():
        if importlib.metadata.version(package) != version:
            raise ValueError("package_version_mismatch")
    component = request["component"]
    source = request["input_path"]
    if component == "qwen3-asr-0.6b":
        return native_asr(request)
    if component == "docling":
        from docling.document_converter import DocumentConverter
        from docling.datamodel.base_models import InputFormat

        # Office/HTML 使用无模型解析；PDF 唯一分流由 pypdf/云端 OCR 承担。
        allowed = [InputFormat.DOCX, InputFormat.XLSX, InputFormat.PPTX, InputFormat.HTML, InputFormat.MD]
        result = DocumentConverter(allowed_formats=allowed).convert(source)
        return {
            "status": "acquired",
            "text": result.document.export_to_markdown(),
            "document_blocks": docling_blocks(result.document),
            "source_kind": "document_parse",
            "component": component,
        }
    raise ValueError("unsupported_component")
