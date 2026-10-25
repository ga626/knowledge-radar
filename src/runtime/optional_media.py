"""可选模型的隔离进程入口；无安装、下载或私有目录发现副作用。"""

from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
from functools import lru_cache
from runtime.paths import runtime_state_dir
from runtime.process import silent_subprocess_run, isolated_worker_run
from runtime.model_roles import COMPONENTS


def file_sha(path):
    source = Path(path)
    stat = source.stat()
    return _file_sha(str(source.resolve()), stat.st_size, stat.st_mtime_ns)


@lru_cache(maxsize=512)
def _file_sha(path, size, modified_ns):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def receipt_root():
    return Path(os.environ.get("KR_OPTIONAL_COMPONENT_RECEIPTS_DIR") or runtime_state_dir() / "optional_components")


def component_plan(component):
    if component not in COMPONENTS:
        raise ValueError("unsupported_optional_component")
    path = receipt_root() / (component + ".json")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError):
        receipt = None
    ready = False
    if receipt:
        try:
            validate_receipt(component)
            ready = True
        except (ValueError, OSError, KeyError):
            pass
    return {
        "schema": "knowledgeradar-optional-media/v1",
        "component": component,
        **COMPONENTS[component],
        "status": "ready" if ready else "not_installed",
        "ready": ready,
        "receipt_present": path.is_file(),
        "automatic_install": False,
        "automatic_model_download": False,
    }


def validate_receipt(component):
    if component not in COMPONENTS:
        raise ValueError("unsupported_optional_component")
    path = receipt_root() / (component + ".json")
    if not path.is_file():
        raise ValueError("optional_component_not_installed_and_approved")
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(receipt, dict):
        raise ValueError("invalid_optional_install_receipt")
    if receipt.get("weight_revision") != COMPONENTS[component]["weight_revision"]:
        raise ValueError("optional_component_role_revision_mismatch")
    if not isinstance(receipt.get("package_versions"), dict) or not isinstance(receipt.get("model_files"), dict):
        raise ValueError("invalid_optional_install_receipt")
    installed = {name.lower().replace("_", "-"): version for name, version in receipt["package_versions"].items()}
    role = COMPONENTS[component]
    frozen = Path(__file__).resolve().parents[2] / role["requirements_lock"]
    if not frozen.is_file() or file_sha(frozen) != role["requirements_sha256"]:
        raise ValueError("optional_frozen_lock_identity_mismatch")
    for line in frozen.read_text(encoding="utf-8").splitlines():
        name, version = line.split(" --hash=", 1)[0].split("==", 1)
        if installed.get(name.lower().replace("_", "-")) != version:
            raise ValueError("optional_component_locked_dependency_mismatch")
    for requirement in COMPONENTS[component]["requirements"]:
        name, version = requirement.split("==", 1)
        if installed.get(name.split("[")[0].lower().replace("_", "-")) != version:
            raise ValueError("optional_component_role_package_mismatch")
    if (
        receipt.get("schema") != "knowledgeradar-optional-install/v1"
        or receipt.get("component") != component
        or receipt.get("license_review") != "APPROVED"
    ):
        raise ValueError("optional_component_license_not_approved")
    if (
        not receipt.get("package_versions")
        or not receipt.get("upstream_revision")
        or not receipt.get("weight_revision")
        or not receipt.get("freeze_sha256")
    ):
        raise ValueError("optional_component_identity_incomplete")
    freeze = Path(str(receipt.get("freeze_path") or ""))
    if not freeze.is_file() or file_sha(freeze) != receipt["freeze_sha256"]:
        raise ValueError("optional_freeze_identity_mismatch")
    python = Path(receipt["python"]).resolve()
    if not python.is_file() or file_sha(python) != receipt.get("python_sha256"):
        raise ValueError("optional_python_identity_mismatch")
    if python == Path(__import__("sys").executable).resolve():
        raise ValueError("isolated_python_required")
    if not (python.parent.parent / "pyvenv.cfg").is_file() and not (python.parent / "pyvenv.cfg").is_file():
        raise ValueError("isolated_environment_required")
    root = Path(receipt["model_root"]).resolve()
    manifest = receipt.get("model_files") or {}
    if not manifest and COMPONENTS[component]["model_required"]:
        raise ValueError("local_model_manifest_required")
    for relative, digest in manifest.items():
        model = (root / relative).resolve()
        if not model.is_relative_to(root) or not model.is_file() or file_sha(model) != digest:
            raise ValueError("optional_model_identity_mismatch")
    if component.startswith("paddle"):
        layout = Path(str(receipt.get("layout_model_root") or "")).resolve()
        layout_files = receipt.get("layout_model_files") or {}
        if not layout_files:
            raise ValueError("paddle_layout_model_required")
        for relative, digest in layout_files.items():
            model = (layout / relative).resolve()
            if not model.is_relative_to(layout) or not model.is_file() or file_sha(model) != digest:
                raise ValueError("optional_layout_identity_mismatch")
    return receipt


def approved_source(payload):
    """先验证用户授权和原件身份，再允许任何解析器读取输入。"""
    root = Path(payload.get("approved_input_root", ""))
    source = Path(payload.get("input_path", ""))
    if not root.is_absolute() or not source.is_absolute():
        raise ValueError("absolute_approved_input_required")
    for path in (source, *source.parents, root, *root.parents):
        if path.is_symlink() or path.is_junction():
            raise ValueError("links_not_allowed")
    root, source = root.resolve(), source.resolve()
    if (
        source == root
        or not source.is_relative_to(root)
        or not source.is_file()
        or source.stat().st_size > 256 * 1024 * 1024
    ):
        raise ValueError("input_outside_approved_root_or_too_large")
    for path in (source, *source.parents):
        if path.is_symlink() or path.is_junction():
            raise ValueError("links_not_allowed")
    if file_sha(source) != payload.get("input_sha256"):
        raise ValueError("input_identity_mismatch")
    return source


def select_component(payload):
    source = approved_source(payload)
    component = payload.get("component") or "auto"
    if component != "auto":
        if component not in COMPONENTS and component != "pypdf":
            raise ValueError("unsupported_optional_component")
        return component
    suffix = source.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(source)
        if not reader.pages:
            raise ValueError("empty_document")
        # 任一无文本页需要整份文档 OCR，不能把混合 PDF 的文字页冒充全文。
        return "pypdf" if all((page.extract_text() or "").strip() for page in reader.pages) else "paddleocr-vl-1.6"
    if suffix in {".docx", ".xlsx", ".pptx", ".html", ".htm", ".md"}:
        return "docling"
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}:
        return "paddleocr-vl-1.6"
    if suffix in {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".mp4", ".webm", ".aac"}:
        return "qwen3-asr-0.6b"
    raise ValueError("unsupported_local_evidence_format")


