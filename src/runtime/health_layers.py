"""Reusable platform health layer builder."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict


@dataclass(frozen=True)
class PlatformLayerSpec:
    platform: str
    login: Callable[[Dict[str, Dict]], Dict]
    search: Callable[[Dict[str, Dict]], Dict]
    detail: Callable[[Dict[str, Dict]], Dict]
    multimodal: Callable[[Dict[str, Dict]], Dict]


def layer(status: str, detail: str = "", **extra) -> Dict:
    data = {"status": status or "unknown", "detail": detail}
    data.update({key: value for key, value in extra.items() if value is not None})
    return data


def build_platform_health_layers(checks: Dict[str, Dict], specs: list[PlatformLayerSpec]) -> Dict[str, Dict[str, Dict]]:
    return {
        spec.platform: {
            "login": spec.login(checks),
            "search": spec.search(checks),
            "detail": spec.detail(checks),
            "multimodal": spec.multimodal(checks),
        }
        for spec in specs
    }
