"""招聘短诊断回归：原生传递、交互语义与终态，不访问真实账户。"""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.tasks import TaskStore, compact_task_ref


@pytest.mark.parametrize('verified', [True, False])
@pytest.mark.parametrize('entry', ['business', 'browser_fallback'])
def test_business_entry_rechecks_pending_login_once_inside_lease(monkeypatch, verified, entry):
    from runtime import recruitment_governance as governance
    from collectors.platform import boss
    events=[]
    ready=False
    def admission(*args, **kwargs):
        return {'allowed':ready,'reason_code':'ok' if ready else 'pending_manual_interaction',
                'manual_action_required':not ready}
    def complete():
        nonlocal ready
        events.append('verify')
        ready=verified
        return {'status':'ok' if verified else 'not_verified'}
    monkeypatch.setattr(governance,'check_platform_admission',admission)
    monkeypatch.setattr(boss,'admission',lambda: {**admission(), 'reason':admission()['reason_code']})
    monkeypatch.setattr(boss,'complete_login',complete)
    monkeypatch.setattr(governance,'acquire_platform_lease',lambda *a,**k:
        events.append('lease') or SimpleNamespace(acquired=True,lease_id='test'))
    monkeypatch.setattr(governance,'release_platform_lease',lambda *a:events.append('release'))
    monkeypatch.setattr(governance,'record_search_outcome',lambda *a,**k:None)
    monkeypatch.setattr('runtime.borrowed_observations.observe_borrowed_result',lambda p,r:r)
    call=lambda: events.append('search') or {'items':[{'title':'job'}]}
    result=(governance.execute_operation('boss','search',call,keyword='推荐算法')
        if entry=='business' else governance.execute_browser_operation('boss',call))
    assert events == (['lease','verify','search','release'] if verified else ['lease','verify','release'])
    assert bool(result['items']) is verified


@pytest.mark.parametrize('reason', ['request_outcome_unknown','platform_cooldown'])
def test_blocked_non_login_does_not_probe_or_replay(monkeypatch, reason):
    from runtime import recruitment_governance as governance
    from collectors.platform import boss
    monkeypatch.setattr(governance,'check_platform_admission',lambda *a,**k:
        {'allowed':False,'reason_code':reason,'manual_action_required':False})
    monkeypatch.setattr(boss,'complete_login',lambda:pytest.fail('non-login failure probed'))
    result=governance.execute_operation('boss','search',lambda:pytest.fail('blocked business replayed'))
    assert result['failure_type']==reason


@pytest.mark.parametrize("role", ["stable", "dev"])
def test_console_supervisor_uses_its_own_native_host_path(tmp_path, monkeypatch, role):
    path = Path(__file__).resolve().parents[1] / "scripts/product_console_launcher.py"
    spec = importlib.util.spec_from_file_location("diag_console_launcher", path)
    module = importlib.util.module_from_spec(spec)
    import sys
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    data, preview = tmp_path / "data", tmp_path / "preview"
    context = SimpleNamespace(role=role, data=data, supervisor_root=preview,
        program=tmp_path / "program", install_root=tmp_path / "install", fingerprint="fixture")
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setenv("KR_BROWSER_LINK_STATE_FILE", str(tmp_path / "stale-profile/host.json"))
    module._configure_environment(context, generation=1)
    writable = data if role == "stable" else preview
    from runtime.browser_link import native_state_path
    assert native_state_path() == writable / "runtime/browser_link/host.json"


def risk_search():
    from platform_adapters.common import response_from_legacy
    from collectors.platform.boss import BossSite, authenticated
    site = BossSite("boss", "BOSS直聘", "", "", authenticated)
    return response_from_legacy("BOSS直聘",
        site.failure("SECURITY_BLOCK", request_executed=True)).to_mcp_dict()


def test_explicit_bounded_risk_is_not_promoted_to_human_login(monkeypatch):
    from runtime import composition
    monkeypatch.setattr(composition, "request_browser_interaction",
        lambda *a, **k: pytest.fail("cooldown promoted into a foreground login handoff"))
    result = composition._promote_manual_interaction_result(risk_search(),
        platform="boss", original_tool="search_recruitment", original_args={})
    assert result["manual_action_required"] is False
    assert result["error"]["manual_action_required"] is False
    assert result["metadata"]["admission_state"]["status_class"] == "RETRY_LATER"


def test_explicit_manual_challenge_never_gets_an_automatic_probe():
    from runtime.task_cooldown import cooldown_plan
    result = {"failure_type": "anti_bot_verification", "manual_action_required": True,
              "error": {"type": "anti_bot_verification", "manual_action_required": True}}
    assert cooldown_plan(result, operation="platform_discovery") is None