def run_optional(payload):
    source = approved_source(payload)
    component = select_component(payload)
    if component == "pypdf":
        from pypdf import PdfReader

        blocks = [
            {"page": i + 1, "kind": "paragraph", "text": page.extract_text() or ""}
            for i, page in enumerate(PdfReader(source).pages)
        ]
        if any(not row["text"].strip() for row in blocks):
            raise ValueError("pdf_requires_ocr")
        return _save_result(
            {
                "status": "acquired",
                "component": component,
                "text": "\n\n".join(row["text"] for row in blocks),
                "document_blocks": blocks,
                "source_kind": "pdf_text",
            },
            payload,
        )
    receipt = validate_receipt(component)
    from runtime.asr_policy import AsrPolicy

    language = AsrPolicy.for_request(payload.get("language")).language
    worker = Path(__file__).with_name("optional_media_worker.py")
    environment = {
        key: os.environ[key]
        for key in ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP", "LOCALAPPDATA")
        if key in os.environ
    }
    cache = receipt_root() / "cache" / component
    cache.mkdir(parents=True, exist_ok=True)
    environment.update(USERPROFILE=str(cache), HOME=str(cache), HF_HOME=str(cache / "huggingface"),
                       PADDLE_PDX_CACHE_HOME=str(cache / "paddlex"))
    threads = max(1, min(8, int(os.environ.get("KR_LOCAL_MODEL_THREADS", "4"))))
    environment.update(
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        HF_HUB_DISABLE_TELEMETRY="1",
        PYTHONIOENCODING="utf-8",
        OMP_NUM_THREADS=str(threads),
        MKL_NUM_THREADS=str(threads),
        TOKENIZERS_PARALLELISM="false",
        PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="True",
    )
    from runtime.dependency_preflight import _ffmpeg_path

    ffmpeg = _ffmpeg_path() if COMPONENTS[component]["kind"] == "asr" else ""
    if COMPONENTS[component]["kind"] == "asr" and not ffmpeg:
        raise ValueError("ffmpeg_not_installed")
    request = {
        "component": component,
        "input_path": str(source),
        "language": language,
        "model_root": receipt["model_root"],
        "expected_packages": receipt["package_versions"],
        "layout_model_root": receipt.get("layout_model_root"),
        "ffmpeg_exe": ffmpeg,
        "device": os.environ.get("KR_ASR_DEVICE", "cpu")
        if COMPONENTS[component]["kind"] == "asr"
        else os.environ.get("KR_OCR_DEVICE", "cpu"),
        "threads": threads,
    }
    from runtime.resource_concurrency import acquire_resource

    with acquire_resource("local_heavy"):
        response = isolated_worker_run(
            [receipt["python"], "-I", "-X", "utf8", str(worker)],
            input=json.dumps(request),
            timeout=min(1800, max(1, float(payload.get("timeout_s", 600)))),
            env=environment,
        )
    if response.returncode:
        raise ValueError("optional_component_execution_failed")
    if len(response.stdout) > 32 * 1024 * 1024:
        raise ValueError("optional_component_output_too_large")
    result = json.loads(response.stdout)
    if result.get("status") != "acquired" or not str(result.get("text") or "").strip():
        raise ValueError("optional_component_empty_or_failed")
    return _save_result(result, payload, receipt=receipt)


def _save_result(result, payload, *, receipt=None):
    from kr_core.evidence_store import EvidenceStore

    result["content"] = result["text"]
    component = result["component"]
    result["collector"] = component
    identity_url = "https://local-evidence.invalid/sha256/" + payload["input_sha256"]
    EvidenceStore().append_detail(
        platform="local_asr" if component.startswith("qwen3-asr") else "local_document",
        url=identity_url,
        response=result,
    )
    return {
        **result,
        "input_sha256": payload["input_sha256"],
        "install_receipt_sha256": file_sha(receipt_root() / (component + ".json")) if receipt else "",
        "coverage": "partial",
        "quality_status": "unmeasured",
    }


def uninstall_component(component, *, purge_downloads=False):
    """显式卸载本组件拥有的目录；与推理共享互斥锁，保留证据和研究数据。"""
    import shutil
    from runtime.resource_concurrency import acquire_resource

    if component not in COMPONENTS:
        raise ValueError("unsupported_optional_component")
    base = receipt_root().resolve()
    targets = [base / "environments" / component, base / "cache" / component]
    if purge_downloads:
        targets.append(base / "artifacts" / component)
    for target in targets:
        if not target.resolve().is_relative_to(base) or any(
            path.is_symlink() or path.is_junction() for path in (target, *target.parents)
        ):
            raise ValueError("unsafe_optional_component_directory")
    with acquire_resource("local_heavy"):
        for target in targets:
            if target.exists():
                shutil.rmtree(target)
        (base / (component + ".json")).unlink(missing_ok=True)
    return {"status": "uninstalled", "component": component, "downloads_removed": bool(purge_downloads),
            "evidence_preserved": True}


def install_component(component, spec):
    """失败时清理本次新建的隔离环境，允许按相同锁重试。"""
    import shutil

    if component not in COMPONENTS:
        raise ValueError("unsupported_optional_component")
    base = receipt_root().resolve()
    target = base / "environments" / component
    if target.exists() or (base / (component + ".json")).exists():
        raise ValueError("optional_install_exists")
    try:
        return _install_component(component, spec)
    except BaseException:
        if (
            target.exists()
            and target.resolve().is_relative_to(base)
            and not target.is_symlink()
            and not target.is_junction()
        ):
            shutil.rmtree(target)
        raise


