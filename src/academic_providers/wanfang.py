"""万方现行官方开放平台：授权关键词检索和文献详情。"""

from __future__ import annotations

import os
from urllib.parse import quote, urlsplit

import httpx

from .chinese_open_access import OpenAccessResults, _fetch_diagnostic, clean_api_text
from .models import AcademicSearchRequest, AcademicWork, normalize_doi


class WanfangProvider:
    name = "wanfang"

    def __init__(self, timeout: float = 15.0):
        # 保存 Key 不代表同意产生费用；默认暂停，恢复前须明确开启。
        self.enabled = os.environ.get("WFDATA_ENABLED", "false").strip().lower() == "true"
        self.api_key = os.environ.get("WFDATA_APP_KEY", "").strip()
        self.base_url = os.environ.get("WFDATA_BASE_URL", "https://api.wfdata.com").rstrip("/")
        self.timeout = timeout

    def status(self):
        return {"configured": bool(self.api_key), "available": bool(self.enabled and self.api_key),
                "enabled": self.enabled,
                "status": "disabled" if not self.enabled else "configured_not_live_verified" if self.api_key else "auth_required",
                "requires_api_key": True, "requires_login": False, "auto_enabled": False,
                "access_mode": "official_api", "full_text_access": "metadata_only",
                "required_env": "WFDATA_APP_KEY", "endpoint": self.base_url + "/openwanfang/getQuery",
                "manual_action": "向万方开放平台申请文献检索应用权限及 X-App-Key，确认套餐、集合与调用额度后填入 WFDATA_APP_KEY。个人网页登录或订阅不等于 API 授权。",
                "degraded_reason": "paid_provider_paused" if not self.enabled else "" if self.api_key else "missing_api_authorization"}

    def _request(self, path, payload):
        if not self.enabled:
            return None, [{"type": "provider_disabled", "retryable": False, "request_executed": False,
                           "message": "万方付费接口已暂停；保留密钥不会启用检索或详情请求。"}]
        if not self.api_key:
            return None, [{"type": "auth_required", "retryable": False, "request_executed": False,
                           "message": self.status()["manual_action"]}]
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
            return None, [{"type": "configuration_error", "retryable": False, "request_executed": False,
                           "message": "WFDATA_BASE_URL 必须为不含凭据和查询参数的 HTTPS 地址；不通过明文 HTTP 传输 Key。"}]
        from runtime.proxy_config import get_httpx_proxy
        try:
            # 禁止跨站重定向携带授权头。
            with httpx.Client(timeout=self.timeout, proxy=get_httpx_proxy(), trust_env=False,
                              follow_redirects=False) as client:
                from runtime.paid_execution import paid_send
                import json
                with paid_send("wanfang", {"path": path, "payload": payload},
                               bound=len(json.dumps(payload).encode("utf-8"))):
                    response = client.post(self.base_url + path, json=payload, headers={"X-App-Key": self.api_key})
                # 网关的 403 还可能是后付费扣款失败；不能误报账号未登录。
                if response.status_code in {402, 403}:
                    try:
                        failure = response.json()
                    except ValueError:
                        failure = {}
                    if failure.get("code") == "AUTH_PAYMENT_FAILED":
                        billing = failure.get("billing") or {}
                        return None, [{"type": "billing_required", "http_status": response.status_code,
                            "api_code": "AUTH_PAYMENT_FAILED", "retryable": False, "request_executed": True,
                            "pending_amount_cny": billing.get("pending_amount"),
                            "billing_mode": billing.get("billing_mode"),
                            "message": "万方后付费扣款失败；本人核对余额、账单并充值后重新订阅，不自动重试或付款。"}]
                response.raise_for_status()
                data = response.json()
            if not isinstance(data, dict) or data.get("success") is False or ("code" in data and str(data["code"]) not in {"0", "200", "OK"}):
                return None, [{"type": "api_contract_or_entitlement_error", "retryable": False,
                               "message": "万方返回业务错误；需核对应用权限、集合和接口版本，不将其算作零结果。"}]
            return (data.get("data") if isinstance(data.get("data"), dict) else data), []
        except (httpx.HTTPError, ValueError) as exc:
            from runtime.paid_execution import PaidAdmissionError
            if isinstance(exc, PaidAdmissionError):
                return None, [{"type": "budget_blocked", "error_code": exc.error_code,
                               "retryable": False, "request_executed": False}]
            diagnostic = _fetch_diagnostic(exc)
            if diagnostic.get("http_status") in {401, 403}:
                diagnostic["type"] = "auth_required"
            return None, [diagnostic]

    def search(self, request: AcademicSearchRequest):
        if request.options.get("document_id"):
            return self.get_document(str(request.options.get("collection") or "OpenPeriodical"),
                                     str(request.options["document_id"]))
        collections = request.options.get("collections") or ["OpenPeriodical"]
        if not isinstance(collections, list) or not all(isinstance(x, str) and x for x in collections):
            return OpenAccessResults([], [{"type": "invalid_input", "retryable": False, "request_executed": False}])
        data, diagnostics = self._request("/openwanfang/getQuery", {
            "collections": collections, "query": request.query, "rows": max(1, min(request.limit, 20)),
            "start": 0, "sort": {"sort_name": "OfflineScore"}})
        if data is None:
            return OpenAccessResults([], diagnostics)
        rows = data.get("documents")
        if not isinstance(rows, list):
            return OpenAccessResults([], [{"type": "search_response_unparsed", "retryable": False}])
        works = [self._work(row) for row in rows if isinstance(row, dict)]
        works = [work for work in works if work]
        return OpenAccessResults(works, [] if works else [{"type": "search_response_unparsed" if rows else "no_match", "retryable": False}])

    def get_document(self, collection: str, document_id: str):
        if not collection.strip() or not document_id.strip():
            return OpenAccessResults([], [{"type": "invalid_input", "retryable": False, "request_executed": False}])
        data, diagnostics = self._request("/openwanfang/getDoc", {"collection": collection, "id": document_id})
        work = self._work(data) if data else None
        if work and work.raw.get('document_id') != document_id:
            return OpenAccessResults([], [{"type": "identity_mismatch", "retryable": False}])
        if work:
            work.raw['identity_match'] = True
        return OpenAccessResults([work] if work else [], diagnostics or ([] if work else [{"type": "detail_response_unparsed", "retryable": False}]))

    def _work(self, row):
        fields = row.get("fields") or {}
        if not isinstance(fields, dict):
            return None
        def value(name):
            item = fields.get(name, "")
            # 官方 protobuf JSON 映射可能以 values 数组表示多值字段。
            if isinstance(item, dict):
                item = item.get("values", item.get("value", item.get("listValue", item)))
                if isinstance(item, dict) and "values" in item:
                    item = item["values"]
            def unpack(v):
                if isinstance(v, dict):
                    return next((v[k] for k in ("stringValue", "numberValue", "intValue", "boolValue") if k in v), "")
                return v
            item = [unpack(v) for v in item] if isinstance(item, list) else unpack(item)
            return item
        def scalar(name):
            item = value(name)
            return clean_api_text(item[0] if isinstance(item, list) and item else item)
        title, identifier = scalar("Title"), scalar("Id") or str(row.get("id") or "")
        resource = str(row.get("resource_type") or scalar("Type") or "Periodical")
        if not title or not identifier:
            return None
        authors = value("Creator")
        authors = authors if isinstance(authors, list) else [authors] if authors else []
        year = scalar("PublishYear")
        return AcademicWork(title=title, url="https://d.wanfangdata.com.cn/" + quote(resource.lower(), safe="") + "/" + quote(identifier, safe=""),
            authors=[clean_api_text(x) for x in authors], year=int(year) if year.isdigit() else None,
            doi=normalize_doi(scalar("DOI")), abstract=scalar("Abstract"), source="万方数据",
            source_database=self.name, access_mode="official_api", full_text_status="metadata_only",
            verification_status="official_api_record", provider_confidence=0.85,
            document_type={"periodical": "journal-article", "thesis": "dissertation", "conference": "proceedings-article", "nstr": "report"}.get(resource.lower(), resource.lower()),
            raw={"document_id": identifier, "resource_type": resource})
