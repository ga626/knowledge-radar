"""评论附件复用公共下载与加工；逐项回执，不把链接算作已取得。"""

from __future__ import annotations

import hashlib
import json


def register_media_source(url, *, source_url, ttl_s=21600, fresh_observation=False):
    """短期签名位置只留私有运行态，跨任务交接只传受控引用。"""
    import time
    from runtime.paths import runtime_state_dir
    from runtime.state_file import write_state
    from kr_core.source_urls import canonical_url

    key = hashlib.sha256(json.dumps([canonical_url(source_url), url]).encode()).hexdigest()
    path = runtime_state_dir() / "comment_media_fetch" / (key + ".json")
    from runtime.file_write_lock import file_write_lock

    with file_write_lock(path):
        # 再次规范化不是重新取得来源，不能为过期签名续命。
        if fresh_observation or not path.is_file():
            write_state(
                path, {"fetch_url": url, "source_url": canonical_url(source_url), "expires_at": time.time() + ttl_s}
            )
    return "urn:kr:comment-media:" + key


def resolve_media_source(ref, *, source_url):
    import re
    import time
    from runtime.paths import runtime_state_dir
    from kr_core.source_urls import canonical_url

    key = ref.removeprefix("urn:kr:comment-media:")
    if not re.fullmatch("[a-f0-9]{64}", key):
        raise ValueError("invalid_comment_media_reference")
    try:
        saved = json.loads((runtime_state_dir() / "comment_media_fetch" / (key + ".json")).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        raise ValueError("expired_resource") from None
    if saved.get("source_url") != canonical_url(source_url) or saved.get("expires_at", 0) < time.time():
        raise ValueError("expired_resource")
    return saved["fetch_url"]


def process_comment_media(data, *, source_url, options, language=None):
    from runtime.execution_context import check_execution, ExecutionStopped
    from runtime.resource_concurrency import ResourceDeferred
    from runtime.media_acquisition import acquire_http_asset, acquire_media_ref
    from runtime.media_execution import process_local_asset
    from runtime.media_cache import media_cache_subdir, record_media_cache_entry
    from runtime.file_write_lock import file_write_lock
    from runtime.paths import runtime_state_dir
    from runtime.state_file import write_state
    from runtime.model_roles import CLOUD_OCR_COMPONENT, ASR_COMPONENT
    from media_bundle import MediaRef, MediaKind
    from kr_core.source_urls import canonical_url

    count = 0
    ledger = []
    for comment in data.get("comments") or []:
        for index, item in enumerate(comment.get("media") or []):
            check_execution()
            row = dict(item, comment_id=comment["comment_id"],
                       discussion_kind=comment.get("discussion_kind", "comments"), media_index=index)
            kind, url = item.get("type"), item.get("url")
            if (kind == "image" and not options.get("include_images", True)) or (
                kind != "image" and not options.get("include_transcript")
            ):
                row.update(status="not_requested", reason="not_requested")
                ledger.append(row)
                continue
            if count >= options.get("media_limit", 20):
                row.update(status="not_processed", reason="budget_reached")
                ledger.append(row)
                continue
            count += 1
            key = hashlib.sha256(
                json.dumps([canonical_url(source_url), comment.get("discussion_kind", "comments"),
                            comment["comment_id"], kind, url], ensure_ascii=False).encode()
            ).hexdigest()
            path = runtime_state_dir() / "comment_media" / (key + ".json")
            with file_write_lock(path):
                try:
                    saved = json.loads(path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    saved = {}
                # A sent-but-unsettled inference must never be silently submitted again.
                if saved.get("processing_state") == "result_unknown":
                    row.update(status="partial", reason="result_unknown", automatic_retry=False)
                    ledger.append(row)
                    continue
                try:
                    if str(url).startswith("urn:kr:xhs-media:"):
                        from runtime.xhs_opencli import _acquire_media_one

                        asset = _acquire_media_one(
                            kind, url, data.get("note_id") or "comments", [saved["asset"]] if saved.get("asset") else []
                        )
                        if asset.get("status") != "acquired":
                            row.update(asset)
                            ledger.append(row)
                            continue
                    elif kind == "image":
                        # 优先核验已获原件；签名过期不应使本地正确SHA原件变得不可读。
                        prior = saved.get("asset") or {}
                        fetch_url = prior.get("source_url") or (
                            resolve_media_source(url, source_url=source_url)
                            if str(url).startswith("urn:kr:comment-media:")
                            else url
                        )
                        asset = acquire_http_asset(
                            kind,
                            fetch_url,
                            media_cache_subdir("comment_images"),
                            previous=[saved["asset"]] if saved.get("asset") else [],
                            headers={"Referer": source_url},
                        )
                    else:
                        fetch_url = (
                            resolve_media_source(url, source_url=source_url)
                            if str(url).startswith("urn:kr:comment-media:")
                            else url
                        )
                        asset = acquire_media_ref(MediaRef(MediaKind(kind), fetch_url))
                    saved["asset"] = asset
                    record_media_cache_entry(
                        asset["path"],
                        kind=kind,
                        source_url=url,
                        metadata={"sha256": asset["sha256"], "comment_id": comment["comment_id"]},
                    )
                    row.update(
                        status="acquired",
                        sha256=asset["sha256"],
                        bytes=asset.get("bytes"),
                        processing_status="not_requested",
                        asset_reference={"sha256": asset["sha256"], "retention": "media_cache_ttl"},
                    )
                    component = (
                        CLOUD_OCR_COMPONENT
                        if kind == "image" and options.get("include_ocr")
                        else ASR_COMPONENT
                        if kind in {"audio", "video"} and options.get("include_transcript")
                        else None
                    )
                    if component and kind == "image":
                        from PIL import Image

                        with Image.open(asset["path"]) as image:
                            frames = getattr(image, "n_frames", 1)
                        if frames > 1:
                            row.update(
                                status="partial",
                                reason="animated_image_ocr_scope_not_requested",
                                processing_status="not_processed",
                                frame_count=frames,
                            )
                            component = None
                    if component:
                        saved["processing_state"] = "result_unknown"
                        write_state(path, saved)
                        try:
                            result = process_local_asset(
                                asset, component, language=language, cached=saved.get("result")
                            )
                        except ResourceDeferred:
                            saved["processing_state"] = "not_sent"
                            write_state(path, saved)
                            raise
                        except ExecutionStopped:
                            raise
                        except Exception as exc:
                            reason = getattr(exc, "error_code", type(exc).__name__)
                            if reason not in {"cloud_ocr_timeout", "cloud_ocr_transport_error"}:
                                saved["processing_state"] = "failed"
                                write_state(path, saved)
                            raise
                        saved.update(result=result, processing_state="completed")
                        row.update(
                            processing_status=result.get("status", "unknown"),
                            text=result.get("text") or result.get("transcript", ""),
                            processing_identity=result.get("processing_identity"),
                            locators=result.get("locators", []),
                            model=result.get("model", ""),
                            provider=result.get("provider", ""),
                        )
                        if result.get("status") not in {"ok", "completed", "success", "acquired"}:
                            row.update(status="partial", reason=result.get("reason") or "media_processing_incomplete")
                    write_state(path, saved)
                except (ExecutionStopped, ResourceDeferred):
                    raise
                except Exception as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    row.update(
                        status="partial" if row.get("sha256") else "failed",
                        reason="expired_or_restricted_resource"
                        if status in {401, 403, 404, 410}
                        else str(exc)
                        if isinstance(exc, ValueError)
                        else getattr(exc, "error_code", type(exc).__name__),
                        **({"http_status": status} if status else {}),
                        automatic_retry=False,
                    )
            item.update({k: v for k, v in row.items() if k != "url"})
            ledger.append(row)
    data["comment_media_ledger"] = ledger
    data["comments_metadata"]["media_gaps"] = [
        {k: row[k] for k in ("comment_id", "discussion_kind", "media_index", "status", "reason") if k in row}
        for row in ledger
        if row.get("status") not in {"acquired", "not_requested"}
    ]
    return data
