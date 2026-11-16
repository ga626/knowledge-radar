"""Explicit redisplay must reach the site's existing attention/reopen path."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_function(path, name, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    node.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[name]


@pytest.mark.parametrize("platform", ["boss", "liepin", "maimai", "zhihu"])
def test_explicit_request_reaches_redisplay(platform):
    calls = []
    site = SimpleNamespace(request_login=lambda **kw: calls.append(kw) or {"status": "waiting_for_user"})
    fn = load_function("src/runtime/chrome_manager.py", "request_browser_interaction", {
        "BORROWED_BROWSER_PLATFORMS": {platform},
        "_borrowed_browser_site": lambda p: site,
    })
    fn(platform, "login", activate=True)
    assert calls == [{"activate": True}]


def test_non_explicit_request_does_not_steal_focus():
    calls = []
    site = SimpleNamespace(request_login=lambda **kw: calls.append(kw) or {})
    fn = load_function("src/runtime/chrome_manager.py", "request_browser_interaction", {
        "BORROWED_BROWSER_PLATFORMS": {"maimai"}, "_borrowed_browser_site": lambda p: site,
    })
    fn("maimai", "login_required")
    assert calls == [{}]


def test_mcp_explicit_interaction_requests_activation():
    calls = []
    fn = load_function("src/runtime/composition.py", "health_check", {
        "managed_browser_platforms": lambda: (),
        "request_browser_interaction": lambda p, r, **kw: calls.append((p, kw)) or {"status": "waiting_for_user"},
        "browser_sessions_summary": lambda **kw: {},
        "chrome_runtime_summary": lambda: {},
        "_compact_chrome_runtime_summary": lambda x: x,
    })
    fn(mode="request_browser_interaction:maimai:login")
    assert calls == [("maimai", {"activate": True})]


def test_empty_detail_cannot_become_an_explicit_login_request():
    fn = load_function("src/runtime/chrome_manager.py", "request_browser_interaction", {
        "BORROWED_BROWSER_PLATFORMS": {"maimai"},
        "_borrowed_browser_site": lambda p: pytest.fail("no login evidence"),
    })
    assert fn("maimai", "empty_detail", activate=True)["reason_code"] == "NO_MANUAL_ACTION_EVIDENCE"
