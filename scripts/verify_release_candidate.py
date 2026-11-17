"""Verify one generated release-candidate ZIP without using local user state."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    "README.md",
    "LICENSE",
    "pyproject.toml",
    "src/server.py",
    "scripts/product_install.py",
    "config/codex-product/plugin/knowledgeradar-research/.codex-plugin/plugin.json",
    "package-provenance.json",
    "SBOM.json",
}
EXPECTED_TOOLS = {"health_check", "get_capabilities", "kr_research", "finalize_research_task"}


def package_content_issues(package_root: Path) -> list[str]:
    script = ROOT / "scripts" / "verify_package_integrity.py"
    spec = importlib.util.spec_from_file_location("knowledgeradar_package_integrity", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load package-integrity verifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.check_package_content(package_root)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_runtime_inputs(package_root: Path) -> None:
    metadata = json.loads((package_root / "config/runtime-artifacts.json").read_text(encoding="utf-8"))
    for relative, key in (("pyproject.toml", "pyproject_sha256"), ("config/runtime-win-py312.lock", "lock_sha256")):
        if sha256_file(package_root / relative) != metadata.get(key):
            raise RuntimeError("candidate runtime lock identity mismatch: " + relative)


def safe_extract(archive: Path, destination: Path) -> Path:
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if any(Path(name).is_absolute() or ".." in Path(name).parts for name in names):
            raise RuntimeError("archive contains unsafe paths")
        roots = {Path(name).parts[0] for name in names if Path(name).parts}
        if roots != {"KnowledgeRadar"}:
            raise RuntimeError("archive must contain one KnowledgeRadar root")
        bundle.extractall(destination)
    return destination / "KnowledgeRadar"


def stdio_probe(package_root: Path) -> list[str]:
    """Use the same isolated owner lifetime as the public source verifier."""
    scripts_path = str(ROOT / "scripts")
    previous_path = list(sys.path)
    try:
        sys.path.insert(0, scripts_path)
        from verify_codex_product import stdio_probe as isolated_stdio_probe

        result = isolated_stdio_probe(package_root)
    finally:
        sys.path[:] = previous_path
    if result.get("status") != "PASS":
        raise RuntimeError("candidate stdio probe failed: " + str(result.get("error", result)))
    return result["tools"]


def business_probe(package_root: Path) -> dict[str, object]:
    """从解包源码验证固定证据→双端归档；只写临时目录，不联网或连接正式库。"""
    code = r'''
import json, sys
from pathlib import Path
sys.path[:] = [str(Path.cwd()/'src')] + [p for p in sys.path if p and 'public-repos' not in p]
from runtime.research_archive import ResearchArchive, BindContext, SubmitReport
from runtime.knowledge_delivery import DeliveryStore, Authorize
from runtime import research_ledger as ledger
from kr_core.evidence_assets import EvidenceAssets
def packaged_modules_only():
    source=(Path.cwd()/'src').resolve()
    assert all(Path(m.__file__).resolve().is_relative_to(source) for name,m in sys.modules.items()
        if name.split('.')[0] in {'runtime','kr_core'} and getattr(m,'__file__',None)), 'business module escaped extracted package'
packaged_modules_only()
state=Path(sys.argv[1]); state.mkdir(exist_ok=True)
assets=EvidenceAssets(state/'evidence_assets')
store=DeliveryStore(state,assets); engine=ResearchArchive(store)
local=state/'reports'; vault=state/'vault'; local.mkdir(); vault.mkdir()
grants=[store.authorize(Authorize(action='authorize_destination',approved_root=str(p),project_id='candidate-probe'))['grant_id'] for p in (local,vault)]
ledger.open_task(objective='candidate offline probe',budget='fast',considered=[],task_id='candidate-probe',project_id='candidate-probe',project_name='candidate-probe')
engine.bind(BindContext(action='bind_context',task_id='candidate-probe',project_id='candidate-probe',project_name='candidate-probe',task_title='candidate-probe',local_grant_id=grants[0],local_report_path=str(local/'report.md'),vault_grant_id=grants[1]))
text='candidate fixed evidence tail: preserved'
ref=assets.put('https://example.invalid/offline-probe',text,title='offline probe',metadata={'coverage':'complete'})
assets.reference_many([ref['version_id']],'candidate-probe',project_id='candidate-probe',task_id='candidate-probe')
assert assets.fetch(ref['version_id'])['text']==text
report='# Candidate probe\nOffline archive check.\n'
engine.submit(SubmitReport(action='submit_report',task_id='candidate-probe',project_id='candidate-probe',title='Candidate probe',content=report))
assert (local/'report.md').read_text(encoding='utf-8')==report
assert any(p.read_text(encoding='utf-8')==report for p in vault.rglob('*.md'))
assert any(text in p.read_text(encoding='utf-8') for p in vault.rglob('*.md'))
packaged_modules_only()
print(json.dumps({'status':'PASS','evidence_readback':True,'local_and_vault_delivery':True}))
'''
    with tempfile.TemporaryDirectory(prefix='kr-candidate-business-') as temp:
        state=Path(temp)
        # 沿用离线执行器的环境隔离合同，但不从源码树导入业务模块。
        env={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','PATH','PATHEXT','TEMP','TMP','COMSPEC'}}
        env.update(PYTHONPATH=str(package_root/'src'), PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1',
            HOME=temp, USERPROFILE=temp, APPDATA=temp, LOCALAPPDATA=temp, CODEX_HOME=str(state/'codex'),
            KR_PROJECT_ROOT=str(package_root), KR_STATE_DIR=str(state/'state'), KR_LOG_DIR=str(state/'logs'),
            KR_DATA_ROOT=str(state), KR_EVIDENCE_ROOT=str(state/'state/evidence_assets'),
            KR_EVALUATION_OFFLINE='1')
        try:
            process=subprocess.run([sys.executable,'-X','utf8','-c',code,str(state/'state')],
                cwd=package_root,env=env,capture_output=True,text=True,encoding='utf-8',timeout=60)
            if process.returncode:
                raise RuntimeError('candidate business probe failed: '+process.stderr[-2000:])
            return json.loads(process.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise RuntimeError('candidate business probe failed: '+type(exc).__name__) from exc


def verify(candidate_dir: Path) -> dict[str, object]:
    archive = candidate_dir / "KnowledgeRadar.zip"
    receipt_path = candidate_dir / "candidate-receipt.json"
    if not archive.is_file() or not receipt_path.is_file():
        raise RuntimeError("candidate archive or receipt is missing")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("source_dirty") is not False or receipt.get("archive_sha256") != sha256_file(archive):
        raise RuntimeError("candidate receipt does not bind the clean archive")
    with tempfile.TemporaryDirectory(prefix="knowledgeradar-candidate-extract-") as temp:
        package_root = safe_extract(archive, Path(temp))
        missing = sorted(item for item in REQUIRED if not (package_root / item).is_file())
        if missing:
            raise RuntimeError(f"candidate is missing required files: {', '.join(missing)}")
        provenance = json.loads((package_root / "package-provenance.json").read_text(encoding="utf-8"))
        if provenance.get("source_dirty") is not False or "source_root" in provenance:
            raise RuntimeError("candidate provenance exposes source state or local path")
        issues = package_content_issues(package_root)
        if issues:
            raise RuntimeError("candidate contains private-content patterns: " + "; ".join(issues))
        verify_runtime_inputs(package_root)
        tools = stdio_probe(package_root)
        business = business_probe(package_root)
    missing_tools = sorted(EXPECTED_TOOLS - set(tools))
    if missing_tools:
        raise RuntimeError(f"candidate stdio surface missing tools: {', '.join(missing_tools)}")
    return {"archive": archive.name, "archive_sha256": sha256_file(archive), "tool_count": len(tools), "tools": tools, "business":business, "status": "PASS"}


def main(argv: list[str] | None = None) -> int:
    import time
    from datetime import datetime, timezone
    started_at, clock = datetime.now(timezone.utc).isoformat(), time.monotonic()
    parser = argparse.ArgumentParser(description="Verify a KnowledgeRadar release candidate.")
    parser.add_argument("--candidate-dir", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = verify(Path(args.candidate_dir).resolve())
    except (ImportError, OSError, RuntimeError, KeyError, TypeError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        result = {"status": "FAIL", "error": str(exc)}
    result['timing'] = {'started_at':started_at, 'finished_at':datetime.now(timezone.utc).isoformat(), 'elapsed_s':time.monotonic()-clock}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
