from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from collectors.platform import gh_cli_sidecar
from kr_core.errors import ErrorCode, KnowledgeRadarError


class _Response:
    status_code = 200

    def json(self):
        return {
            "items": [
                {
                    "full_name": "alibaba/spring-ai-alibaba",
                    "html_url": "https://github.com/alibaba/spring-ai-alibaba",
                    "description": "Agent and AI application framework",
                    "language": "Java",
                    "stargazers_count": 12345,
                    "updated_at": "2026-06-01T00:00:00Z",
                    "owner": {"login": "alibaba"},
                }
            ]
        }


class _MixedResponse:
    status_code = 200

    def json(self):
        return {
            "items": [
                {
                    "full_name": "sindresorhus/awesome",
                    "html_url": "https://github.com/sindresorhus/awesome",
                    "description": "Awesome list of resources",
                    "language": None,
                    "stargazers_count": 400000,
                    "updated_at": "2026-06-01T00:00:00Z",
                    "owner": {"login": "sindresorhus"},
                },
                {
                    "full_name": "assafelovic/gpt-researcher",
                    "html_url": "https://github.com/assafelovic/gpt-researcher",
                    "description": "Autonomous agent for deep research and report generation",
                    "language": "Python",
                    "stargazers_count": 24000,
                    "updated_at": "2026-06-01T00:00:00Z",
                    "owner": {"login": "assafelovic"},
                },
            ]
        }


class _QueryAwareResponse:
    status_code = 200

    def __init__(self, query: str):
        self.query = query

    def json(self):
        if "gpt-researcher" in self.query:
            return {
                "items": [
                    {
                        "full_name": "assafelovic/gpt-researcher",
                        "html_url": "https://github.com/assafelovic/gpt-researcher",
                        "description": "An autonomous agent that conducts deep research on any data using any LLM providers",
                        "language": "Python",
                        "stargazers_count": 27695,
                        "updated_at": "2026-06-01T00:00:00Z",
                        "owner": {"login": "assafelovic"},
                    }
                ]
            }
        return _MixedResponse().json()


@pytest.fixture(autouse=True)
def _disable_breaker(monkeypatch):
    class Policy:
        def is_open(self, _key):
            return {"open": False}

        def mark_success(self, *_args, **_kwargs):
            return None

        def mark_failure(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(gh_cli_sidecar, "get_degradation_policy", lambda: Policy())


def test_github_rest_fallback_runs_after_empty_gh_cli(monkeypatch) -> None:
    monkeypatch.setattr(
        gh_cli_sidecar,
        "_run_gh",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=json.dumps([]), stderr=""),
    )
    monkeypatch.setattr(gh_cli_sidecar.httpx, "get", lambda *_args, **_kwargs: _Response())

    result = gh_cli_sidecar.search_repositories("spring-ai-alibaba deepresearch deep research", limit=3)

    assert result["fallback_used"] is True
    assert result["attempted_providers"] == ["github", "github_rest"]
    assert result["items"][0]["url"] == "https://github.com/alibaba/spring-ai-alibaba"
    assert result["metadata"]["strategy"] == "github_rest_after_empty_cli"


def test_github_rest_fallback_runs_after_gh_cli_error(monkeypatch) -> None:
    def raise_login_required(*_args, **_kwargs):
        raise KnowledgeRadarError(
            "gh not logged in",
            code=ErrorCode.REQUEST_FAILED,
            platform="GitHub",
            retryable=False,
            metadata={"failure_code": "LOGIN_REQUIRED"},
        )

    monkeypatch.setattr(gh_cli_sidecar, "_run_gh", raise_login_required)
    monkeypatch.setattr(gh_cli_sidecar.httpx, "get", lambda *_args, **_kwargs: _Response())

    result = gh_cli_sidecar.search_repositories("spring-ai-alibaba deepresearch deep research", limit=3)

    assert result["fallback_used"] is True
    assert result["items"][0]["source_provider"] == "github_rest"
    assert result["metadata"]["cli_failure_code"] == "LOGIN_REQUIRED"
    assert result["metadata"]["strategy"] == "github_rest_after_cli_error"


def test_github_rest_fallback_runs_after_plain_gh_exception(monkeypatch) -> None:
    def raise_plain_auth_error(*_args, **_kwargs):
        raise RuntimeError("gh auth missing probe")

    monkeypatch.setattr(gh_cli_sidecar, "_run_gh", raise_plain_auth_error)
    monkeypatch.setattr(gh_cli_sidecar.httpx, "get", lambda *_args, **_kwargs: _Response())

    result = gh_cli_sidecar.search_repositories("spring-ai-alibaba deepresearch deep research", limit=3)

    assert result["fallback_used"] is True
    assert result["items"][0]["source_provider"] == "github_rest"
    assert result["metadata"]["cli_failure_code"] == "LOGIN_REQUIRED"
    assert result["metadata"]["strategy"] == "github_rest_after_cli_error"


def test_github_rest_reranks_relevant_repo_above_generic_awesome(monkeypatch) -> None:
    monkeypatch.setattr(
        gh_cli_sidecar,
        "_run_gh",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=json.dumps([]), stderr=""),
    )
    monkeypatch.setattr(gh_cli_sidecar.httpx, "get", lambda *_args, **_kwargs: _MixedResponse())

    result = gh_cli_sidecar.search_repositories("AI research report generator agent open source", limit=2)

    assert result["items"][0]["url"] == "https://github.com/assafelovic/gpt-researcher"
    assert result["items"][0]["relevance_score"] > result["items"][1]["relevance_score"]


def test_github_rest_aggregates_semantic_queries_before_ranking(monkeypatch) -> None:
    monkeypatch.setattr(
        gh_cli_sidecar,
        "_run_gh",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=json.dumps([]), stderr=""),
    )

    def fake_get(*_args, **kwargs):
        return _QueryAwareResponse(str((kwargs.get("params") or {}).get("q") or ""))

    monkeypatch.setattr(gh_cli_sidecar.httpx, "get", fake_get)

    result = gh_cli_sidecar.search_repositories("AI research report generator agent open source", limit=3)

    assert result["items"][0]["url"] == "https://github.com/assafelovic/gpt-researcher"
    assert any("gpt-researcher" in query for query in result["metadata"]["rest_attempted_queries"])
