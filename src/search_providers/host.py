"""Persistent host/Agent platform web search capability cards."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

import httpx

from runtime.paths import runtime_state_dir

from .models import SearchProviderResult, WebSearchRequest, utc_now_iso
from .providers import BaseSearchProvider, SearchProviderError, _clean_limit


CARD_SCHEMA = "knowledgeradar-host-search-card/v1"


def _cards_dir() -> Path:
    return Path(os.environ.get("KR_HOST_SEARCH_CARD_DIR") or (runtime_state_dir() / "search_host_cards"))


def _read_card(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_host_search_cards(cards_dir: Path | None = None) -> List[Dict[str, Any]]:
    root = cards_dir or _cards_dir()
    if not root.exists():
        return []
    cards: List[Dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        card = _read_card(path)
        if card:
            card.setdefault("id", path.stem)
            cards.append(card)
    return cards


def write_host_search_card(card: Dict[str, Any], cards_dir: Path | None = None) -> Path:
    """Persist a host/Agent platform search capability card."""
    root = cards_dir or _cards_dir()
    root.mkdir(parents=True, exist_ok=True)
    platform = str(card.get("platform") or card.get("id") or "unknown").strip().lower()
    card_id = str(card.get("id") or f"{platform}_web_search").strip().lower()
    payload = {
        "schema": CARD_SCHEMA,
        "id": card_id,
        "platform": platform,
        "state": str(card.get("state") or "absent"),
        "kind": str(card.get("kind") or "host_web_search"),
        "enabled": bool(card.get("enabled", False)),
        "detected_at": str(card.get("detected_at") or ""),
        "last_verified_at": str(card.get("last_verified_at") or card.get("detected_at") or ""),
        "validation": {
            **dict(card.get("validation") or {}),
            "repeat_on_startup": bool((card.get("validation") or {}).get("repeat_on_startup", False)),
        },
        "capabilities": dict(card.get("capabilities") or {}),
        "call_contract": dict(card.get("call_contract") or {}),
        "notes": str(card.get("notes") or ""),
    }
    path = root / f"{card_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def ensure_host_search_card(
    platform: str,
    *,
    detector: Any | None = None,
    cards_dir: Path | None = None,
) -> Dict[str, Any]:
    """Return a persisted host card, running first-attach detection only once."""
    normalized = platform.strip().lower()
    for card in load_host_search_cards(cards_dir):
        if str(card.get("platform") or "").lower() == normalized:
            return card
    if detector is None:
        detected: Dict[str, Any] = {
            "platform": normalized,
            "id": f"{normalized}_web_search",
            "state": "absent",
            "enabled": False,
            "notes": "No host search detector was registered for this Agent platform.",
        }
    else:
        raw = detector(normalized)
        if isinstance(raw, dict):
            detected = dict(raw)
        else:
            detected = {"state": "available" if raw else "absent", "enabled": bool(raw)}
        detected.setdefault("platform", normalized)
        detected.setdefault("id", f"{normalized}_web_search")
    write_host_search_card(detected, cards_dir=cards_dir)
    for card in load_host_search_cards(cards_dir):
        if str(card.get("platform") or "").lower() == normalized:
            return card
    return detected


def host_search_card_summary(cards_dir: Path | None = None) -> Dict[str, Any]:
    cards = load_host_search_cards(cards_dir)
    readiness = host_search_readiness(cards_dir)
    return {
        "schema": "knowledgeradar-host-search-cards/v1",
        "cards_dir": str(cards_dir or _cards_dir()),
        "readiness": readiness,
        "cards": [
            {
                "id": card.get("id"),
                "platform": card.get("platform"),
                "state": card.get("state", "absent"),
                "kind": card.get("kind", "host_web_search"),
                "enabled": bool(card.get("enabled", False)),
                "repeat_on_startup": bool((card.get("validation") or {}).get("repeat_on_startup", False)),
            }
            for card in cards
        ],
    }


def host_search_readiness(cards_dir: Path | None = None) -> Dict[str, Any]:
    """Classify host/Agent search capability without pretending absent hosts are failures."""
    cards = load_host_search_cards(cards_dir)
    if not cards:
        return {
            "schema": "knowledgeradar-host-search-readiness/v1",
            "status": "absent",
            "callable": False,
            "available_provider_ids": [],
            "reason": "No host/Agent search capability card is persisted for this deployment.",
            "expected_degraded": True,
        }
    available: List[str] = []
    declared_not_callable: List[str] = []
    absent: List[str] = []
    failed: List[str] = []
    for card in cards:
        card_id = str(card.get("id") or "")
        state = str(card.get("state") or "absent")
        enabled = bool(card.get("enabled", False))
        contract = card.get("call_contract") if isinstance(card.get("call_contract"), dict) else {}
        endpoint = str(contract.get("endpoint") or os.environ.get("KR_HOST_SEARCH_ENDPOINT", ""))
        if state in {"absent", "not_configured"} or not enabled:
            absent.append(card_id)
        elif state == "available" and endpoint:
            available.append(card_id)
        elif state == "available" and not endpoint:
            declared_not_callable.append(card_id)
        else:
            failed.append(card_id)
    if available:
        status = "available"
        reason = "At least one host/Agent search card has a callable endpoint."
        expected_degraded = False
    elif declared_not_callable:
        status = "declared_not_callable"
        reason = "Host/Agent search is declared but KR has no callable endpoint; it cannot join search waves."
        expected_degraded = True
    elif failed:
        status = "failed"
        reason = "Host/Agent search card is present but not currently usable."
        expected_degraded = False
    else:
        status = "absent"
        reason = "All host/Agent search cards are absent or disabled."
        expected_degraded = True
    return {
        "schema": "knowledgeradar-host-search-readiness/v1",
        "status": status,
        "callable": bool(available),
        "available_provider_ids": available,
        "declared_not_callable_provider_ids": declared_not_callable,
        "absent_provider_ids": absent,
        "failed_provider_ids": failed,
        "reason": reason,
        "expected_degraded": expected_degraded,
    }


class HostSearchProvider(BaseSearchProvider):
    name = "host_web_search"

    def __init__(self, card: Dict[str, Any], timeout: float = 15.0) -> None:
        self.card = dict(card)
        self.name = str(self.card.get("id") or "host_web_search")
        self.timeout = timeout
        contract = self.card.get("call_contract") if isinstance(self.card.get("call_contract"), dict) else {}
        self.endpoint = str(contract.get("endpoint") or os.environ.get("KR_HOST_SEARCH_ENDPOINT", ""))
        self.endpoint_explicit = bool(self.endpoint)
        self.api_key = os.environ.get("KR_HOST_SEARCH_API_KEY", "")

    def available(self) -> bool:
        return bool(self.card.get("enabled")) and self.card.get("state") == "available"

    def status(self) -> Dict[str, Any]:
        state = str(self.card.get("state") or "absent")
        available = self.available()
        return {
            "configured": state == "available",
            "available": available,
            "status": state,
            "role": "host_web_search",
            "platform": self.card.get("platform"),
            "card_id": self.card.get("id"),
            "degraded_ok": state in {"absent", "not_configured"},
            "notes": "Host search cards are persistent. absent means no host search capability and is silently skipped; available means invocation failures are real provider errors.",
        }

    def search(self, request: WebSearchRequest) -> List[SearchProviderResult]:
        if not self.available():
            raise SearchProviderError(self.name, "host search capability is absent", error_type="host_search_absent")
        if not self.endpoint:
            raise SearchProviderError(self.name, "host search card has no callable endpoint", error_type="not_callable")
        payload = {
            "query": request.query,
            "limit": _clean_limit(request.limit),
            "freshness": request.freshness,
            "language": request.language,
        }
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            with httpx.Client(timeout=self.timeout, headers=headers) as client:
                response = client.post(self.endpoint, json=payload)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            raise SearchProviderError(self.name, str(exc), error_type="host_search_failed") from exc
        items = data.get("items") if isinstance(data, dict) else []
        now = utc_now_iso()
        results: List[SearchProviderResult] = []
        for item in items or []:
            url = str(item.get("url") or "")
            title = str(item.get("title") or "")
            if not url or not title:
                continue
            results.append(
                SearchProviderResult(
                    title=title,
                    url=url,
                    snippet=str(item.get("snippet") or item.get("content") or ""),
                    source_provider=self.name,
                    published_at=str(item.get("published_at") or ""),
                    retrieved_at=now,
                    raw={"host_card": self.card.get("id"), **dict(item)},
                )
            )
        return results[: _clean_limit(request.limit)]


def host_search_providers(cards_dir: Path | None = None) -> Dict[str, HostSearchProvider]:
    providers: Dict[str, HostSearchProvider] = {}
    for card in load_host_search_cards(cards_dir):
        state = str(card.get("state") or "absent")
        if state != "available":
            continue
        provider = HostSearchProvider(card)
        if not provider.endpoint:
            continue
        providers[provider.name] = provider
    return providers
