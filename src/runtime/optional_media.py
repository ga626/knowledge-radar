"""可选组件的同进程执行入口；推理不安装或下载。"""

from __future__ import annotations
import hashlib
import json
import os
import re
import time
from pathlib import Path
from functools import lru_cache
from runtime.paths import runtime_state_dir
from runtime.process import silent_subprocess_run
from runtime.model_roles import COMPONENTS, CLOUD_OCR_COMPONENT


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
    failure = ''
    if receipt:
        try:
            validated = validate_receipt(component)
            from runtime.component_environment import component_environment
            component_environment(component, validated)
            ready = True
        except (ValueError, OSError, KeyError) as exc:
            failure = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
    return {
        "schema": "knowledgeradar-optional-media/v1",
        "component": component,
        **COMPONENTS[component],
        "status": "ready" if ready else "incompatible" if path.is_file() else "not_installed",
        "failure_type": failure,
        "next_action": 'prepare_verified_replacement' if path.is_file() and not ready else 'none' if ready else 'prepare_component',
        "ready": ready,
        "receipt_present": path.is_file(),
        "automatic_install": False,
        "automatic_model_download": False,
    }


def validate_receipt(component, *, receipt_value=None):
    if component not in COMPONENTS:
        raise ValueError("unsupported_optional_component")
    path = receipt_root() / (component + ".json")
    if receipt_value is None and not path.is_file():
        raise ValueError("optional_component_not_installed_and_approved")
    receipt = receipt_value if receipt_value is not None else json.loads(path.read_text(encoding="utf-8"))
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
    if not (python.parent.parent / "pyvenv.cfg").is_file() and not (python.parent / "pyvenv.cfg").is_file():
        raise ValueError("isolated_environment_required")
    root = Path(receipt["model_root"]).resolve()
    manifest = receipt.get("model_files") or {}
    required = COMPONENTS[component].get('model_files')
    if required and manifest != required:
        raise ValueError('optional_model_role_manifest_mismatch')
    if not manifest and COMPONENTS[component]["model_required"]:
        raise ValueError("local_model_manifest_required")
    for relative, digest in manifest.items():
        model = (root / relative).resolve()
        if not model.is_relative_to(root) or not model.is_file() or file_sha(model) != digest:
            raise ValueError("optional_model_identity_mismatch")
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
        if component not in COMPONENTS and component not in {"pypdf", CLOUD_OCR_COMPONENT}:
            raise ValueError("unsupported_optional_component")
        return component
    suffix = source.suffix.lower()
    if suffix == ".pdf":
        # PDF completion belongs to the shared page-aware processor, including scans.
        return 'pypdf'
    if suffix in {".docx", ".xlsx", ".pptx", ".html", ".htm", ".md"}:
        return "docling"
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}:
        return CLOUD_OCR_COMPONENT
    if suffix in {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".mp4", ".webm", ".aac"}:
        return "qwen3-asr-0.6b"
    raise ValueError("unsupported_local_evidence_format")


