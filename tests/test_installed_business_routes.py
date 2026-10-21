"""Regressions observed in the installed artifact, without private vendor files."""
import json
import shutil
import subprocess

import pytest

from collectors.platform import zhihu
from runtime.login_state import LoginProbe
from runtime.xhs_task_failover import run_browser_accounts


def test_public_zhihu_routes_missing_signer_to_authenticated_browser(monkeypatch, tmp_path):
    monkeypatch.setattr(zhihu, "PROJECT_ROOT", str(tmp_path))
    expected = [{"title": "real page", "url": "https://www.zhihu.com/question/1"}]
    calls = []
    monkeypatch.setattr(zhihu, "zhihu_search_via_cdp_page", lambda keyword, limit: calls.append((keyword, limit)) or expected)
    assert zhihu.zhihu_search_api("Python", "not-persisted", 2) == expected
    assert calls == [("Python", 2)]


def test_zhihu_login_probe_requires_real_business_when_signer_is_absent():
    probe = LoginProbe(platform="zhihu", chrome_debug_url=lambda _: "unused",
                       read_cookie_from_cdp=lambda: "z_c0=local; d_c0=local",
                       zhihu_search_api=lambda *_: [{"title": "real page"}])
    assert probe.inspect(cdp_status="ok")["status"] == "ok"


def test_unknown_account_state_rotates_without_login_or_paid_exhaustion(monkeypatch, tmp_path):
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    registry = {"raw": {"policy": {"default_mode": "safe_auto", "max_switches_per_task": 2}}}
    pool = {"accounts": [{"profile_id": name, "risk": {"risk_score": 0}, "runtime_state": "healthy"} for name in ("A", "B", "C")]}
    recorded = []
    _, receipt = run_browser_accounts(registry, pool, "A", lambda _: {"error": {"failure_type": "auth_state_unconfirmed"}}, lambda *args: recorded.append(args))
    assert len(receipt["attempts"]) == 3
    assert not receipt["exhausted"]
    assert not receipt["common_failure"]
    assert all(reason == "CDP_PORT_UNAVAILABLE" for _, reason in recorded)


def test_xhs_ui_proof_survives_identity_endpoint_failure(monkeypatch):
    import importlib.util
    from pathlib import Path
    source = Path(__file__).resolve().parents[1] / "src/media_platform/xhs/scrapling_adapter.py"
    spec = importlib.util.spec_from_file_location("public_xhs_scrapling_adapter", source)
    scrapling_adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scrapling_adapter)
    executable = shutil.which("node")
    if not executable:
        pytest.skip("node is required by the browser adapter")
    monkeypatch.setattr(scrapling_adapter, "_ensure_xhs_page_ws_url", lambda _: "unused")
    def evaluate(_ws, expression, **_kwargs):
        prelude = """global.location={href:'https://www.xiaohongshu.com/explore'};
        global.document={title:'page', body:{innerText:'通知 我'}, querySelectorAll:s=>s==='a,button'?[{innerText:'通知'},{innerText:'我'}]:[{getAttribute:()=>'/user/profile/local'}]};
        global.fetch=async()=>{throw new Error('identity endpoint unavailable')};
        """
        code = prelude + "Promise.resolve(" + expression + ").then(console.log)"
        result = subprocess.run([executable, "-e", code], capture_output=True, text=True, encoding="utf-8", check=True)
        return json.loads(result.stdout)
    monkeypatch.setattr(scrapling_adapter, "_cdp_json", evaluate)
    result = scrapling_adapter.probe_login_state("unused")
    assert result["ui_authenticated"] is True
    assert result["guest"] is None
    assert result["ok"] is False


def test_xhs_token_recovery_expression_is_valid_and_uses_live_page(monkeypatch):
    from collectors.platform import xiaohongshu as xhs
    import importlib.util
    note = "0123456789abcdef01234567"
    monkeypatch.setattr(xhs, "_cached_xhs_token", lambda _: "")
    monkeypatch.setattr(xhs, "_ensure_chrome_debugging", lambda *_: True)
    monkeypatch.setattr(xhs, "_chrome_debug_url", lambda *_: "http://127.0.0.1:12733")
    import io
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: io.BytesIO(json.dumps([{"type":"page", "url":"https://www.xiaohongshu.com/search_result", "webSocketDebuggerUrl":"ws://local"}]).encode()))
    original = importlib.util.module_from_spec
    def module(spec):
        value = original(spec)
        original_exec = spec.loader.exec_module
        def load(target):
            original_exec(target)
            def evaluate(_ws, expression, **_kwargs):
                prelude = 'global.location={href:"https://www.xiaohongshu.com/search_result"};global.document={querySelectorAll:()=>[{href:"https://www.xiaohongshu.com/explore/' + note + '?xsec_token=memory-only"}]};'
                result = subprocess.run([shutil.which("node"), "-e", prelude + "console.log(" + expression + ")"],capture_output=True,text=True,encoding="utf-8",check=True)
                return json.loads(result.stdout)
            target._cdp_json = evaluate
        spec.loader.exec_module = load
        return value
    monkeypatch.setattr(importlib.util, "module_from_spec", module)
    assert xhs._recover_xhs_xsec_token(note) == "memory-only"
