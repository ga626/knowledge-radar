"""Lightweight address normalization for recruitment search cards.

The normalizer only structures location text that collectors already captured.
It does not open detail pages, call maps, or infer districts from city-only
locations.
"""

from __future__ import annotations

import re
from typing import Any


LOCATION_FIELD_KEYS = (
    "location_raw",
    "area",
    "location",
    "workPlace",
    "work_place",
    "jobArea",
    "job_area",
    "cityName",
    "areaDistrict",
    "area_district",
    "businessDistrict",
    "business_district",
    "districtName",
    "district_name",
    "dq",
    "address_text",
)

NON_DISTRICT_PLATFORMS = {"v2ex", "maimai"}
REMOTE_OR_MULTI_MARKERS = ("远程", "居家", "不限", "全国", "多地", "异地", "各地")
PLATFORM_ALIASES = {
    "BOSS直聘": "boss",
    "boss直聘": "boss",
    "boss": "boss",
    "猎聘": "liepin",
    "liepin": "liepin",
    "智联招聘": "zhilian",
    "智联": "zhilian",
    "zhilian": "zhilian",
    "脉脉": "maimai",
    "maimai": "maimai",
    "V2EX": "v2ex",
    "v2ex": "v2ex",
}

CITY_ALIASES = {
    "北京": ("北京", "北京市"),
    "上海": ("上海", "上海市"),
    "广州": ("广州", "广州市"),
    "深圳": ("深圳", "深圳市"),
    "杭州": ("杭州", "杭州市"),
    "南京": ("南京", "南京市"),
    "苏州": ("苏州", "苏州市"),
    "成都": ("成都", "成都市"),
    "武汉": ("武汉", "武汉市"),
    "西安": ("西安", "西安市"),
    "合肥": ("合肥", "合肥市"),
}

DISTRICT_REGISTRY = {
    "杭州": {
        "上城": "上城区",
        "上城区": "上城区",
        "拱墅": "拱墅区",
        "拱墅区": "拱墅区",
        "西湖": "西湖区",
        "西湖区": "西湖区",
        "滨江": "滨江区",
        "滨江区": "滨江区",
        "萧山": "萧山区",
        "萧山区": "萧山区",
        "余杭": "余杭区",
        "余杭区": "余杭区",
        "临平": "临平区",
        "临平区": "临平区",
        "钱塘": "钱塘区",
        "钱塘区": "钱塘区",
        "富阳": "富阳区",
        "富阳区": "富阳区",
        "临安": "临安区",
        "临安区": "临安区",
        "桐庐": "桐庐县",
        "桐庐县": "桐庐县",
        "淳安": "淳安县",
        "淳安县": "淳安县",
        "建德": "建德市",
        "建德市": "建德市",
    },
    "北京": {
        "朝阳": "朝阳区",
        "朝阳区": "朝阳区",
        "海淀": "海淀区",
        "海淀区": "海淀区",
        "西城": "西城区",
        "西城区": "西城区",
        "东城": "东城区",
        "东城区": "东城区",
        "丰台": "丰台区",
        "丰台区": "丰台区",
        "通州": "通州区",
        "通州区": "通州区",
    },
    "上海": {
        "浦东": "浦东新区",
        "浦东新区": "浦东新区",
        "徐汇": "徐汇区",
        "徐汇区": "徐汇区",
        "闵行": "闵行区",
        "闵行区": "闵行区",
        "静安": "静安区",
        "静安区": "静安区",
        "黄浦": "黄浦区",
        "黄浦区": "黄浦区",
    },
    "深圳": {
        "南山": "南山区",
        "南山区": "南山区",
        "福田": "福田区",
        "福田区": "福田区",
        "罗湖": "罗湖区",
        "罗湖区": "罗湖区",
        "宝安": "宝安区",
        "宝安区": "宝安区",
        "龙岗": "龙岗区",
        "龙岗区": "龙岗区",
    },
    "广州": {
        "天河": "天河区",
        "天河区": "天河区",
        "海珠": "海珠区",
        "海珠区": "海珠区",
        "越秀": "越秀区",
        "越秀区": "越秀区",
        "番禺": "番禺区",
        "番禺区": "番禺区",
    },
}

FULL_ADDRESS_MARKERS = ("路", "大道", "弄", "号", "大厦", "产业园", "中心", "楼", "室")
STREET_MARKERS = ("街道", "镇", "园区", "商圈", "社区")


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_city(value: str) -> str:
    text = _clean(value).rstrip("市")
    for city, aliases in CITY_ALIASES.items():
        if text == city or text in [alias.rstrip("市") for alias in aliases]:
            return city
    return text


def _normalize_platform(value: str) -> str:
    text = _clean(value)
    return PLATFORM_ALIASES.get(text, text.lower())


def _first_location_text(item: dict[str, Any]) -> str:
    for key in LOCATION_FIELD_KEYS:
        value = item.get(key)
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


def _native_location_text(item: dict[str, Any]) -> str:
    """Compose native platform address fields before falling back to generic text."""

    composed = _join_location_parts(
        item.get("cityName") or item.get("city_name") or item.get("city"),
        item.get("areaDistrict") or item.get("area_district") or item.get("districtName") or item.get("district_name"),
        item.get("businessDistrict") or item.get("business_district"),
    )
    if composed:
        return composed
    return _first_location_text(item)


