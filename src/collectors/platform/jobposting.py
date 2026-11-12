"""职位主实体与页头字段：缺失保持未知，推荐卡报价独立留证。"""
import hashlib
import json
import re
from urllib.parse import parse_qsl, urljoin, urlsplit
from bs4 import BeautifulSoup
from collectors.platform.job_domain import job_detail_quality


def _text(value):
    if isinstance(value, str):
        return BeautifulSoup(value, "lxml").get_text(" ", strip=True)
    if isinstance(value, list):
        return " ".join(filter(None, map(_text, value)))
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("value") or "")
    return str(value) if value is not None else ""


def _identity(url):
    p = urlsplit(url)
    return ((p.hostname or "").removeprefix("www."), p.path.rstrip("/"),
            tuple(sorted((k, v) for k, v in parse_qsl(p.query) if not k.startswith("utm_") and k not in {"spm", "from", "ref"})))


def _entries(value):
    if isinstance(value, list):
        for item in value:
            yield from _entries(item)
    elif isinstance(value, dict):
        kind = value.get("@type", [])
        if kind == "JobPosting" or isinstance(kind, list) and "JobPosting" in kind:
            yield value
        for key in ("@graph", "mainEntity"):
            if isinstance(value.get(key), (dict, list)):
                yield from _entries(value[key])


def parse_jobposting(html, *, url=""):
    soup = BeautifulSoup(html, "lxml")
    jobs = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            jobs.extend(_entries(json.loads(script.get_text())))
        except (ValueError, TypeError):
            continue
    headings = [node.get_text(" ", strip=True) for node in soup.select("h1")]
    for job in jobs:
        title = _text(job.get("title"))
        declared = job.get("url") or job.get("@id")
        if url:
            if isinstance(declared, str) and declared:
                if _identity(urljoin(url, declared)) != _identity(url):
                    continue
            elif len(jobs) != 1 or headings and title not in headings:
                continue
        content = BeautifulSoup(str(job.get("description") or ""), "lxml").get_text("\n", strip=True)
        quality = job_detail_quality(content, extraction_source="jobposting_jsonld", title=title)
        if not quality["ok"]:
            continue
        base = job.get("baseSalary")
        base = base if isinstance(base, dict) else {}
        value = base.get("value") or {}
        value = value if isinstance(value, dict) else {"value": value}
        salary = _text(value.get("value"))
        if value.get("minValue") is not None and value.get("maxValue") is not None:
            salary = f"{value['minValue']}-{value['maxValue']}"
        if salary:
            salary += " " + _text(base.get("currency")) + " " + _text(value.get("unitText"))
        employer = job.get("hiringOrganization") or {}
        locations = job.get("jobLocation") or []
        places = []
        for location in locations if isinstance(locations, list) else [locations]:
            address = location.get("address") if isinstance(location, dict) else location
            places.append(" ".join(dict.fromkeys(filter(None, (_text(address.get(k)) for k in
                ("addressRegion", "addressLocality", "streetAddress"))))) if isinstance(address, dict) else _text(address))
        fields = {"title": title, "company": _text(employer.get("name")) if isinstance(employer, dict) else "",
                  "salary": salary.strip(), "location": " / ".join(filter(None, places)),
                  "experience": _text(job.get("experienceRequirements")), "education": _text(job.get("educationRequirements")),
                  "published_at": _text(job.get("datePosted"))}
        return {**fields, "content": content, "jd": content, "detail_quality": quality,
                "salary_claim_allowed": bool(salary), "salary_observations": [],
                "identity_basis": "declared_job_url" if declared else "single_page_job_entity",
                "field_sources": {k: {"source_role": "primary_jsonld", "property": {
                    "company": "hiringOrganization.name", "salary": "baseSalary", "location": "jobLocation.address",
                    "experience": "experienceRequirements", "education": "educationRequirements",
                    "published_at": "datePosted",
                }.get(k, k)} for k, v in fields.items() if v},
                "missing_fields": [k for k in ("company", "salary", "location", "experience", "education") if not fields[k]],
                "extraction_source": "jobposting_jsonld"}
    return None


def _job_page_url(url):
    p = urlsplit(url)
    host = (p.hostname or "").removeprefix("www.").removeprefix("m.")
    return bool((host == "zhaopin.com" and p.path.startswith("/jobdetail/")) or
            (host == "yupao.com" and p.path.startswith("/zhaogong/")) or
            (host == "liepin.com" and re.match(r"^/(job|a)/\d+", p.path)))


