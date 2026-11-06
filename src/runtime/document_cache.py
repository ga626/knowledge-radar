"""公共文档处理快照；共享资产和租约，不另建数据库或平台缓存。"""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import time
import uuid


def input_digest(source):
    if isinstance(source, bytes):
        return hashlib.sha256(source).hexdigest()
    digest = hashlib.sha256()
    with Path(source).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_asset(content, digest):
    """只登记获准保存的原件，复用现有媒体容量/TTL/租约，不混入文本证据。"""
    import os
    from runtime.media_cache import media_cache_subdir, record_media_cache_entry
    from runtime.research_storage import reserve_bytes
    from runtime.file_write_lock import file_write_lock
    from runtime.storage_lifecycle import StorageLeases

    path = media_cache_subdir("documents", content_id=digest) / (digest + ".pdf")
    with file_write_lock(path):
        if not path.is_file() or input_digest(path) != digest:
            with reserve_bytes(path.parent, len(content), category="media"):
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix("." + uuid.uuid4().hex + ".pending")
                try:
                    with temporary.open("xb") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
        StorageLeases().acquire(path)
        record_media_cache_entry(path, kind="document", content_id=digest, metadata={"sha256": digest})
    return {
        "sha256": digest,
        "bytes": len(content),
        "cache_key": "documents/" + digest,
        "retention": "media_cache_ttl; not_permanent_evidence",
        "availability": "acquired",
    }


@contextmanager
def processing_snapshot(key, *, timeout_s):
    from runtime.execution_context import check_execution
    from runtime.leases import get_runtime_lease_coordinator, default_owner
    from kr_core.evidence_assets import EvidenceAssets

    coordinator = get_runtime_lease_coordinator()
    deadline = time.monotonic() + timeout_s
    while True:
        check_execution()
        if time.monotonic() >= deadline:
            raise TimeoutError("document_processing_wait_deadline")
        grant = coordinator.acquire_exclusive(
            "document_processing",
            key,
            owner=default_owner("pdf", request_id=uuid.uuid4().hex),
            ttl_s=int(timeout_s) + 15,
        )
        if grant.acquired:
            break
        time.sleep(0.1)
    try:
        assets = EvidenceAssets()
        with assets.connection() as conn:
            row = conn.execute(
                "SELECT a.version_id FROM assets a JOIN sources s USING(source_id) WHERE s.withdrawn=0 AND a.kind='document_processing' AND json_extract(a.metadata,'$.processing_key')=? ORDER BY a.captured_at DESC LIMIT 1",
                (key,),
            ).fetchone()
        cached = None
        if row:
            part = assets.fetch(row[0], max_chars=24000)
            text = part["text"]
            while part["next_start"] is not None:
                part = assets.fetch(row[0], start=part["next_start"], max_chars=24000)
                text += part["text"]
            cached = json.loads(text)
            if not isinstance(cached, dict):
                raise ValueError("document_processing_snapshot_invalid")

        def save(result):
            # 取消后仍允许结算已经返回的加工事实；不发送新请求。
            # 仅保留处理事实；输入原件仍由各入口自己的授权/下载合同掌管。
            assets.put(
                "https://document-processing.invalid/" + key,
                json.dumps(result, ensure_ascii=False, sort_keys=True),
                kind="document_processing",
                metadata={
                    "extractor": "common-pdf-snapshot",
                    "processing_key": key,
                    "coverage": result.get("coverage", "unknown"),
                },
            )

        yield cached, save, max(0.1, deadline - time.monotonic())
    finally:
        coordinator.release(grant.lease_id)
