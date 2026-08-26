"""Start the stable product setup wizard from the active installation identity.

This is deliberately separate from ``setup_wizard.bat``: the latter remains a
source-checkout helper, while this entry point always writes the active product
data root selected by ``active.json``.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]


def default_install_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "KnowledgeRadar"


def load_active(install_root: Path) -> dict[str, str]:
    path = install_root / "active.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "knowledgeradar-active-install/v1":
        raise RuntimeError("unsupported active KnowledgeRadar installation")
    program_root = Path(str(payload.get("program_root") or "")).resolve()
    data_root = Path(str(payload.get("data_root") or "")).resolve()
    if program_root != ROOT.resolve() or not (program_root / "src" / "server.py").is_file():
        raise RuntimeError("this setup entry point is not the active KnowledgeRadar program")
    if not data_root.is_dir():
        raise RuntimeError("active KnowledgeRadar data root is unavailable")
    return {"program_root": str(program_root), "data_root": str(data_root)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start the active local KnowledgeRadar setup wizard.")
    parser.add_argument("--install-root", default=str(default_install_root()))
    parser.add_argument("--port", type=int, default=18882)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    active = load_active(Path(args.install_root).resolve())
    data_root = Path(active["data_root"])
    os.environ["KR_PROJECT_ROOT"] = active["program_root"]
    os.environ["KR_SOURCE_ROOT"] = str(Path(active["program_root"]) / "src")
    os.environ["KR_DATA_ROOT"] = str(data_root)
    os.environ["KR_RUNTIME_ENV_PATH"] = str(data_root / "config" / "runtime.env")
    os.environ["KR_STATE_DIR"] = str(data_root / "state")
    os.environ["KR_LOG_DIR"] = str(data_root / "logs")
    src = Path(active["program_root"]) / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from onboarding.setup_wizard import run_wizard

    run_wizard(port=args.port, open_browser=not args.no_open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
