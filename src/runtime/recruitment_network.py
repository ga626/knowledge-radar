"""Helpers for extracting recruitment search items from dynamic JSON payloads."""

from __future__ import annotations

import json
import re
from urllib.parse import unquote
from typing import Any

from runtime.recruitment_address import attach_recruitment_address_contract


TITLE_KEYS = (
    "jobName",
    "job_name",
    "jobTitle",
    "positionName",
    "position_name",
    "title",
)
COMPANY_KEYS = (
    "brandName",
    "brand_name",
    "companyName",
    "company_name",
    "compName",
    "company",
    "companyFullName",
)
SALARY_KEYS = ("salaryDesc", "salary_desc", "salary", "salaryText", "salaryName", "salaryNameCn")
AREA_KEYS = (
    "jobArea",
    "job_area",
    "area",
    "cityName",
    "city",
    "location",
    "workPlace",
    "districtName",
    "dq",
)
NATIVE_LOCATION_KEYS = (
    "cityName",
    "city_name",
    "areaDistrict",
    "area_district",
    "districtName",
    "district_name",
    "businessDistrict",
    "business_district",
)
URL_KEYS = ("url", "jobUrl", "job_url", "positionURL", "positionUrl", "link", "href")
ID_KEYS = ("encryptJobId", "jobId", "job_id", "positionId", "position_id", "dataId")


def recruitment_network_observer_script() -> str:
    """Return a tiny in-page observer for fetch/XHR JSON responses.

    The observer only records response URL, status and a bounded text body in the
    current page context. It does not replay requests, bypass login, or alter
    responses consumed by the page.
    """

    return r"""
(() => {
  if (window.__KR_RECRUITMENT_NETWORK__ && window.__KR_RECRUITMENT_NETWORK__.installed) return;
  const state = window.__KR_RECRUITMENT_NETWORK__ = {
    installed: true,
    entries: [],
    maxEntries: 80,
    maxBodyChars: 250000
  };
  const keep = (entry) => {
    try {
      const url = String(entry.url || '');
      const body = String(entry.body || '');
      if (!url || !body) return;
      if (!/(job|search|position|zhaopin|geek|boss|liepin|pc-search|api)/i.test(url + ' ' + body.slice(0, 500))) return;
      state.entries.push({
        url,
        status: entry.status || 0,
        contentType: entry.contentType || '',
        body: body.slice(0, state.maxBodyChars),
        ts: Date.now()
      });
      if (state.entries.length > state.maxEntries) state.entries.splice(0, state.entries.length - state.maxEntries);
    } catch (e) {}
  };
  const originalFetch = window.fetch;
  if (typeof originalFetch === 'function') {
    window.fetch = async function(...args) {
      const response = await originalFetch.apply(this, args);
      try {
        const clone = response.clone();
        const contentType = clone.headers && clone.headers.get ? clone.headers.get('content-type') || '' : '';
        if (/json|text|javascript/i.test(contentType)) {
          clone.text().then(body => keep({
            url: clone.url || String(args[0] || ''),
            status: clone.status,
            contentType,
            body
          })).catch(() => {});
        }
      } catch (e) {}
      return response;
    };
  }
  const OriginalXHR = window.XMLHttpRequest;
  if (typeof OriginalXHR === 'function') {
    const originalOpen = OriginalXHR.prototype.open;
    const originalSend = OriginalXHR.prototype.send;
    OriginalXHR.prototype.open = function(method, url, ...rest) {
      this.__krUrl = url;
      return originalOpen.call(this, method, url, ...rest);
    };
    OriginalXHR.prototype.send = function(...args) {
      this.addEventListener('load', function() {
        try {
          const contentType = this.getResponseHeader ? this.getResponseHeader('content-type') || '' : '';
          if (/json|text|javascript/i.test(contentType) || typeof this.responseText === 'string') {
            keep({
              url: this.responseURL || this.__krUrl || '',
              status: this.status,
              contentType,
              body: this.responseText || ''
            });
          }
        } catch (e) {}
      });
      return originalSend.apply(this, args);
    };
  }
})();
"""


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _pick(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, (str, int, float)) and _clean(value):
            return _clean(value)
    return ""


def _join_location_parts(*parts: Any) -> str:
    seen: set[str] = set()
    cleaned_parts: list[str] = []
    for part in parts:
        text = _clean(part)
        if not text:
            continue
        normalized = text.rstrip("市")
        if normalized in seen:
            continue
        seen.add(normalized)
        cleaned_parts.append(text)
    return "·".join(cleaned_parts)


