"""Verify one generated release-candidate ZIP without using local user state."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    "README.md",
    "LICENSE",
    "pyproject.toml",
    "src/server.py",
    "scripts/setup_codex_product.py",
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


def request(process: subprocess.Popen[str], lines: queue.Queue[str], request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    assert process.stdin is not None
    process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
    process.stdin.flush()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            message = json.loads(lines.get(timeout=0.2).strip())
        except queue.Empty:
            if process.poll() is not None:
                raise RuntimeError(f"stdio server exited with {process.returncode}")
            continue
        except json.JSONDecodeError:
            continue
        if message.get("id") == request_id:
            if "error" in message:
                raise RuntimeError(json.dumps(message["error"], ensure_ascii=False))
            return message
    raise RuntimeError(f"timed out waiting for {method}")


def stdio_probe(package_root: Path) -> list[str]:
    env = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix="knowledgeradar-candidate-state-") as state:
        env.update({
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(package_root / "src"), "KR_MCP_TRANSPORT": "stdio",
            "KR_PROJECT_ROOT": str(package_root), "KR_SOURCE_ROOT": str(package_root / "src"),
            "KR_STATE_DIR": state, "KR_LOG_DIR": str(Path(state) / "logs"),
        })
        process = subprocess.Popen([sys.executable, "-X", "utf8", str(package_root / "src" / "server.py")], cwd=package_root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        lines: queue.Queue[str] = queue.Queue()
        errors: queue.Queue[str] = queue.Queue()
        try:
            assert process.stdout is not None
            threading.Thread(target=lambda: [lines.put(line) for line in iter(process.stdout.readline, "")], daemon=True).start()
            assert process.stderr is not None
            threading.Thread(target=lambda: [errors.put(line) for line in iter(process.stderr.readline, "")], daemon=True).start()
            request(process, lines, 1, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "kr-release-candidate-verifier", "version": "1"}})
            response = request(process, lines, 2, "tools/list", {})
            return sorted(item["name"] for item in response.get("result", {}).get("tools", []) if isinstance(item, dict) and item.get("name"))
        except RuntimeError as exc:
            tail: list[str] = []
            while not errors.empty():
                tail.append(errors.get_nowait().strip())
            detail = " ".join(item for item in tail[-8:] if item)
            raise RuntimeError(f"{exc}; stderr: {detail[:1200]}") from exc
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()


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
        tools = stdio_probe(package_root)
    missing_tools = sorted(EXPECTED_TOOLS - set(tools))
    if missing_tools:
        raise RuntimeError(f"candidate stdio surface missing tools: {', '.join(missing_tools)}")
    return {"archive": archive.name, "archive_sha256": sha256_file(archive), "tool_count": len(tools), "tools": tools, "status": "PASS"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a KnowledgeRadar release candidate.")
    parser.add_argument("--candidate-dir", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = verify(Path(args.candidate_dir).resolve())
    except (ImportError, OSError, RuntimeError, KeyError, TypeError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        result = {"status": "FAIL", "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