def _install_component(component, spec):
    """把经过审阅的离线 wheel/模型集合安装到独立环境，不修改核心 Python。"""
    import sys
    import tempfile

    if component not in COMPONENTS:
        raise ValueError("unsupported_optional_component")
    if spec.get("installation_acknowledged") is not True or spec.get("license_review") != "APPROVED":
        raise ValueError("reviewed_installation_required")
    if spec.get("weight_revision") != COMPONENTS[component]["weight_revision"]:
        raise ValueError("optional_component_role_revision_mismatch")
    for key in ("upstream_revision", "weight_revision", "requirements_sha256"):
        if not spec.get(key):
            raise ValueError("optional_component_identity_incomplete")
    if spec["requirements_sha256"] != COMPONENTS[component]["requirements_sha256"]:
        raise ValueError("optional_frozen_lock_identity_mismatch")
    if COMPONENTS[component]["model_required"] and not spec.get("model_files"):
        raise ValueError("local_model_manifest_required")
    lock, wheels = Path(spec["requirements_lock"]).resolve(), Path(spec["wheelhouse"]).resolve()
    if file_sha(lock) != spec["requirements_sha256"] or not wheels.is_dir():
        raise ValueError("optional_lock_identity_mismatch")
    base = receipt_root()
    target = base / "environments" / component
    if target.exists() or (base / (component + ".json")).exists():
        raise ValueError("optional_install_exists")
    model_root = Path(spec["model_root"]).resolve()
    for relative, expected in spec["model_files"].items():
        model = (model_root / relative).resolve()
        if not model.is_relative_to(model_root) or not model.is_file() or file_sha(model) != expected:
            raise ValueError("optional_model_identity_mismatch")
    # 只接受带 hash 的精确离线锁；pip 执行合同同时拒绝未列出的依赖。
    target.parent.mkdir(parents=True, exist_ok=True)
    base_python = os.environ.get("KR_OPTIONAL_BASE_PYTHON") or sys.executable
    version = silent_subprocess_run(
        [base_python, "-c", 'import sys;print("%d.%d" % sys.version_info[:2])'],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if version.stdout.strip() != "3.12":
        raise ValueError("optional_python_312_required")
    silent_subprocess_run([base_python, "-m", "venv", str(target)], check=True, capture_output=True, timeout=120)
    python = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    silent_subprocess_run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(wheels),
            "--require-hashes",
            "-r",
            str(lock),
        ],
        check=True,
        capture_output=True,
        timeout=1800,
    )
    freeze = silent_subprocess_run(
        [str(python), "-m", "pip", "freeze", "--all"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    frozen = target / "installed-freeze.txt"
    frozen.write_text(freeze.stdout, encoding="utf-8")
    packages = silent_subprocess_run(
        [
            str(python),
            "-c",
            'import importlib.metadata,json;print(json.dumps({d.metadata["Name"]:d.version for d in importlib.metadata.distributions()}))',
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    receipt = {
        "schema": "knowledgeradar-optional-install/v1",
        "component": component,
        "license_review": "APPROVED",
        "python": str(python),
        "python_sha256": file_sha(python),
        "freeze_path": str(frozen),
        "freeze_sha256": file_sha(frozen),
        "package_versions": json.loads(packages.stdout),
        "model_root": str(model_root),
        **{key: spec[key] for key in ("upstream_revision", "weight_revision", "model_files")},
    }
    for key in ("layout_model_root", "layout_model_files"):
        if key in spec:
            receipt[key] = spec[key]
    base.mkdir(parents=True, exist_ok=True)
    destination = base / (component + ".json")
    with tempfile.NamedTemporaryFile(dir=base, suffix=".pending", delete=False, mode="w", encoding="utf-8") as handle:
        temporary = Path(handle.name)
        json.dump(receipt, handle, ensure_ascii=False, indent=2)
    try:
        os.replace(temporary, destination)
        validate_receipt(component)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "status": "installed_identity_verified",
        "component": component,
        "receipt_sha256": file_sha(destination),
        "quality_status": "not_measured",
        "core_environment_modified": False,
    }
