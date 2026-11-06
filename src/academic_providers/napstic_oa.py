"""国家科技期刊平台开放获取库：复用原站公开检索接口。"""

from __future__ import annotations

import re
from urllib.parse import quote

from .chinese_open_access import (
    ChineseOpenAccessError,
    ChineseOpenAccessProvider,
    OpenAccessPlatformConfig,
    OpenAccessResults,
    clean_api_text,
    json_response,
    public_json_search,
)
from .models import AcademicWork, normalize_doi


class NapsticOaProvider(ChineseOpenAccessProvider):
    name = "napstic_oa"
    endpoint = "https://opaj.napstic.cn/search/literature"
    timeout_s = 20.0
    config = OpenAccessPlatformConfig(
        name=name,
        display_name="国家科技期刊平台开放获取库",
        homepage="https://opaj.napstic.cn/periodicals",
        status="available",
        available=True,
        auto_enabled=True,
        access_mode="public_search_api",
        full_text_access="article_dependent_download_login_not_integrated",
        coverage="中文期刊论文的题名、作者、年份、摘要和 DOI；原站下载另受登录与授权约束",
        stable=False,
        manual_action="检索无需账号或 Key；全文请打开原文按站方要求登录，不能把 OA 标记当作匿名下载成功。",
    )

    def search_urls(self, request):
        return ("https://opaj.napstic.cn/search?q=" + quote(request.query.strip()),)

    def search(self, request):
        if not request.query.strip():
            return OpenAccessResults([], [{"type": "invalid_input", "request_executed": False,
                                           "retryable": False, "message": "检索词不能为空"}])

        def fetch(client):
            payload = json_response(client.get(self.endpoint, params={
                "q": request.query.strip(), "size": max(1, min(int(request.limit or 5), 20)), "pageNo": 1,
            }, headers={"Referer": self.search_urls(request)[0]}))
            if not isinstance(payload, dict):
                return None
            status = payload.get("status")
            if status != 200:
                kind = ("auth_required" if status in {401, 403} else "rate_limited" if status == 429
                        else "provider_api_error")
                raise ChineseOpenAccessError([{"type": kind, "business_status": status,
                    "retryable": kind in {"rate_limited", "provider_api_error"},
                    "backoff_suggested": kind == "rate_limited",
                    "message": "原站检索接口返回业务错误，不能判为零命中"}])
            result = payload.get("result")
            search = result.get("searchResult") if isinstance(result, dict) else None
            return search.get("list") if isinstance(search, dict) else None

        try:
            return public_json_search(self, request, fetch, self._make_record)
        except ChineseOpenAccessError as exc:
            return OpenAccessResults([], exc.diagnostics)

    def _make_record(self, row):
        identifier = row.get("identifier") if isinstance(row.get("identifier"), dict) else {}
        article_id = str(identifier.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", article_id):
            return None
        publication = row.get("publication") if isinstance(row.get("publication"), dict) else {}
        urls = row.get("url") if isinstance(row.get("url"), dict) else {}
        date = row.get("date") if isinstance(row.get("date"), dict) else {}
        year = str(publication.get("year") or date.get("issue") or "")
        creators = row.get("creators") if isinstance(row.get("creators"), list) else []
        is_oa = publication.get("isOA") in (1, "1", True)
        return AcademicWork(
            title=_text(row.get("title")),
            url="https://opaj.napstic.cn/periodicalArticle/" + article_id,
            authors=[clean_api_text(a["name"]) for a in creators if isinstance(a, dict) and a.get("name")][:12],
            year=int(year) if re.fullmatch(r"\d{4}", year) else None,
            doi=normalize_doi(str(urls.get("doi") or "")),
            abstract=_text(row.get("abstract"))[:5000],
            source=_text(publication.get("name")) or self.config.display_name,
            source_database=self.name,
            document_type="journal-article",
            oa_status="open" if is_oa else "unknown",
            access_mode="public_search_api",
            full_text_status="article_landing_unverified",
            provider_confidence=0.85,
            verification_status="public_search_record",
            license_scope="unknown",
            raw={"platform_id": article_id, "has_fulltext": publication.get("hasFulltext"),
                 "fulltext_verified": False, "download_access": "site_login_and_entitlement_apply",
                 "search_endpoint": self.endpoint},
        )


def _text(value):
    if isinstance(value, dict):
        value = value.get("zh") or value.get("raw") or value.get("en") or value.get("HL") or ""
    return clean_api_text(value)
