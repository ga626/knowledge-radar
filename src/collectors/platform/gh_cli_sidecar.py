"""GitHub CLI sidecar wrapper.

The wrapper keeps gh as an isolated L7 sidecar: it is optional, timeout-bound,
normalizes output, and never changes the public MCP tool surface.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import shutil
import subprocess
from runtime.process import silent_subprocess_run
import time
from typing import Any, Dict, List, Optional
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed

import httpx

from kr_core.errors import ErrorCode, KnowledgeRadarError
from runtime.cost_latency import TTLCache, attach_runtime_metadata, budget_envelope, stable_key
from runtime.degradation import get_degradation_policy


SRC_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO_ROOT = os.path.dirname(SRC_ROOT)

DEFAULT_GH_CANDIDATES = [
    os.path.join(os.path.expanduser("~"), ".agent-reach", "tools", "gh", "bin", "gh.exe"),
    os.path.join(os.path.expanduser("~"), "gh.exe"),
]

_HEALTH_CACHE = TTLCache("github.health", ttl_s=float(os.environ.get("KR_GH_HEALTH_TTL_SECONDS", "300")), max_items=4)
_SEARCH_CACHE = TTLCache("github.search", ttl_s=float(os.environ.get("KR_GITHUB_SEARCH_CACHE_TTL_S", "900")), max_items=128)

ERROR_MAP = {
    "authentication required": "LOGIN_REQUIRED",
    "not logged into": "LOGIN_REQUIRED",
    "not logged in": "LOGIN_REQUIRED",
    "auth missing": "LOGIN_REQUIRED",
    "authentication": "LOGIN_REQUIRED",
    "unauthorized": "LOGIN_REQUIRED",
    "could not resolve": "NETWORK_ERROR",
    "connection": "NETWORK_ERROR",
    "timeout": "TIMEOUT",
    "rate limit": "RATE_LIMITED",
    "api rate limit": "RATE_LIMITED",
    "unknown json field": "SCHEMA_CHANGED",
    "not found": "NOT_FOUND",
}


def _enabled() -> bool:
    return os.environ.get("KR_GH_CLI_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}


def _timeout(default: float = 20.0) -> float:
    try:
        return float(os.environ.get("KR_GH_CLI_TIMEOUT_SECONDS", str(default)))
    except ValueError:
        return default


def _gh_exe() -> str:
    configured = os.environ.get("KR_GH_CLI_EXE", "").strip()
    if configured:
        return configured
    found = shutil.which("gh")
    if found:
        return found
    for candidate in DEFAULT_GH_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return "gh"


def _account_hash(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _classify_error(message: str) -> str:
    text = (message or "").lower()
    for marker, code in ERROR_MAP.items():
        if marker in text:
            return code
    return "UNKNOWN"


def _base_env() -> Dict[str, str]:
    env = dict(os.environ)
    env.setdefault("GH_NO_UPDATE_NOTIFIER", "1")
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _run_gh(args: List[str], *, timeout: Optional[float] = None) -> subprocess.CompletedProcess[str]:
    if not _enabled():
        raise KnowledgeRadarError(
            "gh CLI sidecar is disabled by KR_GH_CLI_ENABLED",
            code=ErrorCode.REQUEST_FAILED,
            platform="GitHub",
            retryable=True,
            metadata={"failure_code": "PROVIDER_UNAVAILABLE"},
        )
    exe = _gh_exe()
    if exe == "gh" and not shutil.which("gh"):
        raise KnowledgeRadarError(
            "gh executable not found; set KR_GH_CLI_EXE",
            code=ErrorCode.REQUEST_FAILED,
            platform="GitHub",
            retryable=True,
            metadata={"failure_code": "DEPENDENCY_CONFLICT"},
        )
    if exe != "gh" and not os.path.isfile(exe):
        raise KnowledgeRadarError(
            f"gh executable not found: {exe}",
            code=ErrorCode.REQUEST_FAILED,
            platform="GitHub",
            retryable=True,
            metadata={"failure_code": "DEPENDENCY_CONFLICT"},
        )
    return silent_subprocess_run(
        [exe, *args],
        cwd=REPO_ROOT,
        env=_base_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout or _timeout(),
    )


def _status_account() -> Dict[str, Any]:
    proc = _run_gh(["auth", "status"], timeout=min(_timeout(), 8.0))
    text = "\n".join(part for part in [proc.stdout, proc.stderr] if part)
    account = ""
    for line in text.splitlines():
        line = line.strip()
        if "Logged in to github.com account " in line:
            account = line.split("Logged in to github.com account ", 1)[1].split()[0]
            break
    return {
        "returncode": proc.returncode,
        "authenticated": proc.returncode == 0,
        "account_id_hash": _account_hash(account),
        "raw_summary": text[:500],
    }


def health(*, stale_ok: bool = False, force_refresh: bool = False) -> Dict[str, Any]:
    cache_key = stable_key("github.health", _gh_exe(), _enabled())
    if not force_refresh:
        cached = _HEALTH_CACHE.get(cache_key, allow_stale=stale_ok)
        if cached:
            return cached
    started = time.time()
    breaker = get_degradation_policy().is_open("sidecar:gh_cli")
    exe = _gh_exe()
    configured = (exe == "gh" and bool(shutil.which("gh"))) or (exe != "gh" and os.path.isfile(exe))
    base = {
        "schema": "knowledgeradar-gh-cli-sidecar-health/v1",
        "enabled": _enabled(),
        "configured": configured,
        "available": False,
        "exe": exe,
        "breaker": breaker,
        "strategy": "gh_cli_sidecar_backup_candidate",
    }
    if breaker.get("open"):
        result = {**base, "status": "degraded", "detail": f"gh CLI breaker open: {breaker.get('last_reason') or 'recent failures'}", "retryable": True}
        _HEALTH_CACHE.set(cache_key, result)
        return attach_runtime_metadata(result, tool_name="gh_cli_sidecar.health", capability_id="github.search", started=started, budget=budget_envelope("fast"))
    try:
        version_proc = _run_gh(["--version"], timeout=min(_timeout(), 5.0))
        version = (version_proc.stdout or version_proc.stderr or "").splitlines()[0].strip()
        status = _status_account()
        if not status.get("authenticated"):
            result = {
                **base,
                "status": "degraded",
                "detail": "gh CLI is installed but not authenticated",
                "version": version,
                "failure_code": "LOGIN_REQUIRED",
                "retryable": True,
            }
            cache_meta = _HEALTH_CACHE.set(cache_key, result)
            return attach_runtime_metadata(result, tool_name="gh_cli_sidecar.health", capability_id="github.search", started=started, budget=budget_envelope("fast"), cache=cache_meta)
        result = {
            **base,
            "available": True,
            "status": "ok",
            "detail": "gh CLI authenticated and available",
            "version": version,
            "account_id_hash": status.get("account_id_hash", ""),
        }
        cache_meta = _HEALTH_CACHE.set(cache_key, result)
        return attach_runtime_metadata(result, tool_name="gh_cli_sidecar.health", capability_id="github.search", started=started, budget=budget_envelope("fast"), cache=cache_meta)
    except KnowledgeRadarError as exc:
        result = {**base, "status": "degraded", "detail": str(exc), "failure_code": exc.metadata.get("failure_code", "UNKNOWN"), "retryable": exc.retryable}
    except subprocess.TimeoutExpired:
        result = {**base, "status": "degraded", "detail": "gh CLI health timed out", "failure_code": "TIMEOUT", "retryable": True}
    except Exception as exc:
        result = {**base, "status": "degraded", "detail": f"gh CLI health failed: {exc}", "failure_code": "UNKNOWN", "retryable": True}
    cache_meta = _HEALTH_CACHE.set(cache_key, result)
    return attach_runtime_metadata(result, tool_name="gh_cli_sidecar.health", capability_id="github.search", started=started, budget=budget_envelope("fast"), cache=cache_meta)


def _normalize_repo(item: Dict[str, Any]) -> Dict[str, Any]:
    full_name = str(item.get("fullName") or "")
    url = str(item.get("url") or (f"https://github.com/{full_name}" if full_name else ""))
    desc = str(item.get("description") or "")
    title = full_name or str(item.get("name") or url)
    return {
        "title": title,
        "url": url,
        "snippet": desc,
        "source_provider": "gh_cli",
        "published_at": "",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "score": item.get("stargazersCount"),
        "raw": {
            "full_name": full_name,
            "description": desc,
            "language": item.get("language"),
            "stargazers_count": item.get("stargazersCount"),
            "updated_at": item.get("updatedAt"),
            "source": "gh_cli_sidecar",
        },
    }


def _normalize_rest_repo(item: Dict[str, Any]) -> Dict[str, Any]:
    full_name = str(item.get("full_name") or "")
    url = str(item.get("html_url") or (f"https://github.com/{full_name}" if full_name else ""))
    desc = str(item.get("description") or "")
    owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
    return {
        "title": full_name or str(item.get("name") or url),
        "url": url,
        "snippet": desc,
        "source_provider": "github_rest",
        "published_at": "",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "score": item.get("stargazers_count"),
        "raw": {
            "full_name": full_name,
            "description": desc,
            "language": item.get("language"),
            "stargazers_count": item.get("stargazers_count"),
            "updated_at": item.get("updated_at"),
            "owner": owner.get("login"),
            "source": "github_rest_search",
        },
    }


def _rest_queries(query: str) -> list[str]:
    clean = " ".join(str(query or "").split())
    if not clean:
        return []
    terms = set(_query_terms(clean))
    semantic_queries: list[str] = []
    if {"research", "agent"} & terms and ({"report", "generator", "researcher"} & terms):
        semantic_queries.extend(
            [
                '"gpt-researcher" OR "gpt researcher" in:name,description,readme',
                '"deep research" agent in:name,description,readme',
                '"research agent" report in:name,description,readme',
                '"report generator" agent in:name,description,readme',
            ]
        )
    queries = [
        f"{clean} in:name,description,readme",
        clean,
    ]
    hyphenated = clean.replace(" ", "-")
    if hyphenated != clean:
        queries.append(f"{hyphenated} in:name,description,readme")
    compact = clean.replace(" ", "")
    if compact and compact != clean:
        queries.append(f"{compact} in:name,description,readme")
    seen: set[str] = set()
    result: list[str] = []
    for item in [*semantic_queries, *queries]:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


_STOP_TERMS = {
    "a",
    "an",
    "and",
    "the",
    "for",
    "with",
    "open",
    "source",
    "project",
    "repo",
    "repository",
    "github",
    "ai",
    "llm",
}
_GENERIC_REPO_MARKERS = (
    "awesome",
    "public-apis",
    "free-programming-books",
    "developer-roadmap",
    "system-design-primer",
)


def _query_terms(query: str) -> list[str]:
    terms = [term.lower() for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]+", str(query or ""))]
    result: list[str] = []
    seen: set[str] = set()
    for term in terms:
        normalized = term.replace("_", "-")
        if len(normalized) < 2 or normalized in _STOP_TERMS or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _repo_relevance_score(query: str, item: Dict[str, Any]) -> float:
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    title = str(item.get("title") or raw.get("full_name") or "").lower()
    desc = str(item.get("snippet") or raw.get("description") or "").lower()
    language = str(raw.get("language") or "").lower()
    text = f"{title} {desc} {language}"
    terms = _query_terms(query)
    score = 0.0
    clean_query = " ".join(str(query or "").lower().split())
    if clean_query and clean_query in text:
        score += 10
    for term in terms:
        variants = {term, term.replace("-", ""), term.replace("-", " ")}
        if term == "research":
            variants.update({"researcher", "researching"})
        if term == "agent":
            variants.update({"agents", "agentic", "autonomous agent"})
        if term == "generator":
            variants.update({"generation", "generates", "generate"})
        if any(variant and variant in title for variant in variants):
            score += 4
        elif any(variant and variant in desc for variant in variants):
            score += 2
    if "deep research" in text:
        score += 5
    if "autonomous agent" in text or "autonomous agents" in text:
        score += 4
    if any(phrase in text for phrase in ("report generator", "report generation", "generates reports", "generate reports")):
        score += 4
    if any(phrase in text for phrase in ("research report", "research reports")):
        score += 3
    if any(marker in title for marker in _GENERIC_REPO_MARKERS) and not any(marker in clean_query for marker in _GENERIC_REPO_MARKERS):
        score -= 20
    if any(marker in desc for marker in ("curated list", "awesome list", "collection of", "tutorials", "papers", "skills for")):
        score -= 8
    if any(marker in text for marker in ("offensive security", "vulnerability", "pentest", "job search")):
        score -= 6
    try:
        stars = float(item.get("score") or raw.get("stargazers_count") or 0)
    except Exception:
        stars = 0.0
    score += min(stars, 100_000) / 100_000
    return score


def _rank_repositories(query: str, items: List[Dict[str, Any]], *, limit: int) -> List[Dict[str, Any]]:
    ranked = []
    for index, item in enumerate(items):
        copy = dict(item)
        relevance = _repo_relevance_score(query, copy)
        copy["relevance_score"] = round(relevance, 3)
        ranked.append((relevance, index, copy))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return [item for _score, _index, item in ranked[:limit]]


def _github_rest_search(query: str, *, limit: int, timeout: Optional[float] = None) -> Dict[str, Any]:
    started = time.time()
    total_budget_s = float(os.environ.get("KR_GITHUB_REST_TOTAL_BUDGET_S", "8"))
    per_query_timeout = min(float(timeout or min(_timeout(), 8.0)), max(1.0, total_budget_s / 2))
    max_workers = max(1, min(int(os.environ.get("KR_GITHUB_REST_MAX_WORKERS", "4")), 6))
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "KnowledgeRadar-MCP",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last_error = ""
    seen_urls: set[str] = set()
    items: list[Dict[str, Any]] = []
    attempted_queries = _rest_queries(query)

    def _fetch(rest_query: str) -> tuple[str, list[Dict[str, Any]], str]:
        try:
            response = httpx.get(
                "https://api.github.com/search/repositories",
                params={"q": rest_query, "per_page": max(1, min(max(limit * 4, limit), 20)), "sort": "stars", "order": "desc"},
                headers=headers,
                timeout=per_query_timeout,
            )
            if response.status_code in {403, 429}:
                return rest_query, [], f"github REST rate limited: HTTP {response.status_code}"
            if response.status_code >= 400:
                return rest_query, [], f"github REST search failed: HTTP {response.status_code}"
            payload = response.json()
        except Exception as exc:
            return rest_query, [], str(exc)
        return rest_query, [_normalize_rest_repo(raw_item) for raw_item in payload.get("items") or [] if isinstance(raw_item, dict)], ""

    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_fetch, rest_query) for rest_query in attempted_queries]
        try:
            future_iter = as_completed(futures, timeout=total_budget_s)
            for future in future_iter:
                completed += 1
                _rest_query, fetched_items, error = future.result()
                if error:
                    last_error = error
                    continue
                if time.time() - started > total_budget_s:
                    last_error = "github REST search total budget exhausted"
                    break
                for item in fetched_items:
                    url = item.get("url") or ""
                    if url and url in seen_urls:
                        continue
                    if url:
                        seen_urls.add(url)
                    items.append(item)
                items = _rank_repositories(query, items, limit=max(limit, min(limit * 4, 20)))
                if len(items) >= limit and completed >= min(2, len(attempted_queries)):
                    break
        except FuturesTimeoutError:
            last_error = "github REST search total budget exhausted"
        finally:
            for future in futures:
                future.cancel()
    return {
        "items": _rank_repositories(query, items, limit=limit),
        "error": last_error,
        "attempted_queries": attempted_queries,
        "attempted_queries_count": len(attempted_queries),
        "completed_queries_count": completed,
        "elapsed_s": round(time.time() - started, 3),
        "total_budget_s": total_budget_s,
        "per_query_timeout_s": per_query_timeout,
    }


def search_repositories(query: str, *, limit: int = 5) -> Dict[str, Any]:
    started = time.time()
    clean_limit = max(1, min(int(limit or 5), 20))
    budget = budget_envelope("balanced", max_sync_wait_s=min(_timeout(), 10.0), max_wall_time_s=min(_timeout() + 8.0, 24.0))
    cache_context = {
        "exe": _gh_exe(),
        "enabled": _enabled(),
        "run_gh_impl": getattr(_run_gh, "__module__", "") + "." + getattr(_run_gh, "__name__", ""),
    }
    cache_key = stable_key("github.search", query, clean_limit, cache_context)
    cached = _SEARCH_CACHE.get(cache_key, allow_stale=True)
    if cached:
        return cached
    policy = get_degradation_policy()
    breaker_key = "sidecar:gh_cli"
    breaker = policy.is_open(breaker_key)
    if breaker.get("open"):
        raise KnowledgeRadarError(
            f"gh CLI sidecar breaker open: {breaker.get('last_reason') or 'recent failures'}",
            code=ErrorCode.REQUEST_FAILED,
            platform="GitHub",
            retryable=True,
            metadata={"failure_code": "PROVIDER_UNAVAILABLE", "breaker": breaker},
        )
    args = [
        "search",
        "repos",
        query,
        "--limit",
        str(clean_limit),
        "--json",
        "fullName,description,url,stargazersCount,updatedAt,language",
    ]
    gh_error: KnowledgeRadarError | None = None
    try:
        proc = _run_gh(args, timeout=_timeout())
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if proc.returncode != 0:
            failure_code = _classify_error(stderr or stdout)
            raise KnowledgeRadarError(
                stderr or stdout or f"gh CLI exited {proc.returncode}",
                code=ErrorCode.REQUEST_FAILED,
                platform="GitHub",
                retryable=failure_code not in {"LOGIN_REQUIRED", "DEPENDENCY_CONFLICT"},
                metadata={"failure_code": failure_code, "returncode": proc.returncode},
            )
        data = json.loads(stdout or "[]")
        if not isinstance(data, list):
            raise KnowledgeRadarError(
                "gh CLI returned non-list repository search payload",
                code=ErrorCode.PARSE_FAILED,
                platform="GitHub",
                retryable=True,
                metadata={"failure_code": "SCHEMA_CHANGED"},
            )
        items = _rank_repositories(query, [_normalize_repo(item) for item in data if isinstance(item, dict)], limit=clean_limit)
        if not items:
            rest_started = time.time()
            rest = _github_rest_search(query, limit=clean_limit)
            if rest.get("items"):
                items = list(rest["items"])
                policy.mark_success(breaker_key, "github_rest_fallback", {"query": query, "item_count": len(items)})
                result = {
                    "query": query,
                    "provider": "github",
                    "items": items,
                    "total": len(items),
                    "fallback_used": True,
                    "attempted_providers": ["github", "github_rest"],
                    "metadata": {
                        "sidecar": "gh_cli",
                        "strategy": "github_rest_after_empty_cli",
                        "elapsed_s": round(time.time() - started, 3),
                        "cli_elapsed_s": round(rest_started - started, 3),
                        "rest_elapsed_s": round(float(rest.get("elapsed_s") or 0), 3),
                        "attempted_queries_count": rest.get("attempted_queries_count", len(rest.get("attempted_queries") or [])),
                        "first_result_elapsed_s": round(time.time() - started, 3),
                        "exe": _gh_exe(),
                        "rest_attempted_queries": rest.get("attempted_queries") or [],
                    },
                }
                cache_meta = _SEARCH_CACHE.set(cache_key, result)
                return attach_runtime_metadata(result, tool_name="search_github_repositories", capability_id="github.search", started=started, budget=budget, cache=cache_meta)
        policy.mark_success(breaker_key, "gh_cli_sidecar", {"query": query, "item_count": len(items)})
        result = {
            "query": query,
            "provider": "github",
            "items": items,
            "total": len(items),
            "fallback_used": False,
            "attempted_providers": ["github"],
            "metadata": {
                "sidecar": "gh_cli",
                "strategy": "gh_cli_sidecar_backup_candidate",
                "elapsed_s": round(time.time() - started, 3),
                "cli_elapsed_s": round(time.time() - started, 3),
                "rest_elapsed_s": 0,
                "attempted_queries_count": 0,
                "first_result_elapsed_s": round(time.time() - started, 3),
                "exe": _gh_exe(),
            },
        }
        cache_meta = _SEARCH_CACHE.set(cache_key, result)
        return attach_runtime_metadata(result, tool_name="search_github_repositories", capability_id="github.search", started=started, budget=budget, cache=cache_meta)
    except json.JSONDecodeError:
        gh_error = KnowledgeRadarError(
            "gh CLI returned non-json output",
            code=ErrorCode.PARSE_FAILED,
            platform="GitHub",
            retryable=True,
            metadata={"failure_code": "SCHEMA_CHANGED"},
        )
    except subprocess.TimeoutExpired:
        gh_error = KnowledgeRadarError(
            "gh CLI search timed out",
            code=ErrorCode.REQUEST_FAILED,
            platform="GitHub",
            retryable=True,
            metadata={"failure_code": "TIMEOUT"},
        )
    except KnowledgeRadarError as exc:
        gh_error = exc
    except Exception as exc:
        failure_code = _classify_error(str(exc))
        gh_error = KnowledgeRadarError(
            str(exc),
            code=ErrorCode.REQUEST_FAILED,
            platform="GitHub",
            retryable=failure_code not in {"LOGIN_REQUIRED", "DEPENDENCY_CONFLICT"},
            metadata={"failure_code": failure_code},
        )

    if gh_error is not None:
        rest_started = time.time()
        rest = _github_rest_search(query, limit=clean_limit)
        if rest.get("items"):
            items = list(rest["items"])
            policy.mark_success(breaker_key, "github_rest_fallback", {"query": query, "item_count": len(items)})
            result = {
                "query": query,
                "provider": "github",
                "items": items,
                "total": len(items),
                "fallback_used": True,
                "attempted_providers": ["github", "github_rest"],
                "metadata": {
                    "sidecar": "gh_cli",
                    "strategy": "github_rest_after_cli_error",
                    "elapsed_s": round(time.time() - started, 3),
                    "cli_elapsed_s": round(rest_started - started, 3),
                    "rest_elapsed_s": round(float(rest.get("elapsed_s") or 0), 3),
                    "attempted_queries_count": rest.get("attempted_queries_count", len(rest.get("attempted_queries") or [])),
                    "first_result_elapsed_s": round(time.time() - started, 3),
                    "exe": _gh_exe(),
                    "cli_failure_code": gh_error.metadata.get("failure_code", "UNKNOWN"),
                    "rest_attempted_queries": rest.get("attempted_queries") or [],
                    "rest_error": rest.get("error") or "",
                },
            }
            cache_meta = _SEARCH_CACHE.set(cache_key, result)
            return attach_runtime_metadata(result, tool_name="search_github_repositories", capability_id="github.search", started=started, budget=budget, cache=cache_meta)
        policy.mark_failure(
            breaker_key,
            "gh_cli_sidecar",
            str(gh_error),
            metadata={"query": query, "failure_code": gh_error.metadata.get("failure_code", "UNKNOWN"), "rest_error": rest.get("error") or ""},
            retryable=gh_error.retryable,
        )
        raise gh_error
