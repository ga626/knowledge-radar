"""显式设置工作树导入优先级，兼容忽略 PYTHONPATH 的 Windows 嵌入式 Python。"""
from pathlib import Path
import os
import sys
import tempfile

if __name__ == "__main__":
    root = Path.cwd()
    isolated = Path(tempfile.mkdtemp(prefix="kr-eval-tests-"))
    os.environ.setdefault("KR_STATE_DIR", str(isolated / "runtime"))
    os.environ.setdefault("KR_DATA_ROOT", str(isolated / "data"))
    os.environ.setdefault("KR_DESKTOP_NOTIFICATIONS_DISABLED", "1")
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "src"))
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    import pytest
    raise SystemExit(pytest.main(sys.argv[1:]))
