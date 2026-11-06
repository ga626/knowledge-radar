import server


class _FailingZhihuAdapter:
    def search(self, _request):
        raise RuntimeError("cookie missing probe")


class _ZhihuRegistry:
    def runtime_identity(self, _platform): return {"generation":"fixture"}
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


def test_github_failure_and_control_signals_survive_public_entry(monkeypatch):
    import pytest
    from collectors.platform.github_api import GitHubError
    from runtime.execution_context import ExecutionStopped
    from runtime.resource_concurrency import ResourceDeferred
    monkeypatch.setattr(server, '_record_search_evidence', lambda *args: None)
    def fail(*args, **kwargs):
        raise GitHubError('RATE_LIMITED', 'Wait for this identity.', retryable=True, retry_at=1234)
    monkeypatch.setattr(server.gh_cli_sidecar, 'search_repositories', fail)
    result = server.search_github_repositories('repo:o/r')
    assert result['status'] == 'failed'
    assert result['error']['type'] == 'RATE_LIMITED'
    assert result['error']['retryable'] is True and result['error']['retry_at'] == 1234
    for signal in (ExecutionStopped('cancelled'), ResourceDeferred('github_api:context:core')):
        def interrupted(*args, **kwargs):
            raise signal
        monkeypatch.setattr(server.gh_cli_sidecar, 'search_repositories', interrupted)
        with pytest.raises(type(signal)):
            server.search_github_repositories('repo:o/r')


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


def test_search_wechat_articles_uses_shared_discovery_contract(monkeypatch):
    from use_cases import web_search
    calls=[]
    def public(request,**kwargs):
        calls.append(request.query)
        return {'provider':'fake','items':[{'title':'台湾研究','url':'https://mp.weixin.qq.com/s/example'},
             {'title':'外部','url':'https://example.com/not-wechat'}]}
    monkeypatch.setattr(web_search,'execute',public)
    result=server.search_wechat_articles('台湾青年政治态度变化',limit=3,source='web')
    assert calls and all(q.startswith('site:mp.weixin.qq.com') for q in calls)
    assert result['platform']=='微信'
    assert result['metadata']['detail_tool_supported'] is True
    assert result['items'][0]['recommended_extract_tool']=='get_content_detail'
    assert result['items'][0]['detail_tool_supported'] is True
    assert len(result['acquired_items'])==1


def test_search_zhihu_exception_words_do_not_invent_login(monkeypatch):
    monkeypatch.setattr(server, 'registry', _ZhihuRegistry())
    monkeypatch.setattr(server, 'request_browser_interaction', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('must not open login')))
    result = server.search_zhihu('cookie probe', limit=1)
    assert result['items'] == []
    assert result['error']['type'] == 'collector_error'
    assert result['error']['manual_action_required'] is False


def test_wechat_opaque_hits_stay_in_shared_candidates(monkeypatch):
    from use_cases import web_search
    monkeypatch.setattr(web_search,'execute',lambda *a,**k:{'items':[
        {'url':'https://mp.weixin.qq.com/s/opaque','title':'unknown'},
        {'url':'https://mp.weixin.qq.com/s/relevant','title':'PaddleOCR 免费服务'}]})
    result=server.search_wechat_articles('PaddleOCR API',limit=1,source='web')
    assert result['items'][0]['url']=='https://mp.weixin.qq.com/s/relevant'
    assert len(result['acquired_items'])==2
    assert result['metadata']['unverified_candidate_count']==1
