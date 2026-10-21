"""职位结果语义筛选：导航、首页与公司主页不能作为职位。"""
from urllib.parse import urlsplit
import re


def normalize_jobs(items, limit=10):
    result, seen = [], set()
    for item in items or []:
        url = str(item.get("url") or "")
        parsed = urlsplit(url)
        title = str(item.get("title") or "").strip()
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"jobs.zhaopin.com", "sou.zhaopin.com"}:
            continue
        if not re.search(r"/(?:CC[^/?#]+|[^/?#]*\d{5,}[^/?#]*)\.(?:htm|html)$", parsed.path, re.I):
            continue
        if not title or title in {"首页", "智联招聘", "职位搜索", "登录"} or url in seen:
            continue
        seen.add(url)
        result.append({**item, "title": title, "url": url, "content_type": "job_posting", "evidence_type": "search_card"})
        if len(result) >= limit:
            break
    return result


def parse_job_html(html, limit=10):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for anchor in soup.select('a[href*="jobs.zhaopin.com/"]'):
        card = anchor.find_parent(class_=lambda c: c and any(x in c for x in ["job-card", "joblist", "position"])) or anchor.parent
        rows.append({"title": anchor.get_text(" ", strip=True), "url": anchor.get("href", ""),
                     "summary": card.get_text(" ", strip=True)[:1000]})
    return normalize_jobs(rows, limit)
