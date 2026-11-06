"""微信采集/增强分段；清单固定后释放浏览器，公共媒体按原件SHA恢复。"""

from __future__ import annotations
from urllib.parse import urlsplit
from kr_core import DetailResponse
from collectors.platform import wechat


def _checkpoint(request):
    from runtime.operation_checkpoint import OperationCheckpoint
    from runtime.paths import runtime_state_dir

    return OperationCheckpoint(
        runtime_state_dir() / "wechat" / "checkpoints",
        {
            "url": request.url,
            "scope": request.task_scope_id,
            "run": request.options.get("wechat_checkpoint_id", ""),
            "transport": request.options.get("wechat", {}).get("transport", "http"),
            "include_comments":request.options.get("include_comments",False),
            "comments":request.options.get("comments",{}),
            "document":request.options.get("document",{}),
        },
        {"contract": wechat.CONTRACT},
    )


def collect(request):
    from runtime.execution_context import check_execution

    options = request.options.get("wechat") or {}
    checkpoint = _checkpoint(request)
    saved = checkpoint.read()
    if saved.get("body_verified") and not options.get("refresh"):
        data = {**saved, "collection_reused": True}
    else:
        check_execution()
        data = wechat.collect_article(request.url, transport=options.get("transport", "http"))
        if data.get("error"):
            return data
        checkpoint.write(data)
    if request.options.get('include_comments'):
        data.update(comments=[], comments_complete=False, comments_attempted=False,
                    comments_coverage={"status": "paused", "reason": "paused_by_user", "source_exhausted": False},
                    comment_gaps=[{"reason": "paused_by_user", "message": "微信读者留言已暂停；正文与媒体可独立读取。"}])
        checkpoint.write(data)
    enhanced = any(request.options.get(k) for k in ("include_ocr", "include_transcript")) or options.get(
        "include_attachments"
    )
    if enhanced:
        data.update(status="partial", enhancement_status="queued", _enhancement_pending=True)
        checkpoint.write(data)
        if request.options.get("_detail_stage") == "collection":
            return data
        return enhance_detail(request, data)
    if request.options.get("include_comments"):
        if not data.get('comments_complete'):
            data['status']='partial'
    return data


def _approved_image(url):
    try:
        p = urlsplit(url)
        return (
            p.scheme == "https"
            and not p.username
            and not p.password
            and p.port in {None, 443}
            and any((p.hostname or "") == d or (p.hostname or "").endswith("." + d) for d in ("qpic.cn", "qlogo.cn"))
        )
    except ValueError:
        return False


