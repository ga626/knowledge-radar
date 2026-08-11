"""TikHub Xiaohongshu response normalization helpers.

The adapter is pure and offline: it never calls TikHub, opens browsers, or
switches accounts. Live callers are responsible for billing and policy gates.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

from kr_core.models import SearchResponse, SearchResultItem


XHS_PLATFORM = "小红书"


def normalize_tikhub_xhs_search_response(payload: Dict[str, Any], *, keyword: str = "", limit: int = 10) -> SearchResponse:
    """Normalize TikHub XHS search JSON into KnowledgeRadar search results."""
    items = []
    for raw_item in _extract_items(payload):
        note = raw_item.get("note") if isinstance(raw_item, dict) and isinstance(raw_item.get("note"), dict) else raw_item
        if not isinstance(note, dict):
            continue
        normalized = _normalize_note(note, raw_item=raw_item, keyword=keyword)
        if normalized:
            items.append(normalized)
        if limit > 0 and len(items) >= limit:
            break
    return SearchResponse(
        platform=XHS_PLATFORM,
        items=items,
        metadata={
            "provider": "tikhub",
            "source": "api_search_discovery_fallback",
            "keyword": keyword,
            "requested_limit": limit,
            "raw_item_count": len(_extract_items(payload)),
            "raw_path": "data.data.items",
            "billing_warning": _billing_warning(payload),
        },
    )


def normalize_tikhub_xhs_detail_response(payload: Dict[str, Any], *, note_id: str = "") -> Dict[str, Any]:
    """Normalize TikHub XHS detail JSON into Xiaohongshu noteData."""
    note = _extract_detail_note(payload)
    if not note:
        return {}
    user = _first_dict(note, "user", "author", "user_info", "owner", "note_user")
    content = _first_text(note, "desc", "description", "content", "text")
    title = _first_text(note, "display_title", "title", "note_card_title") or content[:80]
    return {
        "title": title,
        "desc": content,
        "content": content,
        "author": _first_text(user, "nickname", "name", "user_name", "userName") if user else _first_text(note, "nickname", "author"),
        "images": _extract_detail_images(note),
        "note_id": _first_text(note, "id", "note_id", "noteId", "noteIdStr") or note_id,
        "source": "tikhub_api_detail",
        "raw_keys": sorted(str(key) for key in note.keys())[:80],
    }


def _extract_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates: Iterable[Any] = (
        (((payload.get("data") or {}).get("data") or {}).get("items") if isinstance(payload.get("data"), dict) else None),
        ((payload.get("data") or {}).get("items") if isinstance(payload.get("data"), dict) else None),
        payload.get("items"),
    )
    for candidate in candidates:
        if isinstance(candidate, list):
            return [row for row in candidate if isinstance(row, dict)]
    return []


def _extract_detail_note(payload: Dict[str, Any]) -> Dict[str, Any]:
    candidates: Iterable[Any] = (
        (((payload.get("data") or {}).get("data") or {}).get("note") if isinstance(payload.get("data"), dict) else None),
        (((payload.get("data") or {}).get("data") or {}).get("item") if isinstance(payload.get("data"), dict) else None),
        ((payload.get("data") or {}).get("note") if isinstance(payload.get("data"), dict) else None),
        ((payload.get("data") or {}).get("item") if isinstance(payload.get("data"), dict) else None),
        payload.get("note"),
        payload.get("item"),
    )
    for candidate in candidates:
        if isinstance(candidate, dict):
            return candidate
    data = payload.get("data")
    if isinstance(data, dict):
        nested = data.get("data")
        if isinstance(nested, dict):
            return nested
        return data
    return {}


def _extract_detail_images(note: Dict[str, Any]) -> List[str]:
    raw_images = _first_value(note, "images", "image_list", "imageList", "imgs", "image")
    images: List[str] = []
    if isinstance(raw_images, list):
        for row in raw_images:
            if isinstance(row, str):
                images.append(row)
            elif isinstance(row, dict):
                value = _first_text(row, "url", "src", "original", "trace_id")
                if value:
                    images.append(value)
    elif isinstance(raw_images, str):
        images.append(raw_images)
    return [image for image in images if image][:20]


def _normalize_note(note: Dict[str, Any], *, raw_item: Dict[str, Any], keyword: str) -> SearchResultItem | None:
    note_id = _first_text(note, "id", "note_id", "noteId", "noteIdStr", "note_id_str")
    xsec_token = _first_text(note, "xsec_token", "xsecToken", "xsec_token_str")
    title = _first_text(note, "display_title", "title", "desc", "content", "note_card_title") or keyword
    summary = _first_text(note, "desc", "description", "content", "display_title", "title")
    user = _first_dict(note, "user", "author", "user_info", "owner")
    author = _first_text(user, "nickname", "name", "user_name", "userName") if user else _first_text(note, "nickname", "author")
    url = _note_url(note_id, xsec_token)
    if not note_id and not url and not title:
        return None
    stats = _stats(note)
    metadata = {
        "provider": "tikhub",
        "raw_model_type": raw_item.get("model_type") if isinstance(raw_item, dict) else "",
        "note_id": note_id,
        "xsec_token_present": bool(xsec_token),
        "raw_keys": sorted(str(key) for key in note.keys())[:60],
    }
    if user:
        metadata["user_keys"] = sorted(str(key) for key in user.keys())[:40]
    return SearchResultItem(
        title=title[:200],
        url=url,
        platform=XHS_PLATFORM,
        author=author,
        summary=(summary or title)[:300],
        content_type=_first_text(note, "type", "note_type", "noteType") or "note",
        source="tikhub_api",
        stats=stats,
        metadata=metadata,
    )


def _note_url(note_id: str, xsec_token: str) -> str:
    if not note_id:
        return ""
    url = f"https://www.xiaohongshu.com/explore/{note_id}"
    if xsec_token:
        url += f"?xsec_token={xsec_token}&xsec_source=pc_search"
    return url


def _stats(note: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in {
            "like": _first_value(note, "liked_count", "like_count", "likes", "likedCount"),
            "favorite": _first_value(note, "collected_count", "collect_count", "favorite_count", "collectedCount"),
            "reply": _first_value(note, "comment_count", "comments", "commentCount"),
            "share": _first_value(note, "share_count", "shareCount"),
        }.items()
        if value not in (None, "")
    }


def _billing_warning(payload: Dict[str, Any]) -> bool:
    text = " ".join(str(payload.get(key) or "") for key in ("message", "message_zh", "cache_message", "cache_message_zh"))
    return "charge" in text.lower() or "计费" in text or "扣费" in text


def _first_dict(source: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    for key in keys:
        value = source.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _first_text(source: Dict[str, Any], *keys: str) -> str:
    value = _first_value(source, *keys)
    return "" if value in (None, "") else str(value)


def _first_value(source: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return value
    return ""
