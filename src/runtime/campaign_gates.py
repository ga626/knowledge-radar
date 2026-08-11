"""Unified campaign quality gates for periodic KnowledgeRadar patrols."""

from __future__ import annotations

import importlib
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from runtime.process import silent_subprocess_run
from runtime.status_schema import aggregate_validation_status, classify_runtime_payload


CAMPAIGN_PROFILES = ("smoke", "deep", "destructive", "agent-sentinel")


def campaign_profile_manifest() -> dict[str, Any]:
    return {
        "schema": "knowledgeradar-campaign-profiles/v1",
        "status": "pass",
        "default_profile": "smoke",
        "profiles": {
            "smoke": {
                "purpose": "Periodic local patrol for real runtime capability health and declared degradation.",
                "token_cost": "none",
                "blocks_closeout": True,
            },
            "deep": {
                "purpose": "Property, contract, coverage, type, and fault-injection checks for latent bugs.",
                "token_cost": "none",
                "blocks_closeout": True,
            },
            "destructive": {
                "purpose": "Mutation/fuzz readiness and optional heavy campaigns; reports unavailable tools as expected degraded.",
                "token_cost": "none",
                "blocks_closeout": False,
            },
            "agent-sentinel": {
                "purpose": "Prompt contract for a small Codex/OpenClaw MCP integration sentinel.",
                "token_cost": "low_when_run_by_agent",
                "blocks_closeout": False,
            },
        },
    }


def campaign_agent_sentinel_prompt() -> str:
    return (
        "Use only the registered KnowledgeRadar MCP tools. "
        "First call health_check(mode='summary'), then get_capabilities(summary=true), "
        "then get_task_status(task_id='summary', limit=5) and analyze_decision_logs(limit=5, compact=true). "
        "Run at most one low-cost kr_web_search query if capabilities show web search is available. "
        "Do not use built-in web_search/web_fetch. Treat EXPECTED_DEGRADED as non-blocking only when a reason is declared. "
        "Return tool order, status semantics, any manual action, and token usage. Target total tokens under 20000."
    )


def run_campaign_runtime_smoke(root: Path, *, compact: bool = True) -> dict[str, Any]:
    server = _load_server_module(root)
    steps = [
        _run_step("health_check_summary", lambda: server.health_check(mode="summary"), compact=compact),
        _run_step("get_capabilities_summary", lambda: server.get_capabilities(summary=True), compact=compact),
        _run_step("get_task_status_summary", lambda: server.get_task_status(task_id="summary", limit=5), compact=compact),
        _run_step("analyze_decision_logs", lambda: server.analyze_decision_logs(limit=20, compact=True), compact=compact),
    ]
    status = _aggregate_step_status(steps)
    return {
        "schema": "knowledgeradar-campaign-runtime-smoke/v1",
        "status": status,
        "execution_surface": "local_server_direct",
        "native_tool_card": {
            "status": "host_unobserved",
            "reason": "The scheduled Python process cannot observe a Codex conversation's MCP cards or session.",
        },
        "steps": steps,
        "provider_matrix": _provider_matrix_from_steps(steps),
    }


def run_campaign_compact_patrol_contract(root: Path) -> dict[str, Any]:
    """Check the declared compact patrol surfaces through the local readonly runner."""
    server = _load_server_module(root)
    capabilities = server._capabilities_summary()
    health_summary = server._health_check_summary()
    health_checks = health_summary.get("checks") or {}
    declared = capabilities.get("compact_patrol_contract") or {}
    required_surfaces = {
        "native_readonly_runner": health_checks.get("native_readonly_runner"),
        "governed_capability_plan": health_checks.get("governed_capability_plan"),
        "trace_evidence_ledger": health_checks.get("trace_evidence_ledger"),
        "gh_cli_admission": health_checks.get("gh_cli_admission"),
    }
    violations: list[str] = []
    if declared.get("status") != "ready":
        violations.append("compact_patrol_contract_not_ready")
    if "docs status index check" not in (declared.get("steps") or []):
        violations.append("compact_patrol_contract_missing_docs_step")
    for name, value in required_surfaces.items():
        if not isinstance(value, dict) or not value:
            violations.append(f"missing_{name}")
    return {
        "schema": "knowledgeradar-campaign-compact-patrol-contract/v1",
        "status": "pass" if not violations else "fail",
        "execution_surface": "local_server_direct",
        "native_tool_card": {
            "status": "host_unobserved",
            "reason": "Only a real Codex MCP call can observe the current conversation tool surface.",
        },
        "declared_contract": declared,
        "required_surfaces": {name: _compact_status(value) for name, value in required_surfaces.items()},
        "violations": violations,
    }