def run_optional(payload):
    started = time.monotonic()
    source = approved_source(payload)
    component = select_component(payload)
    validation_s = round(time.monotonic()-started, 3)
    if component == CLOUD_OCR_COMPONENT:
        from runtime.cloud_ocr import run_cloud_ocr
        result = run_cloud_ocr(source, timeout_s=float(payload.get('timeout_s', 600)))
        result['execution_timings'] = {'input_validation_s': validation_s, 'provider_s': result['elapsed_s']}
        return _save_result(result, payload, started=started)
    if component == "pypdf":
        from runtime.pdf_text import process_pdf
        options = payload.get('document') or {}
        parsed = process_pdf(str(source), timeout_s=float(payload.get('timeout_s', 600)),
            page_numbers=options.get('page_numbers'),allow_ocr=options.get('include_ocr') is not False,
            enhance_suspicious=options.get('include_ocr') is True,storage_allowed=options.get('storage_allowed',True))
        if not parsed['text'].strip():
            code = 'pdf_text_unavailable_ocr_not_requested' if options.get('include_ocr') is False else 'pdf_text_unavailable'
            return {'status': 'failed', 'failure_type': code, 'request_satisfied': False,
                    'error': {'type': code, 'message': 'PDF没有可用文字层；本次未请求OCR。' if options.get('include_ocr') is False else 'PDF未取得可用正文，请检查文字层及OCR处理结果。'},
                    'component': component, 'ocr_status': parsed.get('ocr_status', 'not_requested'),
                    'evidence_assets': [], 'automatic_retry': False}
        return _save_result({'status': 'acquired', 'component': component, **parsed,
                             'content_locators': parsed['locators'], 'source_kind': 'pdf_text',
                             'missing_text_pages':parsed.get('missing_text_pages', parsed['empty_pages']),
                             'ocr_recommendation':'inspect_ocr_gap' if not parsed.get('request_satisfied') else 'not_needed'}, payload, started=started)
    receipt_started = time.monotonic()
    receipt = validate_receipt(component)
    receipt_validation_s = round(time.monotonic()-receipt_started, 3)
    from runtime.asr_policy import AsrPolicy

    language = AsrPolicy.for_request(payload.get("language")).language if COMPONENTS[component]['kind'] == 'asr' else ''
    from runtime.component_environment import component_environment
    component_environment(component, receipt, activate=True)
    threads = max(1, min(8, int(os.environ.get('KR_LOCAL_MODEL_THREADS', '4'))))
    request = {'component': component, 'input_path': str(source), 'input_sha256': payload['input_sha256'],
               'language': language, 'model_root': receipt['model_root'], 'expected_packages': {},
               'model_identity': file_sha(receipt_root() / (component + '.json')),
               'device': 'cpu', 'threads': threads}
    from runtime.resource_concurrency import acquire_resource
    from runtime.execution_context import record_execution_progress
    record_execution_progress('waiting_resource')
    with acquire_resource("local_heavy") as lease:
        record_execution_progress('processing', resource_wait_s=round(float(lease.get('lease_wait_s',0))+float(lease.get('wait_s',0)),3))
        worker_started = time.monotonic()
        from runtime.optional_media_worker import execute
        from runtime.execution_context import check_execution
        check_execution()
        result = execute(request)
        check_execution()
        worker_s = round(time.monotonic()-worker_started, 3)
    # 模型对有效音频明确返回无语音时，没有文字是正常结果；不能将解码失败/未知空输出当成功。
    metrics = result.get('asr_metrics') or {}
    no_speech = (component == 'qwen3-asr-0.6b' and result.get('speech_status') == 'no_speech_detected'
                 and metrics.get('chunks', 0) > 0 and metrics.get('audio_duration_s', 0) > 0)
    if result.get('status') not in ({'acquired', 'partial'} if component == 'docling' else {'acquired'}) or (not str(result.get('text') or '').strip() and not no_speech):
        raise ValueError('optional_component_empty_or_failed')
    result['execution_timings'] = {'input_validation_s': validation_s,
        'receipt_validation_s': receipt_validation_s, 'processing_s': worker_s,
        'resource_wait_s': round(float(lease.get('lease_wait_s', 0)) + float(lease.get('wait_s', 0)), 3)}
    return _save_result(result, payload, receipt=receipt, started=started)


