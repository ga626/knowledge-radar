"""Quality gate helpers for closeout and drift checks."""

from __future__ import annotations

import json
from pathlib import Path
from runtime.process import silent_subprocess_run
import subprocess
import sys
from typing import Any

from runtime.project_state import project_root, project_state_freshness
from runtime.status_schema import classify_runtime_payload


MANIFEST_PATH = "config/quality-gates.manifest.json"


def load_quality_gate_manifest(root: Path | None = None) -> dict[str, Any]:
    repo = root or project_root()
    path = repo / MANIFEST_PATH
    return json.loads(path.read_text(encoding="utf-8"))


def git_changed_paths(root: Path | None = None, *, include_untracked: bool = True) -> list[str]:
    repo = root or project_root()
    commands = [["git", "diff", "--name-only", "HEAD"]]
    if include_untracked:
        commands.append(["git", "ls-files", "--others", "--exclude-standard"])
    paths: list[str] = []
    for command in commands:
        proc = silent_subprocess_run(
            command,
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        for line in proc.stdout.splitlines():
            item = line.strip().replace("\\", "/")
            if item and item not in paths:
                paths.append(item)
    return paths


def scan_forbidden_source_outputs(root: Path | None = None) -> dict[str, Any]:
    repo = root or project_root()
    manifest = load_quality_gate_manifest(repo)
    globs = manifest.get("runtime_boundaries", {}).get("forbidden_source_output_globs", [])
    hits: list[str] = []
    for pattern in globs:
        for path in repo.glob(pattern):
            if path.is_file():
                hits.append(path.relative_to(repo).as_posix())
    return {
        "schema": "knowledgeradar-runtime-boundary-scan/v1",
        "status": "ok" if not hits else "fail",
        "forbidden_globs": globs,
        "hits": sorted(set(hits)),
    }


def validate_provider_status_contract(status_by_provider: dict[str, Any]) -> dict[str, Any]:
    violations: list[dict[str, str]] = []
    for name, status in (status_by_provider or {}).items():
        if not isinstance(status, dict):
            violations.append({"provider": str(name), "reason": "status_not_object"})
            continue
        if "available" not in status and "configured" not in status:
            violations.append({"provider": str(name), "reason": "missing_available_or_configured"})
        if status.get("status") in {"EXPECTED_DEGRADED", "degraded"} and not (
            status.get("notes")
            or status.get("reason")
            or status.get("role")
            or status.get("detail")
            or status.get("strategy")
            or status.get("degraded_reason")
        ):
            violations.append({"provider": str(name), "reason": "degraded_without_reason"})
    return {
        "schema": "knowledgeradar-provider-status-contract/v1",
        "status": "ok" if not violations else "fail",
        "violations": violations,
    }


def classify_campaign_status(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload or {}
    classification = classify_runtime_payload(
        data,
        required=bool(data.get("main_chain", False)),
        main_chain=bool(data.get("main_chain", False)),
        configured=bool(data.get("configured", True)),
        has_declared_reason=bool(
            data.get("reason")
            or data.get("degraded_reason")
            or data.get("role")
            or data.get("manual_action")
            or data.get("detail")
        ),
        optional=not bool(data.get("main_chain", False)) or not bool(data.get("configured", True)),
    )
    return {
        "schema": "knowledgeradar-campaign-status-classification/v1",
        **classification,
    }


def gate_overview(root: Path | None = None) -> dict[str, Any]:
    repo = root or project_root()
    from runtime.quality_state import quality_state_freshness

    return {
        "schema": "knowledgeradar-quality-gate-overview/v1",
        "manifest": {
            "path": MANIFEST_PATH,
            "content": load_quality_gate_manifest(repo),
        },
        "project_state_freshness": project_state_freshness(repo),
        "quality_state_freshness": quality_state_freshness(repo),
        "runtime_boundaries": scan_forbidden_source_outputs(repo),
        "changed_paths": git_changed_paths(repo),
    }


def run_command(command: list[str], root: Path | None = None, *, timeout: int = 120) -> dict[str, Any]:
    repo = root or project_root()
    try:
        proc = silent_subprocess_run(
            command,
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return {
            "command": " ".join(command),
            "returncode": 124,
            "status": "fail",
            "failure_type": "timeout",
            "timeout_s": timeout,
            "stdout": str(stdout).strip()[-3000:],
            "stderr": str(stderr).strip()[-3000:],
        }
    return {
        "command": " ".join(command),
        "returncode": proc.returncode,
        "status": "pass" if proc.returncode == 0 else "fail",
        "stdout": (proc.stdout or "").strip()[-3000:],
        "stderr": (proc.stderr or "").strip()[-3000:],
    }


def python_command(*args: str) -> list[str]:
    bundled = project_root() / ".python312" / "python.exe"
    executable = str(bundled) if bundled.exists() else sys.executable
    return [executable, *args]