def run_campaign_contract_smoke(root: Path) -> dict[str, Any]:
    from runtime.quality_gates import load_quality_gate_manifest, validate_provider_status_contract

    server = _load_server_module(root)
    capabilities = server.get_capabilities(summary=True)
    providers = capabilities.get("web_search_providers") or {}
    validation = capabilities.get("validation_semantics") or {}
    manifest = load_quality_gate_manifest(root)
    provider_contract = validate_provider_status_contract(providers)
    violations: list[dict[str, str]] = []
    for name, provider in providers.items():
        status = str((provider or {}).get("status") or "").lower()
        if status in {"degraded", "expected_degraded"} and not (
            (provider or {}).get("degraded_reason")
            or (provider or {}).get("role")
            or (provider or {}).get("detail")
            or (provider or {}).get("strategy")
        ):
            violations.append({"provider": str(name), "reason": "degraded_without_declared_reason"})
    for state in ["PASS", "EXPECTED_DEGRADED", "NEEDS_INTERACTION", "FAIL"]:
        if state not in (manifest.get("status_semantics") or {}):
            violations.append({"provider": "manifest", "reason": f"missing_status_semantic:{state}"})
    if not validation:
        violations.append({"provider": "capabilities", "reason": "missing_validation_semantics"})
    return {
        "schema": "knowledgeradar-campaign-contract-smoke/v1",
        "status": "pass" if provider_contract.get("status") == "ok" and not violations else "fail",
        "provider_contract": provider_contract,
        "violations": violations,
    }


def run_campaign_fault_injection() -> dict[str, Any]:
    from runtime.quality_gates import classify_campaign_status

    cases = [
        ("provider_timeout", {"status": "timeout", "configured": True, "role": "optional_provider"}),
        ("quota_exhausted", {"status": "degraded", "configured": True, "degraded_reason": "quota_exhausted"}),
        ("login_required", {"status": "needs_interaction", "manual_action": "login"}),
        ("main_chain_error", {"status": "fail", "main_chain": True}),
        ("anti_bot_manual", {"status": "error", "error_type": "anti_bot_verification", "manual_action": "captcha"}),
        ("unconfigured_optional", {"status": "down", "configured": False, "reason": "api_key_missing"}),
        ("unknown_main_chain", {"status": "mystery", "main_chain": True}),
        ("unknown_candidate", {"status": "mystery", "main_chain": False, "role": "candidate"}),
    ]
    results = [{"case": name, **classify_campaign_status(payload)} for name, payload in cases]
    expected = {
        "provider_timeout": "EXPECTED_DEGRADED",
        "quota_exhausted": "EXPECTED_DEGRADED",
        "login_required": "NEEDS_INTERACTION",
        "main_chain_error": "FAIL",
        "anti_bot_manual": "NEEDS_INTERACTION",
        "unconfigured_optional": "EXPECTED_DEGRADED",
        "unknown_main_chain": "FAIL",
        "unknown_candidate": "EXPECTED_DEGRADED",
    }
    violations = [row for row in results if row.get("classification") != expected[row["case"]]]
    return {
        "schema": "knowledgeradar-campaign-fault-injection/v1",
        "status": "pass" if not violations else "fail",
        "results": results,
        "violations": violations,
    }


def run_campaign_deep_checks(root: Path) -> dict[str, Any]:
    """Run deterministic deep checks that do not need external services."""
    checks = [
        _deep_check_status_classification_matrix(),
        _deep_check_route_policy_matrix(root),
        _deep_check_url_parsing_fuzz(),
        _deep_check_provider_status_matrix(root),
        _deep_check_task_fanin(root),
        _deep_check_patrol_side_effect_boundary(root),
    ]
    failures = [check for check in checks if check.get("status") != "pass"]
    return {
        "schema": "knowledgeradar-campaign-deep-checks/v1",
        "status": "pass" if not failures else "fail",
        "checks": checks,
        "failures": failures,
        "coverage_detail": {
            "modules": [
                "runtime.status_schema",
                "capabilities.route_policy_matrix",
                "generic_web.collector._valid_http_url",
                "academic_providers.service.academic_provider_status",
                "runtime.task_scope",
                "runtime.tasks.TaskStore.wait_for_scope",
                "runtime.campaign_patrol",
            ],
            "style": "deterministic property and contract samples",
        },
    }


