"""公共 PDF：先读文字层，再由既有 OCR 通道只补缺页。"""
from io import BytesIO
import re

MAX_PDF_BYTES = 32 * 1024 * 1024
MAX_PDF_PAGES = 500
MAX_TEXT_CHARS = 2_000_000
MAX_EXPANDED_CONTENT_BYTES = 96 * 1024 * 1024
PDF_PROCESSING_CONTRACT = 'page-ledger-ocr-v4'


class DocumentError(ValueError):
    """稳定机器分类，不携带密码、原文、文件路径或pypdf内部异常。"""
    def __init__(self, code):
        super().__init__(code)
        self.error_code = code
        self.next_action = {
            'document_password_required': 'provide_an_authorized_decrypted_pdf_copy',
            'document_password_incorrect': 'provide_an_authorized_decrypted_pdf_copy',
            'document_corrupt': 'replace_with_a_valid_pdf_from_the_source',
        }.get(code, 'inspect_document_processing_limits')

def extract_pdf_text(source, *, max_pages=None, max_bytes=MAX_PDF_BYTES, max_chars=MAX_TEXT_CHARS, page_numbers=None):
    from pypdf.errors import FileNotDecryptedError, PdfReadError, ParseError, LimitReachedError
    try:
        return _extract_pdf_text(source, max_pages=max_pages, max_bytes=max_bytes,
                                 max_chars=max_chars, page_numbers=page_numbers)
    except FileNotDecryptedError:
        raise DocumentError('document_password_required') from None
    except LimitReachedError:
        raise ValueError('document_expansion_budget_exceeded') from None
    except (PdfReadError, ParseError, EOFError):
        raise DocumentError('document_corrupt') from None


def _extract_pdf_text(source, *, max_pages, max_bytes, max_chars, page_numbers):
    from pypdf import PdfReader
    from kr_core.evidence_locators import document_text
    from pathlib import Path
    from runtime.execution_context import check_execution
    size = len(source) if isinstance(source, bytes) else Path(source).stat().st_size
    if size > max_bytes:
        raise ValueError('document_too_large')
    reader = PdfReader(BytesIO(source) if isinstance(source, bytes) else source)
    if reader.is_encrypted and not reader.decrypt(""):
        raise DocumentError('document_password_required') from None
    count = len(reader.pages)
    if count > MAX_PDF_PAGES and page_numbers is None and max_pages is None:
        raise ValueError('document_page_budget_exceeded')
    from runtime.document_contract import selected_pages
    selected = selected_pages(count, max_pages=max_pages, page_numbers=page_numbers)
    if len(selected) > MAX_PDF_PAGES:
        raise ValueError('document_page_budget_exceeded')
    limit = len(selected)
    blocks, empty_pages, suspicious_pages = [], [], []
    quality_warnings = []
    total_chars, pages_read, truncated, expanded_bytes = 0, 0, False, 0
    for number in selected:
        page = reader.pages[number - 1]
        check_execution()
        # pypdf also bounds individual filter expansion. Bound the aggregate
        # decoded page streams without changing process-global library limits.
        contents = page.get('/Contents')
        if contents is not None:
            from pypdf.generic import ArrayObject
            contents = contents.get_object()
            streams = contents if isinstance(contents, ArrayObject) else [contents]
            for stream in streams:
                from pypdf.errors import LimitReachedError
                try:
                    expanded_bytes += len(stream.get_object().get_data())
                except LimitReachedError as exc:
                    raise ValueError('document_expansion_budget_exceeded') from exc
                if expanded_bytes > MAX_EXPANDED_CONTENT_BYTES:
                    raise ValueError('document_expansion_budget_exceeded')
                check_execution()
        content = page.extract_text() or ''
        pages_read += 1
        if not content.strip():
            empty_pages.append(number)
        else:
            replacement_ratio = content.count('\ufffd') / max(1, len(content))
            resources = page.get('/Resources')
            resources = resources.get_object() if resources else {}
            objects = resources.get('/XObject')
            objects = objects.get_object() if objects else {}
            large_image = any(obj.get_object().get('/Subtype') == '/Image' and
                int(obj.get_object().get('/Width', 0))*int(obj.get_object().get('/Height', 0)) > 200000
                for obj in objects.values())
            if replacement_ratio > .08 or (large_image and sum(ch.isalnum() for ch in content) < 60):
                suspicious_pages.append(number)
                quality_warnings.append({'page':number, 'type':'garbled_text_layer' if replacement_ratio>.08 else 'image_with_sparse_text',
                    'interpretation':'suspected_missing_body; not a semantic completeness verdict'})
        for line in content.splitlines():
            text = re.sub(r'\s+', ' ', line).strip()
            if text:
                room = max_chars - total_chars - bool(blocks)
                if len(text) > room:
                    text = text[:max(0, room)]
                    truncated = True
                if not text:
                    break
                total_chars += len(text) + bool(blocks)
                blocks.append({'page': number, 'kind': 'paragraph', 'text': text})
                if truncated:
                    break
        if truncated:
            break
    text, locators = document_text(blocks)
    return {'text': text, 'page_count': count, 'pages_read': pages_read, 'truncated': truncated,
            'document_blocks': blocks, 'locators': locators, 'empty_pages': empty_pages, 'expanded_content_bytes':expanded_bytes,
            'suspicious_text_pages':suspicious_pages, 'quality_warnings':quality_warnings,
            'requested_pages':selected, 'processed_pages':selected[:pages_read],
            'coverage_scope':'requested_page_text_only; layout_tables_formula_accuracy_not_asserted',
            'coverage': 'complete_selectable_text' if pages_read >= limit and not empty_pages and not suspicious_pages and not truncated else 'partial'}


