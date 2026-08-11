"""Baidu Scholar metadata provider via Qianfan official API."""

from __future__ import annotations

import os
import time
import uuid
from typing import Any, Dict, List

import httpx

from .models import AcademicSearchRequest, AcademicWork, normalize_doi


class BaiduScholarError(Exception):
    pass


class BaiduScholarAuthError(BaiduScholarError):
    pass


class BaiduScholarUnavailableError(BaiduScholarError):
    pass


class BaiduScholarRateLimitError(BaiduScholarError):
    pass


class BaiduScholarProvider:
    name = "baidu_scholar"

    def __init__(self, endpoint: str = "https://qianfan.baidubce.com/v2/tools/baidu_scholar/search", timeout: float = 20.0) -> None:
        self.endpoint = os.environ.get("BAIDU_QIANFAN_SCHOLAR_ENDPOINT", endpoint).strip() or endpoint
        self.timeout = timeout
        self.bearer_token = os.environ.get("BAIDU_QIANFAN_BEARER_TOKEN", "").strip()
        self.daily_limit = _int_env("KR_ACADEMIC_BAIDU_DAILY_LIMIT", 50)

    def status(self) -> Dict[str, Any]:
        configured = bool(self.bearer_token)
        return {
            "configured": configured,
            "available": configured,
            "endpoint": self.endpoint,
            "requires_api_key": True,
            "api_key_configured": configured,
            "quota": {"daily_limit": self.daily_limit, "source": "Baidu Qianfan Baidu Scholar free trial"},
            "degraded_reason": "" if configured else "BAIDU_QIANFAN_BEARER_TOKEN is not configured",
            "legal_boundary": "Official Qianfan Baidu Scholar API only; no Baidu Scholar webpage crawling.",
        }

    def search(self, request: AcademicSearchRequest) -> List[AcademicWork]:
        if not self.bearer_token:
            raise BaiduScholarAuthError("BAIDU_QIANFAN_BEARER_TOKEN is not configured")
        limit = max(1, min(int(request.limit or 5), 20))
        page_num = max(0, int(request.options.get("pageNum") or request.options.get("page_num") or 0))
        enable_ai_abstract = _truthy(request.options.get("enable_ai_abstract", False))
        params = {
            "wd": request.query,
            "pageNum": page_num,
            # The official table says enable_ai_abstract, but the live gateway
            # accepts the sample spelling. The table spelling returns a plain
            # 404, then the immediate retry can trip Baidu Scholar QPS limits.
            "enable_abstract": str(enable_ai_abstract).lower(),
        }
        headers = {
            # The Baidu Scholar tool doc and Qianfan API Key docs both specify
            # the generic Qianfan Bearer header. AppBuilder search examples may
            # use X-Appbuilder-Authorization, but Scholar should stay aligned
            # with its own official API contract.
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": "application/json",
            "X-Appbuilder-Request-Id": str(uuid.uuid4()),
        }
        try:
            with httpx.Client(timeout=self.timeout, headers=headers) as client:
                response = client.get(self.endpoint, params=params)
                if response.status_code == 429 and "RATE_LIMIT_QPS" in _response_message(response):
                    time.sleep(1.2)
                    response = client.get(self.endpoint, params=params)
                if response.status_code in {401, 403}:
                    raise BaiduScholarAuthError(_response_message(response))
                if response.status_code == 404:
                    raise BaiduScholarUnavailableError(
                        "Baidu Scholar Qianfan tool endpoint returned HTTP 404; verify the API Key has Baidu Scholar/tool permission or submit a Qianfan work order for authorization"
                    )
                if response.status_code == 429:
                    raise BaiduScholarRateLimitError(_response_message(response) or "Baidu Scholar API rate limited (HTTP 429)")
                response.raise_for_status()
                data = response.json()
        except (BaiduScholarAuthError, BaiduScholarUnavailableError, BaiduScholarRateLimitError):
            raise
        except Exception as exc:
            raise BaiduScholarError(str(exc)) from exc

        code = str(data.get("code") or "")
        if code and code != "0":
            message = str(data.get("message") or f"Baidu Scholar API returned code={code}")
            if code in {"401", "403", "InvalidHTTPAuthHeader"}:
                raise BaiduScholarAuthError(message)
            if code == "404":
                raise BaiduScholarUnavailableError(message)
            if code == "429" or "RATE_LIMIT" in code or "QUOTA" in code or "BILLING" in code:
                raise BaiduScholarRateLimitError(message)
            raise BaiduScholarError(message)
        items = [self._work_from_baidu(item) for item in data.get("data") or [] if isinstance(item, dict)]
        return [item for item in items if item.title][:limit]

    def _work_from_baidu(self, item: Dict[str, Any]) -> AcademicWork:
        publish_info = item.get("publishInfo") if isinstance(item.get("publishInfo"), dict) else {}
        doi = normalize_doi(str(item.get("doi") or ""))
        abstract = str(item.get("aiAbstract") or item.get("abstract") or "")[:2000]
        year = item.get("publishYear")
        try:
            year = int(year) if year not in (None, "") else None
        except Exception:
            year = None
        return AcademicWork(
            title=str(item.get("title") or ""),
            url=str(item.get("url") or ""),
            authors=[],
            year=year,
            doi=doi,
            abstract=abstract,
            source=str(publish_info.get("journalName") or "Baidu Scholar"),
            oa_status="unknown",
            license="",
            source_database=self.name,
            access_mode="official_api",
            full_text_status="metadata_only",
            provider_confidence=0.78,
            verification_status="doi_matched" if doi else "unverified",
            license_scope="unknown",
            degraded_reason="endpoint_unavailable_for_current_key" if not item.get("url") else "",
            raw={
                "paperId": item.get("paperId"),
                "keyword": item.get("keyword"),
                "publishInfo": publish_info,
                "provider": self.name,
            },
        )


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except Exception:
        return default


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _response_message(response: httpx.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            return str(data.get("message") or data.get("error") or data)
    except Exception:
        pass
    text = response.text.strip()
    return text[:500] if text else f"HTTP {response.status_code}"