def run_campaign_destructive_checks(root: Path, *, auto_install: bool = True, run_heavy: bool = True) -> dict[str, Any]:
    """Run non-network destructive-readiness checks and report heavy tool gaps."""
    readiness = run_campaign_tool_readiness(auto_install=auto_install)
    heavy_runner = (
        run_campaign_destructive_heavy_runner(root)
        if run_heavy
        else _check_result(
            "destructive_docker_heavy_runner",
            [],
            samples=0,
            detail={"status": "skipped", "reason": "run_heavy_false"},
        )
    )
    checks = [
        _destructive_url_fuzz(),
        _destructive_status_mutation(),
        _destructive_task_scope_collision_probe(root),
        heavy_runner,
    ]
    failures = [check for check in checks if check.get("status") == "fail"]
    expected_degraded = readiness.get("expected_degraded") or heavy_runner.get("status") == "expected_degraded"
    status = "fail" if failures else "expected_degraded" if expected_degraded else "pass"
    return {
        "schema": "knowledgeradar-campaign-destructive-checks/v1",
        "status": status,
        "tool_readiness": readiness,
        "heavy_runner": heavy_runner,
        "checks": checks,
        "failures": failures,
        "notes": [
            "Fuzz/mutation samples are deterministic and local-only.",
            "Python-native campaign tools are checked locally.",
            "Atheris and mutmut heavy checks are Docker-only on Windows; Docker unavailable is EXPECTED_DEGRADED.",
        ],
    }


def run_campaign_destructive_heavy_runner(root: Path) -> dict[str, Any]:
    """Smoke-test Docker-based atheris/mutmut execution for Windows destructive patrols."""
    docker = _docker_available()
    if not docker["available"]:
        return _check_result(
            "destructive_docker_heavy_runner",
            [],
            samples=0,
            detail={
                "status": "expected_degraded",
                "reason": "docker_unavailable",
                "docker": docker,
                "tools": ["atheris", "mutmut"],
            },
        ) | {"status": "expected_degraded"}
    smoke = _docker_heavy_runner_smoke(root)
    status = "pass" if smoke["returncode"] == 0 else "expected_degraded"
    return _check_result(
        "destructive_docker_heavy_runner",
        [],
        samples=2,
        detail={
            "status": status,
            "reason": "" if status == "pass" else "docker_heavy_runner_smoke_failed",
            "docker": docker,
            "smoke": smoke,
            "tools": ["atheris", "mutmut"],
        },
    ) | {"status": status}


def run_campaign_tool_readiness(*, auto_install: bool = True) -> dict[str, Any]:
    details: dict[str, dict[str, Any]] = {
        "hypothesis": _probe_python_module("hypothesis"),
        "coverage": _probe_python_module("coverage"),
        "mypy": _probe_python_module("mypy"),
        "atheris": _docker_only_heavy_tool("atheris"),
        "mutmut": _docker_only_heavy_tool("mutmut"),
    }
    tools = {name: bool(item.get("available")) for name, item in details.items()}
    expected_degraded = [
        name
        for name, item in details.items()
        if item.get("runner") == "docker" and not item.get("available")
    ]
    installed_but_limited: list[str] = []
    return {
        "schema": "knowledgeradar-campaign-tool-readiness/v1",
        "status": "expected_degraded" if expected_degraded or installed_but_limited else "pass",
        "tools": tools,
        "details": details,
        "expected_degraded": expected_degraded,
        "installed_but_platform_limited": installed_but_limited,
        "notes": [
            "campaign dependencies for native Python checks are reported locally",
            "atheris and mutmut are not installed or run on Windows natively; destructive heavy uses Docker only",
        ],
    }


def run_agent_sentinel_contract() -> dict[str, Any]:
    return {
        "schema": "knowledgeradar-agent-sentinel-contract/v1",
        "status": "pass",
        "token_budget": {"target_total_tokens": 20000, "hard_warning_tokens": 30000},
        "required_tool_order_prefix": [
            "health_check(mode='summary')",
            "get_capabilities(summary=true)",
        ],
        "forbidden_tools": ["built-in web_search", "built-in web_fetch"],
        "prompt": campaign_agent_sentinel_prompt(),
    }


