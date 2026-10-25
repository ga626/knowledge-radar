"""Start the local-only KnowledgeRadar first-run configuration wizard."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the loopback-only KnowledgeRadar setup wizard.")
    parser.add_argument("--port", type=int, default=18882, help="正式网站固定使用 18882。")
    parser.add_argument("--no-open", action="store_true", help="Do not open the default browser.")
    args = parser.parse_args()
    if args.port != 18882:
        parser.error('正式入口固定为 18882。')
    install_root = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'KnowledgeRadar'
    helper = install_root / 'console_product.py'
    if not helper.is_file():
        raise RuntimeError('请先安装 KnowledgeRadar；开发入口使用明确选择的开发 checkout。')
    command = [sys.executable, str(helper), '--role', 'stable', '--port', '18882', '--install-root', str(install_root)]
    if args.no_open:
        command.append('--no-open')
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