def _source_for(item: dict[str, Any], provided_source: str) -> str:
    if provided_source:
        return provided_source
    source_text = " ".join(_clean(item.get(key)) for key in ("source", "strategy"))
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    source_text = " ".join(part for part in (source_text, _clean(metadata.get("strategy")), _clean(metadata.get("fallback_from"))) if part)
    if "network_search" in source_text or "network_json" in source_text:
        return "network_json"
    if _clean(item.get("address_text")):
        return "detail_or_map"
    return "list_dom"


def _cities_in(text: str) -> list[str]:
    found: list[str] = []
    for city, aliases in CITY_ALIASES.items():
        if any(alias and alias in text for alias in aliases):
            found.append(city)
    return found


def _split_location(text: str) -> list[str]:
    parts = re.split(r"[·・/\\,，、;；|｜\-—–()\[\]【】\s]+", text)
    return [_clean(part) for part in parts if _clean(part)]


def _is_multi_or_remote(text: str, cities: list[str]) -> bool:
    if any(marker in text for marker in REMOTE_OR_MULTI_MARKERS):
        return True
    if len(set(cities)) >= 2:
        return True
    return False


def _find_district(text: str, city: str) -> tuple[str, str]:
    registry = DISTRICT_REGISTRY.get(city, {})
    if not registry:
        match = re.search(r"([\u4e00-\u9fff]{2,8}(?:区|县|市))", text)
        return (match.group(1), "") if match else ("", "")

    for alias in sorted(registry, key=len, reverse=True):
        canonical = registry[alias]
        if alias in text:
            return canonical, alias
    return "", ""


def _street_after(parts: list[str], city: str, district_alias: str) -> str:
    city_aliases = {city, f"{city}市", ""}
    district_seen = False
    for part in parts:
        normalized_part = part.rstrip("市")
        if normalized_part in city_aliases:
            continue
        if district_seen:
            return part
        if district_alias and district_alias in part:
            district_seen = True
            suffix = _clean(part.replace(district_alias, ""))
            if suffix:
                return suffix
            continue
    return ""


def _looks_full_address(text: str) -> bool:
    return any(marker in text for marker in FULL_ADDRESS_MARKERS) and len(text) >= 8


def normalize_recruitment_address(
    item: dict[str, Any],
    *,
    platform: str = "",
    default_city: str = "",
    source: str = "",
) -> dict[str, Any]:
    """Return address contract fields for a recruitment item.

    District claims are allowed only when the item itself has district-or-better
    evidence and the platform is a structured recruitment source.
    """

    normalized_platform = _normalize_platform(platform or _clean(item.get("platform")))
    raw = _clean(item.get("location_raw")) or _native_location_text(item)
    city_hint = _normalize_city(_clean(item.get("city")) or default_city)
    cities = _cities_in(raw)
    city = cities[0] if len(set(cities)) == 1 else city_hint
    parts = _split_location(raw)
    district = _clean(item.get("district"))
    district_alias = ""
    street_or_area = _clean(item.get("street_or_area"))
    address_text = _clean(item.get("address_text"))
    precision = _clean(item.get("address_precision")) or "unknown"
    confidence = _clean(item.get("address_confidence")) or "low"
    claim_allowed = bool(item.get("district_claim_allowed")) if item.get("district_claim_allowed") is not None else False

    if not raw:
        return {
            "location_raw": "",
            "city": city,
            "district": district,
            "street_or_area": street_or_area,
            "address_text": address_text,
            "address_source": _source_for(item, source),
            "address_precision": precision,
            "address_confidence": confidence,
            "district_claim_allowed": False,
        }

    if _is_multi_or_remote(raw, cities):
        return {
            "location_raw": raw,
            "city": city,
            "district": "",
            "street_or_area": "",
            "address_text": address_text,
            "address_source": _source_for(item, source),
            "address_precision": "unknown",
            "address_confidence": "low",
            "district_claim_allowed": False,
        }

    if not city and parts:
        maybe_city = _normalize_city(parts[0])
        if maybe_city in CITY_ALIASES:
            city = maybe_city

    if not district and city:
        district, district_alias = _find_district(raw, city)
    elif district:
        district_alias = district

    if not street_or_area and district and city:
        street_or_area = _street_after(parts, city, district_alias or district)

    if district:
        if _looks_full_address(raw):
            precision = "full_address"
            address_text = address_text or raw
            confidence = "high"
        elif street_or_area or any(marker in raw for marker in STREET_MARKERS):
            precision = "street"
            confidence = "high"
        else:
            precision = "district"
            confidence = "high"
        claim_allowed = normalized_platform not in NON_DISTRICT_PLATFORMS
    elif city or raw in {f"{city_hint}市", city_hint}:
        precision = "city"
        confidence = "medium"
        claim_allowed = False
    else:
        precision = "unknown"
        confidence = "low"
        claim_allowed = False

    return {
        "location_raw": raw,
        "city": city,
        "district": district,
        "street_or_area": street_or_area,
        "address_text": address_text,
        "address_source": _source_for(item, source),
        "address_precision": precision,
        "address_confidence": confidence,
        "district_claim_allowed": bool(claim_allowed),
    }


def attach_recruitment_address_contract(
    item: dict[str, Any],
    *,
    platform: str = "",
    default_city: str = "",
    source: str = "",
) -> dict[str, Any]:
    """Return a copy of item with missing address contract fields populated."""

    result = dict(item)
    contract = normalize_recruitment_address(result, platform=platform, default_city=default_city, source=source)
    for key, value in contract.items():
        if result.get(key) in (None, ""):
            result[key] = value
        elif key == "district_claim_allowed":
            result[key] = bool(result.get(key)) and bool(value)
    return result
