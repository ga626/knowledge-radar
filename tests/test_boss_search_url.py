from collectors.platform import boss
from runtime import chrome_manager


def test_boss_search_url_uses_city_code_for_hangzhou() -> None:
    url = boss._build_boss_search_url("AI产品运营", "杭州")

    assert url == "https://www.zhipin.com/web/geek/jobs?query=AI%E4%BA%A7%E5%93%81%E8%BF%90%E8%90%A5&city=101210100"


def test_boss_search_url_keeps_unknown_city_value() -> None:
    url = boss._build_boss_search_url("Agent", "火星")

    assert url == "https://www.zhipin.com/web/geek/jobs?query=Agent"


def test_boss_unknown_city_stops_before_browser() -> None:
    result = boss.boss_search_via_cdp_state("Agent", "火星", 1)

    assert result["failure_type"] == "city_mapping_missing"
    assert result["manual_action_required"] is False
    assert result["diagnostics"]["layers"]["params"]["status"] == "city_mapping_missing"


def test_boss_startup_url_is_neutral_hangzhou_jobs_page() -> None:
    assert chrome_manager.BOSS_STARTUP_URL == "https://www.zhipin.com/web/geek/jobs?city=101210100"
    assert "Python" not in chrome_manager.BOSS_STARTUP_URL
    assert "%E5%BC%80%E5%8F%91" not in chrome_manager.BOSS_STARTUP_URL


def test_local_rate_gate_is_not_a_login_or_captcha_event(monkeypatch):
    monkeypatch.setattr(boss, "probe_boss_auth_state", lambda **_: {"status": "ok"})
    monkeypatch.setattr(boss, "check_search_gate", lambda *_args, **_kwargs: {"allowed": False, "reason": "搜索间隔过短（需等待10秒）"})
    result = boss.legacy_search_boss("产品经理", "", 1)
    assert result["error"]["type"] == "rate_limited"
    assert not result["error"]["manual_action_required"]
    assert "anti_bot_verification" not in result["error"]["failure_tags"]


def test_visible_login_modal_overrides_background_cards():
    state = boss._classify_boss_page_state({"url": "https://www.zhipin.com/web/geek/jobs", "loginModalCount": 1, "items": [{"title": "职位"}]})
    assert state["auth_state"] == "login_required"
