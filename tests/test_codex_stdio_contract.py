from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def _load_script(name: str, filename: str | None = None):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{filename or name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generate_mcp_config_main = _load_script("generate_mcp_config", "generate-mcp-config").main
_setup_codex_product = _load_script("setup_codex_product")
_marketplace_path = _setup_codex_product._marketplace_path
_update_marketplace = _setup_codex_product._update_marketplace
_public_product = _load_script("verify_codex_product")
_stdio_probe = _public_product.stdio_probe


def test_direct_stdio_probe_exposes_research_surface() -> None:
    result = _stdio_probe()

    assert result["status"] == "PASS", result
    assert result["tool_count"] >= 22
    assert "kr_research" in result["tools"]
    assert "finalize_research_task" in result["tools"]
    assert "search_xiaohongshu" in result["tools"]


def test_marketplace_update_keeps_one_local_entry(tmp_path: Path) -> None:
    destination = tmp_path / "plugins" / "knowledgeradar-research"
    marketplace = _marketplace_path(destination)
    marketplace.parent.mkdir(parents=True)
    marketplace.write_text(
        json.dumps(
            {
                "name": "personal",
                "interface": {"displayName": "Personal"},
                "plugins": [
                    {"name": "knowledgeradar-research", "source": {"source": "local", "path": "./old"}},
                    {"name": "codex-praetor", "source": {"source": "local", "path": "./plugins/codex-praetor"}},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = _update_marketplace(destination)
    payload = json.loads(result.read_text(encoding="utf-8"))
    entries = [item for item in payload["plugins"] if item.get("name") == "knowledgeradar-research"]

    assert len(entries) == 1
    assert entries[0]["source"] == {"source": "local", "path": "./plugins/knowledgeradar-research"}
    assert any(item.get("name") == "codex-praetor" for item in payload["plugins"])


def test_codex_config_generator_uses_src_server_directly(monkeypatch, capsys, tmp_path: Path) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["generate-mcp-config.py", "--agent", "codex", "--root", str(tmp_path), "--python", "python"],
    )

    assert generate_mcp_config_main() == 0
    payload = json.loads(capsys.readouterr().out)
    config = payload["knowledgeradar"]

    assert config["type"] == "stdio"
    assert config["args"][-1] == str(tmp_path / "src" / "server.py")
    assert config["env"]["KR_MCP_TRANSPORT"] == "stdio"
    assert "kr_stdio_host.py" not in json.dumps(payload)
