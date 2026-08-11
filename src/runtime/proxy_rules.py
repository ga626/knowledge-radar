"""Domain rule subscriptions for proxy/direct decisions.

This module is side-effect-light by default. It can classify a host from cached
rule files, and tests can inject a fetcher for refresh without touching network.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from typing import Callable, Iterable, Sequence

import httpx

from runtime.paths import proxy_rule_cache_dir


DEFAULT_DIRECT_RULE_URLS = (
    "https://raw.githubusercontent.com/Loyalsoldier/clash-rules/release/direct.txt",
)
DEFAULT_PROXY_RULE_URLS = (
    "https://raw.githubusercontent.com/Loyalsoldier/clash-rules/release/proxy.txt",
)
DEFAULT_TTL_SECONDS = 7 * 24 * 3600


@dataclass(frozen=True)
class DomainRule:
    kind: str
    value: str


@dataclass(frozen=True)
class ProxyRuleSet:
    direct: tuple[DomainRule, ...]
    proxy: tuple[DomainRule, ...]
    cache_dir: str
    updated_at: float = 0.0

    def classify_host(self, host: str) -> dict[str, object]:
        normalized = _normalize_host(host)
        if not normalized:
            return {"decision": "unknown", "host": host, "matched": None}
        direct_match = first_match(normalized, self.direct)
        if direct_match:
            return {"decision": "direct", "host": normalized, "matched": direct_match.__dict__}
        proxy_match = first_match(normalized, self.proxy)
        if proxy_match:
            return {"decision": "proxy", "host": normalized, "matched": proxy_match.__dict__}
        return {"decision": "unknown", "host": normalized, "matched": None}


def _env_list(name: str, defaults: Sequence[str]) -> tuple[str, ...]:
    configured = os.environ.get(name, "").strip()
    if not configured:
        return tuple(defaults)
    return tuple(item.strip() for item in configured.split(",") if item.strip())


def _normalize_host(host: str) -> str:
    value = str(host or "").strip().lower()
    if "://" in value:
        try:
            from urllib.parse import urlparse

            value = urlparse(value).hostname or ""
        except Exception:
            value = ""
    return value.strip(".")


def parse_rule_text(text: str) -> tuple[DomainRule, ...]:
    rules: list[DomainRule] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("payload:") or line in {"---", "[]"}:
            continue
        if line.startswith("-"):
            line = line[1:].strip()
        line = line.strip("'\"")
        if "," in line:
            parts = [part.strip().strip("'\"") for part in line.split(",")]
            rule_type = parts[0].upper()
            value = parts[1] if len(parts) > 1 else ""
            if rule_type in {"DOMAIN", "HOST"}:
                _append_rule(rules, "exact", value)
            elif rule_type in {"DOMAIN-SUFFIX", "HOST-SUFFIX"}:
                _append_rule(rules, "suffix", value)
            elif rule_type == "DOMAIN-KEYWORD":
                _append_rule(rules, "keyword", value)
            continue
        if line.startswith("*."):
            _append_rule(rules, "suffix", line[2:])
        elif line.startswith("+."):
            _append_rule(rules, "suffix", line[2:])
        elif line.startswith("."):
            _append_rule(rules, "suffix", line[1:])
        elif "*" in line:
            _append_rule(rules, "wildcard", line)
        else:
            _append_rule(rules, "exact", line)
    return tuple(rules)


def _append_rule(rules: list[DomainRule], kind: str, value: str) -> None:
    normalized = _normalize_host(value) if kind != "keyword" else str(value or "").strip().lower()
    if normalized:
        rules.append(DomainRule(kind=kind, value=normalized))


def first_match(host: str, rules: Iterable[DomainRule]) -> DomainRule | None:
    normalized = _normalize_host(host)
    for rule in rules:
        value = rule.value
        if rule.kind == "exact" and normalized == value:
            return rule
        if rule.kind == "suffix" and (normalized == value or normalized.endswith("." + value)):
            return rule
        if rule.kind == "keyword" and value in normalized:
            return rule
        if rule.kind == "wildcard":
            prefix, _, suffix = value.partition("*")
            if normalized.startswith(prefix.strip(".")) and normalized.endswith(suffix.strip(".")):
                return rule
    return None


def refresh_rule_cache(
    *,
    cache_dir: str | Path | None = None,
    fetcher: Callable[[str], str] | None = None,
) -> dict[str, object]:
    cache_root = Path(cache_dir or proxy_rule_cache_dir())
    cache_root.mkdir(parents=True, exist_ok=True)
    fetch = fetcher or _fetch_url
    urls = {
        "direct": _env_list("KR_PROXY_RULE_DIRECT_URLS", DEFAULT_DIRECT_RULE_URLS),
        "proxy": _env_list("KR_PROXY_RULE_PROXY_URLS", DEFAULT_PROXY_RULE_URLS),
    }
    counts: dict[str, int] = {}
    sizes: dict[str, int] = {}
    for name, rule_urls in urls.items():
        chunks = []
        for url in rule_urls:
            chunks.append(fetch(url))
        text = "\n".join(chunks)
        (cache_root / f"{name}.txt").write_text(text, encoding="utf-8")
        rules = parse_rule_text(text)
        counts[name] = len(rules)
        sizes[name] = len(text.encode("utf-8"))
    meta = {
        "schema": "knowledgeradar-proxy-rules-cache/v1",
        "updated_at": time.time(),
        "cache_dir": str(cache_root),
        "counts": counts,
        "sizes": sizes,
    }
    (cache_root / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "ok", **meta}


def _fetch_url(url: str) -> str:
    response = httpx.get(url, timeout=20, follow_redirects=True)
    response.raise_for_status()
    return response.text


def load_proxy_rules(cache_dir: str | Path | None = None, *, refresh_if_expired: bool = False) -> ProxyRuleSet:
    cache_root = Path(cache_dir or proxy_rule_cache_dir())
    ttl = int(os.environ.get("KR_PROXY_RULE_TTL_SECONDS") or DEFAULT_TTL_SECONDS)
    meta_path = cache_root / "metadata.json"
    updated_at = 0.0
    if meta_path.is_file():
        try:
            updated_at = float(json.loads(meta_path.read_text(encoding="utf-8")).get("updated_at") or 0)
        except Exception:
            updated_at = 0.0
    if refresh_if_expired and (not updated_at or time.time() - updated_at > ttl):
        refresh_rule_cache(cache_dir=cache_root)
        updated_at = time.time()
    direct_text = _read_optional(cache_root / "direct.txt")
    proxy_text = _read_optional(cache_root / "proxy.txt")
    return ProxyRuleSet(
        direct=parse_rule_text(direct_text),
        proxy=parse_rule_text(proxy_text),
        cache_dir=str(cache_root),
        updated_at=updated_at,
    )


def _read_optional(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def proxy_rules_summary(cache_dir: str | Path | None = None) -> dict[str, object]:
    rules = load_proxy_rules(cache_dir)
    return {
        "schema": "knowledgeradar-proxy-rules/v1",
        "status": "ok" if (rules.direct or rules.proxy) else "not_cached",
        "cache_dir": rules.cache_dir,
        "configured_by": [
            "KR_PROXY_RULE_DIRECT_URLS",
            "KR_PROXY_RULE_PROXY_URLS",
            "KR_PROXY_RULE_CACHE_DIR",
            "KR_PROXY_RULE_TTL_SECONDS",
        ],
        "counts": {"direct": len(rules.direct), "proxy": len(rules.proxy)},
        "updated_at": rules.updated_at,
        "ttl_seconds": int(os.environ.get("KR_PROXY_RULE_TTL_SECONDS") or DEFAULT_TTL_SECONDS),
        "clash_required": False,
        "notes": ["Rules classify domains even when Clash is not installed; proxy execution still uses HTTP_PROXY/HTTPS_PROXY or platform-specific proxy envs."],
    }