def process_pdf(source, *, max_pages=None, max_bytes=MAX_PDF_BYTES,
                max_chars=MAX_TEXT_CHARS, timeout_s=600, allow_ocr=True, enhance_suspicious=False,
                cache_allowed=True, page_numbers=None, storage_allowed=True, read_allowed=True):
    """所有入口共用的接单/结果合同；授权先于缓存，OCR逐批持久化且不重放未知页。"""
    import os
    import json
    import hashlib
    import math
    from runtime.execution_context import check_execution
    from runtime.document_contract import processing_identity, finish_document
    from runtime.document_cache import input_digest, processing_snapshot
    check_execution()
    if not read_allowed:
        raise ValueError('document_read_not_authorized')
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int,float)) or not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError('invalid_document_deadline')
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_PDF_BYTES or type(max_chars) is not int or not 1 <= max_chars <= MAX_TEXT_CHARS:
        raise ValueError('invalid_document_budget')
    from pathlib import Path
    if (len(source) if isinstance(source,bytes) else Path(source).stat().st_size) > max_bytes:
        raise ValueError('document_too_large')
    if not isinstance(source,bytes):
        # The approved input may change after submission; freeze one bounded copy.
        with Path(source).open('rb') as stream:
            source=stream.read(max_bytes+1)
        if len(source)>max_bytes:
            raise ValueError('document_too_large')
    source_sha = input_digest(source)
    original={'sha256':source_sha,'bytes':len(source),'availability':'not_persisted_by_policy'}
    if cache_allowed and storage_allowed and os.environ.get('KR_PDF_CACHE_DISABLED') != '1':
        from runtime.document_cache import source_asset
        original=source_asset(source,source_sha)
    options = dict(max_pages=max_pages,max_bytes=max_bytes,max_chars=max_chars,
                   allow_ocr=bool(allow_ocr),enhance_suspicious=bool(enhance_suspicious),page_numbers=page_numbers)
    identity = processing_identity(source_sha, options)
    key = hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
    def finish(value, reused=False):
        result=finish_document(value, input_sha=source_sha, identity=identity, reused=reused)
        result['original_asset']=original
        return result
    if not cache_allowed or not storage_allowed or os.environ.get('KR_PDF_CACHE_DISABLED') == '1':
        return finish(_process_pdf(source,timeout_s=timeout_s,**options))
    with processing_snapshot(key,timeout_s=timeout_s) as (cached,save,remaining):
        if cached and cached.get('request_satisfied'):
            return finish(cached,True)
        def progress(value):
            save(finish(value, bool(cached)))
        result = _process_pdf(source,timeout_s=remaining,cached=cached,on_progress=progress,**options)
        result = finish(result, bool(cached))
        save(result)
        return result