def _pick_gps(data: dict[str, Any]) -> tuple[str, str]:
    gps = data.get("gps")
    if isinstance(gps, dict):
        lng = _clean(gps.get("longitude") or gps.get("lng") or gps.get("lon"))
        lat = _clean(gps.get("latitude") or gps.get("lat"))
        return lng, lat
    return _clean(data.get("longitude") or data.get("lng") or data.get("lon")), _clean(data.get("latitude") or data.get("lat"))


def _build_location_fields(data: dict[str, Any], platform: str, fallback_area: str) -> dict[str, Any]:
    """Build address fields without hiding platform-native district data.

    BOSS list JSON may expose cityName, areaDistrict, businessDistrict and gps
    separately. A generic first-non-empty pick would often stop at cityName and
    lose the district, so platform-native fields are composed before fallback.
    """

    platform_key = str(platform or "").lower()
    fields: dict[str, Any] = {}
    if platform_key == "boss":
        composed = _join_location_parts(
            data.get("cityName") or data.get("city_name") or data.get("city"),
            data.get("areaDistrict") or data.get("area_district") or data.get("districtName") or data.get("district_name"),
            data.get("businessDistrict") or data.get("business_district"),
        )
        if composed:
            fields["area"] = composed
            fields["location"] = composed
            fields["location_raw"] = composed
        lng, lat = _pick_gps(data)
        if lng:
            fields["geo_lng"] = lng
        if lat:
            fields["geo_lat"] = lat
    if not fields.get("area"):
        area = fallback_area or _pick(data, AREA_KEYS)
        if area:
            fields["area"] = area
            fields["location"] = area
            fields["location_raw"] = area
    for key in NATIVE_LOCATION_KEYS:
        value = data.get(key)
        if isinstance(value, (str, int, float)) and _clean(value):
            fields.setdefault(key, _clean(value))
    return fields


