"""Shared Xiaohongshu selector contract for bridge and CDP detail reads."""

from __future__ import annotations

import json
from typing import Any, Dict, List


XHS_SELECTOR_BUNDLE_VERSION = "xhs-selector-bundle-20260613-v1"

XHS_DETAIL_SELECTORS: Dict[str, List[str]] = {
    "title": [
        "#detail-title",
        'meta[property="og:title"]',
        '[class*="title"]',
    ],
    "body": [
        "#detail-desc",
        '[class*="note-content"]',
        '[class*="note-text"]',
        '[class*="desc"]',
        '[class*="content"]',
    ],
    "author": [
        '[class*="author"] span',
        '[class*="user"] span',
        ".username",
        '[class*="author"]',
        '[class*="user"]',
    ],
    "interaction": [
        '[class*="interaction"]',
    ],
}


def selector_bundle_metadata() -> Dict[str, Any]:
    return {
        "schema": "knowledgeradar-xhs-selector-bundle/v1",
        "version": XHS_SELECTOR_BUNDLE_VERSION,
        "fields": {field: list(selectors) for field, selectors in XHS_DETAIL_SELECTORS.items()},
        "alert_threshold": {
            "window": 5,
            "selector_zero_hits": 3,
            "scheduled_patrol": False,
        },
    }


def all_detail_selectors() -> List[str]:
    seen: set[str] = set()
    selectors: List[str] = []
    for field_selectors in XHS_DETAIL_SELECTORS.values():
        for selector in field_selectors:
            if selector.startswith("meta["):
                continue
            if selector not in seen:
                seen.add(selector)
                selectors.append(selector)
    return selectors


def xhs_detail_selector_js(max_chars: int = 2400) -> str:
    selectors_json = json.dumps(XHS_DETAIL_SELECTORS, ensure_ascii=False)
    version_json = json.dumps(XHS_SELECTOR_BUNDLE_VERSION)
    return f"""
() => {{
  const clean = value => (value || '').replace(/\\s+/g, ' ').trim();
  const bundleVersion = {version_json};
  const fields = {selectors_json};
  const text = document.body ? document.body.innerText || '' : '';
  const captchaSelectors = [
    '[class*="captcha"]', '[id*="captcha"]', '[class*="verify"]', '[id*="verify"]',
    '[class*="geetest"]', '[id*="geetest"]', 'iframe[src*="captcha"]', 'iframe[src*="verify"]'
  ];
  const countMatches = selectors => selectors.reduce((sum, selector) => {{
    try {{ return sum + document.querySelectorAll(selector).length; }} catch (_) {{ return sum; }}
  }}, 0);
  const selectorTexts = {{}};
  const selectorHitsByField = {{}};
  for (const field of Object.keys(fields)) {{
    selectorHitsByField[field] = 0;
    for (const selector of fields[field]) {{
      let values = [];
      if (selector.startsWith('meta[')) {{
        const node = document.querySelector(selector);
        const value = node ? clean(node.getAttribute('content') || node.content || '') : '';
        if (value) values = [value];
      }} else {{
        const nodes = Array.from(document.querySelectorAll(selector)).slice(0, 5);
        values = nodes.map(node => clean(node.textContent)).filter(Boolean).slice(0, 5);
      }}
      if (values.length) {{
        selectorTexts[selector] = values;
        selectorHitsByField[field] += values.length;
      }}
    }}
  }}
  const selectorHitCount = Object.values(selectorHitsByField).reduce((sum, value) => sum + Number(value || 0), 0);
  const images = [];
  const imageAssets = [];
  const imageQuality = {{content_count: 0, unknown_count: 0, noise_count: 0}};
  const seenImages = new Set();
  function imageRole(node) {{
    let current = node;
    const classes = [];
    for (let depth = 0; current && depth < 6; depth += 1, current = current.parentElement) {{
      classes.push(String(current.className || ''));
      classes.push(String(current.getAttribute && current.getAttribute('data-testid') || ''));
    }}
    const context = classes.join(' ').toLowerCase();
    if (/(avatar|comment|reply|author|user|emoji|icon|toolbar|qrcode|qr-code)/i.test(context)) return 'noise';
    if (/(note-content|note-detail|note-item|swiper|carousel|image-container|media|cover)/i.test(context)) return 'content';
    return 'unknown';
  }}
  function addImage(src, node) {{
    src = clean(src);
    if (!src || src.startsWith('data:')) return;
    if (src.startsWith('//')) src = 'https:' + src;
    if (!/^https?:\\/\\//.test(src)) return;
    if (!/(xhscdn|sns-img|xiaohongshu|ci\\.xiaohongshu|xhs)/i.test(src)) return;
    if (!seenImages.has(src)) {{
      seenImages.add(src);
      const role = imageRole(node);
      imageAssets.push({{url: src, role}});
      imageQuality[role + '_count'] = Number(imageQuality[role + '_count'] || 0) + 1;
      if (role !== 'noise') images.push(src);
    }}
  }}
  for (const img of Array.from(document.querySelectorAll('img'))) {{
    addImage(img.currentSrc || img.src || img.getAttribute('data-src') || img.getAttribute('data-original') || '', img);
    const srcset = img.getAttribute('srcset') || '';
    if (srcset) srcset.split(',').forEach(part => addImage(part.trim().split(/\\s+/)[0], img));
  }}
  return {{
    url: location.href,
    title: document.title || '',
    textSample: text.slice(0, {int(max_chars)}),
    textLen: text.length,
    selectorTexts,
    selector_bundle_version: bundleVersion,
    selector_hits_by_field: selectorHitsByField,
    selector_hit_count: selectorHitCount,
    captchaElementCount: countMatches(captchaSelectors),
    loadingState: document.readyState || '',
    image_count: images.length,
    images: images.slice(0, 20),
    image_assets: imageAssets.slice(0, 40),
    image_quality: imageQuality
  }};
}}
"""


def selector_stats_from_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    hits_by_field = snapshot.get("selector_hits_by_field") or snapshot.get("selectorHitsByField") or {}
    if not isinstance(hits_by_field, dict):
        hits_by_field = {}
    try:
        hit_count = int(snapshot.get("selector_hit_count") or snapshot.get("selectorHitCount") or sum(int(v or 0) for v in hits_by_field.values()))
    except Exception:
        hit_count = 0
    return {
        "selector_bundle_version": str(snapshot.get("selector_bundle_version") or snapshot.get("selectorBundleVersion") or XHS_SELECTOR_BUNDLE_VERSION),
        "selector_hits_by_field": {str(k): int(v or 0) for k, v in hits_by_field.items()},
        "selector_hit_count": hit_count,
        "captcha_element_count": _safe_int(snapshot.get("captcha_element_count") or snapshot.get("captchaElementCount"), 0),
        "loading_state": str(snapshot.get("loading_state") or snapshot.get("loadingState") or ""),
        "image_count": _safe_int(snapshot.get("image_count") or snapshot.get("imageCount"), 0),
    }


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default