def _process_pdf(source, *, max_pages=None, max_bytes=MAX_PDF_BYTES,
                 max_chars=MAX_TEXT_CHARS, timeout_s=600, allow_ocr=True, enhance_suspicious=False,
                 cached=None, page_numbers=None, on_progress=None):
    import copy
    import time
    from runtime.execution_context import check_execution
    from runtime.cloud_ocr import CloudOcrError, run_cloud_ocr, MAX_PAGES
    from kr_core.evidence_locators import document_text
    from runtime.pdf_worker import extract_isolated
    started = time.monotonic()
    options = dict(max_pages=max_pages,max_bytes=max_bytes,max_chars=max_chars)
    if page_numbers is not None:
        options['page_numbers'] = page_numbers
    parsed = copy.deepcopy(cached) if cached else extract_isolated(source,timeout_s=timeout_s,**options)
    requested = parsed.get('requested_pages') or list(range(1,parsed['pages_read']+1))
    parsed['requested_pages'] = requested
    parsed.setdefault('processed_pages', requested[:parsed['pages_read']])
    from runtime.execution_context import record_execution_progress
    record_execution_progress('document_text_ready', completed_pages=len(parsed['processed_pages']), requested_pages=len(requested))
    original_empty = parsed.get('text_layer_empty_pages',parsed.get('empty_pages',[]))
    parsed['text_layer_empty_pages'] = list(original_empty)
    missing = sorted((set(original_empty) | (set(parsed.get('suspicious_text_pages',[])) if enhance_suspicious else set())) - set(parsed.get('ocr_pages',[])))
    # Unknown send outcomes are persistent evidence; resuming never resends them.
    unknown = set(parsed.get('ocr_unknown_pages', []))
    parsed.update(missing_text_pages=missing, empty_pages=[p for p in original_empty if p not in parsed.get('ocr_pages',[])])
    parsed.setdefault('ocr_pages',[])
    parsed.setdefault('ocr_errors',[])
    parsed.setdefault('ocr_usage',{})
    parsed.setdefault('processing_method','pypdf')
    if not missing:
        parsed['ocr_status'] = 'complete' if parsed['ocr_pages'] else 'not_needed'
        return parsed
    if not allow_ocr or parsed['truncated']:
        parsed['ocr_status'] = 'not_requested' if not allow_ocr else 'partial'
        return parsed
    pending = [p for p in missing if p not in unknown]
    for offset in range(0,len(pending),MAX_PAGES):
        check_execution()
        batch = pending[offset:offset+MAX_PAGES]
        remaining = timeout_s-(time.monotonic()-started)
        if remaining <= 0:
            parsed['ocr_errors'].append({'type':'cloud_ocr_deadline_exceeded','pages':batch})
            break
        completed_in_batch = set()
        callback_pages = set()
        def apply_block(block):
            number = block.get('page'); value = str(block.get('text') or '').strip()
            if number not in batch or number in parsed['ocr_pages'] or not value:
                return
            replacement = [b for b in parsed['document_blocks'] if not (b['page']==number and number in parsed.get('suspicious_text_pages',[]))]
            candidate = sorted(replacement+[{'page':number,'kind':'ocr','text':value}],key=lambda b:b['page'])
            text,locators = document_text(candidate)
            if len(text)>max_chars:
                parsed['ocr_errors'].append({'page':number,'type':'pdf_text_budget_exceeded'})
                return
            parsed.update(document_blocks=candidate,text=text,locators=locators)
            parsed['ocr_pages'].append(number);completed_in_batch.add(number)
        def page_progress(event):
            number=event.get('page')
            if number not in batch:
                return
            callback_pages.add(number)
            if event['stage']=='sending':
                unknown.add(number)
            elif event['stage']=='completed':
                apply_block(event['block']);unknown.discard(number)
                for key,value in event.get('usage',{}).items():
                    if isinstance(value,(int,float)):
                        parsed['ocr_usage'][key]=parsed['ocr_usage'].get(key,0)+value
                parsed.update(ocr_model=event.get('model',''),processing_method='pypdf+page_ocr')
            elif event['stage']=='failed':
                if event['error']['type'] not in {'cloud_ocr_timeout','cloud_ocr_transport_error'}:
                    unknown.discard(number)
            parsed.update(ocr_unknown_pages=sorted(unknown),
                missing_text_pages=[p for p in missing if p not in parsed['ocr_pages']],
                empty_pages=[p for p in original_empty if p not in parsed['ocr_pages']])
            if on_progress:
                on_progress(parsed)
            if event['stage'] == 'completed':
                record_execution_progress('document_ocr', ocr_completed_pages=len(parsed['ocr_pages']))
        try:
            result = run_cloud_ocr(source,timeout_s=remaining,page_numbers=batch,
                                   allow_partial=True,on_progress=page_progress)
        except CloudOcrError as exc:
            parsed['ocr_errors'].append({'type':exc.error_code,**({'diagnostics':exc.diagnostics} if exc.diagnostics else {})})
            if exc.error_code in {'cloud_ocr_timeout','cloud_ocr_transport_error'} and not callback_pages:
                unknown.update(batch)
            parsed['ocr_unknown_pages']=sorted(unknown)
            break
        for block in result.get('document_blocks',[]):
            apply_block(block)
        errors=result.get('page_errors',[])
        parsed['ocr_errors'].extend(errors)
        for error in errors:
            if error.get('type') in {'cloud_ocr_timeout','cloud_ocr_transport_error'} and isinstance(error.get('page'),int):
                unknown.add(error['page'])
        if not callback_pages:
            for key,value in result.get('usage',{}).items():
                if isinstance(value,(int,float)):
                    parsed['ocr_usage'][key]=parsed['ocr_usage'].get(key,0)+value
        parsed.update(ocr_unknown_pages=sorted(unknown),ocr_model=result.get('model',''),processing_method='pypdf+page_ocr')
        parsed['missing_text_pages'] = [p for p in missing if p not in parsed['ocr_pages']]
        parsed['empty_pages'] = [p for p in original_empty if p not in parsed['ocr_pages']]
        parsed['ocr_status'] = 'complete' if not parsed['missing_text_pages'] else 'partial'
        if on_progress:
            on_progress(parsed)
        if errors or len(completed_in_batch)!=len(batch):
            break  # failed/unknown results do not trigger automatic alternate-copy retries
    parsed['ocr_status'] = 'complete' if not parsed['missing_text_pages'] else 'blocked' if not parsed['ocr_pages'] and any(e.get('type')=='siliconflow_api_key_missing' for e in parsed['ocr_errors']) else 'partial'
    return parsed


