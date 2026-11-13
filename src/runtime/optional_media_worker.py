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


def asr_implementation_identity():
    """只绑定转写算法；Office等无关实现的改动不废弃音频断点。"""
    import hashlib
    import inspect
    from runtime.media_decode import audio_chunks
    return hashlib.sha256('\n'.join(inspect.getsource(fn) for fn in (
        _detected_transcript, _decode_asr_segment, _load_asr_model,
        _language_tokenizer, audio_chunks, native_asr)).encode()).hexdigest()


def _decode_asr_segment(model, samples, rate, start, *, depth=0):
    from runtime.execution_context import check_execution
    check_execution()
    stream = model.create_stream()
    stream.accept_waveform(rate, samples)
    model.decode_stream(stream)
    check_execution()  # native未返回前不释放资源，取消后不保存迟到块。
    if len(stream.result.tokens) >= 1024:
        if depth >= 3 or len(samples) < rate:
            raise ValueError('asr_token_budget_exhausted_no_complete_transcript')
        middle = len(samples)//2
        left, left_splits = _decode_asr_segment(model, samples[:middle], rate, start, depth=depth+1)
        right, right_splits = _decode_asr_segment(model, samples[middle:], rate, start+middle/rate, depth=depth+1)
        return left+right, 1+left_splits+right_splits
    text, detected = _detected_transcript(stream.result)
    return [{'text': text, 'language': detected, 'start_s': start,
             'end_s': start+len(samples)/rate, 'kind': 'audio_chunk'}], 0


