from pathlib import Path

from search_providers.host import (
    HostSearchProvider,
    ensure_host_search_card,
    host_search_card_summary,
    host_search_providers,
    load_host_search_cards,
    write_host_search_card,
)


def test_host_search_card_first_attach_persists_absent_card(tmp_path: Path):
    card = ensure_host_search_card("codex", cards_dir=tmp_path)

    assert card["platform"] == "codex"
    assert card["state"] == "absent"
    assert load_host_search_cards(tmp_path)[0]["platform"] == "codex"
    assert host_search_providers(tmp_path) == {}


def test_host_search_card_first_attach_uses_detector_once(tmp_path: Path):
    calls = []

    def detector(platform: str):
        calls.append(platform)
        return {"platform": platform, "id": "codex_web_search", "state": "available", "enabled": True}

    first = ensure_host_search_card("codex", detector=detector, cards_dir=tmp_path)
    second = ensure_host_search_card("codex", detector=detector, cards_dir=tmp_path)

    assert first["state"] == "available"
    assert second["state"] == "available"
    assert calls == ["codex"]


def test_available_host_card_without_endpoint_is_declared_not_callable(tmp_path: Path):
    write_host_search_card(
        {"platform": "codex", "id": "codex_web_search", "state": "available", "enabled": True},
        cards_dir=tmp_path,
    )
    readiness = host_search_card_summary(tmp_path)["readiness"]

    assert readiness["status"] == "declared_not_callable"
    assert readiness["callable"] is False
    assert readiness["declared_not_callable_provider_ids"] == ["codex_web_search"]
    assert host_search_providers(tmp_path) == {}


def test_absent_host_card_is_silent_skip_not_provider(tmp_path: Path):
    write_host_search_card({"platform": "codex", "id": "codex_web_search", "state": "absent", "enabled": False}, cards_dir=tmp_path)

    assert host_search_providers(tmp_path) == {}


def test_host_provider_status_marks_absent_as_expected_degraded():
    provider = HostSearchProvider({"platform": "codex", "id": "codex_web_search", "state": "absent", "enabled": False})

    status = provider.status()
    assert status["available"] is False
    assert status["degraded_ok"] is True
