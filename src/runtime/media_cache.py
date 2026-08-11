"""Managed media cache for downloaded audio, video, frames, and transcripts."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable

from runtime.paths import runtime_media_cache_dir


DEFAULT_TTL_SECONDS = 7 * 24 * 3600
MANIFEST_FILENAME = "manifest.jsonl"


def safe_cache_key(value: str, *, fallback: str = "media") -> str:
    key = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("._-")
    return key[:120] or fallback


@dataclass(frozen=True)
class MediaCacheEntry:
    path: Path
    kind: str
    content_id: str = ""
    task_id: str = ""
    source_url: str = ""
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    metadata: dict[str, Any] | None = None

    def to_manifest_record(self) -> dict[str, Any]:
        stat = self.path.stat() if self.path.exists() else None
        now = time.time()
        return {
            "schema": "knowledgeradar-media-cache-entry/v1",
            "path": str(self.path),
            "kind": self.kind,
            "content_id": self.content_id,
            "task_id": self.task_id,
            "source_url": self.source_url,
            "size_bytes": stat.st_size if stat else 0,
            "created_at": stat.st_mtime if stat else now,
            "recorded_at": now,
            "ttl_seconds": self.ttl_seconds,
            "expires_at": (stat.st_mtime if stat else now) + self.ttl_seconds,
            "metadata": dict(self.metadata or {}),
        }


def media_cache_root() -> Path:
    return runtime_media_cache_dir()


def media_cache_subdir(kind: str, *, content_id: str = "", task_id: str = "") -> Path:
    parts = [safe_cache_key(kind, fallback="media")]
    if content_id:
        parts.append(safe_cache_key(content_id, fallback="content"))
    if task_id:
        parts.append(safe_cache_key(task_id, fallback="task"))
    path = media_cache_root()
    for part in parts:
        path /= part
    path.mkdir(parents=True, exist_ok=True)
    return path


def manifest_path(root: Path | None = None) -> Path:
    base = root or media_cache_root()
    return base / MANIFEST_FILENAME


def record_media_cache_entry(
    path: str | Path,
    *,
    kind: str,
    content_id: str = "",
    task_id: str = "",
    source_url: str = "",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry = MediaCacheEntry(
        path=Path(path),
        kind=kind,
        content_id=content_id,
        task_id=task_id,
        source_url=source_url,
        ttl_seconds=ttl_seconds,
        metadata=metadata,
    )
    record = entry.to_manifest_record()
    manifest = manifest_path()
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return record


def iter_manifest_records(root: Path | None = None) -> Iterable[dict[str, Any]]:
    manifest = manifest_path(root)
    if not manifest.exists():
        return
    with manifest.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                yield data


def cleanup_expired_media_cache(
    *,
    root: str | Path | None = None,
    now: float | None = None,
    default_ttl_seconds: int = DEFAULT_TTL_SECONDS,
    dry_run: bool = False,
) -> dict[str, Any]:
    base = Path(root) if root is not None else media_cache_root()
    current = time.time() if now is None else now
    deleted: list[str] = []
    kept: list[str] = []
    errors: list[dict[str, str]] = []

    if not base.exists():
        return {"root": str(base), "deleted": deleted, "kept": kept, "errors": errors}

    manifest = manifest_path(base)
    manifest_abs = manifest.resolve() if manifest.exists() else None
    ttl_by_path: dict[Path, int] = {}
    for record in iter_manifest_records(base) or ():
        raw_path = record.get("path")
        if not raw_path:
            continue
        try:
            ttl_by_path[Path(raw_path).resolve()] = int(record.get("ttl_seconds") or default_ttl_seconds)
        except Exception:
            continue

    for path in base.rglob("*"):
        if not path.is_file():
            continue
        try:
            resolved = path.resolve()
            if manifest_abs and resolved == manifest_abs:
                kept.append(str(path))
                continue
            ttl = ttl_by_path.get(resolved, default_ttl_seconds)
            if current - path.stat().st_mtime >= ttl:
                deleted.append(str(path))
                if not dry_run:
                    path.unlink()
            else:
                kept.append(str(path))
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)})

    _remove_empty_dirs(base, dry_run=dry_run)
    return {"root": str(base), "deleted": deleted, "kept": kept, "errors": errors}


def _remove_empty_dirs(root: Path, *, dry_run: bool) -> None:
    if dry_run:
        return
    for path in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda item: len(item.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass

