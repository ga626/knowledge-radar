import server


class _FailingZhihuAdapter:
    def search(self, _request):
        raise RuntimeError("cookie missing probe")


class _ZhihuRegistry:
    def get(self, _platform):
        return _FailingZhihuAdapter()


def test_search_youtube_is_independent_tool(monkeypatch):
    calls = []

    def fake_search(keyword, limit=10):
        calls.append((keyword, limit))
        return {"items": [{"title": "video", "url": "https://www.youtube.com/watch?v=abc12345678"}], "total": 1}

    monkeypatch.setattr(server.youtube_collectors, "search_youtube", fake_search)

    result = server.search_youtube("mcp tutorial", limit=3)

    assert calls == [("mcp tutorial", 3)]
    assert result["platform"] == "YouTube"
    assert result["metadata"]["actual_mcp_tool"] == "search_youtube"


def test_search_github_repositories_is_independent_tool(monkeypatch):
    calls = []

    def fake_search(query, *, limit=5):
        calls.append((query, limit))
        return {
            "query": query,
            "provider": "github",
            "items": [{"title": "owner/repo", "url": "https://github.com/owner/repo"}],
            "total": 1,
            "metadata": {"sidecar": "gh_cli"},
        }

    monkeypatch.setattr(server.gh_cli_sidecar, "search_repositories", fake_search)

    result = server.search_github_repositories("mcp server", limit=4)

    assert calls == [("mcp server", 4)]
    assert result["platform"] == "GitHub"
    assert result["metadata"]["actual_mcp_tool"] == "search_github_repositories"


def test_kr_web_search_youtube_alias_points_to_preferred_tool(monkeypatch):
    def fake_search(keyword, limit=10):
        return {"items": [], "total": 0, "platform": "YouTube", "metadata": {}}

    monkeypatch.setattr(server.youtube_collectors, "search_youtube", fake_search)

    result = server.kr_web_search("agent demo", provider="youtube", limit=2)

    assert result["metadata"]["deprecated_alias"] == "kr_web_search(provider='youtube')"
    assert result["metadata"]["preferred_tool"] == "search_youtube"


def test_kr_web_search_github_alias_points_to_preferred_tool(monkeypatch):
    def fake_search(query, *, limit=5):
        return {"query": query, "provider": "github", "items": [], "total": 0, "metadata": {}}

    monkeypatch.setattr(server.gh_cli_sidecar, "search_repositories", fake_search)

    result = server.kr_web_search("agent repo", provider="github", limit=2)

    assert result["metadata"]["deprecated_alias"] == "kr_web_search(provider='github')"
    assert result["metadata"]["preferred_tool"] == "search_github_repositories"


def test_search_wechat_articles_is_independent_l1_tool(monkeypatch):
    calls = []

    class _FakeWebSearchResponse:
        def __init__(self, payload):
            self.payload = payload

        def to_mcp_dict(self):
            return self.payload

    def fake_cached_search(_platform, _query, _limit, factory, **_kwargs):
        return factory()

    def fake_search_web(request):
        calls.append(request.query)
        return _FakeWebSearchResponse(
            {
                "query": request.query,
                "provider": "fake",
                "items": [
                    {"title": "专业文章", "url": "https://mp.weixin.qq.com/s/example", "summary": "台湾研究"},
                    {"title": "外部页面", "url": "https://example.com/not-wechat", "summary": "not wechat"},
                ],
                "total": 2,
                "attempted_providers": ["fake"],
                "fallback_used": False,
            }
        )

    monkeypatch.setattr(server, "_cached_search", fake_cached_search)
    monkeypatch.setattr(server, "search_web", fake_search_web)

    result = server.search_wechat_articles("台湾青年政治态度变化", limit=3)

    assert any(call.startswith("site:mp.weixin.qq.com") for call in calls)
    assert result["platform"] == "微信公众号"
    assert result["metadata"]["actual_mcp_tool"] == "search_wechat_articles"
    assert result["metadata"]["integration_level"] == "L1_query_templates"
    assert result["metadata"]["detail_tool_supported"] is False
    assert result["items"] == [
        {
            "title": "专业文章",
            "url": "https://mp.weixin.qq.com/s/example",
            "summary": "台湾研究",
            "source_type": "wechat_public_account_article",
            "discovery_query": calls[0],
            "recommended_extract_tool": "extract_web_page",
            "dynamic_fallback_tool": "extract_dynamic_page",
            "detail_tool_supported": False,
        }
    ]


def test_search_zhihu_wraps_cookie_failures(monkeypatch):
    monkeypatch.setattr(server, "registry", _ZhihuRegistry())
    monkeypatch.setattr(server, "managed_browser_platforms", lambda: ("zhihu",))

    result = server.search_zhihu("cookie probe", limit=1)

    assert result["items"] == []
    assert result["error"]["type"] == "login_or_cookie_unavailable"
    assert result["error"]["expected_degraded"] is False
    assert result["error"]["status_class"] == "NEEDS_INTERACTION"
    assert result["error"]["manual_interaction"]["status"] == "action_required_not_opened"
    assert result["error"]["manual_interaction"]["manual_open_mode"] == "health_check(mode='request_browser_interaction:zhihu:login_or_cookie_unavailable')"
    assert result["metadata"]["strategy"] == "zhihu_cookie_governed_fallback"