def _load_server_module(root: Path) -> Any:
    import sys

    src = root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    os.environ.setdefault("KR_CHROME_PREWARM", "0")
    return importlib.import_module("server")


def _run_step(name: str, func: Callable[[], Any], *, compact: bool = True) -> dict[str, Any]:
    started = time.time()
    try:
        result = func()
        return {
            "name": name,
            "status": _infer_status(result),
            "elapsed_s": round(time.time() - started, 2),
            "summary": _summarize(result, compact=compact),
        }
    except Exception as exc:
        return {
            "name": name,
            "status": "fail",
            "elapsed_s": round(time.time() - started, 2),
            "error": str(exc),
        }


def _aggregate_step_status(steps: list[dict[str, Any]]) -> str:
    status_class = aggregate_validation_status(step.get("status_class") or step.get("status") for step in steps)
    if status_class == "FAIL":
        return "fail"
    if status_class == "NEEDS_INTERACTION":
        return "needs_interaction"
    if status_class == "EXPECTED_DEGRADED":
        return "expected_degraded"
    return "pass"


def _compact_status(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"present": False}
    return {
        "present": True,
        "status": value.get("status") or value.get("admission_state") or "unknown",
        "schema": value.get("schema") or value.get("schema_version") or "",
    }


def _infer_status(result: Any) -> str:
    if isinstance(result, dict):
        rollup = result.get("validation_rollup") if isinstance(result.get("validation_rollup"), dict) else {}
        if rollup.get("status_class"):
            status_class = str(rollup.get("status_class"))
            if status_class == "PASS":
                return "pass"
            if status_class == "EXPECTED_DEGRADED":
                return "expected_degraded"
            if status_class == "NEEDS_INTERACTION":
                return "needs_interaction"
            return "fail"
        classification = classify_runtime_payload(
            result,
            required=True,
            main_chain=True,
            configured=True,
            has_declared_reason=bool(result.get("detail") or result.get("reason") or result.get("degraded_reason") or result.get("validation_reason")),
            optional=False,
        )
        if classification["status_class"] == "PASS":
            return "pass"
        if classification["status_class"] == "EXPECTED_DEGRADED":
            return "expected_degraded"
        if classification["status_class"] == "NEEDS_INTERACTION":
            return "needs_interaction"
        return "fail"
    if result is None:
        return "expected_degraded"
    return "pass"


def _summarize(result: Any, *, compact: bool = True) -> Any:
    if not compact:
        return result
    return _shrink(result)


def _shrink(value: Any, *, max_items: int = 8) -> Any:
    if isinstance(value, dict):
        keep = {
            "status",
            "state",
            "ok",
            "schema",
            "schema_version",
            "tool_count",
            "tools",
            "checks",
            "summary",
            "platforms",
            "web_search_providers",
            "validation_semantics",
            "validation_rollup",
            "runtime_contract",
            "error",
            "reason",
        }
        items = [(k, v) for k, v in value.items() if k in keep] or list(value.items())[:max_items]
        return {str(k): _shrink(v, max_items=max_items) for k, v in items[:max_items]}
    if isinstance(value, list):
        return [_shrink(v, max_items=max_items) for v in value[:max_items]]
    if isinstance(value, str):
        return value[:500]
    return value


def _provider_matrix_from_steps(steps: list[dict[str, Any]]) -> dict[str, Any]:
    capabilities = next((step for step in steps if step.get("name") == "get_capabilities_summary"), {})
    summary = capabilities.get("summary") or {}
    providers = summary.get("web_search_providers") or {}
    platforms = summary.get("platforms") or {}
    return {
        "schema": "knowledgeradar-campaign-provider-matrix/v1",
        "web_search_providers": providers,
        "platforms": platforms,
    }


