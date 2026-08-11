"""Platform adapter registry."""

from __future__ import annotations

from typing import Dict, Iterable

from .adapter import PlatformAdapter


class PlatformRegistry:
    def __init__(self) -> None:
        self._adapters: Dict[str, PlatformAdapter] = {}

    def register(self, adapter: PlatformAdapter) -> None:
        self._adapters[adapter.platform] = adapter

    def get(self, platform: str) -> PlatformAdapter:
        try:
            return self._adapters[platform]
        except KeyError as exc:
            available = ", ".join(sorted(self._adapters)) or "<none>"
            raise KeyError(f"Platform adapter not registered: {platform}. Available: {available}") from exc

    def platforms(self) -> Iterable[str]:
        return tuple(sorted(self._adapters))

    def clear(self) -> None:
        self._adapters.clear()


registry = PlatformRegistry()
