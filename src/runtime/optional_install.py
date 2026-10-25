"""显式准备已定组件的精确离线锁和权重；推理路径不下载。"""

from __future__ import annotations

import email
import json
from pathlib import Path
import sys
import time
import zipfile

import httpx

from runtime.model_roles import COMPONENTS
from runtime.optional_media import file_sha, receipt_root
from runtime.process import silent_subprocess_run


def _download_file(client, url, target, expected):
    """固定版本大文件可续传；最终 SHA 不匹配时拒绝发布。"""
    temporary = target.with_suffix(target.suffix + ".pending")
    for attempt in range(3):
        offset = temporary.stat().st_size if expected and temporary.is_file() else 0
        try:
            with client.stream("GET", url, headers={"Range": f"bytes={offset}-"} if offset else {}) as stream:
                stream.raise_for_status()
                append = offset > 0 and stream.status_code == 206
                if append and not stream.headers.get("content-range", "").startswith(f"bytes {offset}-"):
                    raise ValueError("invalid_download_resume_range")
                with temporary.open("ab" if append else "wb") as handle:
                    for block in stream.iter_bytes(1024 * 1024):
                        handle.write(block)
            if expected and file_sha(temporary) != expected:
                temporary.unlink(missing_ok=True)
                raise ValueError("downloaded_weight_hash_mismatch")
            temporary.replace(target)
            return
        except httpx.TransportError:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def _download_model(model, revision, root):
    root.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        response = client.get(f"https://huggingface.co/api/models/{model}/revision/{revision}?blobs=true")
        response.raise_for_status()
        metadata = response.json()
        if metadata.get("sha") != revision:
            raise ValueError("upstream_model_revision_mismatch")
        manifest = {}
        for entry in metadata["siblings"]:
            name = entry["rfilename"]
            # 仅内建模型的安全权重/配置；不下载或执行仓库 Python、pickle 或脚本。
            if not name.endswith(
                (".safetensors", ".json", ".model", ".jinja", ".yml", ".yaml", ".pdiparams")
            ) or name.startswith("."):
                continue
            target = (root / name).resolve()
            if not target.is_relative_to(root.resolve()):
                raise ValueError("unsafe_model_path")
            target.parent.mkdir(parents=True, exist_ok=True)
            expected = (entry.get("lfs") or {}).get("sha256")
            if not target.is_file() or (expected and file_sha(target) != expected):
                _download_file(client, f"https://huggingface.co/{model}/resolve/{revision}/{name}", target, expected)
            manifest[name] = file_sha(target)
    if not manifest:
        raise ValueError("empty_model_manifest")
    return manifest


def prepare_component(component, *, acknowledged=False):
    if acknowledged is not True:
        raise ValueError("explicit_download_acknowledgement_required")
    role = COMPONENTS[component]
    frozen = Path(__file__).resolve().parents[2] / role["requirements_lock"]
    if not frozen.is_file() or file_sha(frozen) != role["requirements_sha256"]:
        raise ValueError("optional_frozen_lock_identity_mismatch")
    root = receipt_root().resolve() / "artifacts" / component
    wheels = root / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    # Wheel-only 安装避免运行不受控构建脚本；每个依赖进入 hash 锁。
    silent_subprocess_run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--disable-pip-version-check",
            "--only-binary=:all:",
            "--dest",
            str(wheels),
            "--require-hashes",
            "-r",
            str(frozen),
        ],
        check=True,
        capture_output=True,
        timeout=1800,
    )
    requirements, inventory = [], []
    names = set()
    for wheel in sorted(wheels.glob("*.whl")):
        with zipfile.ZipFile(wheel) as archive:
            meta_path = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
            meta = email.message_from_bytes(archive.read(meta_path))
        name, version = meta["Name"], meta["Version"]
        normalized = name.lower().replace("_", "-")
        if normalized in names:
            raise ValueError("multiple_package_versions_in_wheelhouse")
        names.add(normalized)
        digest = file_sha(wheel)
        requirements.append(f"{name}=={version} --hash=sha256:{digest}")
        inventory.append(
            {
                "name": name,
                "version": version,
                "sha256": digest,
                "license": meta.get("License-Expression") or meta.get("License") or "not_declared",
            }
        )
    lock = root / "requirements.lock"
    resolved = ("\n".join(requirements) + "\n").encode("utf-8")
    if resolved != frozen.read_bytes():
        raise ValueError("prepared_wheels_do_not_match_frozen_lock")
    lock.write_bytes(resolved)
    model_root = root / "model"
    model_root.mkdir(exist_ok=True)
    files = _download_model(role["model"], role["weight_revision"], model_root) if role["model_required"] else {}
    spec = {
        "installation_acknowledged": True,
        "license_review": "APPROVED",
        "upstream_revision": "pypi:" + ",".join(role["requirements"]),
        "weight_revision": role["weight_revision"],
        "requirements_lock": str(lock),
        "requirements_sha256": file_sha(lock),
        "wheelhouse": str(wheels),
        "model_root": str(model_root),
        "model_files": files,
        "license_basis": {"code": role["code_license"], "weights": role["weight_license"], "source": role["source"]},
    }
    if role.get("layout_model"):
        layout = root / "layout"
        spec.update(
            layout_model_root=str(layout),
            layout_model_files=_download_model(role["layout_model"], role["layout_revision"], layout),
        )
    (root / "wheel-inventory.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
    path = root / "install-spec.json"
    path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "status": "prepared",
        "component": component,
        "spec_path": str(path),
        "requirements_sha256": spec["requirements_sha256"],
        "wheels": len(inventory),
        "automatic_install": False,
    }
