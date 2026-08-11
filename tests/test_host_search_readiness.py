from __future__ import annotations

from pathlib import Path

from search_providers.host import host_search_card_summary, host_search_providers, host_search_readiness, write_host_search_card


def test_host_search_readiness_absent_when_no_cards(tmp_path: Path) -> None:
    readiness = host_search_readiness(tmp_path / "cards")

    assert readiness["status"] == "absent"
    assert readiness["callable"] is False
    assert readiness["expected_degraded"] is True


def test_host_search_declared_without_endpoint_is_not_callable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("KR_HOST_SEARCH_ENDPOINT", raising=False)
    cards = tmp_path / "cards"
    write_host_search_card(
        {
            "id": "codex_web_search",
            "platform": "codex",
            "state": "available",
            "enabled": True,
            "call_contract": {},
        },
        cards_dir=cards,
    )

    readiness = host_search_readiness(cards)

    assert readiness["status"] == "declared_not_callable"
    assert readiness["callable"] is False
    assert readiness["declared_not_callable_provider_ids"] == ["codex_web_search"]
    assert "codex_web_search" in [card["id"] for card in host_search_card_summary(cards)["cards"]]
    assert host_search_providers(cards) == {}


def test_host_search_available_when_endpoint_exists(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KR_HOST_SEARCH_ENDPOINT", "http://127.0.0.1:9999/search")
    cards = tmp_path / "cards"
    write_host_search_card(
        {
            "id": "codex_web_search",
            "platform": "codex",
            "state": "available",
            "enabled": True,
        },
        cards_dir=cards,
    )

    readiness = host_search_readiness(cards)

    assert readiness["status"] == "available"
    assert readiness["callable"] is True
    assert readiness["available_provider_ids"] == ["codex_web_search"]