def fetch_pdf_document(url, *, referer='', timeout_s=600, include_ocr=None, document_options=None):
    """站点只交出合法只读位置；公共层负责有界下载、全页解析与获准OCR。"""
    import hashlib
    import time
    from generic_web.url_policy import guarded_fetch, redact_url
    from runtime.execution_context import check_execution
    started = time.monotonic()
    check_execution()
    response = guarded_fetch(url, timeout=min(60, timeout_s), max_bytes=MAX_PDF_BYTES,
        headers={'Accept':'application/pdf,*/*', **({'Referer':referer} if referer else {})})
    response.raise_for_status()
    if not response.content.startswith(b'%PDF-'):
        return {'status':'EXPECTED_DEGRADED', 'file_url':redact_url(str(response.url)),
                'reason':'not_pdf_bytes', 'pdf_bytes_confirmed':False, 'text_extractable':False,
                'text_probe':{'text':'','coverage':'unknown','error':'not_pdf_bytes'}}
    remaining = timeout_s - (time.monotonic() - started)
    check_execution()
    if remaining <= 0:
        raise TimeoutError('pdf_processing_deadline_exceeded')
    from runtime.document_contract import response_cache_allowed
    document_options = document_options or {}
    if document_options.get('include_ocr') is not None:
        include_ocr = document_options['include_ocr']
    result = process_pdf(response.content, timeout_s=remaining, allow_ocr=include_ocr is not False,
                         enhance_suspicious=include_ocr is True, cache_allowed=response_cache_allowed(response),
                         page_numbers=document_options.get('page_numbers'),storage_allowed=document_options.get('storage_allowed',True))
    text = result.get('text', '')
    processing = {k:v for k,v in result.items() if k not in {'text','locators','document_blocks'}}
    probe = {**result, 'text_length':len(text), 'sample_text_len':len(text),
             'extractable':bool(text.strip()), 'sample':text[:240], 'sample_text':text[:240]}
    return {'status':'PASS' if text.strip() else 'EXPECTED_DEGRADED',
        'file_url':redact_url(str(response.url)), 'pdf_bytes_confirmed':True,
        'text_extractable':bool(text.strip()), 'text_probe':probe,
        'input_sha256':hashlib.sha256(response.content).hexdigest(), 'pdf_processing':processing,
        'pdf_probe':{'status_code':response.status_code,'content_type':response.headers.get('content-type',''),
                     'content_length':str(len(response.content)), 'first_bytes_hex':response.content[:5].hex()}}