def _iter_dicts(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            found.append(item)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


def _merge_scalar_fields(*parts: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for part in parts:
        if not isinstance(part, dict):
            continue
        for key, value in part.items():
            if isinstance(value, (str, int, float, bool)) and _clean(value):
                merged.setdefault(str(key), value)
    return merged


def _decode_json_field(value: Any) -> dict[str, Any]:
    text = unquote(str(value or "").strip())
    if not text or text[0] not in "[{":
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _iter_liepin_job_cards(payload: Any) -> list[dict[str, Any]]:
    """Yield normalized Liepin jobCardList cards from the real PC search API shape."""

    candidates: list[dict[str, Any]] = []
    for data in _iter_dicts(payload):
        card_list = data.get("jobCardList")
        if not isinstance(card_list, list):
            continue
        for card in card_list:
            if not isinstance(card, dict):
                continue
            data_info = _decode_json_field(card.get("dataInfo"))
            merged = _merge_scalar_fields(
                card,
                card.get("job"),
                card.get("comp"),
                card.get("recruiter"),
                card.get("dataParams"),
                data_info,
            )
            if merged:
                candidates.append(merged)
    return candidates


def _iter_candidate_dicts(payload: Any, platform: str) -> list[dict[str, Any]]:
    platform_key = str(platform or "").lower()
    candidates: list[dict[str, Any]] = []
    if platform_key == "liepin":
        candidates.extend(_iter_liepin_job_cards(payload))
    candidates.extend(_iter_dicts(payload))
    return candidates


def _loads_payload(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    text = str(value or "").strip()
    if not text or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _build_url(platform: str, data: dict[str, Any], raw_url: str) -> str:
    url = _pick(data, URL_KEYS)
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http"):
        return url
    if url.startswith("/"):
        host = "https://www.zhipin.com" if platform == "boss" else "https://www.liepin.com"
        return host + url

    item_id = _pick(data, ID_KEYS)
    if not item_id:
        return raw_url if "/job" in raw_url else ""
    if platform == "boss":
        return f"https://www.zhipin.com/job_detail/{item_id}.html"
    if platform == "liepin":
        return f"https://www.liepin.com/job/{item_id}.shtml"
    return ""


def _score_candidate(data: dict[str, Any], title: str, keyword: str, city: str) -> int:
    joined_keys = " ".join(str(key) for key in data.keys())
    joined_values = " ".join(_clean(value) for value in data.values() if isinstance(value, (str, int, float)))
    score = 0
    if any(key in data for key in TITLE_KEYS):
        score += 2
    if any(key in data for key in COMPANY_KEYS):
        score += 2
    if any(key in data for key in SALARY_KEYS):
        score += 1
    if any(key in data for key in AREA_KEYS + NATIVE_LOCATION_KEYS):
        score += 1
    if any(key in data for key in URL_KEYS + ID_KEYS):
        score += 1
    if re.search(r"job|position|salary|brand|company|comp|area|city", joined_keys, re.I):
        score += 1
    if keyword and keyword in joined_values:
        score += 1
    if city and city in joined_values:
        score += 1
    if title and re.search(r"登录|注册|验证码|安全验证|首页|公司$", title):
        score -= 3
    return score


def _has_job_identity(data: dict[str, Any]) -> bool:
    """Return whether a dict looks like an actual job card, not a facet/person/city."""

    has_company = any(_clean(data.get(key)) for key in COMPANY_KEYS)
    has_url_or_id = any(_clean(data.get(key)) for key in URL_KEYS + ID_KEYS)
    return bool(has_company or has_url_or_id)


def _keyword_relevant(data: dict[str, Any], keyword: str) -> bool:
    value = _clean(keyword)
    if not value:
        return True
    joined = " ".join(_clean(item) for item in data.values() if isinstance(item, (str, int, float)))
    if value.lower() in joined.lower():
        return True
    tokens: list[str] = []
    tokens.extend(re.findall(r"[A-Za-z0-9]{2,}", value))
    tokens.extend(re.findall(r"[\u4e00-\u9fff]{2,}", value))
    if len(value) >= 4 and re.search(r"[\u4e00-\u9fff]", value):
        tokens.extend(value[index : index + 2] for index in range(0, len(value) - 1))
    generic_tokens = {"招聘", "职位", "岗位", "产品", "经理", "助理", "运营", "专员", "主管", "销售", "工程"}
    meaningful = [token for token in tokens if token and token not in generic_tokens]
    if not meaningful:
        return True
    lowered = joined.lower()
    return any(token.lower() in lowered for token in meaningful)


def extract_recruitment_items_from_payloads(
    payloads: list[Any],
    *,
    platform: str,
    keyword: str = "",
    city: str = "",
    limit: int = 10,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Extract normalized job cards from observed JSON payloads."""

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    payload_count = 0
    candidate_count = 0

    for entry in payloads:
        raw_url = ""
        body: Any = entry
        if isinstance(entry, dict) and "body" in entry:
            raw_url = str(entry.get("url") or "")
            body = entry.get("body")
        payload = _loads_payload(body)
        if payload is None:
            continue
        payload_count += 1
        for data in _iter_candidate_dicts(payload, platform):
            title = _pick(data, TITLE_KEYS)
            if not title or len(title) > 100:
                continue
            if not _has_job_identity(data):
                continue
            if not _keyword_relevant(data, keyword):
                continue
            score = _score_candidate(data, title, keyword, city)
            if score < 4:
                continue
            company = _pick(data, COMPANY_KEYS)
            salary = _pick(data, SALARY_KEYS)
            area = _pick(data, AREA_KEYS)
            location_fields = _build_location_fields(data, platform, area)
            area = _clean(location_fields.get("area")) or area
            url = _build_url(platform, data, raw_url)
            key = url or f"{title}|{company}|{area}|{salary}"
            if key in seen:
                continue
            seen.add(key)
            candidate_count += 1
            display = "BOSS直聘" if platform == "boss" else "猎聘" if platform == "liepin" else platform
            item = {
                "title": title,
                "salary": salary,
                "company": company,
                "area": area,
                "location": area,
                "url": url,
                "platform": display,
                "source": f"{platform}_network_search",
                "evidence_strength": "strong_platform_network",
                "market_claim_allowed": True,
                "salary_claim_allowed": False,
                "field_confidence": {
                    "title": "high_platform_network",
                    "company": "medium_platform_network" if company else "missing",
                    "salary": "low_search_card" if salary else "missing",
                    "area": "medium_platform_network" if area else "missing",
                    "url": "medium_platform_network" if url else "missing",
                },
            }
            item.update(location_fields)
            items.append(
                attach_recruitment_address_contract(item, platform=platform, default_city=city, source="network_json")
            )
            if len(items) >= limit:
                return items, {
                    "payload_count": payload_count,
                    "candidate_count": candidate_count,
                    "item_count": len(items),
                    "source": "network_json_payload",
                }

    return items, {
        "payload_count": payload_count,
        "candidate_count": candidate_count,
        "item_count": len(items),
        "source": "network_json_payload",
    }
