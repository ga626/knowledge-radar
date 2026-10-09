import builtins
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "xhs_adapter_dependency_test",
    Path(__file__).resolve().parents[1] / "src/media_platform/xhs/scrapling_adapter.py",
)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


@pytest.fixture
def without_scrapling(monkeypatch):
    original = builtins.__import__

    def import_without_scrapling(name, *args, **kwargs):
        if name == "scrapling" or name.startswith("scrapling."):
            raise ModuleNotFoundError("scrapling intentionally unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_scrapling)


def test_native_page_cdp_search_runs_without_optional_fetcher(monkeypatch, without_scrapling):
    monkeypatch.setattr(adapter, "_ensure_xhs_page_ws_url", lambda url: "ws://localhost/devtools/page/test")
    calls = []

    def page_search(endpoint, keyword, **kwargs):
        calls.append((endpoint, keyword, kwargs))
        return [{"id": "test-note", "title": "test"}]

    monkeypatch.setattr(adapter, "_search_current_page_via_cdp", page_search)
    result = adapter.search("test", limit=1, cdp_url="http://localhost:9222")
    assert result[0]["id"] == "test-note"
    assert calls[0][1] == "test"
    assert calls[0][2]["limit"] == 1


def test_dynamic_fetcher_path_reports_missing_dependency(monkeypatch, without_scrapling):
    monkeypatch.setattr(adapter, "_ensure_xhs_page_ws_url", lambda url: "ws://localhost/devtools/browser/test")
    with pytest.raises(adapter.XhsScraplingError) as error:
        adapter.search("test", cdp_url="http://localhost:9222")
    assert error.value.error_type == "dependency_missing"
    assert error.value.login_required is False
