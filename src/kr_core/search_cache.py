"""Short-lived search result cache."""

from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class SearchCache:
    ttl_s: int = 180
    _items: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    _hits: int = 0
    _misses: int = 0
    _expired: int = 0
    _sets: int = 0

    def key(
        self,
        *,
        platform: str,
        query: str,
        limit: int,
        search_type: str = "",
        provider: str = "",
        freshness: str = "",
        include_raw_content: bool = False,
        language: str = "",
        options: Dict[str, Any] | None = None,
    ) -> str:
        raw = json.dumps(
            {
                "platform": platform,
                "query": self.normalize_query(query),
                "limit": int(limit or 0),
                "search_type": search_type or "",
                "provider": provider or "",
                "freshness": freshness or "",
                "include_raw_content": bool(include_raw_content),
                "language": (language or "").strip().lower(),
                "options": self._stable_options(options or {}),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def normalize_query(query: str) -> str:
        return " ".join((query or "").strip().lower().split())

    @staticmethod
    def _stable_options(options: Dict[str, Any]) -> Dict[str, Any]:
        def _normalize(value: Any) -> Any:
            if isinstance(value, dict):
                return {str(k): _normalize(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
            if isinstance(value, (list, tuple, set)):
                return [_normalize(v) for v in value]
            return value

        return {str(k): _normalize(v) for k, v in sorted((options or {}).items(), key=lambda item: str(item[0]))}

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        row = self._items.get(key)
        if not row:
            self._misses += 1
            return None
        if time.time() - float(row.get("created_at", 0)) > self.ttl_s:
            self._items.pop(key, None)
            self._expired += 1
            self._misses += 1
            return None
        self._hits += 1
        result = copy.deepcopy(row.get("result") or {})
        metadata = result.setdefault("metadata", {})
        metadata["cache"] = {
            "hit": True,
            "key": key,
            "ttl_s": self.ttl_s,
            "age_s": round(time.time() - float(row.get("created_at", 0)), 3),
        }
        return result

    def set(self, key: str, result: Dict[str, Any]) -> Dict[str, Any]:
        self._sets += 1
        self._items[key] = {"created_at": time.time(), "result": copy.deepcopy(result)}
        metadata = result.setdefault("metadata", {})
        metadata["cache"] = {"hit": False, "key": key, "ttl_s": self.ttl_s}
        return result

    def summary(self) -> Dict[str, Any]:
        now = time.time()
        live = {k: v for k, v in self._items.items() if now - float(v.get("created_at", 0)) <= self.ttl_s}
        removed = len(self._items) - len(live)
        if removed > 0:
            self._expired += removed
        self._items = live
        lookups = self._hits + self._misses
        hit_rate = round(self._hits / lookups, 4) if lookups else 0.0
        return {
            "status": "ok",
            "schema": "knowledgeradar-search-cache-summary/v1",
            "ttl_s": self.ttl_s,
            "entries": len(self._items),
            "metrics": {
                "hits": self._hits,
                "misses": self._misses,
                "expired": self._expired,
                "sets": self._sets,
                "lookups": lookups,
                "hit_rate": hit_rate,
            },
        }
