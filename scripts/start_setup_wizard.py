"""Start the local-only KnowledgeRadar first-run configuration wizard."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from onboarding.setup_wizard import run_wizard  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the loopback-only KnowledgeRadar setup wizard.")
    parser.add_argument("--port", type=int, default=0, help="127.0.0.1 port; 0 chooses a free port.")
    parser.add_argument("--no-open", action="store_true", help="Do not open the default browser.")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    run_wizard(port=args.port, open_browser=not args.no_open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