def _visible_job_header(soup, url):
    if not _job_page_url(url):
        return None
    h1 = soup.select_one("main h1, .summary-plane h1, h1")
    if not h1:
        return None
    header = h1.find_parent("header") or h1.find_parent(class_=lambda c: c and "summary-plane" in c) or h1.parent
    if header.name in {"body", "html"}:
        return None
    fields = {"title": h1.get_text(" ", strip=True)}
    sources = {"title": {"source_role": "primary_header", "selector": "h1"}}
    selectors = {"company": '[itemprop="hiringOrganization"], .summary-plane__company, .company-name',
                 "salary": '[itemprop="baseSalary"], .summary-plane__salary, .salary',
                 "location": '[itemprop="jobLocation"], .summary-plane__info, .job-location',
                 "experience": '[itemprop="experienceRequirements"], .job-experience',
                 "education": '[itemprop="educationRequirements"], .job-education'}
    for key, selector in selectors.items():
        node = header.select_one(selector)
        value = node.get_text(" ", strip=True) if node else ""
        fields[key] = value if key != "salary" or re.search(r"\d", value) else ""
        if fields[key]:
            sources[key] = {"source_role": "primary_header", "selector": selector}
    if not any(fields.get(key) for key in selectors):
        return None
    fields.update(field_sources=sources, header_text=header.get_text(" ", strip=True), salary_claim_allowed=bool(fields["salary"]),
                  salary_observations=[], missing_fields=[k for k in selectors if not fields[k]], extraction_source="primary_header")
    for anchor in soup.select("a[href]"):
        if _identity(urljoin(url, anchor["href"])) != _identity(url) or fields["title"] not in anchor.get_text(" ", strip=True):
            continue
        # 标题链接与工资常是同一卡片的兄弟节点。只向上取有单一职位身份的局部容器。
        card = anchor
        for _ in range(4):
            parent = card.parent
            if parent is None or parent.name in {'body', 'html', 'main', 'aside'}:
                break
            identities = {_identity(urljoin(url, a['href'])) for a in parent.select('a[href]')
                          if _job_page_url(urljoin(url, a['href']))}
            if identities != {_identity(url)} or len(parent.get_text(' ', strip=True)) > 800:
                break
            card = parent
        for value in re.findall(r"\d+(?:\.\d+)?\s*[-–~至]\s*\d+(?:\.\d+)?\s*(?:万|千|[kK]|元)(?:/[月年日])?(?:[·•]\s*\d+薪)?", card.get_text(" ", strip=True)):
            observation = {"value": value, "source_role": "same_url_recommendation", "url": urljoin(url, anchor["href"]),
                           "identity_basis": "same_job_url"}
            if observation not in fields["salary_observations"]:
                fields["salary_observations"].append(observation)
    return fields


def enrich_job_response(response, *, html, url):
    """在同一次下载或渲染结果上增补字段，不再发请求、不生成推测工资。"""
    from dataclasses import replace
    if not _job_page_url(url) and 'JobPosting' not in html:
        return response
    fields = parse_jobposting(html, url=url) or _visible_job_header(BeautifulSoup(html, "lxml"), url)
    if not fields:
        return response
    if not job_detail_quality(response.content, extraction_source="job_intro_primary", title=fields["title"])["ok"]:
        return response
    raw_content = response.content
    content = raw_content
    blocks, seen, removed = [], set(), 0
    for block in re.split(r"\n\s*\n", content):
        key = " ".join(block.split())
        if len(key) >= 80 and key in seen:
            removed += 1
            continue
        if len(key) >= 80:
            seen.add(key)
        blocks.append(block)
    header = fields.get("header_text") or "；".join(f"{key}：{fields[key]}" for key in
        ("title", "company", "location", "experience", "education", "salary") if fields.get(key))
    supplement = "## 职位信息（原页头部）\n\n" + header
    for observation in fields.get("salary_observations", []):
        supplement += "\n\n同一职位链接的推荐卡报价（非页头工资，非实际offer）：" + observation["value"]
    content = supplement + "\n\n" + "\n\n".join(blocks)
    meta = {k: v for k, v in fields.items() if k not in {"content", "jd", "detail_quality"}}
    meta["removed_duplicate_paragraphs"] = removed
    meta["original_content_sha256"] = hashlib.sha256(raw_content.encode()).hexdigest()
    # 发生派生去重时保留原转译文本；字段增补不替换原来的正文。
    preservation = {"original_extracted_content": raw_content} if removed else {}
    return replace(response, content=content, metadata={**response.metadata, **preservation,
        "job_posting": meta, "content_chars": len(content)})