@pytest.mark.parametrize("status", ["cancelled", "failed", "completed"])
def test_terminal_projection_does_not_keep_waiting_for_user(tmp_path, status):
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    store.upsert_task("diag", "durable_platform_discovery", "boss",
        metadata={"phase": "waiting_for_user", "resume_policy": "explicit_retry",
                  "blocks_final_report": True})
    getattr(store, "mark_" + status)("diag", **({"reason": "by_user"} if status == "cancelled"
        else {"error": "fixture"} if status == "failed" else {}))
    row = store.get_task("diag")
    ref = compact_task_ref(row)
    assert ref["status"] == status
    assert ref["fanin"]["phase"] == status
    # Historic row remains available; the public current-state view is consistent.
    assert row["metadata"]["phase"] == "waiting_for_user"


def test_native_site_preserves_nonsecret_readiness_cause(tmp_path, monkeypatch):
    from runtime.native_site import NativeSite
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    site = NativeSite("liepin", "猎聘", "", "", lambda _: False)
    monkeypatch.setattr(site, "bridge", lambda: SimpleNamespace(request=lambda *a, **kw: {
        "ok": False, "code": "browser_channel_not_connected", "request_executed": False,
        "browser_readiness": {"diagnostics": {"reason": "bound_extension_not_connected"},
                              "connection_reason": "native_host_unavailable"}}))
    result = site.invoke("search", url="https://www.liepin.com/zhaopin/")
    assert result["browser_readiness"]["connection_reason"] == "native_host_unavailable"
    assert result["request_executed"] is False


def test_native_bridge_exposes_the_failed_connection_lookup(tmp_path, monkeypatch):
    from runtime.browser_link import BrowserLink
    from runtime import browser_channel
    bridge = BrowserLink(state_file=tmp_path / "absent.json")
    monkeypatch.delenv("KR_DURABLE_TASK_ID", raising=False)
    monkeypatch.setattr(bridge, "health", lambda: {"ok": False, "code": "native_host_unavailable"})
    def ensure(connected, **kwargs):
        assert not connected()
        return {"ok": False, "code": "browser_channel_not_connected", "request_executed": False}
    monkeypatch.setattr(browser_channel, "ensure_borrowed_browser", ensure)
    result = bridge._request("search", {}, platform="liepin")
    assert result["browser_readiness"]["connection_reason"] == "native_host_unavailable"
    assert result["request_executed"] is False


def test_real_adapted_risk_waits_then_explicit_manual_result_releases_slot(tmp_path, monkeypatch):
    import time
    from runtime import composition
    from runtime.task_service import TaskService
    import runtime.task_service as service_module
    for key, name in [("KR_DATA_ROOT", "data"), ("KR_STATE_DIR", "state"),
                      ("KR_LOG_DIR", "logs"), ("KR_EVIDENCE_ROOT", "assets"),
                      ("KR_RUNTIME_LEASE_DB_PATH", "leases.sqlite3")]:
        monkeypatch.setenv(key, str(tmp_path / name))
    monkeypatch.setattr(TaskService, "start_maintenance", lambda *a, **k: None)
    service = TaskService(TaskStore(str(tmp_path / "tasks.sqlite3")))
    monkeypatch.setattr(service_module, "TaskService", lambda: service)
    calls = []
    monkeypatch.setattr(composition, "request_browser_interaction",
        lambda *a, **k: pytest.fail("bounded risk must not open a login window"))
    monkeypatch.setattr(composition, "execute_platform_discovery", lambda payload: calls.append(payload) or {
        "failure_type": "login_required", "manual_action_required": True, "error": "login required"})
    promoted = composition._promote_manual_interaction_result(risk_search(), platform="boss",
        original_tool="search_recruitment", original_args={})
    accepted = composition._persist_discovery_cooldown(promoted, tool="search_recruitment",
        platform="BOSS直聘", arguments={"platform": "boss", "keyword": "fixture", "research_task_id": ""})
    def settled():
        end = time.monotonic() + 8
        while time.monotonic() < end:
            row = service.store.get_task(accepted["task_id"])
            if row["status"] not in {"queued", "running"}:
                return row
            time.sleep(.01)
        pytest.fail("task did not release slot")
    row = settled()
    assert row["status"] == "waiting_cooldown" and not calls
    assert accepted["manual_action_required"] is False
    assert accepted["error"]["manual_action_required"] is False
    service.store._mark(row["task_id"], "waiting_cooldown", metadata={"not_before": time.time() - 1})
    service._advance_cooldown(row["task_id"])
    assert settled()["status"] == "waiting_for_user" and len(calls) == 1
    service.cancel(row["task_id"])
    assert compact_task_ref(service.store.get_task(row["task_id"]))["fanin"]["phase"] == "cancelled"
