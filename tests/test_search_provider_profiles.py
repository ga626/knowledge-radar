from search_providers.profile import provider_profiles


def test_general_search_profiles_cover_configurable_backends():
    profiles = provider_profiles()

    for name in ["searxng", "anysearch", "brave", "exa", "tavily"]:
        assert name in profiles
        assert profiles[name]["kind"] == "general_web"
        assert profiles[name]["runtime"]["speed"] in {"fast", "medium", "slow"}
        assert profiles[name]["cost_tier"]


def test_tavily_profile_is_paid_supplement():
    profile = provider_profiles()["tavily"]

    assert profile["cost_tier"] == "paid_limited"
    assert profile["default_wave"] == "paid_supplement"
    assert "monthly_quota_limited" in profile["weaknesses"]
