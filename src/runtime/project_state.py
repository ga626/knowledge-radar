"""Sanitized project state packets for AI handoff and governance."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from runtime.process import silent_subprocess_run
from typing import Any

from runtime.status_schema import validation_status_classes


PROJECT_STATUS_DIR_NAME = "project_status"
PROJECT_STATE_FILE = "KnowledgeRadar-Project-State.md"
VALIDATION_SEMANTICS_FILE = "KnowledgeRadar-Validation-Semantics.md"
CODEX_HANDOFF_FILE = "KnowledgeRadar-Codex-Handoff.md"
_STATUS_FIELD_RE = re.compile(r"^- ([a-zA-Z0-9_]+): `([^`]*)`$", re.MULTILINE)


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def status_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / PROJECT_STATUS_DIR_NAME


def redact_path_text(text: str, root: Path | None = None) -> str:
    """Redact known local absolute paths while preserving useful path roles."""
    if not text:
        return text
    repo = root or project_root()
    replacements = {
        str(repo): "{KR_ROOT}",
        str(repo).replace("\\", "/"): "{KR_ROOT}",
        "D:\\AI Studio\\KnowledgeRadar": "{LEGACY_KR_ROOT}",
        "D:/AI Studio/KnowledgeRadar": "{LEGACY_KR_ROOT}",
        "C:\\kr-profiles": "{KR_PROFILE_ROOT}",
        "C:/kr-profiles": "{KR_PROFILE_ROOT}",
        str(Path.home()): "{USER_HOME}",
        str(Path.home()).replace("\\", "/"): "{USER_HOME}",
    }
    redacted = str(text)
    for needle, replacement in replacements.items():
        if needle:
            redacted = redacted.replace(needle, replacement)
    return redacted


def _run_git(args: list[str], root: Path, *, limit: int = 4000) -> str:
    try:
        proc = silent_subprocess_run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except Exception as exc:
        return f"git unavailable: {exc}"
    output = (proc.stdout or proc.stderr or "").strip()
    return output[:limit]


def _split_lines(value: str, limit: int = 80) -> list[str]:
    return [line for line in value.splitlines() if line.strip()][:limit]


def _git_status_lines(root: Path) -> list[str]:
    return _split_lines(_run_git(["status", "--short"], root), limit=200)


def _git_changed_paths_for_state(root: Path) -> list[str]:
    paths: list[str] = []
    for args in (["diff", "--name-only", "HEAD"], ["ls-files", "--others", "--exclude-standard"]):
        for line in _run_git(args, root).splitlines():
            path = line.strip().replace("\\", "/")
            if path and path not in paths:
                paths.append(path)
    return paths


def _status_path(line: str) -> str:
    text = line.rstrip()
    if len(text) >= 3 and text[2] == " ":
        text = text[3:]
    else:
        text = text.strip()
        if len(text) > 3:
            text = text[3:]
    return text.strip().strip('"').replace("\\", "/")


def _is_project_status_path(line: str) -> bool:
    path = _status_path(line)
    return path.startswith(f"{PROJECT_STATUS_DIR_NAME}/") or path in {".coverage", "coverage.xml"} or path.startswith("htmlcov/")


def _is_project_status_file_path(path: str) -> bool:
    item = path.strip().replace("\\", "/")
    return item.startswith(f"{PROJECT_STATUS_DIR_NAME}/") or item in {".coverage", "coverage.xml"} or item.startswith("htmlcov/")


def _head_diff_paths(root: Path, old_head: str, new_head: str) -> list[str]:
    if not old_head or not new_head or old_head == new_head:
        return []
    paths: list[str] = []
    for line in _run_git(["diff", "--name-only", old_head, new_head], root).splitlines():
        path = line.strip().replace("\\", "/")
        if path and path not in paths:
            paths.append(path)
    return paths


def _head_change_is_status_only(root: Path, old_head: str, new_head: str) -> tuple[bool, list[str]]:
    paths = _head_diff_paths(root, old_head, new_head)
    return bool(paths) and all(_is_project_status_file_path(path) for path in paths), paths


def _recorded_dirty_paths_are_head_diff(root: Path, fields: dict[str, str], old_head: str, new_head: str) -> tuple[bool, list[str], list[str]]:
    """Accept a just-committed pre-commit snapshot.

    The pre-commit hook records the staged non-status dirty paths before the
    commit exists. Immediately after commit, HEAD contains exactly those paths
    plus project_status files. Treat that as fresh so project_status commits do
    not recurse forever.
    """
    raw_paths = fields.get("changed_paths_excluding_project_status", "")
    if not raw_paths or not old_head or not new_head or old_head == new_head:
        return False, [], []
    recorded_paths = [item.strip() for item in raw_paths.split(",") if item.strip()]
    if not recorded_paths:
        return False, [], []
    head_paths = _head_diff_paths(root, old_head, new_head)
    non_status_head_paths = [path for path in head_paths if not _is_project_status_file_path(path)]
    ok = bool(head_paths) and sorted(recorded_paths) == sorted(non_status_head_paths)
    return ok, recorded_paths, head_paths


def collect_project_state(root: Path | None = None) -> dict[str, Any]:
    repo = root or project_root()
    status = _git_status_lines(repo)
    changed_paths = _git_changed_paths_for_state(repo)
    changed_paths_excluding_project_status = [path for path in changed_paths if not _is_project_status_file_path(path)]
    recent = _split_lines(_run_git(["log", "--oneline", "-8"], repo), limit=8)
    head = _run_git(["rev-parse", "--short", "HEAD"], repo, limit=80)
    return {
        "schema": "knowledgeradar-project-state/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_root": "{KR_ROOT}",
        "source_of_truth": "{KR_ROOT}",
        "rules_entrypoint": "AGENTS.md",
        "expanded_agent_guide": "docs/AI_WORKING_GUIDE.md",
        "structure_manifest": "config/project-structure.manifest.json",
        "status_dir": PROJECT_STATUS_DIR_NAME,
        "git": {
            "head": head,
            "dirty_count": len(status),
            "dirty_count_excluding_project_status": len(changed_paths_excluding_project_status),
            "changed_paths_excluding_project_status": changed_paths_excluding_project_status,
            "freshness_ignores": [f"{PROJECT_STATUS_DIR_NAME}/**", ".coverage", "coverage.xml", "htmlcov/"],
            "dirty_files": [redact_path_text(line, repo) for line in status],
            "recent_commits": [redact_path_text(line, repo) for line in recent],
        },
        "status_files": [
            f"{PROJECT_STATUS_DIR_NAME}/{PROJECT_STATE_FILE}",
            f"{PROJECT_STATUS_DIR_NAME}/{VALIDATION_SEMANTICS_FILE}",
            f"{PROJECT_STATUS_DIR_NAME}/{CODEX_HANDOFF_FILE}",
            f"{PROJECT_STATUS_DIR_NAME}/Quality-Gate-State.json",
        ],
        "validation_status_classes": validation_status_classes(),
        "known_design_boundaries": [
            "Bilibili provider-side native video_url is blocked by CDN anti-hotlinking; KR uses subtitle/ASR/sample-frame fallback paths.",
            "Windows is the first supported platform; non-Windows is degraded/future support.",
            "Optional providers and quota-limited providers may be EXPECTED_DEGRADED instead of FAIL.",
        ],
    }


def render_project_state_markdown(state: dict[str, Any]) -> str:
    dirty = state["git"]["dirty_files"] or ["clean"]
    commits = state["git"]["recent_commits"] or ["unavailable"]
    lines = [
        "# KnowledgeRadar Project State",
        "",
        f"- schema: `{state['schema']}`",
        f"- generated_at: `{state['generated_at']}`",
        f"- source_of_truth: `{state['source_of_truth']}`",
        f"- rules_entrypoint: `{state['rules_entrypoint']}`",
        f"- structure_manifest: `{state['structure_manifest']}`",
        f"- git_head: `{state['git']['head']}`",
        f"- dirty_count: `{state['git']['dirty_count']}`",
        f"- dirty_count_excluding_project_status: `{state['git'].get('dirty_count_excluding_project_status', state['git']['dirty_count'])}`",
        f"- changed_paths_excluding_project_status: `{', '.join(state['git'].get('changed_paths_excluding_project_status', []))}`",
        f"- freshness_ignores: `{', '.join(state['git'].get('freshness_ignores', []))}`",
        "",
        "## Dirty Files",
        "",
        *[f"- `{item}`" for item in dirty],
        "",
        "## Recent Commits",
        "",
        *[f"- `{item}`" for item in commits],
        "",
        "## Design Boundaries",
        "",
        *[f"- {item}" for item in state["known_design_boundaries"]],
        "",
    ]
    return "\n".join(lines)


def read_recorded_project_state(root: Path | None = None) -> dict[str, Any]:
    repo = root or project_root()
    path = status_dir(repo) / PROJECT_STATE_FILE
    if not path.is_file():
        return {
            "schema": "knowledgeradar-recorded-project-state/v1",
            "status": "missing",
            "path": str(path),
            "fields": {},
        }
    text = path.read_text(encoding="utf-8", errors="replace")
    fields = {match.group(1): match.group(2) for match in _STATUS_FIELD_RE.finditer(text)}
    return {
        "schema": "knowledgeradar-recorded-project-state/v1",
        "status": "ok",
        "path": str(path),
        "fields": fields,
    }


def _safe_int(value: Any, default: int = -1) -> int:
    try:
        return int(str(value))
    except Exception:
        return default


def project_state_freshness(root: Path | None = None) -> dict[str, Any]:
    """Compare the recorded handoff state against the current closeout state.

    `project_status/**` is ignored for dirty-count freshness because refreshing
    the snapshot necessarily changes those files during an uncommitted closeout.
    """
    repo = root or project_root()
    current = collect_project_state(repo)
    recorded = read_recorded_project_state(repo)
    current_git = current["git"]
    fields = recorded.get("fields", {})
    recorded_head = fields.get("git_head", "")
    recorded_dirty = _safe_int(
        fields.get("dirty_count_excluding_project_status", fields.get("dirty_count")),
        default=-1,
    )
    current_dirty = int(current_git.get("dirty_count_excluding_project_status", current_git.get("dirty_count", 0)))
    head_matches = bool(recorded_head) and recorded_head == current_git.get("head")
    raw_status_only_head_change, head_diff_paths = _head_change_is_status_only(repo, recorded_head, str(current_git.get("head") or ""))
    precommit_snapshot_matches, precommit_recorded_paths, precommit_head_diff_paths = _recorded_dirty_paths_are_head_diff(
        repo,
        fields,
        recorded_head,
        str(current_git.get("head") or ""),
    )
    dirty_matches = recorded_dirty == current_dirty
    committed_status_snapshot_matches = raw_status_only_head_change and dirty_matches and current_dirty == 0
    status_only_head_change = raw_status_only_head_change and not committed_status_snapshot_matches
    head_effectively_matches = head_matches or committed_status_snapshot_matches or precommit_snapshot_matches
    dirty_effectively_matches = dirty_matches or (precommit_snapshot_matches and current_dirty == 0)
    fresh = recorded.get("status") == "ok" and head_effectively_matches and dirty_effectively_matches
    return {
        "schema": "knowledgeradar-project-state-freshness/v1",
        "status": "ok" if fresh else "stale",
        "fresh": fresh,
        "recorded_path": recorded.get("path"),
        "recorded": {
            "git_head": recorded_head,
            "dirty_count_excluding_project_status": recorded_dirty,
        },
        "current": {
            "git_head": current_git.get("head"),
            "dirty_count_excluding_project_status": current_dirty,
        },
        "checks": {
            "head_matches": head_matches,
            "head_effectively_matches": head_effectively_matches,
            "committed_status_snapshot_matches": committed_status_snapshot_matches,
            "status_only_head_change": status_only_head_change,
            "precommit_snapshot_matches": precommit_snapshot_matches,
            "precommit_recorded_paths": precommit_recorded_paths,
            "precommit_head_diff_paths": precommit_head_diff_paths,
            "head_diff_paths": head_diff_paths,
            "dirty_count_matches": dirty_effectively_matches,
            "ignored_paths": current_git.get("freshness_ignores", [f"{PROJECT_STATUS_DIR_NAME}/**", ".coverage", "coverage.xml", "htmlcov/"]),
        },
    }


def render_validation_semantics_markdown(state: dict[str, Any]) -> str:
    lines = [
        "# KnowledgeRadar Validation Semantics",
        "",
        "- schema: `knowledgeradar-validation-semantics/v1`",
        "",
        "| Status | Blocks Overall Pass | Meaning |",
        "| --- | --- | --- |",
    ]
    for name, item in state["validation_status_classes"].items():
        lines.append(f"| {name} | {item['blocks_overall_pass']} | {item['meaning']} |")
    lines.extend(
        [
            "",
            "## Rule",
            "",
            "`EXPECTED_DEGRADED` is allowed only for declared optional providers, quota exhaustion, platform/API/login boundaries, or designed fallback paths. It is non-blocking; only `FAIL` and `NEEDS_INTERACTION` block closeout.",
            "",
        ]
    )
    return "\n".join(lines)


def render_codex_handoff_markdown(state: dict[str, Any]) -> str:
    lines = [
        "# KnowledgeRadar Codex Handoff",
        "",
        "- schema: `knowledgeradar-codex-handoff/v1`",
        f"- generated_at: `{state['generated_at']}`",
        "- source_of_truth: `{KR_ROOT}`",
        "- legacy_path: `{LEGACY_KR_ROOT}`",
        "- purpose: `bootstrap a new Codex/OpenClaw thread on the current project root without relying on the old thread context`",
        "",
        "## Required Thread Binding",
        "",
        "Use a new project thread bound to `{KR_ROOT}`. Do not keep developing from `{LEGACY_KR_ROOT}`.",
        "Codex project hooks are discovered from the active project root, so the hooks in `.codex/hooks.json` will not appear while a thread is bound to `{LEGACY_KR_ROOT}`.",
        "",
        "## Startup Checklist For The New Agent",
        "",
        "1. Read `AGENTS.md`.",
        "2. Read `project_status/KnowledgeRadar-Project-State.md`.",
        "3. Read this handoff file.",
        "4. Run `python scripts/kr_project_state.py --check-fresh --json` before code edits.",
        "5. Use `python scripts/kr_quality_gate.py --changed --json` or stricter mode for manual checks.",
        "6. For Codex hook closeout, use `python scripts/kr_quality_gate.py --changed --json --write-state` so `Quality-Gate-State.json` records PASS.",
        "",
        "## Migration Result",
        "",
        "- No active project rule, MCP, hook, or environment configuration was found in `{LEGACY_KR_ROOT}` that needs to be copied into the new source tree.",
        "- Legacy runtime state, browser profiles, local logs, generated artifacts, and secrets must not be copied into the repository.",
        "- The useful old-thread context has been reduced to this handoff plus the committed `project_status` files and governance documents.",
        "- `{KR_ROOT}` remains the only active development source of truth.",
        "",
        "## Current Quality Gate Contract",
        "",
        "- Git hooks are installed through `.githooks/` and run the unified quality gate without writing quality state.",
        "- Codex hooks are declared in `.codex/hooks.json`; the user must trust them in the Codex UI after opening a project thread bound to `{KR_ROOT}`.",
        "- `project_status/Quality-Gate-State.json` is the Codex lifecycle state file; status-only commits are treated as fresh so the state system does not recurse on itself.",
        "",
        "## Design Boundaries To Preserve",
        "",
        *[f"- {item}" for item in state["known_design_boundaries"]],
        "",
        "## Next Human Step",
        "",
        "Open a new Codex project thread rooted at `{KR_ROOT}`. After the Hooks page lists project hooks, trust the hooks. If the Hooks page is empty, verify the project root first.",
        "",
    ]
    return "\n".join(lines)


def write_project_status(root: Path | None = None) -> dict[str, str]:
    repo = root or project_root()
    state = collect_project_state(repo)
    out_dir = status_dir(repo)
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        PROJECT_STATE_FILE: render_project_state_markdown(state),
        VALIDATION_SEMANTICS_FILE: render_validation_semantics_markdown(state),
        CODEX_HANDOFF_FILE: render_codex_handoff_markdown(state),
    }
    written: dict[str, str] = {}
    for name, content in outputs.items():
        path = out_dir / name
        path.write_text(content, encoding="utf-8")
        written[name] = str(path)
    return written


def write_runtime_event(event: dict[str, Any], root: Path | None = None) -> Path:
    repo = root or project_root()
    event_dir = repo / "runtime" / "agent_governance"
    event_dir.mkdir(parents=True, exist_ok=True)
    path = event_dir / "events.jsonl"
    payload = dict(event)
    payload.setdefault("schema", "knowledgeradar-agent-governance-event/v1")
    payload.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    payload = json.loads(redact_path_text(json.dumps(payload, ensure_ascii=False), repo))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def project_governance_manifest(root: Path | None = None) -> dict[str, Any]:
    repo = root or project_root()
    return {
        "schema": "knowledgeradar-project-governance/v1",
        "status": "implemented",
        "rules_entrypoint": "AGENTS.md",
        "status_dir": PROJECT_STATUS_DIR_NAME,
        "status_files": [
            f"{PROJECT_STATUS_DIR_NAME}/{PROJECT_STATE_FILE}",
            f"{PROJECT_STATUS_DIR_NAME}/{VALIDATION_SEMANTICS_FILE}",
            f"{PROJECT_STATUS_DIR_NAME}/{CODEX_HANDOFF_FILE}",
        ],
        "scripts": {
            "project_state": "scripts/kr_project_state.py",
            "pre_change_check": "scripts/kr_pre_change_check.py",
            "closeout_check": "scripts/kr_closeout_check.py",
            "redact_report_paths": "scripts/kr_redact_report_paths.py",
            "quality_gate": "scripts/kr_quality_gate.py",
            "quality_hook_installer": "scripts/install_quality_hooks.py",
        },
        "quality_gate_manifest": "config/quality-gates.manifest.json",
        "quality_state": f"{PROJECT_STATUS_DIR_NAME}/Quality-Gate-State.json",
        "agent_adapters": {
            "codex_hooks": ".codex/hooks.json",
            "git_hooks": ".githooks",
            "openclaw_plugin_contract": "config/quality-gates.manifest.json#agent_adapters.openclaw",
        },
        "runtime_events": {
            "path": "runtime/agent_governance/events.jsonl",
            "release_policy": "exclude_local_runtime",
        },
        "redaction": {
            "repo_root": "{KR_ROOT}",
            "legacy_root": "{LEGACY_KR_ROOT}",
            "profile_root": "{KR_PROFILE_ROOT}",
        },
        "source_of_truth_exists": repo.is_dir(),
    }
