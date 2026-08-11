"""Shared Xiaohongshu search candidate normalization."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List

from runtime.xhs_selector_contract import (
    XHS_SELECTOR_BUNDLE_VERSION,
    selector_stats_from_snapshot,
    xhs_detail_selector_js,
)


NOTE_ID_RE = re.compile(r"/(?:explore|search_result|discovery/item)/([^/?#]+)", re.I)
XSEC_TOKEN_RE = re.compile(r"[?&]xsec_token=([^&#]+)", re.I)


def normalize_xhs_search_candidates(
    items: Iterable[Dict[str, Any]],
    *,
    feed_type: str = "",
    source: str = "scrapling",
) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    by_note_id: Dict[str, Dict[str, Any]] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        note_id = str(item.get("noteId") or item.get("note_id") or "").strip()
        href = str(item.get("url") or item.get("href") or item.get("raw_url") or "").strip()
        if not note_id and href:
            match = NOTE_ID_RE.search(href)
            note_id = match.group(1) if match else ""
        if not note_id:
            continue

        xsec_token = str(item.get("xsecToken") or item.get("xsec_token") or "").strip()
        if not xsec_token and href:
            token_match = XSEC_TOKEN_RE.search(href)
            xsec_token = token_match.group(1) if token_match else ""

        title = str(item.get("title") or "").strip()
        desc = str(item.get("desc") or item.get("description") or "").strip()
        if not title and desc:
            title = desc[:80]
        if not title:
            continue

        note_url = build_xhs_note_url(note_id, xsec_token)
        candidate = {
            "title": title,
            "author": str(item.get("author") or "").strip(),
            "desc": desc[:200],
            "url": note_url,
            "raw_url": href,
            "likes": item.get("likes", 0),
            "note_id": note_id,
            "xsec_token": xsec_token,
            "platform": "小红书",
            "type": feed_type or item.get("noteType") or item.get("type") or "",
            "source": source,
        }
        existing = by_note_id.get(note_id)
        if not existing:
            by_note_id[note_id] = candidate
        elif not existing.get("xsec_token") and xsec_token:
            by_note_id[note_id] = {**existing, **candidate}
    normalized.extend(by_note_id.values())
    return normalized


def build_xhs_note_url(note_id: str, xsec_token: str = "", xsec_source: str = "pc_search") -> str:
    url = f"https://www.xiaohongshu.com/explore/{note_id}"
    if xsec_token:
        url += f"?xsec_token={xsec_token}&xsec_source={xsec_source or 'pc_search'}"
    return url


def visible_click_candidates(items: Iterable[Dict[str, Any]], *, min_size: int = 40) -> List[Dict[str, Any]]:
    """Return candidates with visible root boxes and a safe center click point."""
    visible: List[Dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        box = item.get("rootBox") or item.get("root_box") or {}
        if not isinstance(box, dict):
            continue
        try:
            x = float(box.get("x") or 0)
            y = float(box.get("y") or 0)
            width = float(box.get("width") or 0)
            height = float(box.get("height") or 0)
        except Exception:
            continue
        if not item.get("visible") or width < min_size or height < min_size:
            continue
        enriched = dict(item)
        enriched["click_point"] = {
            "x": round(x + width / 2),
            "y": round(y + height / 2),
        }
        visible.append(enriched)
    return visible


def xhs_search_card_snapshot_js(max_links: int = 80, max_cards: int = 20) -> str:
    """Browser-side JS for read-only XHS search card snapshots."""
    return f"""
