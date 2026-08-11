"""Zhihu detail strategy."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Callable, Dict, Optional

import httpx

from kr_core import DetailRequest, DetailResponse, EvidenceItem


@dataclass(frozen=True)
class ZhihuDetailDeps:
    headers: Dict[str, str]
    profile_dir: Callable[[str], str]
    ensure_chrome_debugging: Callable[[str], bool]
    finish_chrome_automation: Callable[[str, str], None]
    read_cookies_from_cdp: Callable[[], Optional[str]]
    read_cookies_from_profile: Callable[[], Optional[str]]
    strip_html: Callable[[str], str]
    looks_not_found: Callable[[str], bool]
    article_from_html: Callable[[str, str], Optional[Dict]]
    article_via_cdp: Callable[[str], Optional[Dict]]
    attach_routing: Callable[[str, Dict], Dict]
    evidence_builder: Callable[[str, str, Dict], EvidenceItem]
    log_info: Callable[[str], None]
    log_debug: Callable[[str], None]
    log_error: Callable[[str], None]


class ZhihuDetailStrategy:
    platform = "知乎"

    def __init__(self, deps: ZhihuDetailDeps) -> None:
        self.deps = deps

    def extract(self, request: DetailRequest) -> DetailResponse:
        result: Dict = {"platform": self.platform, "title": "", "desc": "", "transcript": "", "url": request.url}
        chrome_started = False
        try:
            chrome_started = bool(self.deps.ensure_chrome_debugging("zhihu"))
            if not chrome_started:
                self.deps.log_error("知乎详情提取无法启动受管 Chrome/CDP，将仅尝试离线 Profile Cookie 兜底")
            data = self._extract(request.url, result, cdp_available=chrome_started)
        except json.JSONDecodeError:
            data = {"platform": self.platform, "error": "知乎页面数据解析失败", "url": request.url}
        except Exception as exc:
            self.deps.log_error(f"知乎详情提取异常: {exc}")
            data = {"platform": self.platform, "error": f"知乎详情提取异常: {exc}", "url": request.url}
        finally:
            if chrome_started:
                self.deps.finish_chrome_automation("zhihu", "zhihu_detail")

        return DetailResponse.from_legacy(
            self.platform,
            request.url,
            data,
            evidence=self.deps.evidence_builder(request.url, self.platform, data),
            metadata={"strategy": "zhihu_detail"},
        )

    def _extract(self, url: str, result: Dict, *, cdp_available: bool = False) -> Dict:
        clean_url = url.split("?")[0]
        profile = os.path.join(self.deps.profile_dir("zhihu"), "Default")
        profile_ok = os.path.isdir(profile) and os.path.exists(os.path.join(profile, "Network", "Cookies"))
        self.deps.log_info(f"  知乎详情提取: {clean_url}")

        api_url, clean_url = self._normalize_url(clean_url)
        cookie_str = self.deps.read_cookies_from_cdp() if cdp_available else None
        cookie_source = "cdp" if cookie_str else ""
        if not cookie_str:
            cookie_str = self.deps.read_cookies_from_profile()
            cookie_source = "profile" if cookie_str else ""
        if cookie_source == "profile":
            self.deps.log_debug("  知乎详情仅使用离线 Profile Cookie 兜底，登录态可靠性低于 CDP")
        headers = dict(self.deps.headers)
        if cookie_str:
            headers["Cookie"] = cookie_str

        short_answer_match = re.search(r"/answers/(\d+)", clean_url)
        if short_answer_match:
            return self._extract_short_answer(short_answer_match.group(1), headers, profile_ok, url, result)

        resp = httpx.get(api_url, headers=headers, timeout=20, follow_redirects=True)
        html = resp.text
        if "登录后查看" in html or resp.status_code in (401, 403):
            if "/p/" in api_url:
                article_fallback = self.deps.article_via_cdp(api_url)
                if article_fallback:
                    result.update(article_fallback)
                    self.deps.log_info(f"  知乎文章 CDP 兜底提取完成: chars={len(result.get('content', ''))}")
                    return self.deps.attach_routing(url, result)
            hint = "登录态过期，请重新扫码" if profile_ok else "首次使用需扫码登录"
            return {"platform": self.platform, "error": f"知乎需要登录: {hint}", "url": url, "status_code": resp.status_code}

        js_data_str = self._extract_initial_data(html)
        if not js_data_str:
            return self._fallback_without_initial_data(html, api_url, clean_url, url, result)

        json_data = json.loads(js_data_str)
        entities = json_data.get("initialState", {}).get("entities", {})

        if "/answer/" in clean_url:
            answers = entities.get("answers", {})
            if not answers:
                return {"platform": self.platform, "error": "无法提取回答数据", "url": url}
            answer = list(answers.values())[0]
            self._fill_answer(result, answer)
        elif "/p/" in clean_url:
            articles = entities.get("articles", {})
            if not articles:
                article_fallback = self.deps.article_via_cdp(api_url)
                if article_fallback:
                    result.update(article_fallback)
                    self.deps.log_info(f"  知乎文章 CDP 兜底提取完成: chars={len(result.get('content', ''))}")
                    return self.deps.attach_routing(url, result)
                return {"platform": self.platform, "error": "无法提取文章数据", "url": url}
            article = list(articles.values())[0]
            self._fill_answer(result, article)
        elif "/zvideo/" in clean_url:
            zvideos = entities.get("zvideos", {})
            if not zvideos:
                return {"platform": self.platform, "error": "无法提取视频数据", "url": url}
            video = list(zvideos.values())[0]
            result["title"] = re.sub(r"<[^>]+>", "", video.get("title", ""))
            result["desc"] = re.sub(r"<[^>]+>", "", video.get("description", ""))[:300]
            result["content"] = result["desc"]
            result["video_url"] = video.get("video_url", "")
            result["votes"] = video.get("voteup_count", 0)
        else:
            return {"platform": self.platform, "error": f"不支持的知乎 URL 类型: {clean_url}", "url": url}

        self.deps.log_info(f"  知乎提取完成: title={result['title'][:50]}")
        return self.deps.attach_routing(url, result)

    def _normalize_url(self, clean_url: str) -> tuple[str, str]:
        if "/answer/" in clean_url:
            parts = clean_url.split("/")
            return f"https://www.zhihu.com/question/{parts[-3]}/answer/{parts[-1]}", clean_url
        if "/articles/" in clean_url:
            article_id = clean_url.rstrip("/").split("/")[-1]
            normalized = f"https://zhuanlan.zhihu.com/p/{article_id}"
            return normalized, normalized
        if "/p/" in clean_url:
            article_id = clean_url.split("/")[-1]
            return f"https://zhuanlan.zhihu.com/p/{article_id}", clean_url
        if "/zvideo/" in clean_url:
            video_id = clean_url.split("/")[-1]
            return f"https://www.zhihu.com/zvideo/{video_id}", clean_url
        if "/question/" in clean_url and clean_url.endswith("/"):
            question_id = clean_url.split("/")[-2]
            return f"https://www.zhihu.com/question/{question_id}", clean_url
        return clean_url, clean_url

    def _extract_short_answer(self, answer_id: str, headers: Dict[str, str], profile_ok: bool, url: str, result: Dict) -> Dict:
        api_resp = httpx.get(
            f"https://www.zhihu.com/api/v4/answers/{answer_id}?include=content,excerpt,voteup_count,comment_count",
            headers={
                **headers,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "X-Api-Version": "3.0.91",
                "X-App-Za": "OS=Web",
                "X-Requested-With": "fetch",
                "Referer": "https://www.zhihu.com/",
            },
            timeout=20,
            follow_redirects=True,
        )
        if api_resp.status_code in (401, 403):
            hint = "登录态过期，请重新扫码" if profile_ok else "首次使用需扫码登录"
            return {"platform": self.platform, "error": f"知乎需要登录: {hint}", "url": url, "status_code": api_resp.status_code}
        api_resp.raise_for_status()
        answer = api_resp.json()
        question = answer.get("question") or {}
        author = answer.get("author") or {}
        result["title"] = self.deps.strip_html(question.get("title") or answer.get("title", ""))
        result["desc"] = self.deps.strip_html(answer.get("excerpt", ""))[:300]
        result["content"] = self.deps.strip_html(answer.get("content", ""))
        result["author"] = author.get("name", "")
        result["votes"] = answer.get("voteup_count", 0)
        self.deps.log_info(f"  知乎回答 API 提取完成: chars={len(result['content'])}")
        return self.deps.attach_routing(url, result)

    def _extract_initial_data(self, html: str) -> str:
        try:
            from lxml import etree
            tree = etree.HTML(html)
            scripts = tree.xpath("//script[@id='js-initialData']/text()")
            if scripts:
                self.deps.log_info("  js-initialData 提取方式: lxml")
                return scripts[0]
        except ImportError:
            pass
        except Exception as exc:
            self.deps.log_debug(f"  lxml 提取异常: {exc}")

        try:
            from parsel import Selector
            js_data_str = Selector(text=html).xpath("//script[@id='js-initialData']/text()").get(default="")
            if js_data_str:
                self.deps.log_info("  js-initialData 提取方式: parsel")
                return js_data_str
        except ImportError:
            pass
        except Exception as exc:
            self.deps.log_debug(f"  parsel 提取异常: {exc}")

        for pattern, label in (
            (r'id="js-initialData"[^>]*>(.*?)</script>', "regex (DOTALL)"),
            (r'<script\s+[^>]*id\s*=\s*["\']js-initialData["\'][^>]*>(.*?)</script>', "regex (flexible)"),
        ):
            match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
            if match:
                self.deps.log_info(f"  js-initialData 提取方式: {label}")
                return match.group(1).strip()
        return ""

    def _fallback_without_initial_data(self, html: str, api_url: str, clean_url: str, url: str, result: Dict) -> Dict:
        if ("/p/" in api_url or "/articles/" in clean_url) and self.deps.looks_not_found(html):
            return {"platform": self.platform, "error": "知乎文章不存在或已不可访问", "url": url, "normalized_url": api_url}
        article_fallback = self.deps.article_from_html(html, api_url)
        if article_fallback:
            result.update(article_fallback)
            self.deps.log_info(f"  知乎文章 HTML 兜底提取完成: chars={len(result.get('content', ''))}")
            return self.deps.attach_routing(url, result)
        if "/p/" in api_url:
            article_fallback = self.deps.article_via_cdp(api_url)
            if article_fallback:
                result.update(article_fallback)
                self.deps.log_info(f"  知乎文章 CDP 兜底提取完成: chars={len(result.get('content', ''))}")
                return self.deps.attach_routing(url, result)
        has_login_hint = "登录" in html or "sign_in" in html
        return {
            "platform": self.platform,
            "error": "无法从页面提取 js-initialData",
            "url": url,
            "hint": "登录态过期" if has_login_hint else "页面结构可能变化，建议检查 URL",
        }

    def _fill_answer(self, result: Dict, item: Dict) -> None:
        result["title"] = self.deps.strip_html(item.get("title", ""))
        result["desc"] = self.deps.strip_html(item.get("excerpt", ""))[:300]
        result["content"] = self.deps.strip_html(item.get("content", ""))
        author = item.get("author", {})
        result["author"] = author.get("name", "") if isinstance(author, dict) else ""
        result["votes"] = item.get("voteup_count", 0)
