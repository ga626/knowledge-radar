"""Short TTL cache for repeated degraded detail states."""

from __future__ import annotations

from dataclasses import dataclass, field
import copy
import hashlib
import json
import time
from typing import Any, Dict, Optional


@dataclass
class FailureStateCache:
    ttl_s: float = 180.0
    _items: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    _hits: int = 0
    _misses: int = 0
    _sets: int = 0

    def key(self, *, platform: str, url: str, failure_type: str = "") -> str:
        raw = json.dumps(
            {"platform": platform or "", "url": url or "", "failure_type": failure_type or ""},
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        row = self._items.get(key)
        if not row:
            self._misses += 1
            return None
        age = time.time() - float(row.get("created_at") or 0.0)
        if age > self.ttl_s:
            self._items.pop(key, None)
            self._misses += 1
            return None
        self._hits += 1
        value = copy.deepcopy(row.get("payload") or {})
        metadata = value.setdefault("metadata", {})
        metadata["failure_cache"] = {"hit": True, "key": key, "ttl_s": self.ttl_s, "age_s": round(age, 3)}
        return value

    def set(self, key: str, payload: Dict[str, Any]) -> None:
        self._sets += 1
        self._items[key] = {"created_at": time.time(), "payload": copy.deepcopy(payload)}

    def summary(self) -> Dict[str, Any]:
        lookups = self._hits + self._misses
        return {
            "schema": "knowledgeradar-failure-state-cache/v1",
            "status": "ok",
            "ttl_s": self.ttl_s,
            "entries": len(self._items),
            "metrics": {
                "hits": self._hits,
                "misses": self._misses,
                "sets": self._sets,
                "lookups": lookups,
                "hit_rate": round(self._hits / lookups, 4) if lookups else 0.0,
            },
        }


detail_failure_cache = FailureStateCache()