def _save_result(result, payload, *, receipt=None, started=None):
    from kr_core.evidence_store import EvidenceStore
    from kr_core.evidence_coverage import content_coverage

    result["content"] = result["text"]
    component = result["component"]
    result['input_sha256'] = payload['input_sha256']
    if component in {'pypdf', 'docling'}:
        result['document'] = {key: result[key] for key in (
            'input_sha256', 'page_count', 'pages_read', 'requested_pages', 'page_ledger',
            'request_satisfied', 'source_pages_exhausted', 'processing_identity', 'resume',
            'original_asset', 'coverage', 'processing_method', 'missing_text_pages',
            'quality_warnings', 'document_audit') if key in result}
    result["collector"] = component
    result['input_sha256'] = payload['input_sha256']
    result['coverage'] = result.get('coverage') or ('processed_audio_chunks' if result.get('source_kind') == 'asr' else 'partial')
    result['content_coverage'] = content_coverage(result)
    result['content_coverage']['parts']['content']['coverage'] = result['coverage']
    if component == 'docling' and result.get('request_satisfied') is False:
        result['content_coverage']['request_satisfied'] = False
        result['content_coverage']['required_gaps'] = list(dict.fromkeys([*result['content_coverage'].get('required_gaps', []), 'content']))
        result['content_coverage']['parts']['content']['missing_reason'] = 'docx_table_retention_unverified'
    # 新归档合同生成新证据版本；不改写旧版本的未知覆盖结论。
    result['detail_metadata'] = {**(result.get('detail_metadata') or {}),
                                 'strategy': ('local-evidence/page-ledger-ocr-v4:' if component == 'pypdf' else 'local-evidence/docx-retention-v3:' if component == 'docling' else 'local-evidence/v2:') + component}
    identity_url = "https://local-evidence.invalid/sha256/" + payload["input_sha256"]
    archive_started = time.monotonic()
    EvidenceStore().record_detail(
        platform="local_asr" if component.startswith("qwen3-asr") else "local_document",
        url=identity_url,
        response=result,
    )
    result.setdefault('execution_timings', {})['evidence_archive_s'] = round(time.monotonic()-archive_started, 3)
    if started is not None:
        result['execution_timings']['total_s'] = round(time.monotonic()-started, 3)
    return {
        **result,
        "input_sha256": payload["input_sha256"],
        "install_receipt_sha256": file_sha(receipt_root() / (component + ".json")) if receipt else "",
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
    targets.extend(path for path in (base / 'environments').glob(component + '-*')
                   if re.fullmatch(re.escape(component) + r'-[0-9a-f]{32}', path.name))
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
    """显式安装或替换过期职责；失败恢复旧环境/回执，不在推理时升级。"""
    import shutil
    import uuid
    from runtime.file_write_lock import file_write_lock
    from runtime.resource_concurrency import acquire_resource
    if component not in COMPONENTS:
        raise ValueError('unsupported_optional_component')
    if spec.get('installation_acknowledged') is not True or spec.get('license_review') != 'APPROVED':
        raise ValueError('reviewed_installation_required')
    base = receipt_root().resolve()
    target, receipt = base / 'environments' / (component + '-' + uuid.uuid4().hex), base / (component + '.json')
    for path in (target, receipt):
        if not path.resolve().is_relative_to(base) or any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
            raise ValueError('unsafe_optional_component_directory')
    base.mkdir(parents=True, exist_ok=True)
    with acquire_resource('local_heavy'), file_write_lock(base / (component + '.install')):
        replacing = receipt.exists()
        old = None
        if replacing:
            old = json.loads(receipt.read_text(encoding='utf-8'))
            if old.get('schema') != 'knowledgeradar-optional-install/v1' or old.get('component') != component:
                raise ValueError('invalid_optional_install_receipt')
            if old.get('weight_revision') == COMPONENTS[component]['weight_revision']:
                try:
                    validate_receipt(component)
                except ValueError as error:
                    # 模型相同仍可能需要新依赖；损坏或身份异常不作为自动替换理由。
                    if str(error) not in {'optional_component_locked_dependency_mismatch',
                                         'optional_component_role_package_mismatch'}:
                        raise
                else:
                    return {'status': 'installed_identity_verified', 'component': component, 'already_installed': True}
        try:
            result = _install_component(component, spec, target=target, publish=False)
            candidate = result.pop('candidate_receipt')
            validate_receipt(component, receipt_value=candidate)
            from runtime.component_environment import component_environment
            component_environment(component, candidate)
            if old:
                candidate['previous_receipt'] = {k:v for k,v in old.items() if k != 'previous_receipt'}
            from runtime.state_file import write_state
            write_state(receipt, candidate)
        except BaseException:
            if target.exists():
                shutil.rmtree(target)
            raise
        return {**result, 'receipt_sha256':file_sha(receipt), 'previous_environment_retained':replacing,
                'activation':'atomic_receipt_switch'}


def rollback_component(component):
    """显式回退上次固定组件；旧职责不兼容时保留真实状态，不伪造新身份。"""
    from runtime.file_write_lock import file_write_lock
    from runtime.resource_concurrency import acquire_resource
    from runtime.state_file import write_state
    if component not in COMPONENTS:
        raise ValueError('unsupported_optional_component')
    base = receipt_root().resolve()
    with acquire_resource('local_heavy'), file_write_lock(base / (component + '.install')):
        path = base / (component + '.json')
        current = json.loads(path.read_text(encoding='utf-8'))
        previous = current.get('previous_receipt')
        if not isinstance(previous, dict) or previous.get('component') != component:
            raise ValueError('previous_component_receipt_unavailable')
        python = Path(previous.get('python') or '').resolve()
        if not python.is_relative_to(base / 'environments') or not python.is_file() or file_sha(python) != previous.get('python_sha256'):
            raise ValueError('previous_component_identity_mismatch')
        write_state(path, previous)
    return {'status':'rolled_back', 'component':component, 'readiness':component_plan(component), 'automatic_retry':False}


def _install_component(component, spec, *, target=None, publish=True):
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
    required = COMPONENTS[component].get('model_files')
    if required and spec.get('model_files') != required:
        raise ValueError('optional_model_role_manifest_mismatch')
    lock, wheels = Path(spec["requirements_lock"]).resolve(), Path(spec["wheelhouse"]).resolve()
    if file_sha(lock) != spec["requirements_sha256"] or not wheels.is_dir():
        raise ValueError("optional_lock_identity_mismatch")
    base = receipt_root()
    target = target or base / "environments" / component
    if target.exists() or publish and (base / (component + ".json")).exists():
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
    if not publish:
        return {'status':'installed_identity_verified', 'component':component, 'candidate_receipt':receipt,
                'quality_status':'not_measured', 'core_environment_modified':False}
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
