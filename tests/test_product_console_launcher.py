from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from onboarding.setup_wizard import WizardServer  # noqa: E402


def load_launcher():
    path = ROOT / "scripts" / "product_console_launcher.py"
    spec = importlib.util.spec_from_file_location("product_console_launcher_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_existing_known_console_is_reused_without_a_second_process(monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = load_launcher()
    server = WizardServer(("127.0.0.1", 0))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    opened: list[str] = []
    try:
        monkeypatch.setattr(launcher, "_start_background", lambda port: pytest.fail("must reuse the known console"))
        monkeypatch.setattr(launcher.webbrowser, "open", opened.append)

        url = launcher.open_console(port=server.server_port, restart=False, open_browser=True)

        assert opened == [url]
        parsed = urlparse(url)
        assert parsed.hostname == "127.0.0.1"
        assert parsed.port == server.server_port
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_foreign_fixed_port_is_reported_instead_of_replacing_it(monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = load_launcher()
    monkeypatch.setattr(launcher, "_health", lambda port: "foreign")

    with pytest.raises(RuntimeError, match="其他程序占用"):
        launcher.open_console(port=18882, restart=False, open_browser=False)


def test_autostart_is_visible_and_never_embeds_user_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = load_launcher()
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setattr(launcher.sys, "executable", str(tmp_path / "runtime" / "python.exe"))

    assert launcher.set_autostart(enabled=True) == "enabled"

    startup = tmp_path / "roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / launcher.STARTUP_FILENAME
    text = startup.read_text(encoding="utf-8")
    assert "--serve --port 18882 --no-open" in text
    assert "runtime.env" not in text
    assert "browser_data" not in text
    assert launcher.set_autostart(enabled=False) == "disabled"
    assert not startup.exists()


def test_version_neutral_entry_starts_restarts_and_stops_the_real_loopback_host(tmp_path: Path) -> None:
    launcher = load_launcher()
    install_root = tmp_path / "install"
    data_root = tmp_path / "data"
    install_root.mkdir()
    (data_root / "config").mkdir(parents=True)
    (data_root / "config" / "runtime.env").write_text("# isolated test runtime\n", encoding="utf-8")
    helper = install_root / "console_product.py"
    shutil.copyfile(ROOT / "scripts" / "product_console_launcher.py", helper)
    (install_root / "active.json").write_text(
        json.dumps(
            {
                "schema": "knowledgeradar-active-install/v1",
                "program_root": str(ROOT),
                "data_root": str(data_root),
            }
        ),
        encoding="utf-8",
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    command = [sys.executable, str(helper), "--port", str(port), "--no-open"]
    try:
        first = subprocess.run(command, text=True, encoding="utf-8", capture_output=True, check=False, timeout=15)
        assert first.returncode == 0, first.stderr
        assert launcher._health(port) == "ready"

        restarted = subprocess.run(command + ["--restart"], text=True, encoding="utf-8", capture_output=True, check=False, timeout=15)
        assert restarted.returncode == 0, restarted.stderr
        assert launcher._health(port) == "ready"
    finally:
        if launcher._health(port) == "ready":
            launcher._request_stop(port)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and launcher._health(port) == "ready":
            time.sleep(0.1)
    assert launcher._health(port) == "absent"