() => {{
  const text = document.body ? document.body.innerText || '' : '';
  const cards = [];
  const seen = new Set();
  function clean(value) {{ return (value || '').replace(/\\s+/g, ' ').trim(); }}
  const links = Array.from(document.querySelectorAll('a[href*="/explore/"], a[href*="/search_result/"]'))
    .sort((a, b) => ((b.href || '').includes('xsec_token=') ? 1 : 0) - ((a.href || '').includes('xsec_token=') ? 1 : 0));
  for (const link of links.slice(0, {int(max_links)})) {{
    const href = link.href || link.getAttribute('href') || '';
    const idMatch = href.match(/\\/(?:explore|search_result)\\/([^/?#]+)/);
    if (!idMatch || seen.has(idMatch[1])) continue;
    const root = link.closest('[class*="note-item"], section, [data-v-], div') || link;
    const rect = root.getBoundingClientRect();
    const linkRect = link.getBoundingClientRect();
    const titleNode = root.querySelector('[class*="title"], [class*="desc"], .title, .desc');
    const authorNode = root.querySelector('[class*="author"], [class*="user"], .name, .username');
    const title = clean(link.getAttribute('title') || link.getAttribute('aria-label') || (titleNode && titleNode.textContent) || link.textContent).slice(0, 120);
    const visible = !!(rect.width && rect.height && rect.bottom > 0 && rect.right > 0 && rect.top < window.innerHeight && rect.left < window.innerWidth);
    seen.add(idMatch[1]);
    cards.push({{
      noteId: idMatch[1],
      url: href,
      title,
      author: authorNode ? clean(authorNode.textContent).slice(0, 80) : '',
      visible,
      rootBox: {{x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)}},
      linkBox: {{x: Math.round(linkRect.x), y: Math.round(linkRect.y), width: Math.round(linkRect.width), height: Math.round(linkRect.height)}}
    }});
  }}
  return {{url: location.href, title: document.title || '', textSample: text.slice(0, 800), rawCount: links.length, cards: cards.slice(0, {int(max_cards)})}};
}}
"""


def xhs_detail_content_snapshot_js(max_chars: int = 2400) -> str:
    """Browser-side JS for read-only XHS detail text/selector snapshots."""
    return xhs_detail_selector_js(max_chars=max_chars)


def normalize_xhs_detail_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a read-only XHS detail snapshot into title/body candidates."""
    selector_texts = snapshot.get("selectorTexts") or snapshot.get("selector_texts") or {}
    if not isinstance(selector_texts, dict):
        selector_texts = {}

    def first_text(*selectors: str) -> str:
        for selector in selectors:
            values = selector_texts.get(selector) or []
            if isinstance(values, str):
                values = [values]
            for value in values:
                text = _clean_text(value)
                if text:
                    return text
        return ""

    title = first_text("#detail-title", '[class*="title"]')
    body = first_text("#detail-desc", '[class*="note-content"]', '[class*="desc"]')
    if body and title and body == title:
        body = ""

    text_sample = _clean_text(snapshot.get("textSample") or snapshot.get("text_sample") or "")
    fallback_title, fallback_body = _xhs_body_from_text_sample(text_sample)
    if not title:
        title = fallback_title
    if not body:
        body = fallback_body
    content_signals = [token for token in ("赞", "评论", "展开", "关注") if token in text_sample]
    selector_keys = [key for key, value in selector_texts.items() if value]
    status = "ok" if title or body else ("weak" if text_sample else "empty")
    selector_stats = selector_stats_from_snapshot(snapshot)
    image_assets, images, image_quality = normalize_xhs_image_assets(snapshot)
    return {
        "schema": "xhs-detail-snapshot-normalized/v1",
        "status": status,
        "selector_bundle_version": selector_stats.get("selector_bundle_version") or XHS_SELECTOR_BUNDLE_VERSION,
        "url": str(snapshot.get("url") or ""),
        "page_title": str(snapshot.get("title") or ""),
        "title": title,
        "body": body,
        "text_len": int(snapshot.get("textLen") or snapshot.get("text_len") or len(text_sample) or 0),
        "selector_keys": selector_keys,
        "selector_hits_by_field": selector_stats.get("selector_hits_by_field") or {},
        "selector_hit_count": int(selector_stats.get("selector_hit_count") or 0),
        "captcha_element_count": int(selector_stats.get("captcha_element_count") or 0),
        "loading_state": str(selector_stats.get("loading_state") or ""),
        "image_count": int(selector_stats.get("image_count") or 0),
        "images": images[:20],
        "image_assets": image_assets[:40],
        "image_quality": image_quality,
        "content_signals": content_signals,
    }


def normalize_xhs_image_assets(payload: Dict[str, Any]) -> tuple[List[Dict[str, str]], List[str], Dict[str, int]]:
    """Keep image provenance and drop only DOM-confirmed UI noise."""
    raw_assets = payload.get("image_assets") if isinstance(payload.get("image_assets"), list) else []
    assets: List[Dict[str, str]] = []
    if raw_assets:
        for item in raw_assets:
            if not isinstance(item, dict):
                continue
            url = _clean_text(item.get("url") or item.get("src") or "")
            role = _clean_text(item.get("role") or "unknown").lower()
            if not url or role not in {"content", "unknown", "noise"}:
                continue
            assets.append({"url": url, "role": role})
    else:
        raw_images = payload.get("images") if isinstance(payload.get("images"), list) else []
        assets = [{"url": _clean_text(item), "role": "unknown"} for item in raw_images if _clean_text(item)]
    quality = {"content_count": 0, "unknown_count": 0, "noise_count": 0}
    for item in assets:
        key = f"{item['role']}_count"
        quality[key] = quality.get(key, 0) + 1
    content = [item["url"] for item in assets if item["role"] == "content"]
    unknown = [item["url"] for item in assets if item["role"] == "unknown"]
    images = list(dict.fromkeys(content or unknown))
    return assets, images, quality


def _xhs_body_from_text_sample(text_sample: str) -> tuple[str, str]:
    text = _clean_text(text_sample)
    if len(text) < 40:
        return "", ""
    blocked_markers = ("登录", "验证码", "安全验证", "请先登录", "滑块", "页面不见了")
    if any(marker in text for marker in blocked_markers):
        return "", ""
    nav_markers = (
        "首页",
        "发现",
        "购物",
        "消息",
        "我",
        "小红书",
        "赞",
        "评论",
        "收藏",
        "分享",
        "展开",
        "关注",
    )
    lines = [
        _clean_text(line)
        for line in re.split(r"[\r\n]+", text_sample)
        if _clean_text(line)
    ]
    content_lines = [
        line
        for line in lines
        if len(line) >= 8
        and not any(line == marker for marker in nav_markers)
        and not re.fullmatch(r"[\d\s万wWkK,.，。]+", line)
    ]
    title = content_lines[0] if content_lines else ""
    body = " ".join(content_lines[1:4]).strip() if len(content_lines) > 1 else ""
    if not body:
        stripped = text
        for marker in nav_markers:
            stripped = stripped.replace(marker, " ")
        stripped = _clean_text(stripped)
        if len(stripped) >= 60:
            body = stripped[:1000]
    if body == title:
        body = ""
    return title[:160], body


def xhs_detail_text_quality(detail: Dict[str, Any]) -> Dict[str, Any]:
    """Score normalized XHS detail text for admission probes."""
    title = _clean_text(detail.get("title"))
    body = _clean_text(detail.get("body"))
    text_len = int(detail.get("text_len") or len(body) or 0)
    selector_keys = list(detail.get("selector_keys") or [])
    has_title = bool(title)
    has_body = bool(body)
    body_len = len(body)
    quality = "ok" if has_title and has_body and body_len >= 12 else ("weak" if has_title or has_body else "empty")
    return {
        "schema": "xhs-detail-text-quality/v1",
        "status": quality,
        "has_title": has_title,
        "has_body": has_body,
        "body_len": body_len,
        "text_len": text_len,
        "selector_count": len(selector_keys),
        "admission": "content_text_ok" if quality == "ok" else "content_text_not_admitted",
    }


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()
