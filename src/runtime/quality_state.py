"""Persistent quality gate state for agent closeout enforcement."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from runtime.process import silent_subprocess_run
from runtime.project_state import PROJECT_STATUS_DIR_NAME, project_root
from runtime.quality_gates import git_changed_paths


QUALITY_STATE_SCHEMA = "knowledgeradar-quality-state/v1"
QUALITY_STATE_FILE = "Quality-Gate-State.json"


def quality_state_path(root: Path | None = None) -> Path:
    repo = root or project_root()
    return repo / PROJECT_STATUS_DIR_NAME / QUALITY_STATE_FILE


def _quality_state_repo_path() -> str:
    return f"{PROJECT_STATUS_DIR_NAME}/{QUALITY_STATE_FILE}"


def _quality_tracked_paths(root: Path) -> list[str]:
    state_path = _quality_state_repo_path()
    return [path for path in git_changed_paths(root) if path != state_path]


def _normalize_repo_path(path: Any) -> str:
    return str(path or "").strip().strip('"').replace("\\", "/")


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _git_output(root: Path, args: list[str]) -> str:
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
    except Exception:
        return ""
    return (proc.stdout or proc.stderr or "").strip()


def _head_diff_paths(root: Path, old_head: str, new_head: str) -> list[str]:
    if not old_head or not new_head or old_head == new_head:
        return []
    paths: list[str] = []
    for line in _git_output(root, ["diff", "--name-only", old_head, new_head]).splitlines():
        path = line.strip().replace("\\", "/")
        if path and path not in paths:
            paths.append(path)
    return paths


def _is_project_status_file_path(path: str) -> bool:
    item = path.strip().replace("\\", "/")
    return item.startswith(f"{PROJECT_STATUS_DIR_NAME}/")


def _head_change_is_status_only(root: Path, old_head: str, new_head: str) -> tuple[bool, list[str]]:
    if not old_head or not new_head or old_head == new_head:
        return False, []
    head_paths = _head_diff_paths(root, old_head, new_head)
    return all(_is_project_status_file_path(path) for path in head_paths), head_paths


def _recorded_dirty_paths_are_head_diff(root: Path, recorded: dict[str, Any], current: dict[str, Any]) -> tuple[bool, list[str]]:
    recorded_head = str(recorded.get("git_head") or "")
    current_head = str(current.get("git_head") or "")
    recorded_paths = [str(path).replace("\\", "/") for path in recorded.get("changed_paths") or []]
    if not recorded_head or not current_head or recorded_head == current_head:
        return False, []
    head_paths = _head_diff_paths(root, recorded_head, current_head)
    status_path = _quality_state_repo_path()
    non_state_head_paths = [path for path in head_paths if path != status_path]
    ok = bool(head_paths) and sorted(recorded_paths) == sorted(non_state_head_paths) and current.get("dirty_count") == 0
    return ok, head_paths


def quality_snapshot(root: Path | None = None) -> dict[str, Any]:
    repo = root or project_root()
    changed_paths = _quality_tracked_paths(repo)
    state_path = _quality_state_repo_path()
    diff_name_status = "\n".join(
        line
        for line in _git_output(repo, ["diff", "--name-status", "HEAD"]).splitlines()
        if not line.rstrip().replace("\\", "/").endswith(state_path)
    )
    untracked = "\n".join(
        line
        for line in _git_output(repo, ["ls-files", "--others", "--exclude-standard"]).splitlines()
        if line.strip().replace("\\", "/") != state_path
    )
    head = _git_output(repo, ["rev-parse", "--short", "HEAD"])
    payload = "\n".join([diff_name_status, untracked])
    return {
        "schema": "knowledgeradar-quality-snapshot/v1",
        "git_head": head,
        "dirty_count": len(changed_paths),
        "changed_paths": changed_paths,
        "changed_paths_hash": _hash_text("\n".join(changed_paths)),
        "dirty_hash": _hash_text(payload),
    }


def status_class_from_summary(summary: dict[str, Any]) -> str:
    status = str(summary.get("status") or "").upper()
    if status == "PASS":
        return "PASS"
    for item in summary.get("results") or []:
        if not (item.get("status") == "fail" or item.get("returncode", 0) != 0):
            continue
        status_text = " ".join(str(item.get(key) or "") for key in ("status", "campaign_status")).lower()
        detail_text = " ".join(str(item.get(key) or "") for key in ("stdout", "stderr")).lower()
        if "needs_interaction" in status_text or any(
            marker in detail_text for marker in ("login_required", "captcha_required", "anti_bot_verification")
        ):
            return "NEEDS_INTERACTION"
    return "FAIL"


def recommended_next_command(mode: str, status_class: str) -> str:
    if status_class == "PASS":
        return "none"
    if mode == "report_light":
        return "python scripts/kr_closeout_router.py --json"
    if mode == "fast":
        return "python scripts/kr_quality_gate.py --fast --json --write-state"
    if mode == "changed":
        return "python scripts/kr_quality_gate.py --changed --json --write-state"
    if mode == "full":
        return "python scripts/kr_quality_gate.py --full --json --write-state"
    return "python scripts/kr_quality_gate.py --campaign --profile smoke --json --write-state"


def _path_should_stay_in_snapshot(root: Path, path: str, current_changed_paths: set[str]) -> bool:
    item = _normalize_repo_path(path)
    if not item:
        return False
    return item in current_changed_paths or (root / item).exists()


def _replace_removed_paths(value: Any, removed_paths: list[str]) -> Any:
    if isinstance(value, str):
        text = value
        for path in removed_paths:
            text = text.replace(path, "[pruned-missing-path]")
        return text
    if isinstance(value, list):
        return [_replace_removed_paths(item, removed_paths) for item in value]
    if isinstance(value, dict):
        return {key: _replace_removed_paths(item, removed_paths) for key, item in value.items()}
    return value


def prune_missing_paths_from_quality_state(root: Path | None = None, *, preserve_pass_status: bool = False) -> dict[str, Any]:
    repo = root or project_root()
    path = quality_state_path(repo)
    if not path.is_file():
        return {"schema": "knowledgeradar-quality-state-prune/v1", "status": "noop", "reason": "missing"}
    state = read_quality_state(repo)
    snapshot = state.get("snapshot") or {}
    raw_paths = snapshot.get("changed_paths") or []
    if not isinstance(raw_paths, list) or not raw_paths:
        return {"schema": "knowledgeradar-quality-state-prune/v1", "status": "noop", "reason": "no_snapshot_paths"}

    current_changed_paths = {_normalize_repo_path(item) for item in git_changed_paths(repo)}
    kept_paths: list[str] = []
    removed_paths: list[str] = []
    for raw_path in raw_paths:
        item = _normalize_repo_path(raw_path)
        if _path_should_stay_in_snapshot(repo, item, current_changed_paths):
            kept_paths.append(item)
        elif item:
            removed_paths.append(item)

    if not removed_paths:
        return {"schema": "knowledgeradar-quality-state-prune/v1", "status": "noop", "reason": "no_missing_paths"}

    snapshot["changed_paths"] = kept_paths
    snapshot["dirty_count"] = len(kept_paths)
    snapshot["changed_paths_hash"] = _hash_text("\n".join(kept_paths))
    state["snapshot"] = snapshot
    if not (preserve_pass_status and str(state.get("status_class") or "").upper() == "PASS"):
        state["status"] = "STALE"
        state["status_class"] = "STALE"
        state["reason"] = "quality_state_pruned_missing_paths"
        state["recommended_next_command"] = "python scripts/kr_quality_gate.py --changed --json --write-state"
    state["maintenance"] = {
        "schema": "knowledgeradar-quality-state-maintenance/v1",
        "pruned_at": datetime.now(timezone.utc).isoformat(),
        "pruned_missing_paths": removed_paths,
    }
    state["failure_summary"] = _replace_removed_paths(state.get("failure_summary", []), removed_paths)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "schema": "knowledgeradar-quality-state-prune/v1",
        "status": "pruned",
        "removed_count": len(removed_paths),
        "removed_paths": removed_paths,
        "kept_count": len(kept_paths),
    }


def write_quality_state(summary: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    repo = root or project_root()
    snapshot = quality_snapshot(repo)
    status_class = status_class_from_summary(summary)
    mode = str(summary.get("mode") or "unknown")
    failed_results = [
        {
            "command": item.get("command", item.get("schema", "check")),
            "status": item.get("status"),
            "returncode": item.get("returncode"),
            "stderr": (item.get("stderr") or "")[-600:],
            "stdout": (item.get("stdout") or "")[-600:],
        }
        for item in (summary.get("results") or [])
        if item.get("status") == "fail" or item.get("returncode", 0) != 0
    ][:8]
    state = {
        "schema": QUALITY_STATE_SCHEMA,
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "profile": summary.get("profile"),
        "status": summary.get("status"),
        "status_class": status_class,
        "total": summary.get("total"),
        "failed": summary.get("failed"),
        "snapshot": snapshot,
        "recommended_next_command": recommended_next_command(mode, status_class),
        "failure_summary": failed_results,
    }
    path = quality_state_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def refresh_quality_state_snapshot(root: Path | None = None, *, event: str = "snapshot_refresh") -> dict[str, Any]:
    repo = root or project_root()
    state = read_quality_state(repo)
    if state.get("schema") != QUALITY_STATE_SCHEMA:
        return state
    mode = str(state.get("mode") or "unknown")
    status_class = str(state.get("status_class") or "STALE")
    state["generated_at"] = datetime.now(timezone.utc).isoformat()
    state["snapshot"] = quality_snapshot(repo)
    state["recommended_next_command"] = recommended_next_command(mode, status_class)
    state["maintenance"] = {
        "schema": "knowledgeradar-quality-state-maintenance/v1",
        "refreshed_at": state["generated_at"],
        "event": event,
    }
    path = quality_state_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def read_quality_state(root: Path | None = None) -> dict[str, Any]:
    path = quality_state_path(root)
    if not path.is_file():
        return {
            "schema": QUALITY_STATE_SCHEMA,
            "status_class": "STALE",
            "fresh": False,
            "reason": "quality_state_missing",
            "path": str(path),
        }
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "schema": QUALITY_STATE_SCHEMA,
            "status_class": "STALE",
            "fresh": False,
            "reason": f"quality_state_unreadable:{exc}",
            "path": str(path),
        }
    if state.get("schema") != QUALITY_STATE_SCHEMA or state.get("schema_version") != 1:
        return {
            "schema": QUALITY_STATE_SCHEMA,
            "status_class": "STALE",
            "fresh": False,
            "reason": "quality_state_schema_mismatch",
            "path": str(path),
            "recorded_schema": state.get("schema"),
            "recorded_schema_version": state.get("schema_version"),
        }
    return state


def quality_state_freshness(root: Path | None = None) -> dict[str, Any]:
    repo = root or project_root()
    state = read_quality_state(repo)
    if state.get("status_class") == "STALE" and not state.get("snapshot"):
        return {
            "schema": "knowledgeradar-quality-state-freshness/v1",
            "fresh": False,
            "status_class": "STALE",
            "reason": state.get("reason", "quality_state_unavailable"),
            "recommended_next_command": "python scripts/kr_closeout_router.py --json",
        }
    current = quality_snapshot(repo)
    recorded = state.get("snapshot") or {}
    fresh = (
        recorded.get("git_head") == current.get("git_head")
        and recorded.get("dirty_hash") == current.get("dirty_hash")
        and recorded.get("changed_paths_hash") == current.get("changed_paths_hash")
    )
    committed_snapshot_matches, head_diff_paths = _recorded_dirty_paths_are_head_diff(repo, recorded, current)
    status_only_head_change = False
    if not fresh and not committed_snapshot_matches:
        status_only_head_change, head_diff_paths = _head_change_is_status_only(
            repo,
            str(recorded.get("git_head") or ""),
            str(current.get("git_head") or ""),
        )
    fresh = fresh or committed_snapshot_matches
    status_class = state.get("status_class", "FAIL") if fresh else "STALE"
    return {
        "schema": "knowledgeradar-quality-state-freshness/v1",
        "fresh": fresh,
        "status_class": status_class,
        "mode": state.get("mode"),
        "profile": state.get("profile"),
        "recorded": {
            "git_head": recorded.get("git_head"),
            "dirty_hash": recorded.get("dirty_hash"),
            "changed_paths_hash": recorded.get("changed_paths_hash"),
            "dirty_count": recorded.get("dirty_count"),
        },
        "current": {
            "git_head": current.get("git_head"),
            "dirty_hash": current.get("dirty_hash"),
            "changed_paths_hash": current.get("changed_paths_hash"),
            "dirty_count": current.get("dirty_count"),
        },
        "checks": {
            "committed_snapshot_matches": committed_snapshot_matches,
            "status_only_head_change": status_only_head_change,
            "head_diff_paths": head_diff_paths,
        },
        "recommended_next_command": state.get("recommended_next_command")
        if fresh
        else "python scripts/kr_quality_gate.py --changed --json --write-state",
    }


def mark_quality_state_stale(reason: str, root: Path | None = None, *, event: str = "unknown") -> dict[str, Any]:
    repo = root or project_root()
    snapshot = quality_snapshot(repo)
    state = {
        "schema": QUALITY_STATE_SCHEMA,
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "status": "STALE",
        "status_class": "STALE",
            "reason": reason,
        "snapshot": snapshot,
        "recommended_next_command": "python scripts/kr_closeout_router.py --json",
        "failure_summary": [],
    }
    path = quality_state_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state
