from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import runtime.quality_gates as quality_gates


def test_run_command_returns_structured_timeout(monkeypatch, tmp_path: Path) -> None:
    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["python", "-m", "pytest", "-q"],
            timeout=420,
            output="partial stdout",
            stderr="partial stderr",
        )

    monkeypatch.setattr(quality_gates, "silent_subprocess_run", fake_run)

    result = quality_gates.run_command(["python", "-m", "pytest", "-q"], tmp_path, timeout=420)

    assert result["status"] == "fail"
    assert result["returncode"] == 124
    assert result["failure_type"] == "timeout"
    assert result["timeout_s"] == 420
    assert result["stdout"] == "partial stdout"
    assert result["stderr"] == "partial stderr"
