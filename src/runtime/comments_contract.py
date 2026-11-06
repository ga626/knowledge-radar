"""评论只统一证据语义；平台仍负责合法取数、分页和关系取证。"""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit

CONTRACT = "knowledgeradar-comments/v2"


def platform_comment_scope(platform):
    """可执行对象与关系边界，不把支持声明冒充真实验收或内容领域限制。"""
    return {
        "知乎": {
            "objects": ["answer"],
            "streams": ["answer_comments"],
            "relationship": "native_id_and_dom_thread_root; direct_parent_unknown",
            "channel": "opencli_normal_reader",
            "limits": ["article_comments_not_implemented", "time_order_not_exposed", "visible_threads_only_no_hidden_pagination"],
        },
        "小红书": {
            "objects": ["note"],
            "streams": ["visible_dom_comments"],
            "relationship": "native_or_snapshot_root; direct_parent_unknown_unless_observed",
            "channel": "opencli",
            "limits": [
                "max_50_roots_200_total",
                "scroll_round_budget; no_source_exhaustion_without_proof",
                "time_order_not_exposed",
            ],
        },
        "GitHub": {
            "objects": ["issue", "pull_request"],
            "streams": ["issue_comments", "reviews", "review_comments"],
            "relationship": "native_reply_id_in_review_comments; parallel_streams_keep_identity",
            "channel": "official_readonly_api",
            "limits": ["chronological_stream; no_relevance_sort", "no_deeper_parent_from_mentions"],
        },
        "B站": {
            "objects": ["video"],
            "streams": ["video_reply"],
            "relationship": "native_rpid_parent_root",
            "channel": "public_readonly_api",
            "limits": ["login_or_risk_stops_collection"],
        },
        "YouTube": {
            "objects": ["video"],
            "streams": ["commentThreads", "comments"],
            "relationship": "api_parent_is_top_level",
            "channel": "youtube_data_api_v3",
            "limits": ["requires_configured_key_and_quota", "audio_video_not_exposed_by_comment_api"],
        },
        "微信": {
            "objects": ["public_article"],
            "streams": ["selected_reader_visible_comments"],
            "relationship": "observed_dom_reply_only",
            "channel": "opencli",
            "limits": [
                "reader_container_or_permission_may_be_absent",
                "single_visible_snapshot_not_all_comments",
                "owner_management_api_not_used",
                "time_order_not_exposed",
            ],
        },
    }.get(platform, {"objects": [], "streams": [], "relationship": "unverified", "limits": ["scope_not_declared"]})


def media_from_body(body):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(body or "", "html.parser")
    rows = []
    for kind, tag in (("image", "img"), ("audio", "audio"), ("video", "video")):
        for node in soup.find_all(tag):
            if any(
                "emoji" in str(value) or "emoticon" in str(value) or "avatar" in str(value)
                for value in node.get("class", [])
            ):
                continue
            source = node.get("data-src") or node.get("src")
            if not source and node.find("source"):
                source = node.find("source").get("src")
            if source:
                rows.append({"type": kind, "url": source})
    rows.extend(
        {"type": "image", "url": match}
        for match in re.findall(r"!\[[^\]]*\]\((https?://[^\s)]+)(?:\s+[^)]*)?\)", body or "")
    )
    return rows


