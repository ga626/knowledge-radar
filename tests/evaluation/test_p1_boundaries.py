from datetime import datetime, timezone
import socket

import httpx
import pytest

from generic_web import collector
from generic_web.models import GenericWebRequest, GenericWebResponse
from generic_web.url_policy import URLPolicyError, public_addresses, reader_allowed, redact_url, guarded_fetch
from understanding.evidence import build_detail_evidence, freshness_for_platform


@pytest.fixture(autouse=True)
def isolated_proxy(monkeypatch):
    monkeypatch.setattr("runtime.proxy_config.get_httpx_proxy", lambda: None)


def test_fake_ip_requires_independent_public_resolution(monkeypatch):
    from generic_web import url_policy
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("198.18.0.4", 443))])
    monkeypatch.setattr(url_policy, "_public_dns_for_fake_ip", lambda host: ["104.16.253.120"])
    assert public_addresses("https://pip.pypa.io/")[2] == ["104.16.253.120"]
    with pytest.raises(URLPolicyError):
        public_addresses("https://198.18.0.4/")


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://169.254.169.254/", "http://10.0.0.1/", "http://[::1]/",
                                  "http://[::ffff:127.0.0.1]/", "http://localhost/", "http://user:password@example.org/",
                                  "file:///x", "https://example.org:22/x", "http://example.org\\@127.0.0.1/x"])
def test_restricted_targets_rejected(url):
    with pytest.raises(URLPolicyError):
        public_addresses(url)


def test_mixed_dns_answer_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", (ip, 443)) for ip in ["93.184.216.34", "10.1.2.3"]])
    with pytest.raises(URLPolicyError):
        public_addresses("https://example.org/")


def test_reader_never_exports_query_or_secret_path(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    assert reader_allowed("https://example.org/public")
    assert not reader_allowed("https://example.org/public?q=private")
    assert not reader_allowed("https://example.org/auth/value")
    assert "private-secret" not in redact_url("https://example.org/?token=private-secret&lang=zh#fragment")
    assert "lang=zh" in redact_url("https://example.org/?token=private-secret&lang=zh")


def test_plain_text_bounded_and_preserves_unicode(monkeypatch):
    response = httpx.Response(200, text="中文说明\n" * 100, headers={"content-type": "text/plain; charset=utf-8"}, request=httpx.Request("GET", "https://example.org/readme.txt"))
    monkeypatch.setattr(collector, "_fetch_html", lambda *a, **k: (response, response.headers["content-type"]))
    result = collector._collect_via_trafilatura(GenericWebRequest("https://example.org/readme.txt", use_jina=False))
    assert result.collector == "plain_text"
    assert result.content.startswith("中文说明")


def test_dates_use_clock_not_literal_year():
    clock = datetime(2028, 3, 1, tzinfo=timezone.utc)
    assert freshness_for_platform("web", {"published_at": "2028-02-28T23:00:00+08:00"}, now=clock) == "recent"
    assert freshness_for_platform("web", {"published_at": "2025-02-19"}, now=clock) == "dated"
    assert freshness_for_platform("web", {"published_at": "not a date"}, now=clock) == "unknown"
    assert freshness_for_platform("web", {"published_at": "2099-01-01"}, now=clock) == "future"


@pytest.mark.parametrize("data", [{}, {"title": "only title"}, {"comments": [{"text": "comment"}]}, {"content": "short"}])
def test_extraction_is_not_claim_verification(data):
    evidence = build_detail_evidence("https://example.org/", "web", data)
    assert evidence.verification_status != "已验证"
    assert evidence.metadata["claim_verification"] == "unverified"
    assert evidence.metadata["extraction_status"] == "insufficient_content"


def test_candidate_strength_not_counted_as_verified():
    from search_providers.baseline import snapshot_from_response
    result = snapshot_from_response("fixture", {"items": [{"evidence_strength": s} for s in ["candidate_until_transcript_detail_or_cross_checked", "stronger_after_repository_detail_inspection", "invented", "strong_platform_network"]]})
    assert result["strong_or_contextual_evidence_count"] == 1


def test_redirect_validates_next_target_before_connecting(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    calls = []
    def respond(request):
        calls.append(request)
        assert request.url.host == "93.184.216.34"
        assert request.headers["Host"] == "example.org"
        assert request.extensions["sni_hostname"] == "example.org"
        return httpx.Response(302, headers={"location": "https://127.0.0.1/private"})
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    with pytest.raises(URLPolicyError, match="non-public"):
        guarded_fetch("https://example.org/start")
    assert len(calls) == 1


def test_cross_host_redirect_does_not_export_platform_cookie(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    seen = []
    def respond(request):
        seen.append(request)
        if request.headers["Host"] == "example.org":
            return httpx.Response(302, headers={"location": "https://other.example/final"})
        assert "cookie" not in request.headers and "authorization" not in request.headers
        return httpx.Response(200, text="public")
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    assert guarded_fetch("https://example.org/start", headers={"Cookie": "PRIVATE_CANARY", "Authorization": "Bearer private"}).text == "public"
    assert len(seen) == 2


@pytest.mark.parametrize("url,platform", [("http://127.0.0.1/?zhihu.com", "知乎"), ("https://zhihu.com.evil.example/a", "知乎"), ("https://www.zhihu.com@evil.example/a", "知乎")])
def test_detail_url_rejected_before_any_profile_or_handler_access(url, platform):
    from kr_core import DetailRequest
    from use_cases.content_detail import execute_detail_request
    def forbidden(*args, **kwargs):
        raise AssertionError("unsafe URL must be rejected before side effects")
    result = execute_detail_request(DetailRequest(url=url, platform=platform), failure_cache=None,
                                    handle=forbidden, bind_tasks=forbidden, record_evidence=forbidden, chrome_operation=forbidden)
    assert result["failure_type"] == "restricted_url"


def test_response_body_is_bounded(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 1024)), **kw))
    with pytest.raises(URLPolicyError, match="bounded"):
        guarded_fetch("https://example.org/large", max_bytes=100)


def test_console_host_and_origin_boundaries():
    import http.client
    import threading
    from onboarding.setup_wizard import WizardServer
    server = WizardServer(("127.0.0.1", 0))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    def get(headers):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request("GET", "/api/health", headers=headers)
        response = connection.getresponse()
        response.read()
        connection.close()
        return response.status
    try:
        assert get({"Host": "audit.invalid"}) == 403
        assert get({"Origin": "https://external.invalid"}) == 403
        assert get({"Host": f"localhost:{server.server_port}"}) == 200
        assert get({}) == 200
    finally:
        server.shutdown()
        worker.join(timeout=3)
        server.server_close()


def test_sensitive_get_needs_page_token_before_computing_snapshot(monkeypatch):
    import http.client
    import threading
    import onboarding.setup_wizard as wizard
    calls = []
    monkeypatch.setattr(wizard, "diagnostic_snapshot", lambda: calls.append(True) or {"status": "ok"})
    server = wizard.WizardServer(("127.0.0.1", 0))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    def get(path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        response.read()
        connection.close()
        return response.status
    try:
        for path in ["status", "configuration", "diagnostics", "account-alerts", "storage", "dashboard"]:
            assert get("/api/" + path) == 403
        assert calls == []
        assert get("/api/health") == 200
        assert get("/api/diagnostics", {"X-KR-Setup-Token": server.setup_token}) == 200
        assert get("/api/diagnostics", {"X-KR-Setup-Token": server.setup_token, "Sec-Fetch-Site": "same-site"}) == 403
        assert calls == [True]
    finally:
        server.shutdown()
        worker.join(timeout=3)
        server.server_close()
