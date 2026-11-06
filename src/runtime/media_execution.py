"""媒体计划的库内执行器；不拥有队列、预算或第二份任务状态。"""
import base64
import hashlib
import json
from pathlib import Path
import time
from media_bundle import MediaBundle, MediaKind, TextKind
from media_router import MediaActionPlan, MediaOperation
from runtime.execution_context import check_execution, ExecutionStopped
from runtime.resource_concurrency import ResourceDeferred


def processing_identity(component, language=None):
    from runtime.asr_policy import AsrPolicy
    from runtime.model_roles import ASR_COMPONENT
    from runtime.optional_media import file_sha, receipt_root
    receipt = receipt_root() / (component + '.json')
    return {'contract': 1, 'component': component,
        'language': AsrPolicy.for_request(language).language if component == ASR_COMPONENT else '',
        'install': file_sha(receipt) if receipt.is_file() else 'not_installed',
        'implementation': {name: file_sha(Path(__file__).with_name(name + '.py'))
            for name in ('model_roles', 'optional_media', 'optional_media_worker', 'cloud_ocr', 'asr_policy')}}


def process_local_asset(asset, component, *, language=None, cached=None, timeout_s=600):
    """图片与音视频共用处理合同；旧身份不明的缓存不能命中。"""
    from runtime.optional_media import approved_source, run_optional
    check_execution()
    identity = processing_identity(component, language)
    payload = {'component': component, 'input_path': asset['path'],
               'approved_input_root': str(Path(asset['path']).parent),
               'input_sha256': asset['sha256'], 'language': identity['language'], 'timeout_s': timeout_s}
    if (cached and cached.get('processing_identity') == identity
            and cached.get('input_sha256') == asset['sha256']
            and cached.get('status') in {'ok', 'completed', 'success', 'acquired'}):
        approved_source(payload)
        return {**cached, 'reused': True}
    result = run_optional(payload)
    check_execution()
    return {**result, 'input_sha256': asset['sha256'], 'processing_identity': identity}


def enhance_asset_batch(assets, component, *, language=None, previous=(), on_progress=None, failure_reason=None):
    """逐材料断点、错误隔离与服务级停止；资源忙/取消必须交还调度器。"""
    from runtime.cloud_ocr import CloudOcrError
    rows, batch_failure = [], ''
    for asset in assets:
        check_execution()
        started = time.monotonic()
        cached = next((row for row in previous if row.get('input_sha256') == asset['sha256']), None)
        if batch_failure:
            row = {'status': 'skipped', 'reason': batch_failure, 'input_sha256': asset['sha256']}
        else:
            try:
                row = process_local_asset(asset, component, language=language, cached=cached)
            except (ResourceDeferred, ExecutionStopped):
                raise
            except Exception as exc:
                reason = failure_reason(exc) if failure_reason else getattr(exc, 'error_code', type(exc).__name__)
                row = {'status': 'failed', 'reason': reason, 'input_sha256': asset['sha256']}
                if isinstance(exc, CloudOcrError):
                    row['diagnostics'] = exc.diagnostics
                    batch_failure = reason
                if reason.startswith('paid_'):
                    batch_failure = reason
        if not row.get('reused'):
            row['model_elapsed_s'] = round(time.monotonic() - started, 3)
        rows.append(row)
        if on_progress:
            on_progress(list(rows))
    return rows


