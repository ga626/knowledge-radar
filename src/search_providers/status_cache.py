"""Short TTL cache for provider status rows."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import time
from typing import Any, Callable, Dict


def default_status_ttl_s() -> float:
    try:
        return max(0.0, float(os.environ.get("KR_PROVIDER_STATUS_TTL_S", "60")))
    except Exception:
        return 60.0


@dataclass
class ProviderStatusCache:
    ttl_s: float = field(default_factory=default_status_ttl_s)
    _created_at: float = 0.0
    _value: Dict[str, Dict[str, object]] | None = None
    _hits: int = 0
    _misses: int = 0

    def get(self, loader: Callable[[], Dict[str, Dict[str, object]]], *, force_refresh: bool = False) -> Dict[str, Dict[str, object]]:
        now = time.time()
        if (
            not force_refresh
            and self._value is not None
            and self.ttl_s > 0
            and now - self._created_at <= self.ttl_s
        ):
            self._hits += 1
            return {key: dict(value) for key, value in self._value.items()}
        self._misses += 1
        value = loader()
        self._value = {key: dict(row) for key, row in value.items()}
        self._created_at = now
        return {key: dict(row) for key, row in self._value.items()}

    def summary(self) -> Dict[str, Any]:
        lookups = self._hits + self._misses
        return {
            "schema": "knowledgeradar-provider-status-cache/v1",
            "status": "ok",
            "ttl_s": self.ttl_s,
            "cached": self._value is not None,
            "age_s": round(time.time() - self._created_at, 3) if self._value is not None else 0.0,
            "metrics": {
                "hits": self._hits,
                "misses": self._misses,
                "lookups": lookups,
                "hit_rate": round(self._hits / lookups, 4) if lookups else 0.0,
            },
        }


provider_status_cache = ProviderStatusCache()
