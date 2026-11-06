"""Platform adapter registry."""

from __future__ import annotations

from typing import Callable, Dict, Iterable

from .adapter import PlatformAdapter


class PlatformRegistry:
    def __init__(self) -> None:
        self._adapters: Dict[str, PlatformAdapter] = {}
        self._details: Dict[str, Callable] = {}
        self._after_detail: Dict[str, Callable] = {}

    def register(self, adapter: PlatformAdapter, *, detail_factory=None, after_detail=None) -> None:
        self._adapters[adapter.platform] = adapter
        if detail_factory:
            self._details[adapter.platform] = detail_factory
        else:
            self._details.pop(adapter.platform, None)
        if after_detail:
            self._after_detail[adapter.platform] = after_detail
        else:
            self._after_detail.pop(adapter.platform, None)

    def detail_strategy(self, platform):
        factory = self._details.get(platform)
        return factory() if factory else None

    def platform_for_url(self, url):
        matches = [name for name, adapter in self._adapters.items()
            if (predicate := getattr(adapter, "matches_url", None)) and predicate(url)]
        if len(matches) > 1:
            raise ValueError("ambiguous_platform_url")
        return matches[0] if matches else ""

    def resource_ready(self, platform, resource):
        hook = getattr(self._adapters.get(platform), "resource_ready", None)
        return hook(resource) if hook else None

    def runtime_identity(self, platform):
        adapter = self._adapters.get(platform)
        identity = getattr(adapter, "runtime_identity", None)
        return identity() if identity else {}

    def interaction_status(self, platform):
        hook = getattr(self._adapters.get(platform), "interaction_status", None)
        return hook() if hook else "none"

    def cancel_task(self, platform, task_id):
        hook = getattr(self._adapters.get(platform), 'cancel_task', None)
        return hook(task_id) if hook else None

    def prepare_detail(self, request):
        adapter = self._adapters.get(request.platform)
        hook = getattr(adapter, "prepare_detail", None)
        return hook(request) if hook else request

    def supports_staged_detail(self, platform):
        return callable(getattr(self._adapters.get(platform), "enhance_detail", None))

    def enhance_detail(self, request, collected):
        hook = getattr(self.get(request.platform), "enhance_detail", None)
        if not hook:
            raise ValueError("platform_has_no_enhancement_stage")
        return hook(request, collected)

    def after_detail(self, platform, result):
        hook = self._after_detail.get(platform)
        return hook(result) if hook else result

    def set_after_detail(self, platform, hook):
        if platform not in self._adapters:
            raise KeyError(platform)
        self._after_detail[platform] = hook

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
        self._details.clear()
        self._after_detail.clear()


registry = PlatformRegistry()
