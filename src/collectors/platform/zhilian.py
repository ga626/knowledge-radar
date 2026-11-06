"""智联历史页面的离线解析；主动搜索、浏览器采集已退役。"""
from __future__ import annotations
import json
from typing import Dict
from runtime.page_access_state import classify_page_access_state


def _parse_zhilian_detail_html(html: str, url: str) -> Dict:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)
    blocked = any(s in text for s in ("确认您是真人", "正在验证连接安全性", "Security Verification", "安全验证", "滑动验证"))
    title = soup.select_one("h1")
    jd = soup.select_one(".job-describe, .describtion, .job-description, [class*='jobDetail-description'], [class*='job-detail-description']")
    content = jd.get_text("\n", strip=True) if jd else ""
    # Structured JobPosting descriptions are also official page content.
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            entries = json.loads(script.string or script.get_text())
            entries = entries if isinstance(entries, list) else [entries]
            for entry in entries:
                if isinstance(entry, dict) and entry.get("@type") == "JobPosting":
                    content = content or BeautifulSoup(entry.get("description") or "", "lxml").get_text("\n", strip=True)
        except (ValueError, TypeError):
            continue
    state = classify_page_access_state(platform="zhilian", operation="detail", blocked_marker=blocked,
        login_marker=any(s in text for s in ("扫码登录", "请先登录", "验证码登录")),
        content_readable=len(content) >= 80, content_chars=len(content))
    return {"platform": "智联招聘", "url": url, "title": title.get_text(" ", strip=True) if title else "",
            "content": content, **state}
