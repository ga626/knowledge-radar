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