def enhance_detail(request, collected):
    from runtime.execution_context import check_execution, ExecutionStopped
    from runtime.resource_concurrency import ResourceDeferred
    from runtime.media_cache import media_cache_subdir
    from runtime.media_acquisition import acquire_http_asset
    from runtime.media_execution import enhance_asset_batch
    from runtime.model_roles import CLOUD_OCR_COMPONENT, ASR_COMPONENT
    from kr_core.evidence_locators import document_text

    checkpoint = _checkpoint(request)
    saved = checkpoint.read()
    if not collected.get("body_verified") or not collected.get("collection_identity"):
        return {
            **collected,
            "status": "partial",
            "enhancement_status": "collection_snapshot_unverified",
            "_enhancement_pending": False,
        }
    same = saved.get("collection_identity") == collected["collection_identity"]
    data = {**collected, **(saved if same else {})}
    data.pop("_enhancement_pending", None)
    options = request.options.get("wechat") or {}
    rows = data.get("media_ledger", []) if same else []
    previous = data.get("downloaded_media", []) if same else []
    manifest = data.get("media_manifest", [])
    # 公共下载层以原件SHA命名并验证缓存根；不按可变字典顺序再造目录身份。
    # 同根中已有的候选子目录仍经来源URL、格式、SHA及非链接检查后复用。
    folder = media_cache_subdir("wechat")
    gaps = []

    def save():
        data["media_ledger"] = rows
        data["media_gaps"] = gaps
        checkpoint.write(data)

    def put(row):
        rows[:] = [
            r
            for r in rows
            if not (r.get("locator") == row.get("locator") and r.get("operation") == row.get("operation"))
        ]
        rows.append(row)
        save()

    if request.options.get("include_ocr"):
        images = [r for r in manifest if r["type"] == "image"]
        indexes = options.get("image_indexes")
        selected = images if indexes is None else [r for r in images if r.get("image_index") in indexes]
        if indexes is not None:
            gaps.extend(
                {"image_index": i, "reason": "image_index_not_observed"}
                for i in indexes
                if not any(r.get("image_index") == i for r in images)
            )
        assets = []
        for offset, image in enumerate(selected):
            check_execution()
            base = {
                "locator": image["locator"],
                "image_index": image["image_index"],
                "operation": "ocr",
                "source_url": image["url"],
            }
            if offset >= options.get("media_limit", 30):
                gap = {**base, "status": "not_run", "reason": "request_media_limit"}
                gaps.append(gap)
                put(gap)
                continue
            if not _approved_image(image["url"]):
                gap = {**base, "status": "failed", "reason": "image_source_not_supported"}
                gaps.append(gap)
                put(gap)
                continue
            try:
                asset = acquire_http_asset(
                    "image", image["url"], folder, previous=previous, headers={"Referer": "https://mp.weixin.qq.com/"}
                )
                asset.update(locator=image["locator"], image_index=image["image_index"])
                assets.append(asset)
                previous = [r for r in previous if r.get("source_url") != asset["source_url"]] + [asset]
                data["downloaded_media"] = previous
                put({**base, "status": "acquired", "input_sha256": asset["sha256"]})
            except (ExecutionStopped, ResourceDeferred):
                raise
            except Exception as exc:
                code = (
                    "media_access_forbidden_or_expired"
                    if getattr(getattr(exc, "response", None), "status_code", 0) in {401, 403}
                    else getattr(exc, "error_code", type(exc).__name__)
                )
                gap = {**base, "status": "failed", "reason": code}
                gaps.append(gap)
                put(gap)

        def progress(results):
            located = []
            for i, result in enumerate(results):
                asset = assets[i]
                row = {
                    **result,
                    "locator": asset["locator"],
                    "image_index": asset["image_index"],
                    "source_url": asset["source_url"],
                    "operation": "ocr",
                }
                located.append(row)
                put(row)
            data["ocr"] = {"status": "partial", "results": located, "request_complete": False}
            data["ocr_text"], data["ocr_text_locators"] = document_text(
                [
                    {"text": r["text"], "kind": "ocr", "image_index": r["image_index"]}
                    for r in located
                    if r.get("status") in {"ok", "completed", "success", "acquired"} and r.get("text")
                ]
            )
            data["ocr_provenance"] = {
                "ocr_images": [
                    {k: r[k] for k in ("image_index", "source_url", "input_sha256", "model", "provider") if k in r}
                    for r in located
                ]
            }
            save()

        old_results = (data.get("ocr") or {}).get("results", [])
        results = enhance_asset_batch(assets, CLOUD_OCR_COMPONENT, previous=old_results, on_progress=progress)
        complete = (
            not gaps
            and len(results) == len(selected)
            and all(r.get("status") in {"ok", "completed", "success", "acquired"} for r in results)
        )
        data["ocr"] = {
            **data.get("ocr", {}),
            "status": ("ok" if selected else "not_applicable") if complete else "partial",
            "request_complete": complete,
            "reason": "no_selected_images" if not selected else "",
            "coverage": {
                "requested_images": len(images) if indexes is None else len(indexes),
                "downloaded_images": len(assets),
                "processed_images": sum(r.get("status") in {"ok", "completed", "success", "acquired"} for r in results),
            },
        }
        save()
    if request.options.get("include_transcript"):
        from kr_core.source_urls import canonical_url
        from kr_core.evidence_locators import transcript_locators
        media = [r for r in manifest if r["type"] in {"audio", "video"}]
        selected = media[:options.get("media_limit", 30)]
        embedded = [r for r in selected if r["media_access"] != "direct_candidate"]
        resolved = {}
        resolution_error = None
        if embedded:
            from collectors.platform.wechat_media import resolve
            resolution = resolve(request.url, embedded)
            resolved = {r["locator"]:r for r in resolution.get("items", [])}
            resolution_error = (resolution.get("error") or {}).get("type")
        transcripts, locations, mappings = [], [], []
        processed, cursor = 0, 0
        for offset, original in enumerate(media):
            check_execution()
            base = {"locator":original["locator"], "operation":"asr"}
            if offset >= options.get("media_limit", 30):
                gap = {**base, "status":"not_run", "reason":"request_media_limit"}
                gaps.append(gap)
                put(gap)
                continue
            ref = resolved.get(original["locator"], original)
            if ref["media_access"] != "direct_candidate":
                gap = {**base, "status":"failed", "reason":resolution_error or "embedded_player_not_direct_media"}
                gaps.append(gap)
                put(gap)
                continue
            try:
                from media_bundle import MediaRef, MediaKind
                from runtime.media_acquisition import acquire_media_ref
                if ref["type"] == "video" and ref.get("resolution_method") == "original_article_browser_dom":
                    # 已解析的具体MP4没有清晰度可选；不要送进网页下载器的480p选流器。
                    # 签名仅用于本次下载；复用须同locator、稳定URL、受控路径及原件SHA均一致。
                    reusable = [{**r,"source_url":ref["url"]} for r in previous
                                if r.get("locator")==ref["locator"] and canonical_url(r.get("source_url",""))==canonical_url(ref["url"])]
                    asset = acquire_http_asset("video", ref["url"], folder, previous=reusable,
                                               headers={"Referer":request.url})
                else:
                    asset = acquire_media_ref(MediaRef(
                        MediaKind.AUDIO if ref["type"] == "audio" else MediaKind.VIDEO, ref["url"],
                        metadata={"headers":{"Referer":request.url}}))
                asset.update(locator=ref["locator"], type=ref["type"])
                previous = [r for r in previous if r.get("locator") != ref["locator"]]+[asset]
                data["downloaded_media"] = previous
                old = next((r for r in rows if r.get("locator")==ref["locator"] and r.get("operation")=="asr"), None)
                def failure_reason(exc):
                    code = str(exc)
                    known = {"optional_component_empty_or_failed", "media_has_no_audio",
                             "optional_component_not_installed_and_approved", "optional_component_role_revision_mismatch",
                             "asr_token_budget_exhausted_no_complete_transcript"}
                    return getattr(exc,"error_code", code if code in known else type(exc).__name__)
                result = enhance_asset_batch([asset], ASR_COMPONENT, language=request.options.get("language"),
                    previous=[old] if old else [], failure_reason=failure_reason)[0]
                put({**result, **base, "source_url":canonical_url(ref["url"]),
                     "resolution_method":ref.get("resolution_method", "original_direct_media"),
                     "asset_sha256":asset["sha256"]})
                text = result.get("text") or result.get("transcript") or ""
                if text:
                    if transcripts:
                        cursor += 2
                    segments = result.get("segments")
                    if segments is None and result.get("content_locators"):
                        from kr_core.evidence_locators import validate_locators
                        native = validate_locators(text, result["content_locators"])
                        segments = [{**r,"text":text[r["start"]:r["end"]]}
                                    for r in native if "start_s" in r and "end_s" in r]
                    mapping = transcript_locators(text, segments)
                    if mapping["status"] == "aligned":
                        locations.extend({**r,"start":r["start"]+cursor,"end":r["end"]+cursor,
                                          "media_locator":ref["locator"],"input_sha256":asset["sha256"]}
                                         for r in mapping["segments"])
                    mappings.append({"media_locator":ref["locator"],"input_sha256":asset["sha256"],
                                     "start":cursor,"end":cursor+len(text),"mapping_status":mapping["status"]})
                    transcripts.append(text)
                    cursor += len(text)
                if result.get("status") in {"ok", "completed", "success", "acquired"}:
                    processed += 1
                else:
                    gaps.append({**base,"reason":result.get("reason", "asr_failed")})
            except (ExecutionStopped, ResourceDeferred):
                raise
            except Exception as exc:
                gap = {**base,"status":"failed","reason":getattr(exc,"error_code",type(exc).__name__)}
                gaps.append(gap)
                put(gap)
            save()
        data["transcript"] = "\n\n".join(transcripts)
        aligned = bool(mappings) and all(r["mapping_status"]=="aligned" for r in mappings)
        data["transcript_metadata"] = {"source":"local_asr","language":request.options.get("language") or "auto",
            "status":"aligned" if aligned else "missing", "segments":locations if aligned else [],
            "media_maps":mappings,"coverage":"observed_media_only"}
        complete = processed == len(media)
        data["transcript_result"] = {
            "status":("ok" if media else "not_applicable") if complete else "partial",
            "request_complete":complete,"reason":"no_observed_audio_video" if not media else "",
            "requested_media":len(media),"processed_media":processed,
            "selected_media":len(selected),"resolved_players":len(resolved)}
    if options.get("include_attachments"):
        from runtime.pdf_text import fetch_pdf_document
        from kr_core.evidence_store import EvidenceStore

        attachments = data.get("attachment_results", []) if same else []
        for offset, ref in enumerate(data.get("attachments", [])):
            check_execution()
            if any(r.get("url") == ref["url"] and r.get("request_complete") for r in attachments):
                continue
            if offset >= options.get("media_limit", 30):
                gaps.append({"url": ref["url"], "reason": "request_media_limit"})
                continue
            try:
                result = fetch_pdf_document(
                    ref["url"],
                    referer="https://mp.weixin.qq.com/",
                    include_ocr=request.options.get("include_ocr", False),
                    document_options=request.options.get("document") or {},
                )
                probe = result.get("text_probe", {})
                document = result.get("pdf_processing") or {}
                item = {
                    "url": ref["url"],
                    "platform": "web",
                    "title": ref["title"] or "微信文章附件",
                    "content": probe.get("text", ""),
                    "document_blocks": probe.get("document_blocks"),
                    "content_locators": probe.get("locators", []),
                    "input_sha256": result.get("input_sha256"),
                    "request_complete": bool(result.get("text_extractable"))
                    and document.get("request_satisfied") is True and bool(document.get("page_ledger")),
                    "metadata": document,
                    "document": document,
                    "provenance": {"discovered_from": request.url, "relation": "attachment"},
                }
                if item["content"]:
                    EvidenceStore().record_detail(platform="web", url=ref["url"], response=item)
                if item.get("evidence_storage_gaps") or not item.get("evidence_assets"):
                    item["request_complete"] = False
                if not item["request_complete"]:
                    gaps.append({"url": ref["url"], "reason": "attachment_incomplete"})
                attachments = [r for r in attachments if r.get("url") != ref["url"]] + [item]
                data["attachment_results"] = attachments
                put(
                    {
                        "locator": ref["url"],
                        "operation": "attachment",
                        "status": "ok" if item["request_complete"] else "partial",
                        "input_sha256": result.get("input_sha256"),
                        "evidence_assets": item.get("evidence_assets", []),
                    }
                )
            except (ExecutionStopped, ResourceDeferred):
                raise
            except Exception as exc:
                gaps.append({"url": ref["url"], "reason": getattr(exc, "error_code", type(exc).__name__)})
            save()
        requested = data.get("attachments", [])
        completed = sum(
            any(row.get("url") == ref["url"] and row.get("request_complete") for row in attachments)
            for ref in requested
        )
        data["attachment_result"] = {
            "status": ("ok" if requested else "not_applicable") if completed == len(requested) else "partial",
            "request_complete": completed == len(requested),
            "requested_attachments": len(requested),
            "processed_attachments": completed,
            "reason": "no_observed_pdf_attachment_links" if not requested else "",
            "scope": "observed_pdf_links_only",
        }
    if request.options.get("include_comments"):
        if not data.get('comments_complete'):
            gaps.append({"part":"comments","reason":data.get('comments_gap','reader_window_not_exhausted')})
    data["completed_stages"] = list(dict.fromkeys([*data.get("completed_stages", []), "media"]))
    data["enhancement_status"] = (
        "partial"
        if gaps or any((data.get(k) or {}).get("request_complete") is False for k in ("ocr", "transcript_result"))
        else "ok"
    )
    data["status"] = "partial" if data["enhancement_status"] == "partial" else "ok"
    save()
    return data


class WechatDetailStrategy:
    def extract(self, request):
        return DetailResponse.from_legacy(
            "微信", request.url, collect(request), metadata={"strategy": "wechat_readonly"}
        )
