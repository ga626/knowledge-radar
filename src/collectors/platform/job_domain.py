"""职位正文的共同质量边界；不包含站点路由或浏览器。"""
import re

def job_detail_quality(text: str, *, extraction_source: str = "", title: str = "") -> dict:
    """Score whether extracted text looks like a usable recruitment JD."""
    import html
    content = re.sub(r"<[^>]+>", " ", html.unescape(str(text or ""))).strip()
    positive_tokens = ["职位介绍", "职责描述", "任职要求", "岗位职责", "岗位要求", "工作内容", "职位描述"]
    negative_tokens = ["首页 职位 校园", "登录/注册", "登录获取更匹配职位"]
    positive = [token for token in positive_tokens if token in content]
    negative = [token for token in negative_tokens if token in content[:500]]
    semantic = re.findall(r"负责|熟悉|经验|本科|学历|技能|开发|设计|responsibilit\w*|experience|qualification\w*|proficien\w*|develop\w*|degree", content, re.I)
    trusted_body = extraction_source in {"jobposting_jsonld", "job_intro_primary"} and bool(title.strip()) and len(set(s.lower() for s in semantic)) >= 2
    ok = len(content) >= (100 if trusted_body else 180) and (bool(positive) or trusted_body) and len(negative) < 2
    return {
        "ok": ok,
        "chars": len(content),
        "positive_tokens": positive,
        "negative_tokens": negative,
        "extraction_source": extraction_source or "unattributed",
        "quality_reason": "usable_jd" if ok else "short_or_unattributed_job_body",
    }



REMOTE_CITY_MARKERS = ("远程", "全国", "不限", "居家", "remote")
CITY_MARKERS = (
    "北京", "上海", "广州", "深圳", "杭州", "南京", "苏州", "成都", "重庆", "武汉", "西安", "天津",
    "合肥", "长沙", "郑州", "青岛", "济南", "厦门", "福州", "宁波", "无锡", "常州", "佛山", "东莞",
    "珠海", "中山", "南昌", "南宁", "昆明", "贵阳", "海口", "三亚", "沈阳", "大连", "长春", "哈尔滨",
    "石家庄", "太原", "呼和浩特", "兰州", "银川", "西宁", "乌鲁木齐", "拉萨",
)


def _city_match_label(area: str, requested_city: str, extra_text: str = "") -> str:
    city = str(requested_city or "").strip()
    text = " ".join(part for part in (str(area or "").strip(), str(extra_text or "").strip()) if part)
    if not city:
        return "not_requested"
    if not text:
        return "unknown"
    lowered = text.lower()
    if any(marker.lower() in lowered for marker in REMOTE_CITY_MARKERS):
        return "remote"
    if city in text or text in city:
        return "match"
    if any(marker in text for marker in CITY_MARKERS if marker != city):
        return "mismatch"
    return "unknown"


def _apply_city_filter(items: list[dict], requested_city: str) -> tuple[list[dict], dict]:
    if not requested_city:
        return items, {
            "requested_city": "",
            "kept_count": len(items),
            "dropped_count": 0,
            "mismatch_count": 0,
        }
    kept: list[dict] = []
    dropped = 0
    mismatch = 0
    for item in items:
        area = str(item.get("area") or item.get("location") or item.get("city") or "")
        extra_text = " ".join(
            str(item.get(key) or "")
            for key in ("title", "salary", "company", "url")
        )
        label = _city_match_label(area, requested_city, extra_text=extra_text)
        item["requested_city"] = requested_city
        item["city_match"] = label
        if label == "mismatch":
            dropped += 1
            mismatch += 1
            continue
        kept.append(item)
    return kept, {
        "requested_city": requested_city,
        "kept_count": len(kept),
        "dropped_count": dropped,
        "mismatch_count": mismatch,
    }
