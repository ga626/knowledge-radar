"""浏览器通道描述与借用资源租约；不拥有或终止用户浏览器进程。"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import uuid
from contextvars import ContextVar
import threading
import subprocess
from typing import Any, Dict, Iterable, List, Optional

_HELD_SITES=ContextVar("kr_browser_held_sites",default=frozenset())

def browser_site_key(session):
    value=str(session).removeprefix("kr:").split(":",1)[0].lower()
    aliases={"xhs":"xiaohongshu","b站":"bilibili","知乎":"zhihu","boss直聘":"boss","wechat-weread":"wechat"}
    return "daily-chrome:"+aliases.get(value,value)

def _daily_chrome_running():
    """只判断正常日常Chrome；不读取账号，不把无头／受管实例当成日常浏览器。"""
    import os
    from runtime.process import silent_subprocess_run
    if os.name != 'nt':
        return None
    script = (
        "$ErrorActionPreference='Stop'; "
        "$p=@(Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        "Where-Object {$_.CommandLine -and $_.CommandLine -notmatch "
        "'--(?:type|headless|remote-debugging|user-data-dir)'}); "
        "if($p.Count){'true'}else{'false'}"
    )
    try:
        result = silent_subprocess_run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
            capture_output=True, text=True, encoding='utf-8', timeout=5)
        value = result.stdout.strip()
        return value == 'true' if result.returncode == 0 and value in {'true', 'false'} else None
    except (OSError, TimeoutError, subprocess.TimeoutExpired):
        return None


def _launch_daily_chrome():
    from runtime.executables import resolve_managed_chrome
    from runtime.browser_bootstrap import start
    selection = resolve_managed_chrome()
    if selection is None:
        raise OSError('google_chrome_not_found')
    # 不传调试端口、临时Profile或扩展参数；不持有关闭用户进程的权限。
    return start(selection.path)


def ensure_borrowed_browser(connected, *, timeout_s=8):
    """仅执行入口按需唤起；全通道共用启动锁，不重放业务命令。"""
    import json
    import os
    import time
    from runtime.execution_context import check_execution, execution_value
    from runtime.file_write_lock import file_write_lock
    from runtime.paths import runtime_state_dir
    from runtime.state_file import write_state
    check_execution()
    if connected():
        return {'ok': True, 'launched': False}
    # 旧隔离子进程不得成为日常Chrome的父进程；新内核的工作线程运行在owner内。
    if execution_value('KR_DURABLE_WORKER') == '1' and os.environ.get('KR_RUNTIME_ROLE') != 'owner':
        return {'ok': False, 'code': 'browser_channel_not_connected', 'request_executed': False}
    path = runtime_state_dir() / 'borrowed-browser-start.json'
    with file_write_lock(path):
        check_execution()
        if connected():
            return {'ok': True, 'launched': False}
        try:
            previous = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            previous = {}
        except (OSError, ValueError):
            return {'ok': False, 'code': 'browser_start_state_unreadable', 'request_executed': False}
        if not isinstance(previous, dict) or not isinstance(previous.get('attempted_at', 0), (int, float)):
            return {'ok': False, 'code': 'browser_start_state_unreadable', 'request_executed': False}
        running = _daily_chrome_running()
        launched = False
        launcher = None
        # 成功任务回收最后一个工作窗口后，下一次明确调用可以立即冷启动。
        # 30秒冷却只阻止失败启动风暴，不能让连续的正常任务互相阻断。
        if running is False and (previous.get('connection_established') is True
                                 or time.time() - previous.get('attempted_at', 0) >= 30):
            write_state(path, {'attempted_at': time.time()})
            check_execution()
            try:
                launcher = _launch_daily_chrome()
                launched = True
            except OSError:
                return {'ok': False, 'code': 'browser_start_failed', 'request_executed': False}
        deadline = time.monotonic() + min(30, max(0, timeout_s))
        while True:
            check_execution()
            if connected():
                if launched:
                    write_state(path, {'attempted_at': time.time(), 'connection_established': True})
                return {'ok': True, 'launched': launched}
            if time.monotonic() >= deadline:
                now_running=_daily_chrome_running()
                return {'ok': False, 'code': 'browser_channel_not_connected', 'request_executed': False,
                    'diagnostics':{'initial_daily_chrome_running':running,'daily_chrome_running':now_running,
                        'quiet_launch_attempted':launched,
                        'launcher_exit_code':launcher.poll() if launcher else None,
                        'reason':'quiet_start_did_not_keep_browser_alive' if launched and now_running is False
                            else 'bound_extension_not_connected' if now_running is True else 'browser_process_state_unconfirmed',
                        'foreground_fallback':False}}
            time.sleep(.1)


def browser_resource_contract(*, ownership, resource_id, generation, driver, pid=None,
                             profile_id="", profile_dir_hash="", window_id=None, hwnd=None):
    """描述权限，不接管驱动或替站点判断登录。代际未确认时不能关闭进程。"""
    if ownership not in {"owned", "borrowed"} or not resource_id:
        raise ValueError("invalid_browser_resource_contract")
    return {"schema": "knowledgeradar-browser-resource/v1", "ownership": ownership,
            "resource_id": resource_id, "generation": generation, "driver": driver,
            "pid": pid, "profile_id": profile_id, "profile_dir_hash": profile_dir_hash,
            "window_id": window_id, "hwnd": hwnd,
            "may_close_process": ownership == "owned" and bool(generation),
            "may_close_owned_tabs": True}


def may_close_process(contract, observed_generation):
    return bool(contract.get("ownership") == "owned" and contract.get("may_close_process")
                and contract.get("generation") and contract["generation"] == observed_generation)


@dataclass(frozen=True)
class BorrowedBrowser:
    """资源身份；不持有用户浏览器进程或关闭它的权限。"""
    context_id: str
    session: str
    generation: str
    driver: str = ""
    ownership: str = "borrowed"
    profile_id: str = ""
    pid: Optional[int] = None
    window_id: Optional[int] = None
    hwnd: Optional[int] = None

    def __post_init__(self):
        if self.ownership != "borrowed":
            raise ValueError("borrowed_browser_cannot_acquire_process_ownership")

    def resource_contract(self):
        return browser_resource_contract(ownership=self.ownership,
            resource_id=self.context_id + ":" + self.session, generation=self.generation, driver=self.driver,
            pid=self.pid, profile_id=self.profile_id or self.context_id,
            window_id=self.window_id, hwnd=self.hwnd)

    @contextmanager
    def lease(self, operation: str):
        from .leases import get_runtime_lease_coordinator, default_owner
        key=browser_site_key(self.session)
        held=_HELD_SITES.get()
        coordinator = get_runtime_lease_coordinator()
        grant = coordinator.acquire_exclusive("browser_site", key,
            owner=default_owner(operation, request_id=uuid.uuid4().hex), ttl_s=180,
            metadata=self.resource_contract())
        if not grant.acquired:
            raise RuntimeError("browser_session_busy")
        stop = threading.Event()
        def renew():
            while not stop.wait(45):
                if not coordinator.heartbeat(grant.lease_id, ttl_s=180):
                    return
        heart = threading.Thread(target=renew, daemon=True, name="kr-browser-lease")
        heart.start()
        token=_HELD_SITES.set(held | {key})
        try:
            yield grant
        finally:
            _HELD_SITES.reset(token)
            stop.set()
            heart.join(timeout=2)
            coordinator.release(grant.lease_id)


@dataclass(frozen=True)
class BrowserChannel:
    channel_id: str
    platform: str
    profile_id: str
    browser_base: str
    automation: str
    role: str
    launch_policy: str
    account_pool_member: bool
    main_chain_allowed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "platform": self.platform,
            "profile_id": self.profile_id,
            "browser_base": self.browser_base,
            "automation": self.automation,
            "role": self.role,
            "launch_policy": self.launch_policy,
            "account_pool_member": self.account_pool_member,
            "main_chain_allowed": self.main_chain_allowed,
        }


def channels_from_profiles(profiles: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    channels: List[Dict[str, Any]] = []
    for profile in profiles or []:
        if not isinstance(profile, dict):
            continue
        explicit_main_chain = profile.get("main_chain_allowed")
        channel = BrowserChannel(
            channel_id=str(profile.get("channel_id") or ""),
            platform=str(profile.get("platform") or ""),
            profile_id=str(profile.get("profile_id") or ""),
            browser_base=str(profile.get("browser_base") or ""),
            automation=_automation_for_channel(str(profile.get("channel_id") or ""), str(profile.get("browser_base") or "")),
            role=str(profile.get("role") or ""),
            launch_policy=str(profile.get("launch_policy") or ""),
            account_pool_member=bool(profile.get("account_pool_member", False)),
            main_chain_allowed=bool(explicit_main_chain) if explicit_main_chain is not None else str(profile.get("role") or "") == "primary",
        )
        channels.append(channel.to_dict())
    return channels


def browser_channel_summary(profiles: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    channels = channels_from_profiles(profiles)
    ownership = browser_ownership_summary(channels)
    return {
        "schema": "knowledgeradar-browser-channel-summary/v1",
        "status": ownership["status"],
        "production_routing": "unchanged",
        "channels": channels,
        "ownership": ownership,
        "counts": {
            "total": len(channels),
            "main_chain_allowed": sum(1 for item in channels if item.get("main_chain_allowed")),
            "account_pool_members": sum(1 for item in channels if item.get("account_pool_member")),
            "orphan_browser_records": ownership["orphan_count"],
        },
    }


def browser_ownership_summary(channels: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    orphan_records: list[Dict[str, Any]] = []
    registered_platforms: set[str] = set()
    for index, channel in enumerate(channels or []):
        platform = str(channel.get("platform") or "")
        if platform:
            registered_platforms.add(platform)
        missing = [
            field
            for field in ("channel_id", "platform", "profile_id", "browser_base", "launch_policy")
            if not str(channel.get(field) or "")
        ]
        if missing:
            orphan_records.append(
                {
                    "index": index,
                    "platform": platform,
                    "profile_id": str(channel.get("profile_id") or ""),
                    "missing": missing,
                }
            )
    return {
        "schema": "knowledgeradar-browser-ownership-summary/v1",
        "status": "ok" if not orphan_records else "degraded",
        "registered_platforms": sorted(registered_platforms),
        "orphan_count": len(orphan_records),
        "orphan_records": orphan_records,
        "policy": "Every browser-backed profile must declare channel_id, platform, profile_id, browser_base, and launch_policy before it is treated as managed.",
    }


def _automation_for_channel(channel_id: str, browser_base: str) -> str:
    text = f"{channel_id} {browser_base}".lower()
    if "playwright" in text:
        if "cdp" in text:
            return "playwright_cdp"
        return "playwright_dom"
    if "camoufox" in text:
        return "playwright_dom"
    if "scrapling" in text or "cdp" in text:
        return "scrapling_cdp"
    return "unknown"
