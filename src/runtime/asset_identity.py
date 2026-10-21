"""可选资产身份，内容hash按文件大小和mtime缓存；不下载资产。"""
from functools import lru_cache
import hashlib
from pathlib import Path


@lru_cache(maxsize=256)
def _digest(path, size, modified_ns):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_identity(path):
    path = Path(path)
    if not path.is_file():
        return {"status": "missing"}
    stat = path.stat()
    return {"name": path.name, "size": stat.st_size, "sha256": _digest(str(path.resolve()), stat.st_size, stat.st_mtime_ns)}


def model_identity(policy):
    root = Path(policy.model_cache_dir)
    direct = Path(policy.primary_model)
    if direct.is_dir():
        candidates = [direct]
    else:
        snapshots = root / ("models--Systran--faster-whisper-" + policy.primary_model) / "snapshots"
        candidates = sorted(snapshots.glob("*")) if snapshots.is_dir() else []
    files = []
    for folder in candidates:
        for file in sorted(folder.rglob("*")):
            if file.is_file():
                files.append({"relative": folder.name + "/" + file.relative_to(folder).as_posix(), **file_identity(file)})
    return {"model": policy.primary_model, "status": "recorded" if files else "not_provisioned", "files": files}
