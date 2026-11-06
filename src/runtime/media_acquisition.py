"""媒体获取写口。只下载/校验，不加载模型，也不持有模型租约。"""
from pathlib import Path
import hashlib
import json
import time

from media_bundle import MediaKind, MediaLocationType, MediaRef
from runtime.execution_context import check_execution
from runtime.research_storage import reserve_bytes, admit_bytes
from runtime.optional_media import file_sha, approved_source
from runtime.media_cache import media_cache_subdir, record_media_cache_entry


class MediaAccessError(RuntimeError):
    def __init__(self, code):
        self.error_code = code
        super().__init__(code)


def acquire_asset_batch(jobs, fetch, *, on_progress=None, max_workers=2):
    """只编排独立下载；来源、请求头、清单选择由适配器提供。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from contextvars import copy_context
    from runtime.resource_concurrency import ResourceConcurrencyPolicy
    if not jobs:
        return []
    rows = [None] * len(jobs)
    workers = min(max_workers, len(jobs), ResourceConcurrencyPolicy.from_env().media_download)
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix='kr-media-download') as pool:
        futures = {pool.submit(copy_context().run, fetch, job): i for i, job in enumerate(jobs)}
        for future in as_completed(futures):
            rows[futures[future]] = future.result()
            if on_progress:
                on_progress([row for row in rows if row is not None])
    return rows


def classify_media_error(exc):
    text = str(exc).lower()
    for code, words in (
        ('verification_required', ('captcha', 'verify you', 'sign in', 'bot', '412')),
        ('rate_limited', ('429', 'too many requests')),
        ('private_or_authorization_required', ('private', 'members-only', '403', '401', 'login')),
        ('region_restricted', ('country', 'region', 'geo-restrict')),
        ('javascript_runtime_required', ('javascript', 'js runtime', 'ejs', 'po token')),
        ('media_not_found', ('404', 'not available', 'removed')),
        ('network_timeout', ('timeout', 'timed out')),
    ):
        if any(word in text for word in words):
            return code
    return 'media_acquisition_failed'


def acquire_http_asset(kind, url, folder, *, previous=(), headers=None, timeout_s=60):
    """已由适配器确认来源的CDN材料；格式、SHA与原子落盘共用。"""
    from generic_web.url_policy import guarded_fetch
    from runtime.storage_lifecycle import StorageLeases
    from runtime.resource_concurrency import acquire_resource
    from io import BytesIO
    from PIL import Image
    import uuid
    check_execution()
    folder = Path(folder).resolve()
    for row in previous:
        path = Path(row.get('path', ''))
        if (row.get('source_url') == url and row.get('type') == kind and row.get('status') == 'acquired'
                and path.is_file() and path.resolve().is_relative_to(folder)
                and not any(p.is_symlink() or p.is_junction() for p in (path, *path.parents))
                and file_sha(path) == row.get('sha256')):
            StorageLeases().acquire(path)
            return {**row, 'reused': True}
    started = time.monotonic()
    with reserve_bytes(folder,64*1024*1024,category='media'),acquire_resource('media_download', wait_timeout_s=timeout_s) as lease:
        response = guarded_fetch(url, headers=headers or {}, timeout=timeout_s, max_bytes=64 * 1024 * 1024)
        response.raise_for_status()
        content = response.content
        check_execution()
        if kind == 'image':
            with Image.open(BytesIO(content)) as image:
                image.load()
                extension = {'WEBP': '.webp', 'JPEG': '.jpg', 'PNG': '.png', 'GIF': '.gif'}.get(image.format)
            if not extension:
                raise ValueError('image_format_unsupported')
        elif kind == 'video' and b'ftyp' in content[:32]:
            extension = '.mp4'
        else:
            raise ValueError('media_format_unsupported')
        digest = hashlib.sha256(content).hexdigest()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (digest + extension)
        temporary = path.with_suffix('.' + uuid.uuid4().hex + '.pending')
        try:
            temporary.write_bytes(content)
            check_execution()
            # 同一内容可能被多个URL并行命中；只在提交时持锁，避免Windows替换读中文件失败。
            from runtime.file_write_lock import file_write_lock
            with file_write_lock(path):
                if not path.is_file() or file_sha(path) != digest:
                    temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        StorageLeases().acquire(path)
        return {'type': kind, 'status': 'acquired', 'path': str(path), 'sha256': digest,
            'bytes': len(content), 'source_url': url, 'elapsed_s': round(time.monotonic() - started, 3),
            'fetch_validate_s': round(time.monotonic() - started, 3),
            'resource_wait_s': round(float(lease.get('lease_wait_s', 0)) + float(lease.get('wait_s', 0)), 3)}


def acquire_media_ref(ref: MediaRef):
    check_execution()
    if ref.location_type == MediaLocationType.LOCAL_PATH:
        source = approved_source({'input_path': ref.uri, 'approved_input_root': ref.metadata.get('approved_input_root', ''),
                                  'input_sha256': ref.metadata.get('sha256', '')})
        return {'path': str(source), 'sha256': file_sha(source), 'kind': ref.kind.value, 'download_s': 0}
    if ref.location_type != MediaLocationType.URL:
        raise MediaAccessError('unsupported_media_location')
    from generic_web.url_policy import validate_detail_target, canonical_url
    validate_detail_target(ref.uri, '')
    identity = hashlib.sha256((canonical_url(ref.uri) + '|' + ref.kind.value).encode()).hexdigest()
    folder = media_cache_subdir('acquired', content_id=identity)
    from runtime.file_write_lock import file_write_lock
    from runtime.resource_concurrency import acquire_resource
    from runtime.leases import get_runtime_lease_coordinator, default_owner
    from runtime.resource_concurrency import ResourceDeferred
    import threading
    import tempfile
    coordinator = get_runtime_lease_coordinator()
    lease = coordinator.acquire_exclusive('media_asset', identity, owner=default_owner('media_acquisition'), ttl_s=60)
    if not lease.acquired:
        raise ResourceDeferred('media_download')
    stopped = threading.Event()
    def renew():
        while not stopped.wait(15):
            coordinator.heartbeat(lease.lease_id, ttl_s=60)
            from runtime.storage_lifecycle import StorageLeases
            StorageLeases().acquire(folder,ttl_s=3600)
    heartbeat = threading.Thread(target=renew, daemon=True)
    heartbeat.start()
    try:
        receipt = folder / 'asset.json'
        try:
            data = json.loads(receipt.read_text(encoding='utf-8'))
            path = Path(data['path']).resolve()
            if path.is_relative_to(folder.resolve()) and path.is_file() and file_sha(path) == data['sha256']:
                return {**data, 'cache_hit': True, 'download_s': 0}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        started = time.monotonic()
        with reserve_bytes(folder,256*1024*1024,category='media'),acquire_resource('media_download'), tempfile.TemporaryDirectory(prefix='download-', dir=folder) as temporary_dir:
            from yt_dlp import YoutubeDL
            from runtime.proxy_config import get_yt_dlp_proxy
            def progress(event):
                check_execution()
                admit_bytes(folder,1024*1024)
                if int(event.get('downloaded_bytes') or 0)>256*1024*1024:
                    raise MediaAccessError('download_missing_or_exceeds_limit')
            options = {'format': 'bestaudio/best' if ref.kind == MediaKind.AUDIO else 'bestvideo[height<=480]/best[height<=480]',
                'outtmpl': str(Path(temporary_dir) / 'source.%(ext)s'), 'noplaylist': True, 'playlist_items': '1',
                'socket_timeout': 15, 'retries': 0, 'fragment_retries': 0,
                'max_filesize': 256 * 1024 * 1024, 'quiet': True, 'no_warnings': True,
                'progress_hooks': [progress], 'overwrites': True}
            proxy = get_yt_dlp_proxy()
            if proxy:
                options['proxy'] = proxy
            # 站点专用公开请求头由适配器提供，绝不搬Cookie或启动浏览器。
            options['http_headers'] = {k: v for k, v in ref.metadata.get('headers', {}).items()
                                       if k.lower() in {'referer', 'origin', 'user-agent'}}
            try:
                with YoutubeDL(options) as downloader:
                    info = downloader.extract_info(ref.uri, download=True)
                    requests = info.get('requested_downloads') or []
                    candidate = requests[0].get('filepath') if requests else downloader.prepare_filename(info)
            except Exception as exc:
                from runtime.execution_context import ExecutionStopped
                if isinstance(exc, (ExecutionStopped,MediaAccessError)):
                    raise
                raise MediaAccessError(classify_media_error(exc)) from exc
            check_execution()
            path = Path(candidate or '').resolve()
            if not path.is_relative_to(Path(temporary_dir).resolve()) or not path.is_file() or not 0 < path.stat().st_size <= 256 * 1024 * 1024:
                raise MediaAccessError('download_missing_or_exceeds_limit')
            digest = file_sha(path)
            final_path = folder / (digest + path.suffix)
            path.replace(final_path)
            path = final_path
            data = {'path': str(path), 'sha256': digest, 'kind': ref.kind.value,
                    'download_s': round(time.monotonic() - started, 3), 'source_url': canonical_url(ref.uri)}
            temporary = receipt.with_suffix('.pending')
            temporary.write_text(json.dumps(data), encoding='utf-8')
            with file_write_lock(receipt):
                check_execution()
                temporary.replace(receipt)
            record_media_cache_entry(path, kind=ref.kind.value, source_url=ref.uri, metadata={'sha256': data['sha256']})
            return data
    finally:
        stopped.set()
        heartbeat.join(timeout=2)
        coordinator.release(lease.lease_id)
