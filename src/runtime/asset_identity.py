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
    return {
        "name": path.name,
        "size": stat.st_size,
        "sha256": _digest(str(path.resolve()), stat.st_size, stat.st_mtime_ns),
    }


def model_identity(policy):
    from runtime.model_roles import ASR_COMPONENT
    from runtime.optional_media import receipt_root, file_sha, validate_receipt

    receipt = receipt_root() / (ASR_COMPONENT + ".json")
    if not receipt.is_file():
        return {"model": ASR_COMPONENT, "engine": "transformers_native", "status": "not_provisioned"}
    identity = validate_receipt(ASR_COMPONENT)
    return {
        "model": ASR_COMPONENT,
        "engine": "transformers_native",
        "status": "recorded",
        "install_receipt_sha256": file_sha(receipt),
        "weight_revision": identity["weight_revision"],
        "model_files": identity["model_files"],
    }