def normalize_comments(data, *, platform, source_url, options=None):
    """保留原字段/原文；缺少关系不按昵称、顺序或@猜测。"""
    options = options or {}
    fresh_observation = (data.get('comments_metadata') or {}).get('schema') != CONTRACT
    raw = data.get("comments") or []
    if not isinstance(raw, list):
        raise ValueError("invalid_comments_payload")
    snapshot = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
    rows, seen, gaps = [], {}, []
    for index, original in enumerate(raw):
        if not isinstance(original, dict):
            gaps.append({"reason": "invalid_comment_row", "index": index})
            continue
        row = dict(original)
        body = str(row.get("content", row.get("body", row.get("text", ""))) or "")
        native = row.get("comment_id") or row.get("id") or row.get("native_comment_id")
        identifier = str(native) if native else f"snapshot:{snapshot}:{index}"
        stream = str(row.get("discussion_kind") or "comments")
        key = stream + ":" + identifier
        if key in seen:
            if seen[key] != original:
                gaps.append({"reason": "conflicting_comment_identity", "comment_id": identifier, "stream": stream})
            continue
        seen[key] = original
        parent = row.get("parent_comment_id", row.get("parent_id", row.get("in_reply_to_id")))
        root = row.get("root_comment_id", row.get("root_id"))
        parent = None if parent in (None, "", 0, "0") else str(parent)
        root = None if root in (None, "", 0, "0") else str(root)
        is_reply = bool(row.get("is_reply") or parent or (root and root != identifier))
        confidence = row.get("relationship_confidence") or (
            "native_parent" if parent else "unknown" if is_reply else "root"
        )
        if not is_reply:
            root = identifier
        if platform == "GitHub" and parent:
            confidence = "native_parent"
            is_reply = True
        # The normalized manifest is authoritative on repeated passes. Public
        # URL disclosure may re-encode query values in legacy source fields;
        # rediscovering them would download the same signed image twice.
        media = list(row.get("media") or [])
        if fresh_observation:
            media += media_from_body(body)
        for kind, field in (("image", "images"), ("audio", "audio"), ("video", "video")) if fresh_observation else ():
            values = row.get(field) or []
            for value in values if isinstance(values, list) else [values]:
                url = value.get("url") if isinstance(value, dict) else value
                if isinstance(url, str):
                    media.append({"type": kind, "url": url})
        clean, media_seen = [], set()
        for item in media:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "")
            try:
                target = urlsplit(url)
                private_ref = url.startswith("urn:kr:comment-media:") or (
                    platform == "小红书" and url.startswith("urn:kr:xhs-media:")
                )
                valid = private_ref or (
                    target.scheme in {"https", "http"}
                    and bool(target.hostname)
                    and not target.username
                    and not target.password
                )
            except ValueError:
                valid = False
            if not valid:
                continue
            if not private_ref:
                from runtime.comment_media import register_media_source

                url = register_media_source(url, source_url=source_url, fresh_observation=fresh_observation)
            identity = (item.get("type"), url)
            if identity in media_seen:
                continue
            media_seen.add(identity)
            clean.append(
                {
                    **item,
                    "url": url,
                    "source_host": item.get("source_host") or target.hostname or "",
                    "status": item.get("status", "manifest_only"),
                    "locator": {
                        "kind": "comment_media",
                        "comment_id": identifier,
                        "stream": stream,
                        "media_index": len(clean),
                    },
                }
            )
        row.update(
            comment_id=identifier,
            id_scope=row.get("id_scope") or ("platform" if native else "snapshot"),
            snapshot_id=row.get("snapshot_id", snapshot),
            platform=platform,
            source_url=source_url,
            content=body,
            parent_comment_id=parent,
            root_comment_id=root,
            is_reply=is_reply,
            relationship_confidence=confidence,
            discussion_kind=stream,
            media=clean,
            locator={"kind": "platform_comment", "comment_id": identifier, "stream": stream},
        )
        rows.append(row)
    data["comments"] = rows
    old = data.get("comments_metadata") or {}
    coverage = data.get("comments_coverage")
    structured = coverage if isinstance(coverage, dict) else {}
    observed_zero = data.get("comments_empty_confirmed") is True or coverage == "observed_zero_comments"
    satisfied = (
        data.get("comments_complete") is True
        or old.get("request_window_complete") is True
        or structured.get("request_satisfied") is True
        or observed_zero
    )
    exhausted = (
        data.get("comments_exhausted") is True
        or old.get("complete_source") is True
        or structured.get("exhausted") is True
        or observed_zero
    )
    pagination = data.get("comments_pagination") or {}
    if pagination:
        exhausted = all(item.get("all_comments_exhausted") is True for item in pagination.values())
    reason = (
        data.get("comments_gap")
        or old.get("stop_reason")
        or structured.get("stop_reason")
        or ("source_exhausted" if exhausted else "requested_window" if satisfied else "coverage_unverified")
    )
    if gaps:
        satisfied = False
    if platform in {"知乎", "小红书", "微信"} and options.get("order") == "time":
        gaps.append({"reason": "requested_sort_not_exposed_by_channel", "field": "order"})
        satisfied = False
    if platform == 'GitHub' and options.get('order') == 'relevance':
        # 保留原生时间流而非伪造热度排序；只是排序差异，不否定已取得窗口。
        old={**old,'sort_note':'GitHub comment streams are chronological; relevance applies only to the derived reading view'}
    data.update(
        comments_complete=satisfied,
        comments_exhausted=exhausted,
        comments_metadata={
            **old,
            "schema": CONTRACT,
            "returned_comments": len(rows),
            "request": options,
            "request_window_complete": satisfied,
            "complete_source": exhausted,
            "stop_reason": reason,
            "identity_gaps": gaps,
            "original_coverage": coverage,
            "streams": pagination,
            "actual_order": "platform_default"
            if platform in {"知乎", "小红书", "微信"}
            else "chronological_stream"
            if platform == "GitHub"
            else options.get("order", "relevance"),
            "media_scope": "only_observed_references; no_hidden_attachments_assertion",
        },
    )
    return data
