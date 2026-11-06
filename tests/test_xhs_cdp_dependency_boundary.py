"""日常浏览器链不能回到退役的专用浏览器执行器。"""
from pathlib import Path
import builtins


def test_xhs_adapter_does_not_import_retired_drivers(monkeypatch):
    from runtime.platform_execution import ensure_bindings
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        assert name not in {"runtime.xhs_task_failover", "runtime.xhs_account_pool",
            "media_platform.xhs.mcp_client", "media_platform.xhs.scrapling_adapter"}
        assert not name.startswith("scrapling")
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,"__import__",guarded)
    ensure_bindings()


def test_private_managed_chrome_entry_cannot_launch_daily_browser(monkeypatch):
    from runtime import chrome_manager as manager
    monkeypatch.setattr(manager,"_find_chrome_with_debug_port",lambda *a,**k: (_ for _ in ()).throw(AssertionError("must stop before discovery")))
    assert manager._ensure_chrome_debugging_locked("xhs",target_profile_id="old-A") is False
    assert manager._managed_chrome_profile_dir("xhs",target_profile_id="old-A") == ""
    root=Path(__file__).resolve().parents[1]
    assert not (root/"bridge/xhs_mcp_bridge.cjs").exists()
    assert not (root/"src/media_platform/xhs/scrapling_adapter.py").exists()
