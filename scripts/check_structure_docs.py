from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    script = ROOT / "scripts" / "generate_structure_docs.py"
    result = subprocess.run([sys.executable, str(script), "--check"], cwd=ROOT)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
