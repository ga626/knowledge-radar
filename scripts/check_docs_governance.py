from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
CURRENT_REPORT_QUALITY_CONTRACT_FROM = date.fromisoformat("2026-06-17")
STABLE_REPORTS = [
    DOCS / "reference" / "知识雷达文档治理.md",
    DOCS / "reference" / "感知层自然调用机制.md",
]
_manifest = json.loads((ROOT / 'config/project-structure.manifest.json').read_text(encoding='utf-8'))
if 'stable_research_reports' in _manifest:
    STABLE_REPORTS = [ROOT / item for item in _manifest['stable_research_reports']]


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _run(command: list[str]) -> dict[str, Any]:
    proc = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    return {
        "command": " ".join(command),
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def _sidecar_generated_date(evidence: Path) -> date | None:
    try:
        payload = json.loads(evidence.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    generated_at = str(payload.get("generated_at") or (payload.get("artifact") or {}).get("created_at") or "")
    if len(generated_at) < 10:
        return None
    try:
        return date.fromisoformat(generated_at[:10])
    except ValueError:
        return None


def _check_sidecars() -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for report in STABLE_REPORTS:
        evidence = report.with_suffix(".evidence.json")
        if not report.is_file():
            results.append({"status": "FAIL", "report": str(report), "reason": "report_missing"})
            continue
        if not evidence.is_file():
            results.append({"status": "FAIL", "report": str(report), "evidence": str(evidence), "reason": "evidence_missing"})
            continue
        generated_date = _sidecar_generated_date(evidence)
        if generated_date and generated_date < CURRENT_REPORT_QUALITY_CONTRACT_FROM:
            results.append(
                {
                    "status": "LEGACY_SKIPPED",
                    "report": report.relative_to(ROOT).as_posix(),
                    "evidence": evidence.relative_to(ROOT).as_posix(),
                    "reason": "before_current_report_quality_contract",
                    "generated_date": generated_date.isoformat(),
                }
            )
            continue
        proc = _run(
            [
                sys.executable,
                str(ROOT / "scripts" / "check_research_quality.py"),
                "--report",
                str(report),
                "--evidence",
                str(evidence),
                "--json",
            ]
        )
        status = "FAIL"
        payload: dict[str, Any] = {}
        try:
            payload = json.loads(proc["stdout"])
            status = "PASS" if payload.get("status") == "pass" else "FAIL"
        except Exception:
            pass
        results.append(
            {
                "status": status,
                "report": report.relative_to(ROOT).as_posix(),
                "evidence": evidence.relative_to(ROOT).as_posix(),
                "quality_status": payload.get("status"),
                "returncode": proc["returncode"],
            }
        )
    return results


def run_checks() -> dict[str, Any]:
    commands = [
        _run([sys.executable, str(ROOT / "scripts" / "generate_docs_index.py"), "--check", "--json"]),
        _run([sys.executable, str(ROOT / "scripts" / "generate_script_lifecycle_inventory.py"), "--check", "--json"]),
        _run([sys.executable, str(ROOT / "scripts" / "generate_process_docs_index.py"), "--check", "--json"]),
    ]
    sidecars = _check_sidecars()
    failures = [item for item in commands if item["returncode"] != 0] + [item for item in sidecars if item["status"] == "FAIL"]
    return {
        "schema": "knowledgeradar-docs-governance-check/v1",
        "status": "PASS" if not failures else "FAIL",
        "commands": commands,
        "stable_reports": sidecars,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = run_checks()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Docs governance: {result['status']}")
        for item in result["stable_reports"]:
            print(f"- {item.get('report')}: {item.get('status')}")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
