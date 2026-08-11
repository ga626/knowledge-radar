from __future__ import annotations

import importlib.util
import base64
import json
from pathlib import Path

import pytest

from runtime import continuity_fallback


ROOT = Path(__file__).resolve().parents[1]


def _config(tmp_path: Path, *, server_path: Path | None = None) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    actual_server = server_path or (root / "src" / "server.py")
    actual_server.write_text("# server\n", encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join(
            [
                "[mcp_servers.knowledgeradar]",
                'command = "python"',
                f'args = ["-X", "utf8", "{actual_server.as_posix()}"]',
                f'cwd = "{root.as_posix()}"',
                "enabled = true",
                "[mcp_servers.knowledgeradar.env]",
                'PYTHONPATH = "src"',
            ]
        ),
        encoding="utf-8",
    )
    return config


def test_configured_stdio_server_accepts_only_current_project_source(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = continuity_fallback.configured_stdio_server(config_path=config, project_root=tmp_path / "repo")
    assert result["command"] == "python"
    assert result["cwd"] == str((tmp_path / "repo").resolve())
    assert result["env"] == {"PYTHONPATH": "src"}


def test_configured_stdio_server_rejects_different_source_root(tmp_path: Path) -> None:
    other = tmp_path / "other.py"
    other.write_text("# other\n", encoding="utf-8")
    config = _config(tmp_path, server_path=other)
    with pytest.raises(continuity_fallback.FallbackContractError, match="does_not_target_current_project_source"):
        continuity_fallback.configured_stdio_server(config_path=config, project_root=tmp_path / "repo")


def test_invoke_uses_validated_config_and_returns_only_config_identity(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(
        continuity_fallback,
        "_invoke_sync",
        lambda server, tool, arguments: {"result": {"ok": True}, "tools": [tool], "tool_list_fingerprint": "sha256:test", "mcp_call_status": "ok"},
    )
    result = continuity_fallback.invoke_configured_tool(
        tool="health_check",
        arguments={"mode": "summary"},
        config_path=config,
        project_root=tmp_path / "repo",
    )
    assert result["result"] == {"ok": True}
    assert result["server"]["config_path"] == str(config.resolve())
    assert "env" not in result["server"]


def test_continuity_cli_does_not_mark_mcp_tool_error_as_success(monkeypatch, capsys) -> None:
    module = _continuity_cli_module()
    monkeypatch.setattr(module, "record_fallback", lambda **_kwargs: {})
    monkeypatch.setattr(module, "source_fingerprint", lambda _root: "source-test")
    monkeypatch.setattr(
        module,
        "invoke_configured_tool",
        lambda **_kwargs: {"result": {"isError": True}, "tool_list_fingerprint": "sha256:tools", "mcp_call_status": "error"},
    )
    monkeypatch.setattr(module, "record_fallback_call", lambda **kwargs: {"access_path": "continuity_fallback", **kwargs})
    assert module.main(["call", "--reason", "transport_closed", "--tool", "health_check"]) == 1
    assert '"status": "tool_error"' in capsys.readouterr().out


def _continuity_cli_module():
    spec = importlib.util.spec_from_file_location("kr_mcp_continuity_test", ROOT / "scripts" / "kr_mcp_continuity.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_continuity_cli_requires_task_for_non_readiness_call(capsys) -> None:
    module = _continuity_cli_module()
    assert module.main(["call", "--reason", "transport_closed", "--tool", "kr_web_search"]) == 2
    assert "task-id is required" in capsys.readouterr().out


def test_continuity_cli_returns_explicit_non_native_receipt(monkeypatch, capsys) -> None:
    module = _continuity_cli_module()
    monkeypatch.setattr(module, "record_fallback", lambda **_kwargs: {})
    monkeypatch.setattr(module, "source_fingerprint", lambda _root: "source-test")
    monkeypatch.setattr(
        module,
        "invoke_configured_tool",
        lambda **_kwargs: {"result": {"status": "ok"}, "tool_list_fingerprint": "sha256:tools", "mcp_call_status": "ok"},
    )
    monkeypatch.setattr(module, "record_fallback_call", lambda **kwargs: {"access_path": "continuity_fallback", **kwargs})
    assert module.main(["call", "--reason", "transport_closed", "--tool", "health_check", "--argument", "mode=summary"]) == 0
    output = capsys.readouterr().out
    assert '"access_path": "continuity_fallback"' in output
    assert '"native_mcp_claim": "not_claimed"' in output


def test_continuity_cli_accepts_base64url_json_arguments(monkeypatch, capsys) -> None:
    module = _continuity_cli_module()
    observed: dict[str, object] = {}
    monkeypatch.setattr(module, "record_fallback", lambda **_kwargs: {})
    monkeypatch.setattr(module, "source_fingerprint", lambda _root: "source-test")
    monkeypatch.setattr(
        module,
        "invoke_configured_tool",
        lambda **kwargs: observed.update(kwargs) or {"result": {"status": "ok"}, "tool_list_fingerprint": "sha256:tools", "mcp_call_status": "ok"},
    )
    monkeypatch.setattr(module, "record_fallback_call", lambda **kwargs: {"access_path": "continuity_fallback", **kwargs})
    encoded = base64.urlsafe_b64encode(json.dumps({"candidates": [{"url": "https://example.test", "score": 1}]}).encode("utf-8")).decode("ascii").rstrip("=")
    assert module.main(["call", "--reason", "transport_closed", "--tool", "health_check", "--arguments-base64url", encoded]) == 0
    assert observed["arguments"] == {"candidates": [{"url": "https://example.test", "score": 1}]}
    assert '"status": "ok"' in capsys.readouterr().out