def native_asr(request):
    """单实例CPU INT8、逐30秒块检测与转写；外语整次失败，不归档部分正文。"""
    from runtime.execution_context import check_execution, current_execution, record_execution_progress
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
    record_execution_progress('model_loading')
    try:
        model, hit = _load_asr_model(request['model_root'], request['model_identity'], request.get('threads', 4))
    finally:
        load_s = time.monotonic() - model_started
        record_execution_progress('model_loading', model_load_s=round(load_s,3))
    record_execution_progress('audio_decode', model_load_s=round(load_s,3), model_cache_hit=hit)
    identity = {key: request[key] for key in ('input_sha256', 'model_identity', 'language', 'device', 'threads')}
    identity.update(engine=ASR_ENGINE, chunk_seconds=30, max_tokens=1024, language_contract=3,
                    implementation=asr_implementation_identity(), chunk_contract=2)
    checkpoint = OperationCheckpoint(media_cache_subdir('asr_checkpoints'), identity, identity, max_age_s=None)
    rows = checkpoint.read().get('chunks', [])
    if not rows:
        # PR48与上一候选的ASR四个函数AST一致；只接纳这两个已核对实现（含CRLF）。
        # 不接纳任意旧实现，输入/模型/语言/线程身份及逐块时间仍须完全匹配。
        for implementation in (
            '5f6bc27756b2ed4f87fd30ec4ed45a7fb784c9e60c172908c1263de0075f6bf5',
            '9d9f1da393e8c91fe3d7b2085a158b29a5b97d7981201e098fb54a0f5acca97a',
            '1790745abf823b63e3121578a0cdbae53f0678bac6da3a57e1d08696f984092f',
            '3ed6ab9f06e2fae5b3c85b3e76560a4f3f54b99e5776bc846cb446be1650e72f',
        ):
            legacy = {k:v for k,v in identity.items() if k != 'chunk_contract'}
            legacy['implementation'] = implementation
            rows = OperationCheckpoint(media_cache_subdir('asr_checkpoints'),legacy,legacy,max_age_s=None).read().get('chunks',[])
            if rows:
                break
    reused = len(rows)
    record_execution_progress('audio_decode', resumed_chunks=reused, completed_chunks=reused,
                              processed_audio_s=rows[-1]['end_s'] if rows else 0)
    inference_s, decode_s, count, split_chunks = 0., 0., 0, 0
    decode_started = time.monotonic()
    for index, (start, samples, rate) in enumerate(audio_chunks(request['input_path'])):
        decode_s += time.monotonic() - decode_started
        end = start + len(samples) / rate
        check_execution()
        if index < reused:
            if rows[index]['start_s'] != start or rows[index]['end_s'] != end:
                raise ValueError('asr_checkpoint_chunk_identity_mismatch')
        else:
            inference_started = time.monotonic()
            record_execution_progress('transcription', audio_decode_s=round(decode_s,3))
            try:
                segments, splits = _decode_asr_segment(model, samples, rate, start)
                split_chunks += splits
            finally:
                inference_s += time.monotonic() - inference_started
                record_execution_progress('transcription', transcription_s=round(inference_s,3),
                                          worker_total_s=round(time.monotonic()-started,3))
            check_execution()  # native返回前保留租约；取消后不读回、不保存迟到块。
            rows.append({'text': '\n'.join(x['text'] for x in segments if x['text']),
                         'language': segments[0]['language'], 'languages': sorted({x['language'] for x in segments if x['language']}),
                         'start_s': start, 'end_s': end, 'kind': 'audio_chunk', 'segments': segments})
            context = current_execution() or {}
            checkpoint.write({'chunks': rows, 'writer_attempt': context.get('attempt_id', ''),
                              'writer_execution': context.get('execution_id', '')})
        count += 1
        record_execution_progress('audio_decode', completed_chunks=count,
                                  processed_audio_s=end, audio_decode_s=round(decode_s,3))
        decode_started = time.monotonic()
    if count != len(rows):
        raise ValueError('asr_checkpoint_duration_mismatch')
    locators, cursor, parts = [], 0, []
    for row in [segment for row in rows for segment in row.get('segments', [row])]:
        if not row['text']:
            continue
        parts.append(row['text'])
        locators.append({'start': cursor, 'end': cursor + len(row['text']), 'unit': 'unicode_code_point',
                         'start_s': row['start_s'], 'end_s': row['end_s'], 'kind': 'audio_chunk'})
        cursor += len(row['text']) + 1
    duration = rows[-1]['end_s'] if rows else 0
    languages = sorted({lang for row in rows for lang in row.get('languages', [row.get('language')]) if lang})
    record_execution_progress('transcript_complete', completed_chunks=count, processed_audio_s=duration,
                              worker_total_s=round(time.monotonic()-started,3))
    return {'status': 'acquired', 'text': '\n'.join(parts), 'source_kind': 'asr', 'component': request['component'],
        'speech_status': 'speech_detected' if parts else 'no_speech_detected' if count else 'no_decoded_audio',
        'content_locators': locators, 'timestamp_precision': 'input_chunk_only', 'word_timestamps': 'not_acquired',
        'device': 'cpu', 'dtype': 'int8', 'engine': ASR_ENGINE,
        'language_hint': request.get('language') or 'auto', 'decoding_language': 'auto',
        'detected_languages': languages, 'detected_language': languages[0] if len(languages) == 1 else 'zh-en' if languages else '',
        'language_detection': 'qwen_generated_prefix/v1',
        'asr_metrics': {'audio_duration_s': duration, 'chunks': count, 'resumed_chunks': reused, 'split_chunks': split_chunks,
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


def audit_docx_tables(source, result):
    """有界检查正文表格文字保留；不推断排版，不改源文件，不读取外部关系。"""
    import html
    import re
    import zipfile
    from xml.etree import ElementTree as ET

    result = {**result, 'document_blocks': list(result.get('document_blocks') or []),
              'quality_warnings': list(result.get('quality_warnings') or [])}
    audit = {'schema': 'docx-table-retention/v1', 'scope': 'main_document_table_cell_text_only',
             'status': 'checked', 'tables': [], 'unretained_cells': 0, 'layout_verified': False}
    result['document_audit'] = audit
    normalize = lambda text: re.sub(r'[\s|*_`\\]', '', html.unescape(str(text)))
    converted = normalize(result.get('text', '') + '\n' + '\n'.join(str(b.get('text', '')) for b in result['document_blocks']))
    try:
        with zipfile.ZipFile(source) as archive:
            if len(archive.infolist()) > 4096:
                raise ValueError('member_budget')
            info = archive.getinfo('word/document.xml')
            if info.file_size > 8 * 1024 * 1024:
                raise ValueError('xml_budget')
            with archive.open(info) as stream:
                xml = stream.read(8 * 1024 * 1024 + 1)
        if len(xml) > 8 * 1024 * 1024 or b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
            raise ValueError('xml_unsafe_or_oversize')
        root = ET.fromstring(xml)
        parents = {}; stack = [(root, 0)]; count = 0
        while stack:
            node, depth = stack.pop(); count += 1
            if depth > 128 or count > 100000:
                raise ValueError('xml_structure_budget')
            for child in node:
                parents[child] = node; stack.append((child, depth + 1))
        ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        def owner(node, tag):
            node = parents.get(node)
            while node is not None:
                if node.tag == ns + tag: return node
                node = parents.get(node)
            return None
        def number(node):
            value = int(node.get(ns + 'val', '0')) if node is not None else 0
            if not 0 <= value <= 10000: raise ValueError('grid_value_budget')
            return value
        cells_seen = 0; text_size = 0; salvaged = []
        for table_no, table in enumerate(root.iter(ns + 'tbl'), 1):
            grid = table.find(ns + 'tblGrid'); declared = len(grid) if grid is not None else 0
            record = {'table': table_no, 'declared_columns': declared, 'inferred_columns': 0,
                      'structural_risks': []}
            audit['tables'].append(record)
            if not declared: record['structural_risks'].append('missing_grid')
            rows = [r for r in table.iter(ns + 'tr') if owner(r, 'tbl') is table]
            for row_no, row in enumerate(rows, 1):
                width = number(row.find(ns + 'trPr/' + ns + 'gridBefore')) + number(row.find(ns + 'trPr/' + ns + 'gridAfter'))
                cells = [c for c in row.iter(ns + 'tc') if owner(c, 'tr') is row and owner(c, 'tbl') is table]
                for cell_no, cell in enumerate(cells, 1):
                    cells_seen += 1
                    if cells_seen > 8000: raise ValueError('cell_budget')
                    width += max(1, number(cell.find(ns + 'tcPr/' + ns + 'gridSpan')))
                    paragraphs = []
                    for paragraph in cell.iter(ns + 'p'):
                        if owner(paragraph, 'tbl') is table:
                            paragraphs.append(''.join(t.text or '' for t in paragraph.iter(ns + 't')))
                    text = '\n'.join(paragraphs).strip(); text_size += len(text)
                    if text_size > 2000000: raise ValueError('text_budget')
                    if text and normalize(text) not in converted:
                        audit['unretained_cells'] += 1
                        block = {'kind': 'raw_table_cell', 'text': f'[原始单元格文字；排版未恢复] {text}',
                                 'table_id': str(table_no), 'row': row_no, 'column': cell_no,
                                 'locator': {'table': table_no, 'row': row_no, 'cell': cell_no},
                                 'processing_method': 'raw_ooxml_cell_text_not_layout'}
                        salvaged.append(block)
                record['inferred_columns'] = max(record['inferred_columns'], width)
            if declared and record['inferred_columns'] > declared:
                record['structural_risks'].append('short_grid')
        result['document_blocks'].extend(salvaged)
        if salvaged:
            result['text'] = str(result.get('text') or '') + '\n\n[原始单元格文字；表格排版未恢复]\n' + '\n'.join(
                f"表{b['locator']['table']} 行{b['locator']['row']} 单元格{b['locator']['cell']}: {b['text']}" for b in salvaged)
            result['quality_warnings'].append('docx_table_text_unverified_in_primary_conversion')
        result['request_satisfied'] = not bool(salvaged)
    except (ValueError, KeyError, OSError, zipfile.BadZipFile, ET.ParseError) as exc:
        audit['status'] = 'failed'; audit['failure_type'] = type(exc).__name__
        result['quality_warnings'].append('docx_table_retention_audit_failed')
        result['request_satisfied'] = False
    if not result['request_satisfied']:
        result['coverage'] = 'partial'
        result['status'] = 'partial'
    return result


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
        is_docx = Path(source).suffix.lower() == '.docx'
        preflight = audit_docx_tables(source, {'status':'partial', 'text':'', 'document_blocks':[],
                                             'source_kind':'document_parse', 'component':component}) if is_docx else None
        if preflight and preflight['document_audit']['status'] == 'failed':
            return preflight
        try:
            result = DocumentConverter(allowed_formats=allowed).convert(source)
        except Exception as exc:
            if not preflight or not preflight.get('text'):
                raise
            preflight['quality_warnings'].append('docling_conversion_failed_raw_table_text_only')
            preflight['document_audit']['conversion_failure'] = type(exc).__name__
            return preflight
        converted = {
            "status": "acquired",
            "text": result.document.export_to_markdown(),
            "document_blocks": docling_blocks(result.document),
            "source_kind": "document_parse",
            "component": component,
            "processing_identity": {"docling-slim":importlib.metadata.version('docling-slim'),
                                    "docling-core":importlib.metadata.version('docling-core')},
        }
        return audit_docx_tables(source, converted) if Path(source).suffix.lower() == '.docx' else converted
    raise ValueError("unsupported_component")
