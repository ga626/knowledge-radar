from runtime.recruitment_city import resolve_recruitment_city


def test_recruitment_city_registry_maps_platform_specific_values() -> None:
    assert resolve_recruitment_city("boss", "杭州")["param_value"] == "101210100"
    assert resolve_recruitment_city("zhilian", "杭州市")["param_value"] == "653"
    assert resolve_recruitment_city("liepin", "杭州")["param_value"] == "070020"


def test_liepin_registry_uses_verified_dq_codes_for_configured_cities() -> None:
    expected = {
        "北京": "010",
        "上海": "020",
        "广州": "050020",
        "深圳": "050090",
        "杭州": "070020",
        "成都": "280020",
        "南京": "060020",
        "武汉": "170020",
        "西安": "270020",
        "苏州": "060080",
    }

    for city, code in expected.items():
        resolved = resolve_recruitment_city("liepin", city)
        assert resolved["param_value"] == code
        assert resolved["value_kind"] == "liepin_dq_code"
        assert resolved["query_param_names"] == ["city", "dq"]
        assert resolved["source_url"].startswith("https://www.liepin.com/city-")


def test_recruitment_city_registry_does_not_guess_unknown_code_platforms() -> None:
    boss = resolve_recruitment_city("boss", "火星")
    zhilian = resolve_recruitment_city("zhilian", "火星")
    liepin = resolve_recruitment_city("liepin", "火星")

    assert boss["status"] == "missing"
    assert boss["param_value"] == ""
    assert zhilian["status"] == "missing"
    assert zhilian["param_value"] == ""
    assert liepin["status"] == "missing"
    assert liepin["param_value"] == ""
    assert liepin["passthrough_when_missing"] is False