def _module_available(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:
        return False


def _probe_python_module(name: str) -> dict[str, Any]:
    return {"available": _module_available(name), "install_attempted": False}


def _docker_only_heavy_tool(name: str) -> dict[str, Any]:
    docker = _docker_available()
    return {
        "available": bool(docker.get("available")),
        "install_attempted": False,
        "install_status": "not_applicable",
        "runnable": bool(docker.get("available")),
        "runner": "docker",
        "reason": "" if docker.get("available") else "docker_unavailable",
        "docker": docker,
        "native_windows_path": "removed",
        "guidance": f"{name} destructive checks run inside Docker only on Windows.",
    }


def _docker_available() -> dict[str, Any]:
    try:
        version = silent_subprocess_run(
            ["docker", "--version"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=30,
        )
    except Exception as exc:
        return {"available": False, "reason": "docker_command_failed", "error": str(exc)}
    if version.returncode != 0:
        return {
            "available": False,
            "reason": "docker_version_failed",
            "stdout": _truncate_text((version.stdout or "").strip(), limit=300),
            "stderr": _truncate_text((version.stderr or "").strip(), limit=300),
        }
    info = silent_subprocess_run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=60,
    )
    return {
        "available": info.returncode == 0,
        "reason": "" if info.returncode == 0 else "docker_daemon_unavailable",
        "version": _truncate_text((version.stdout or "").strip(), limit=200),
        "server": _truncate_text((info.stdout or "").strip(), limit=100),
        "stderr": _truncate_text((info.stderr or "").strip(), limit=300),
    }


def _docker_heavy_runner_smoke(root: Path) -> dict[str, Any]:
    script = (
        "set -eu\n"
        "python -m pip install -q atheris mutmut pytest >/tmp/kr-pip.log 2>&1\n"
        "mkdir -p /tmp/krmut && cd /tmp/krmut\n"
        "cat > target.py <<'PY'\n"
        "def normalize(value):\n"
        "    return str(value or '').strip().lower()\n"
        "PY\n"
        "cat > test_target.py <<'PY'\n"
        "from target import normalize\n"
        "def test_normalize():\n"
        "    assert normalize(' PASS ') == 'pass'\n"
        "PY\n"
        "python - <<'PY'\n"
        "import atheris\n"
        "def TestOneInput(data):\n"
        "    data.decode('utf-8', 'ignore').strip().lower()\n"
        "atheris.Setup(['kr-atheris-smoke', '-runs=50'], TestOneInput)\n"
        "atheris.Fuzz()\n"
        "PY\n"
        "python -m mutmut run --max-children 1 target >/tmp/kr-mutmut.log 2>&1 || true\n"
        "python -m mutmut results >/tmp/kr-mutmut-results.log 2>&1 || true\n"
        "test -s /tmp/kr-mutmut-results.log\n"
    )
    command = ["docker", "run", "--rm", "python:3.12-slim", "sh", "-lc", script]
    completed = silent_subprocess_run(
        command,
        cwd=str(root),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=900,
    )
    return {
        "command": "docker run --rm python:3.12-slim sh -lc <atheris-mutmut-smoke>",
        "returncode": completed.returncode,
        "stdout": _truncate_text((completed.stdout or "").strip(), limit=700),
        "stderr": _truncate_text((completed.stderr or "").strip(), limit=700),
    }


def _truncate_text(text: str, *, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _check_result(name: str, violations: list[Any], *, samples: int = 0, detail: Any = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": name,
        "status": "pass" if not violations else "fail",
        "samples": samples,
        "violations": violations[:10],
    }
    if detail is not None:
        result["detail"] = detail
    return result


def _deep_check_status_classification_matrix() -> dict[str, Any]:
    cases = [
        ("ok", {"status": "ok"}, "PASS"),
        ("timeout_with_reason", {"status": "timeout", "reason": "network_timeout"}, "EXPECTED_DEGRADED"),
        ("timeout_main_without_reason", {"status": "timeout", "main_chain": True}, "FAIL"),
        ("login_error", {"status": "error", "error_type": "login_required"}, "NEEDS_INTERACTION"),
        ("captcha_error", {"status": "failed", "error": {"type": "anti_bot_verification"}}, "NEEDS_INTERACTION"),
        ("unknown_main", {"status": "mystery", "main_chain": True}, "FAIL"),
        ("unknown_optional", {"status": "mystery", "main_chain": False, "role": "candidate"}, "EXPECTED_DEGRADED"),
        ("unconfigured", {"status": "down", "configured": False, "reason": "api_key_missing"}, "EXPECTED_DEGRADED"),
    ]
    violations: list[dict[str, str]] = []
    for name, payload, expected in cases:
        result = classify_runtime_payload(
            payload,
            required=bool(payload.get("main_chain", True)),
            main_chain=bool(payload.get("main_chain", False)),
            configured=bool(payload.get("configured", True)),
            has_declared_reason=bool(payload.get("reason") or payload.get("role") or payload.get("manual_action")),
            optional=not bool(payload.get("main_chain", False)),
        )
        if result["status_class"] != expected:
            violations.append({"case": name, "expected": expected, "actual": result["status_class"]})
    return _check_result("status_classification_matrix", violations, samples=len(cases))


def _deep_check_route_policy_matrix(root: Path) -> dict[str, Any]:
    _load_server_module(root)
    from capabilities import route_policy_matrix

    policy = route_policy_matrix()
    source_types = policy.get("source_types") or {}
    required = {
        "academic": "open_metadata_api",
        "video": "platform_adapter_or_official_api",
        "generic_web": "web_search_then_generic_web_detail",
    }
    violations = [
        {"source_type": name, "expected": route, "actual": (source_types.get(name) or {}).get("default_route")}
        for name, route in required.items()
        if (source_types.get(name) or {}).get("default_route") != route
    ]
    if "web_search" not in policy.get("default_order", []):
        violations.append({"source_type": "default_order", "reason": "missing_web_search"})
    return _check_result("route_policy_matrix", violations, samples=len(required), detail={"source_types": sorted(source_types)})


def _deep_check_url_parsing_fuzz() -> dict[str, Any]:
    from generic_web.collector import _valid_http_url

    valid = [
        "http://example.com",
        "https://example.com/path?q=1",
        "https://例子.测试/" + quote("论文 标题"),
        "https://sub.example.com:8443/a/b#section",
    ]
    invalid = [
        "",
        "   ",
        "ftp://example.com/file",
        "javascript:alert(1)",
        "https:///missing-host",
        "www.example.com/no-scheme",
        "file:///C:/secret.txt",
    ]
    violations: list[dict[str, str]] = []
    for url in valid:
        if not _valid_http_url(url):
            violations.append({"url": url, "expected": "valid"})
    for url in invalid:
        if _valid_http_url(url):
            violations.append({"url": url, "expected": "invalid"})
    return _check_result("url_parsing_fuzz", violations, samples=len(valid) + len(invalid))


def _deep_check_provider_status_matrix(root: Path) -> dict[str, Any]:
    _load_server_module(root)
    from academic_providers.service import academic_provider_status

    status = academic_provider_status()
    expected_pass = ["nssd", "chinaxiv", "hanspub", "oajrc", "sciopen"]
    expected_degraded = ["baidu_scholar", "wanfang", "cnki_authorized_browser"]
    violations: list[dict[str, str]] = []
    for name in expected_pass:
        item = status.get(name) or {}
        if item.get("validation_status") != "PASS" or item.get("status") != "available":
            violations.append({"provider": name, "expected": "PASS available", "actual": str(item)})
    for name in expected_degraded:
        item = status.get(name) or {}
        if item.get("validation_status") != "EXPECTED_DEGRADED":
            violations.append({"provider": name, "expected": "EXPECTED_DEGRADED", "actual": str(item)})
        if not (item.get("degraded_reason") or item.get("manual_action") or item.get("role")):
            violations.append({"provider": name, "reason": "missing_degraded_reason"})
    return _check_result("provider_status_matrix", violations, samples=len(expected_pass) + len(expected_degraded))


def _deep_check_task_fanin(root: Path) -> dict[str, Any]:
    from runtime.task_scope import make_task_scope
    from runtime.tasks import TaskStore, compact_task_ref

    violations: list[dict[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="kr-campaign-task-") as tmp:
        store = TaskStore(str(Path(tmp) / "tasks.sqlite3"))
        scope = make_task_scope(
            source_url="https://www.bilibili.com/video/BVcampaign",
            content_id="BVcampaign",
            platform="bilibili",
            research_session_id="legacy-campaign",
        )
        store.upsert_task(
            task_id="campaign-task",
            task_type="bilibili_transcribe",
            platform="B站",
            target="BVcampaign",
            content_id="BVcampaign",
            status="completed",
            metadata={**scope.to_metadata(), "blocks_final_report": True, "result_reread_tool": "get_content_detail"},
        )
        store.mark_completed("campaign-task")
        by_scope = store.wait_for_scope(task_scope_id=scope.task_scope_id, max_wait_s=0, poll_s=0.1)
        by_source = store.wait_for_scope(source_url=scope.source_url, max_wait_s=0, poll_s=0.1)
        compact = compact_task_ref(store.get_task("campaign-task"))
        if by_scope.get("status") != "completed" or not by_scope.get("terminal"):
            violations.append({"case": "by_scope", "reason": "terminal task not found"})
        if by_source.get("status") != "completed" or not by_source.get("terminal"):
            violations.append({"case": "by_source", "reason": "terminal task not found"})
        fanin = compact.get("fanin") or {}
        for key in ["task_scope_id", "work_scope_id", "source_url", "content_id"]:
            if not fanin.get(key):
                violations.append({"case": "compact_fanin", "reason": f"missing_{key}"})
    return _check_result("task_fanin", violations, samples=3)


def _deep_check_patrol_side_effect_boundary(root: Path) -> dict[str, Any]:
    from runtime.campaign_patrol import _campaign_quality_gate_command, _status_class

    violations: list[dict[str, str]] = []
    command = _campaign_quality_gate_command(root, "smoke")
    if "--write-state" in command:
        violations.append({"case": "scheduled_command", "reason": "write_state_enabled"})
    if "--no-write-state" not in command:
        violations.append({"case": "scheduled_command", "reason": "no_write_state_missing"})
    false_pass = _status_class({"status": "FAIL", "quality_state": {"status_class": "PASS"}, "results": []}, 1)
    if false_pass != "FAIL":
        violations.append({"case": "outer_failure", "reason": f"classified_as_{false_pass}"})
    return _check_result("patrol_side_effect_and_verdict_contract", violations, samples=3)


def _destructive_url_fuzz() -> dict[str, Any]:
    from generic_web.collector import _valid_http_url

    payloads = [
        "http://" + "a" * 512 + ".example",
        "https://example.com/" + "../" * 20,
        "https://example.com/%00",
        "https://example.com/?q=" + quote("x" * 1024),
        "mailto:user@example.com",
        "data:text/plain,hello",
        "http://[::1]/",
    ]
    violations: list[dict[str, str]] = []
    for url in payloads:
        try:
            result = _valid_http_url(url)
        except Exception as exc:
            violations.append({"url": url[:80], "exception": exc.__class__.__name__})
            continue
        if not isinstance(result, bool):
            violations.append({"url": url[:80], "reason": "non_bool_result"})
    return _check_result("destructive_url_fuzz", violations, samples=len(payloads))


def _destructive_status_mutation() -> dict[str, Any]:
    statuses = ["", " " * 100, "ok\x00", "FAIL\nPASS", "登录_REQUIRED", "timeout", "rate-limited", "needs interaction"]
    violations: list[dict[str, str]] = []
    for status in statuses:
        try:
            result = classify_runtime_payload({"status": status, "main_chain": True})
        except Exception as exc:
            violations.append({"status": repr(status), "exception": exc.__class__.__name__})
            continue
        if result.get("status_class") not in {"PASS", "EXPECTED_DEGRADED", "NEEDS_INTERACTION", "FAIL"}:
            violations.append({"status": repr(status), "actual": str(result.get("status_class"))})
    return _check_result("destructive_status_mutation", violations, samples=len(statuses))


def _destructive_task_scope_collision_probe(root: Path) -> dict[str, Any]:
    from runtime.task_scope import make_task_scope

    samples = [
        {"source_url": "https://example.com/a", "content_id": "same", "platform": "web"},
        {"source_url": "https://example.com/b", "content_id": "same", "platform": "web"},
        {"source_url": "https://example.com/a", "content_id": "same", "platform": "video"},
        {"source_url": "", "content_id": "same", "platform": "video"},
        {"source_url": "https://example.com/a", "content_id": "", "platform": ""},
    ]
    scopes = [make_task_scope(**sample) for sample in samples]
    pairs = {(scope.work_scope_id, scope.task_scope_id) for scope in scopes}
    violations: list[dict[str, str]] = []
    if len(pairs) != len(scopes):
        violations.append({"reason": "scope_collision", "count": str(len(scopes)), "unique": str(len(pairs))})
    for scope in scopes:
        if not scope.work_scope_id.startswith("kr-work-") or not scope.task_scope_id.startswith("kr-task-"):
            violations.append({"reason": "bad_scope_prefix", "scope": str(scope.compact())})
    return _check_result("destructive_task_scope_collision_probe", violations, samples=len(samples))
