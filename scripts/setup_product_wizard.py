"""Start the stable product setup wizard from the active installation identity.

This is deliberately separate from ``setup_wizard.bat``: the latter remains a
source-checkout helper, while this entry point always writes the active product
data root selected by ``active.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
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
    if args.port != 18882:
        parser.error('正式入口固定为 18882。')
    install_root = Path(args.install_root).resolve()
    load_active(install_root)
    helper = install_root / 'console_product.py'
    if not helper.is_file():
        raise RuntimeError('稳定安装缺少统一启动入口，请运行安装修复。')
    command = [sys.executable, str(helper), '--role', 'stable', '--port', '18882', '--install-root', str(install_root)]
    if args.no_open:
        command.append('--no-open')
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
