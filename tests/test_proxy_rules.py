from runtime.proxy_rules import load_proxy_rules, parse_rule_text, refresh_rule_cache


def test_proxy_rules_parse_and_classify_suffix(tmp_path):
    (tmp_path / "direct.txt").write_text("DOMAIN-SUFFIX,cn\nexample.cn\n", encoding="utf-8")
    (tmp_path / "proxy.txt").write_text("*.google.com\nDOMAIN-KEYWORD,youtube\n", encoding="utf-8")

    rules = load_proxy_rules(tmp_path)

    assert rules.classify_host("api.example.cn")["decision"] == "direct"
    assert rules.classify_host("mail.google.com")["decision"] == "proxy"
    assert rules.classify_host("www.youtube.com")["decision"] == "proxy"
    assert rules.classify_host("unknown.test")["decision"] == "unknown"


def test_proxy_rules_refresh_uses_injected_fetcher(tmp_path):
    def fetcher(url: str) -> str:
        if "direct" in url:
            return "DOMAIN,direct.example\n"
        return "DOMAIN-SUFFIX,proxy.example\n"

    result = refresh_rule_cache(cache_dir=tmp_path, fetcher=fetcher)
    rules = load_proxy_rules(tmp_path)

    assert result["status"] == "ok"
    assert result["counts"]["direct"] == 1
    assert rules.classify_host("direct.example")["decision"] == "direct"
    assert rules.classify_host("a.proxy.example")["decision"] == "proxy"


def test_parse_rule_text_supports_clash_styles():
    rules = parse_rule_text("""
    # comment
    DOMAIN,exact.example
    DOMAIN-SUFFIX,suffix.example
    DOMAIN-KEYWORD,keyword
    *.wild.example
    """)

    assert [rule.kind for rule in rules] == ["exact", "suffix", "keyword", "suffix"]


def test_parse_rule_text_supports_loyalsoldier_payload_style():
    rules = parse_rule_text("""
    payload:
      - 'github.com'
      - '+.baidu.com'
      - '*.google.com'
    """)

    assert [rule.kind for rule in rules] == ["exact", "suffix", "suffix"]
    assert [rule.value for rule in rules] == ["github.com", "baidu.com", "google.com"]


import pytest

@pytest.fixture
def route_env(monkeypatch):
    from runtime import proxy_config as pc
    for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY'):
        monkeypatch.delenv(key,raising=False);monkeypatch.delenv(key.lower(),raising=False)
    monkeypatch.setattr(pc,'_read_windows_user_proxy',lambda:'http://127.0.0.1:7897')
    return pc


def test_handoff_loopback_is_direct_without_no_proxy(route_env):
    for url in ('http://localhost:18882/','http://127.0.0.2:18882/','http://[::1]/'):
        assert route_env.proxy_decision(url)['decision']=='direct'
        assert route_env.proxy_decision(url)['reason']=='loopback'


def test_handoff_scheme_and_no_proxy_selection(route_env,monkeypatch):
    monkeypatch.setenv('HTTP_PROXY','http://localhost:8111')
    monkeypatch.setenv('HTTPS_PROXY','http://localhost:8222')
    monkeypatch.setenv('NO_PROXY','example.cn')
    assert route_env.httpx_options('http://example.org')['proxy']=='http://localhost:8111'
    assert route_env.httpx_options('https://example.org')['proxy']=='http://localhost:8222'
    assert route_env.httpx_options('https://a.example.cn')['proxy'] is None
    assert route_env.httpx_options('https://badexample.cn')['proxy'] is not None


def test_handoff_redirect_reselects_route_without_replaying(route_env,monkeypatch):
    import httpx
    calls=[]
    class Transport(httpx.BaseTransport):
        def __init__(self,**kw):self.proxy=kw.get('proxy')
        def handle_request(self,request):
            calls.append((str(request.url),self.proxy))
            if request.url.host=='remote.example':return httpx.Response(302,headers={'location':'http://localhost:18882/ok'})
            return httpx.Response(200,text='ok')
    monkeypatch.setattr(httpx,'HTTPTransport',Transport)
    with route_env.http_client(follow_redirects=True) as client:
        assert client.get('https://remote.example/start').text=='ok'
    assert calls==[('https://remote.example/start','http://127.0.0.1:7897'),('http://localhost:18882/ok',None)]


def test_handoff_route_receipt_does_not_leak_proxy_password_or_target_query(route_env,monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY','http://user:supersecret@proxy.example:8888/path?token=secret')
    result=route_env.proxy_decision('https://example.org/private?token=source-secret')
    assert result['source']=='HTTPS_PROXY'
    assert not any(x in str(result) for x in ('supersecret','source-secret','user:','/private','token='))
    assert result['decision']=='proxy'


def test_handoff_owned_browser_gets_proxy_bypass(route_env):
    from generic_web.browser_runtime import launch_options
    options=launch_options()
    assert options['proxy']['server']=='http://127.0.0.1:7897'
    assert 'localhost' in options['proxy']['bypass']


def test_handoff_failed_proxy_does_not_retry_direct(route_env,monkeypatch):
    import httpx
    calls=[]
    class Transport(httpx.BaseTransport):
        def __init__(self,**kw):calls.append(kw.get('proxy'))
        def handle_request(self,request):raise httpx.ConnectError('proxy offline',request=request)
    monkeypatch.setattr(httpx,'HTTPTransport',Transport)
    with route_env.http_client() as client,pytest.raises(httpx.ConnectError):
        client.get('https://example.org')
    assert calls==['http://127.0.0.1:7897']