def execute_media_lanes(lanes):
    """云端与本地独立lane；不持有资源等另一资源，不创建第二执行进程。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from contextvars import copy_context
    from runtime.tool_trace import current_tool_trace, set_current_tool_trace
    trace = current_tool_trace()
    def execute(lane):
        previous = current_tool_trace()
        set_current_tool_trace(trace)
        try:
            return lane()
        finally:
            set_current_tool_trace(previous)
    if lanes:
        with ThreadPoolExecutor(max_workers=min(2, len(lanes)), thread_name_prefix='kr-media-enhance') as pool:
            futures = [pool.submit(copy_context().run, execute, lane) for lane in lanes]
            for future in as_completed(futures):
                future.result()


def execute_media_plan(bundle: MediaBundle, plan: MediaActionPlan, *, language=None):
    from runtime.media_acquisition import acquire_media_ref
    from runtime.model_roles import ASR_COMPONENT, CLOUD_OCR_COMPONENT
    from runtime.asr_policy import AsrPolicy
    language = AsrPolicy.for_request(language).language
    from runtime.media_decode import sample_frames
    check_execution()
    result = {'status': 'acquired', 'plan': plan.to_dict(), 'steps': [], 'artifacts': []}
    text = next((item for item in bundle.texts if item.kind in {TextKind.SUBTITLE, TextKind.TRANSCRIPT} and item.text.strip()), None)
    if text:
        result.update(transcript=text.text, transcript_metadata={'source': text.source, 'language': text.language, **text.metadata})
    operations = set(plan.operations)
    from runtime.operation_checkpoint import OperationCheckpoint
    from runtime.media_cache import media_cache_subdir
    from runtime.execution_context import execution_value
    from runtime.optional_media import file_sha, receipt_root
    identity = {'contract': 2, 'bundle': bundle.to_dict(), 'plan': plan.to_dict(), 'language': language,
                'models': file_sha(Path(__file__).with_name('model_roles.py')),
                'asr_install': file_sha(receipt_root() / (ASR_COMPONENT + '.json')) if (receipt_root() / (ASR_COMPONENT + '.json')).is_file() else 'not_installed',
                'device': execution_value('KR_ASR_DEVICE', 'cpu'),
                'threads': execution_value('KR_LOCAL_MODEL_THREADS', '4'),
                'implementation': {name: file_sha(Path(__file__).with_name(name + '.py'))
                                   for name in ('media_execution', 'media_decode', 'optional_media_worker')},
                'scope': execution_value('KR_PAID_TASK_SCOPE', '')}
    checkpoint = OperationCheckpoint(media_cache_subdir('media_steps'), identity, identity)
    saved = checkpoint.read().get('completed', {})
    def persist(key, value):
        saved[key] = value
        checkpoint.write({'completed': saved})

    supported = {MediaOperation.READ_BASIC_TEXT, MediaOperation.READ_TRANSCRIPT, MediaOperation.RUN_ASR,
                 MediaOperation.RUN_OCR, MediaOperation.SAMPLE_FRAMES}
    if operations - supported:
        raise ValueError('unsupported_media_plan_operation')
    for operation, refs in ((MediaOperation.RUN_ASR, bundle.audio_refs or bundle.video_refs),
                            (MediaOperation.SAMPLE_FRAMES, bundle.video_refs),
                            (MediaOperation.RUN_OCR, bundle.image_refs)):
        if operation not in operations or (operation == MediaOperation.RUN_ASR and text):
            continue
        if operation == MediaOperation.RUN_OCR and not refs and result.get('frames'):
            refs = []
            from media_bundle import MediaRef, MediaLocationType
            folder = media_cache_subdir('frames')
            for frame in result['frames']:
                raw = base64.b64decode(frame['image_base64'], validate=True)
                path = folder / (hashlib.sha256(raw).hexdigest() + '.jpg')
                if not path.is_file():
                    from runtime.file_write_lock import file_write_lock
                    with file_write_lock(path):
                        if not path.is_file():
                            import os
                            import tempfile
                            with tempfile.NamedTemporaryFile(dir=folder, suffix='.pending', delete=False) as stream:
                                temporary = Path(stream.name)
                                stream.write(raw)
                                stream.flush()
                                os.fsync(stream.fileno())
                            try:
                                check_execution()
                                temporary.replace(path)
                            finally:
                                temporary.unlink(missing_ok=True)
                refs.append(MediaRef(MediaKind.IMAGE, str(path), location_type=MediaLocationType.LOCAL_PATH,
                    metadata={'sha256': file_sha(path), 'approved_input_root': str(folder), 'pts_s': frame['pts_s']}))
        if not refs:
            result['status'] = 'partial'
            result['steps'].append({'operation': operation.value, 'status': 'failed', 'error_code': 'required_media_reference_missing'})
        for ref in refs:
            check_execution()
            started = time.monotonic()
            try:
                if ref.is_remote and not plan.allows_download:
                    raise ValueError('media_plan_download_not_allowed')
                key = hashlib.sha256(json.dumps({'operation': operation.value, 'ref': ref.to_dict()}, sort_keys=True).encode()).hexdigest()
                cached = saved.get(key)
                if cached:
                    asset = cached['asset']
                    if not Path(asset['path']).is_file() or file_sha(asset['path']) != asset['sha256']:
                        cached = None
                asset = cached['asset'] if cached else acquire_media_ref(ref)
                result['artifacts'].append(asset)
                check_execution()
                if operation == MediaOperation.SAMPLE_FRAMES:
                    frames = cached['frames'] if cached else sample_frames(asset['path'], max_frames=plan.budget.max_frames or 8)
                    if not frames:
                        raise ValueError('no_usable_video_frames')
                    result['frames'] = frames
                    persist(key, {'asset': asset, 'frames': frames})
                    detail = {'frame_count': len(frames), 'frame_pts_s': [frame['pts_s'] for frame in frames]}
                else:
                    component = ASR_COMPONENT if operation == MediaOperation.RUN_ASR else CLOUD_OCR_COMPONENT
                    detail = process_local_asset(asset, component, language=language,
                        cached=cached['detail'] if cached else None, timeout_s=plan.budget.timeout_seconds or 600)
                    persist(key, {'asset': asset, 'detail': detail})
                    if operation == MediaOperation.RUN_ASR:
                        result['transcript'] = detail['text']
                        result['transcript_metadata'] = {'source': detail.get('engine') or component,
                            'language': detail.get('detected_language', ''), 'language_hint': language,
                            'detected_languages': detail.get('detected_languages', []),
                            'status': 'aligned', 'segments': detail.get('content_locators', []),
                            'timestamp_precision': detail.get('timestamp_precision')}
                    else:
                        previous = result.get('ocr_text', '')
                        start = len(previous) + (1 if previous else 0)
                        result['ocr_text'] = '\n'.join(filter(None, [previous, detail['text']]))
                        result.setdefault('ocr_text_locators', []).append({'kind': 'video_frame' if 'pts_s' in ref.metadata else 'image',
                            'start': start, 'end': start + len(detail['text']), 'unit': 'unicode_code_point',
                            **({'start_s': ref.metadata['pts_s'], 'end_s': ref.metadata['pts_s']} if 'pts_s' in ref.metadata else {})})
                result['steps'].append({'operation': operation.value, 'status': 'acquired',
                    'resumed': bool(cached) if operation == MediaOperation.SAMPLE_FRAMES else bool(detail.get('reused')), 'asset_sha256': asset['sha256'], 'download_s': asset['download_s'],
                    'elapsed_s': round(time.monotonic() - started, 3),
                    'metrics': detail.get('asr_metrics', {}), 'execution_timings': detail.get('execution_timings', {}),
                    **({k: detail[k] for k in ('frame_count', 'frame_pts_s') if k in detail})})
            except (ResourceDeferred, ExecutionStopped):
                raise
            except Exception as exc:
                result['status'] = 'partial'
                result['steps'].append({'operation': operation.value, 'status': 'failed',
                    'error_code': getattr(exc, 'error_code', str(exc)),
                    **({'error_message': str(exc)} if getattr(exc, 'error_code', '') == 'unsupported_language' else {}),
                    'elapsed_s': round(time.monotonic() - started, 3)})
    check_execution()
    return result
