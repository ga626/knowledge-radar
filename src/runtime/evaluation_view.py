"""评测的稳定路径与只读、低敏投影；不加载开发执行器或触发业务。"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path


def evaluation_store(root: Path) -> Path:
    explicit = os.environ.get("KR_EVALUATION_STORE")
    if explicit:
        return Path(explicit).expanduser().resolve()
    root = Path(root).resolve()
    # 仅已知的开发投影布局向上寻找；普通 checkout / 用户安装不读任意祖先。
    if root.parent.name in {"public-repos", "worktrees"} and root.parent.parent.name == ".codex":
        return root.parent.parent.parent / "local/evaluation"
    return root / "local/evaluation"


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def evaluation_summary(root: Path | None = None) -> dict:
    root = root or Path(os.environ.get("KR_PROJECT_ROOT") or Path(__file__).resolve().parents[2])
    path = evaluation_store(root) / "summary.json"
    empty = {"schema": "knowledgeradar-evaluation-view/v1", "gate": "INCOMPLETE", "cases": [],
             "boundary": "development_evidence_only", "installed_acceptance": "NOT_CLAIMED"}
    if not path.is_file():
        return {**empty, "availability": "missing"}
    try:
        row = json.loads(path.read_text(encoding="utf-8-sig"))
        if row.get("integrity") != _digest({k: v for k, v in row.items() if k != "integrity"}):
            raise ValueError("summary integrity")
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["generated_at"])).total_seconds()
        watched = row.get("watched_files", {})
        source_matches = bool(watched) and row.get("source_root") == str(root.resolve())
        for name, checksum in watched.items():
            file = (root / name).resolve()
            if not file.is_relative_to(root.resolve()) or not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != checksum:
                source_matches = False
                break
        # 检查新增源码；不遍历依赖、缓存、认证和运行态目录。
        for prefix in ("src", "scripts", "tests", "config", "bridge", ".github/workflows"):
            for file in (root / prefix).rglob("*"):
                relative = file.relative_to(root).as_posix()
                if file.suffix in {".py", ".json", ".toml", ".yaml", ".yml"} and not any(p in {"__pycache__", "local", "data", "node_modules"} for p in file.relative_to(root).parts) and relative not in {"config/profile_registry.json", "config/mcp-approvals.json"} and file.is_file() and relative not in watched:
                    source_matches = False
        fresh = source_matches and 0 <= age <= 900
        cases = [{k: x.get(k) for k in ("case_id", "layer", "verdict", "applicability", "readiness", "run_id", "checked_at")}
                 for x in row["cases"]]
        # 读取不重新扫描源码或延长有效期；发布摘要超过15分钟只显示上次结果。
        external = [{"run_id": x["run_id"], "checked_at": x["checked_at"], "counts": x["counts"],
                     "source_commit": x["target"].get("source_commit"), "kind": x["target"].get("kind")}
                    for x in row.get("external_observations", [])]
        return {**empty, "availability": "snapshot" if fresh else "stale_snapshot",
                "gate": row["gate"] if fresh else "INCOMPLETE", "generated_at": row["generated_at"],
                "snapshot_hash": row["snapshot_hash"], "history_runs": row["history_runs"], "cases": cases,
                "external_observations": external,
                "ci_gate": row.get('ci_gate'),
                "counts": {k: dict(Counter(x[k] for x in cases)) for k in ("verdict", "applicability")}}
    except (OSError, ValueError, KeyError, TypeError):
        return {**empty, "availability": "invalid"}
