"""硅基流动单一云端 OCR 路径；失败不加载本地模型。"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import time
import uuid

import httpx
from PIL import Image, ImageOps

from runtime.model_roles import CLOUD_OCR_COMPONENT, CLOUD_OCR_MODEL
from runtime.paid_execution import PaidAdmissionError, paid_send

MAX_PAGES = 20
MAX_IMAGE_SIDE = 2400
OCR_REQUEST_PROFILE = 'paddle-task-ocr-fp05-v1'


def _usage(value):
    return {key: value[key] for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')
            if isinstance(value, dict) and type(value.get(key)) is int and value[key] >= 0}


class CloudOcrError(ValueError):
    def __init__(self, code, *, diagnostics=None):
        super().__init__(code)
        self.error_code = code
        self.diagnostics = diagnostics or {}


def ensure_ready():
    if not os.environ.get('SILICONFLOW_API_KEY', '').strip():
        raise CloudOcrError('siliconflow_api_key_missing')


def _jpeg(image):
    image = ImageOps.exif_transpose(image).convert('RGB')
    image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    output = io.BytesIO()
    image.save(output, format='JPEG', quality=92)
    return output.getvalue()


def normalize_image_bytes(content):
    with Image.open(io.BytesIO(content)) as image:
        return _jpeg(image)


def document_images(source, *, page_numbers=None):
    from runtime.execution_context import ExecutionStopped
    try:
        yield from _document_images(source, page_numbers=page_numbers)
    except (CloudOcrError, ExecutionStopped):
        raise
    except Exception as exc:
        raise CloudOcrError('document_render_failed', diagnostics={'kind': type(exc).__name__}) from exc


def _document_images(source, *, page_numbers=None):
    from pathlib import Path
    from runtime.execution_context import check_execution
    if isinstance(source, bytes) or Path(source).suffix.lower() == '.pdf':
        try:
            import pypdfium2 as pdfium
        except ImportError as exc:
            raise CloudOcrError('pdf_renderer_not_installed') from exc
        with pdfium.PdfDocument(source if isinstance(source, bytes) else str(source)) as document:
            selected = list(range(1, len(document) + 1)) if page_numbers is None else list(page_numbers)
            if (not 0 < len(selected) <= MAX_PAGES or len(set(selected)) != len(selected)
                    or any(type(n) is not int or not 1 <= n <= len(document) for n in selected)):
                raise CloudOcrError('cloud_ocr_page_budget_exceeded')
            for number in selected:
                check_execution()
                page = document[number - 1]
                try:
                    scale = min(2.0, MAX_IMAGE_SIDE / max(page.get_size()))
                    bitmap = page.render(scale=scale)
                    try:
                        yield number, _jpeg(bitmap.to_pil())
                    finally:
                        bitmap.close()
                finally:
                    page.close()
    else:
        with Image.open(source) as image:
            count = getattr(image, 'n_frames', 1)
            if not 0 < count <= MAX_PAGES:
                raise CloudOcrError('cloud_ocr_page_budget_exceeded')
            for index in range(count):
                image.seek(index)
                yield index + 1, _jpeg(image.copy())


def recognize_image(client, image_bytes, *, timeout, provenance=None):
    ensure_ready()
    started = time.monotonic()
    # Known before sending, so even a no-response timeout can be looked up by the provider.
    request_identity = {'request_trace_id': 'kr-ocr-' + uuid.uuid4().hex,
                        'request_model': CLOUD_OCR_MODEL,
                        'submitted_image_sha256': hashlib.sha256(image_bytes).hexdigest()}
    try:
        with paid_send("siliconflow_ocr", {"image_sha256": hashlib.sha256(image_bytes).hexdigest(), "model": CLOUD_OCR_MODEL, "max_tokens": 4096}, bound=len(image_bytes)):
            response = client.post('https://api.siliconflow.cn/v1/chat/completions',
                headers={'Authorization': 'Bearer ' + os.environ['SILICONFLOW_API_KEY'].strip(),
                         'X-Trace-Id': request_identity['request_trace_id']},
                json={'model': CLOUD_OCR_MODEL, 'messages': [{'role': 'user', 'content': [
                    {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(image_bytes).decode('ascii')}},
                    # Task-specific VLM, not a general chat model. Use its documented task token.
                    {'type': 'text', 'text': 'OCR:'}]}],
                    'temperature': 0, 'frequency_penalty': 0.5, 'max_tokens': 4096}, timeout=timeout)
    except PaidAdmissionError as exc:
        raise CloudOcrError(exc.error_code) from exc
    except httpx.TimeoutException as exc:
        raise CloudOcrError('cloud_ocr_timeout', diagnostics={
            **request_identity, 'elapsed_s': round(time.monotonic()-started, 3), 'timeout_kind': type(exc).__name__}) from exc
    except httpx.RequestError as exc:
        raise CloudOcrError('cloud_ocr_transport_error', diagnostics={
            **request_identity, 'elapsed_s': round(time.monotonic()-started, 3), 'transport_error_kind': type(exc).__name__}) from exc
    trace = response.headers.get('x-siliconcloud-trace-id', '')
    safe_trace = trace if re.fullmatch(r'[A-Za-z0-9._:/-]{1,200}', trace) else ''
    response_identity = {**request_identity, 'http_status': response.status_code, 'trace_id': safe_trace}
    if response.status_code != 200:
        raise CloudOcrError('cloud_ocr_http_' + str(response.status_code), diagnostics=response_identity)
    try:
        body = response.json()
        choice = body['choices'][0]
        text = choice['message']['content']
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise CloudOcrError('cloud_ocr_invalid_response') from exc
    if choice.get('finish_reason') == 'length':
        # Invalid output is never archived as recognized text; failed calls may still be billed.
        raise CloudOcrError('cloud_ocr_output_truncated', diagnostics={
            'finish_reason': 'length', 'usage': _usage(body.get('usage')),
            'output_chars': len(text) if isinstance(text, str) else 0,
            'elapsed_s': round(time.monotonic()-started, 3), 'request_profile': OCR_REQUEST_PROFILE})
    if not isinstance(text, str) or not text.strip():
        raise CloudOcrError('cloud_ocr_empty_result')
    if provenance is not None:
        reported = body.get('model')
        provenance.update(response_identity, reported_model=reported[:200] if isinstance(reported, str) else '',
                          finish_reason=str(choice.get('finish_reason') or '')[:40])
    return text.strip(), _usage(body.get('usage'))


def run_cloud_ocr(source, *, timeout_s=600, page_numbers=None, allow_partial=False, on_progress=None):
    ensure_ready()
    started = time.monotonic()
    blocks, usage, pages, page_errors = [], {}, [], []
    from runtime.execution_context import check_execution
    from runtime.resource_concurrency import acquire_resource
    with acquire_resource('provider_siliconflow', wait_timeout_s=min(60, timeout_s)) as lease, httpx.Client(follow_redirects=False) as client:
        images = iter(document_images(source, page_numbers=page_numbers))
        try:
            while True:
                page = None
                page_start = time.monotonic()
                try:
                    check_execution()
                    page, image = next(images)
                    remaining = timeout_s - (time.monotonic() - started)
                    if remaining <= 0:
                        raise CloudOcrError('cloud_ocr_deadline_exceeded')
                    response_identity = {}
                    # Connection failure and slow inference are different waits; both remain bounded.
                    request_timeout = httpx.Timeout(min(180, remaining), connect=min(10, remaining))
                    if on_progress:
                        on_progress({'stage':'sending','page':page})
                    text, observed = recognize_image(client, image, timeout=request_timeout, provenance=response_identity)
                except StopIteration:
                    break
                except CloudOcrError as exc:
                    exc.diagnostics.update(page=page, completed_pages=len(blocks), completed_usage=usage,
                                           total_elapsed_s=round(time.monotonic()-started, 3))
                    if allow_partial:
                        page_errors.append({'page': page, 'type': exc.error_code,
                                            'diagnostics': dict(exc.diagnostics)})
                        if on_progress:
                            on_progress({'stage':'failed','page':page,'error':page_errors[-1]})
                        break  # No automatic replay after a failed or unknown provider outcome.
                    raise
                blocks.append({'page': page, 'kind': 'ocr', 'text': text})
                pages.append({'page': page, 'elapsed_s': round(time.monotonic()-page_start, 3), 'usage': observed,
                              'response_identity': response_identity})
                for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                    if isinstance(observed.get(key), int):
                        usage[key] = usage.get(key, 0) + observed[key]
                if on_progress:
                    on_progress({'stage':'completed','page':page,'block':blocks[-1],
                                 'usage':observed,'model':CLOUD_OCR_MODEL})
        finally:
            close = getattr(images, 'close', None)
            if close:
                close()
    if not blocks and not allow_partial:
        raise CloudOcrError('cloud_ocr_empty_document')
    return {'status': 'acquired', 'component': CLOUD_OCR_COMPONENT, 'model': CLOUD_OCR_MODEL,
            'provider': 'siliconflow', 'source_kind': 'ocr', 'text': '\n\n'.join(row['text'] for row in blocks),
            'document_blocks': blocks, 'page_count': len(blocks), 'usage': usage,
            'elapsed_s': round(time.monotonic() - started, 3), 'automatic_cpu_fallback': False,
            'request_profile': OCR_REQUEST_PROFILE, 'page_metrics': pages,
            'page_errors': page_errors,
            'resource_wait_s': round(float(lease.get('lease_wait_s', 0)) + float(lease.get('wait_s', 0)), 3),
            'quality_status': 'unmeasured', 'billing_status': 'provider_account_authoritative'}
