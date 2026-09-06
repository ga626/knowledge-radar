"""Role-isolated lifecycle entry point for the local KnowledgeRadar console.

Stable and development previews are deliberately separate processes.  A
supervisor owns exactly one loopback worker for its role, persists a redacted
status record, and never treats a merely-open port as proof that the intended
console generation is running.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
from datetime import datetime, timezone
import hashlib
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
from typing import Any
import webbrowser


CONSOLE_HOST = "127.0.0.1"
CONSOLE_PORT = 18882
DEV_CONSOLE_PORT = 18883
CONSOLE_HEALTH_SCHEMA = "knowledgeradar-local-console/v2"
STABLE_TASK_NAME = "KnowledgeRadar Stable Local Console"
DEV_TASK_PREFIX = "KnowledgeRadar Development Preview"
STARTUP_FILENAME = "KnowledgeRadar Local Console.cmd"  # legacy entry to remove on migration
MAX_RESTARTS = 5
RESTART_WINDOW_SECONDS = 120.0


class ConsoleContext:
    def __init__(self, *, role: str, port: int, install_root: Path, program: Path, data: Path, supervisor_root: Path, fingerprint: str) -> None:
        self.role = role
        self.port = port
        self.install_root = install_root
        self.program = program
        self.data = data
        self.supervisor_root = supervisor_root
        self.fingerprint = fingerprint

    @property
    def state_dir(self) -> Path:
        return self.supervisor_root / "state" / "console_supervisor" / self.role

    @property
    def status_path(self) -> Path:
        return self.state_dir / "status.json"

    @property
    def restart_path(self) -> Path:
        return self.state_dir / "restart.request.json"

    @property
    def shutdown_path(self) -> Path:
        return self.state_dir / "shutdown.request.json"

    @property
    def log_path(self) -> Path:
        return self.supervisor_root / "logs" / "console" / f"{self.role}-supervisor.log"


def _install_root() -> Path:
    return Path(__file__).resolve().parent


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_active(root: Path, *, program_override: Path | None = None) -> tuple[dict[str, Any], dict[str, Path]]:
    payload = json.loads((root / "active.json").read_text(encoding="utf-8"))
    program = (program_override or Path(str(payload.get("program_root") or ""))).resolve()
    data = Path(str(payload.get("data_root") or "")).resolve()
    if payload.get("schema") != "knowledgeradar-active-install/v1" or not (program / "src" / "onboarding" / "setup_wizard.py").is_file() or not data.is_dir():
        raise RuntimeError("当前 KnowledgeRadar 安装不可用；请运行产品安装器的 inspect 后再试。")
    return payload, {"program": program, "data": data}


def _fingerprint(*, role: str, program: Path, active: dict[str, Any]) -> str:
    safe_identity = {"role": role, "program": str(program), "version": str(active.get("version") or ""), "archive_sha256": str(active.get("archive_sha256") or "")}
    return hashlib.sha256(json.dumps(safe_identity, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()[:20]


def resolve_context(*, role: str, port: int, install_root: Path, program_override: Path | None, preview_state_root: Path | None = None) -> ConsoleContext:
    if role not in {"stable", "dev"}:
        raise RuntimeError("console role must be stable or dev")
    if role == "stable" and (port != CONSOLE_PORT or program_override is not None):
        raise RuntimeError("stable console must use port 18882 and the active installed program; program overrides are forbidden")
    if role == "dev" and (port != DEV_CONSOLE_PORT or program_override is None):
        raise RuntimeError("development preview must use port 18883 with an explicit candidate program root")
    active, paths = _load_active(install_root, program_override=program_override)
    fingerprint = _fingerprint(role=role, program=paths["program"], active=active)
    if role == "stable":
        supervisor_root = paths["data"]
    else:
        preview_base = Path(os.environ.get("KR_DEV_PREVIEW_STATE_ROOT", Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "KnowledgeRadar" / "dev-preview"))
        supervisor_root = (preview_state_root or (preview_base / fingerprint)).resolve()
    return ConsoleContext(role=role, port=port, install_root=install_root, program=paths["program"], data=paths["data"], supervisor_root=supervisor_root, fingerprint=fingerprint)


def _configure_environment(context: ConsoleContext, *, generation: int) -> None:
    os.environ.update(
        {
            "KR_PROJECT_ROOT": str(context.program), "KR_SOURCE_ROOT": str(context.program / "src"), "KR_INSTALL_ROOT": str(context.install_root),
            "KR_DATA_ROOT": str(context.data), "KR_RUNTIME_ENV_PATH": str(context.data / "config" / "runtime.env"),
            "KR_PROFILE_REGISTRY_PATH": str(context.data / "config" / "profile_registry.json"), "KR_BROWSER_DATA_DIR": str(context.data / "browser_data"),
            "KR_STATE_DIR": str(context.data / "state"), "KR_LOG_DIR": str(context.data / "logs"), "KR_MEDIA_CACHE_DIR": str(context.data / "state" / "media_cache"),
            "KR_CONSOLE_ROLE": context.role, "KR_CONSOLE_FINGERPRINT": context.fingerprint, "KR_CONSOLE_GENERATION": str(generation),
            "KR_CONSOLE_READ_ONLY": "1" if context.role == "dev" else "0",
        }
    )
    source = str(context.program / "src")
    if source not in sys.path:
        sys.path.insert(0, source)


def _health_details(port: int) -> dict[str, Any]:
    try:
        connection = HTTPConnection(CONSOLE_HOST, port, timeout=0.8)
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8")) if response.status == 200 else {}
        if payload.get("schema") != CONSOLE_HEALTH_SCHEMA or payload.get("status") != "ready":
            return {"state": "foreign"}
        return {"state": "ready", "role": str(payload.get("role") or ""), "fingerprint": str(payload.get("fingerprint") or ""), "generation": int(payload.get("generation") or 0)}
    except (ConnectionError, OSError, TimeoutError, HTTPException, ValueError, json.JSONDecodeError):
        return {"state": "absent"}


def _health(port: int, expected: ConsoleContext | None = None) -> str:
    """Return ready, absent, foreign, or stale without exposing runtime data."""
    detail = _health_details(port)
    if detail["state"] != "ready" or expected is None:
        return str(detail["state"])
    return "ready" if detail["role"] == expected.role and detail["fingerprint"] == expected.fingerprint else "stale"


def _request_stop(context: ConsoleContext) -> None:
    if _health(context.port, context) != "ready":
        raise RuntimeError("现有控制台的角色或安装身份不匹配；不会停止它。")
    connection = HTTPConnection(CONSOLE_HOST, context.port, timeout=2)
    connection.request("GET", "/")
    page = connection.getresponse().read().decode("utf-8")
    marker = 'nonce="'
    start = page.find(marker)
    if start < 0:
        raise RuntimeError("现有控制台不支持安全重启；请先检查其 supervisor 状态。")
    start += len(marker)
    token = page[start : page.find('"', start)]
    if not token:
        raise RuntimeError("现有控制台未返回本地会话令牌；不会强制结束它。")
    connection = HTTPConnection(CONSOLE_HOST, context.port, timeout=2)
    connection.request("POST", "/api/console/stop", body="{}", headers={"Content-Type": "application/json", "Origin": f"http://{CONSOLE_HOST}:{context.port}", "X-KR-Setup-Token": token})
    if connection.getresponse().status != 200:
        raise RuntimeError("无法安全停止现有控制台。")


def _command_context(context: ConsoleContext) -> list[str]:
    command = ["--role", context.role, "--install-root", str(context.install_root)]
    if context.role == "dev":
        command.extend(["--program-root", str(context.program), "--preview-state-root", str(context.supervisor_root)])
    return command


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _write_status(context: ConsoleContext, state: str, **extra: Any) -> None:
    payload = {"schema": "knowledgeradar-console-supervisor-status/v1", "role": context.role, "port": context.port, "fingerprint": context.fingerprint, "state": state, "updated_at": _utc_now(), "log": str(Path("logs") / "console" / f"{context.role}-supervisor.log"), **extra}
    _write_json_atomic(context.status_path, payload)


def _log(context: ConsoleContext, event: str, **details: Any) -> None:
    context.log_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"at": _utc_now(), "event": event, **details}, ensure_ascii=False, sort_keys=True)
    with context.log_path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


class _ConsoleMutex:
    """A per-user Windows named mutex; non-Windows test environments use a lock file."""

    def __init__(self, context: ConsoleContext) -> None:
        self.context, self.handle, self.fd = context, None, None

    def acquire(self) -> bool:
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            ctypes.set_last_error(0)
            self.handle = kernel32.CreateMutexW(None, False, f"Local\\KnowledgeRadarConsoleSupervisor-{self.context.role}")
            if not self.handle:
                raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
            if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(self.handle)
                self.handle = None
                return False
            return True
        try:
            self.context.state_dir.mkdir(parents=True, exist_ok=True)
            self.fd = os.open(self.context.state_dir / "supervisor.lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            return True
        except FileExistsError:
            return False

    def release(self) -> None:
        if self.handle is not None:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self.handle)
            self.handle = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
            (self.context.state_dir / "supervisor.lock").unlink(missing_ok=True)


def _spawn(command: list[str], *, log_path: Path) -> subprocess.Popen[Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stream = log_path.open("a", encoding="utf-8")
    options: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": stream, "stderr": stream, "close_fds": True}
    if os.name == "nt":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        return subprocess.Popen(command, **options)
    finally:
        stream.close()


def _start_background(context: ConsoleContext) -> None:
    """Start a supervisor outside the invoking terminal's Windows job tree.

    A development preview is deliberately *not* an auto-start task. It is an
    on-demand scheduled task: the Task Scheduler gives it an independent host
    while every explicit open still resolves the current candidate identity.
    """
    if os.name == "nt":
        _run_task_script(_task_script(context=context, enabled=True, start_now=True))
        return
    _spawn([sys.executable, str(Path(__file__).resolve()), "--supervise", "--port", str(context.port), "--no-open", *_command_context(context)], log_path=context.log_path)


def _wait_until_ready(context: ConsoleContext, *, timeout: float = 12.0, generation_after: int | None = None) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = _health_details(context.port)
        if _health(context.port, context) == "ready" and (generation_after is None or int(detail.get("generation") or 0) > generation_after):
            return True
        time.sleep(0.15)
    return False


def _request_restart(context: ConsoleContext, *, observed_generation: int) -> None:
    _write_json_atomic(context.restart_path, {"schema": "knowledgeradar-console-restart-request/v1", "requested_at": _utc_now(), "observed_generation": observed_generation})


def request_supervisor_shutdown(context: ConsoleContext) -> None:
    """Request an intentional stop; this is never used for ordinary restarts."""
    _write_json_atomic(context.shutdown_path, {"schema": "knowledgeradar-console-shutdown-request/v1", "requested_at": _utc_now()})


def open_console(*, context: ConsoleContext, restart: bool, open_browser: bool) -> str:
    state = _health(context.port, context)
    if state in {"foreign", "stale"}:
        raise RuntimeError(f"{CONSOLE_HOST}:{context.port} 已被其他程序或错误控制台身份占用；不会抢占或改用随机端口。")
    snapshot = status_snapshot(context)
    supervisor_state = str(snapshot["supervisor"].get("state") or "")
    if state == "ready" and supervisor_state in {"STALE_SUPERVISOR_STATE", "WORKER_WITHOUT_LIVE_SUPERVISOR"}:
        # A live orphan worker cannot recover after a later failure. Its
        # identity is checked before asking it to stop, then a fresh task-owned
        # supervisor is started.
        _request_stop(context)
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and _health(context.port, context) == "ready":
            time.sleep(0.15)
        if _health(context.port, context) != "absent":
            raise RuntimeError("检测到没有存活 supervisor 的控制台，且无法安全接管；请用 --status 查看日志。")
        _start_background(context)
        if not _wait_until_ready(context):
            raise RuntimeError(f"KnowledgeRadar {context.role} 控制台未能由新的 supervisor 接管；请用 --status 查看日志和状态。")
    elif state == "ready" and restart:
        generation = _health_details(context.port)["generation"]
        _request_restart(context, observed_generation=generation)
        if not _wait_until_ready(context, generation_after=generation):
            raise RuntimeError("supervisor 未在限定时间内完成受控重启；请用 --status 查看日志和状态。")
    elif state == "absent":
        _start_background(context)
        if not _wait_until_ready(context):
            raise RuntimeError(f"KnowledgeRadar {context.role} 控制台未能启动；请用 --status 查看日志和状态。")
    url = f"http://{CONSOLE_HOST}:{context.port}/"
    if open_browser:
        webbrowser.open(url)
    print(url)
    return url


def serve_console(*, context: ConsoleContext, generation: int) -> None:
    _configure_environment(context, generation=generation)
    from onboarding.setup_wizard import run_wizard

    run_wizard(port=context.port, open_browser=False)


def _restart_delay(restarts: list[float]) -> float:
    return min(8.0, 0.5 * (2 ** max(0, len(restarts) - 1)))


def supervise_console(context: ConsoleContext, *, legacy_healthless: bool = False) -> None:
    """Own one console worker and restart it after an unexpected exit.

    ``legacy_healthless`` is a narrowly-scoped recovery bridge for an already
    installed stable artifact that predates ``/api/health``. It never accepts a
    foreign process before spawning, but once it owns the child it uses that
    child PID as the recovery authority until the stable artifact is upgraded.
    """
    mutex = _ConsoleMutex(context)
    if not mutex.acquire():
        _log(context, "supervisor_already_running")
        return
    child: subprocess.Popen[Any] | None = None
    restarts: list[float] = []
    generation = 0
    shutdown_requested = False
    exit_state = "SUPERVISOR_EXITED"
    exit_reason = "loop-ended"
    try:
        _write_status(context, "STARTING", supervisor_pid=os.getpid())
        _log(context, "supervisor_started", supervisor_pid=os.getpid())
        while True:
            if child is None and context.shutdown_path.is_file():
                context.shutdown_path.unlink(missing_ok=True)
                _log(context, "supervisor_shutdown_requested_without_worker")
                exit_state, exit_reason = "STOPPED", "shutdown-requested"
                return
            state = _health(context.port, context)
            if child is None:
                if state in {"foreign", "stale"}:
                    _write_status(context, "FOREIGN_PORT_OWNER", supervisor_pid=os.getpid(), observed_state=state)
                    _log(context, "foreign_port_owner", observed_state=state)
                    time.sleep(2.0)
                    continue
                if state == "ready":
                    _write_status(context, "KNOWN_WORKER_WITHOUT_OWNER", supervisor_pid=os.getpid())
                    time.sleep(1.0)
                    continue
                generation += 1
                command = [sys.executable, str(Path(__file__).resolve()), "--serve", "--port", str(context.port), "--generation", str(generation), "--no-open", *_command_context(context)]
                child = _spawn(command, log_path=context.log_path)
                _write_status(context, "STARTING_WORKER", supervisor_pid=os.getpid(), worker_pid=child.pid, generation=generation)
                _log(context, "worker_started", worker_pid=child.pid, generation=generation)
            while child.poll() is None:
                if context.shutdown_path.is_file():
                    shutdown_requested = True
                    _write_status(context, "STOPPING", supervisor_pid=os.getpid(), worker_pid=child.pid, generation=generation)
                    _log(context, "supervisor_shutdown_requested", generation=generation)
                    try:
                        _request_stop(context)
                    except (OSError, RuntimeError, HTTPException) as error:
                        _log(context, "shutdown_request_failed", error=str(error))
                    context.shutdown_path.unlink(missing_ok=True)
                if context.restart_path.is_file():
                    _write_status(context, "RESTARTING", supervisor_pid=os.getpid(), worker_pid=child.pid, generation=generation)
                    _log(context, "restart_requested", generation=generation)
                    try:
                        _request_stop(context)
                    except (OSError, RuntimeError, HTTPException) as error:
                        _log(context, "restart_request_failed", error=str(error))
                    context.restart_path.unlink(missing_ok=True)
                state = _health(context.port, context)
                if legacy_healthless:
                    # The pre-v2 stable page intentionally has no health
                    # endpoint. A foreign response is expected only after this
                    # supervisor has successfully launched its own child.
                    _write_status(context, "READY_LEGACY" if state == "foreign" else "STARTING_WORKER", supervisor_pid=os.getpid(), worker_pid=child.pid, generation=generation, health_contract="legacy-healthless")
                elif state in {"foreign", "stale"}:
                    _log(context, "worker_identity_mismatch", observed_state=state, worker_pid=child.pid)
                    child.terminate()
                else:
                    _write_status(context, "READY" if state == "ready" else "STARTING_WORKER", supervisor_pid=os.getpid(), worker_pid=child.pid, generation=generation)
                time.sleep(0.2)
            code = child.returncode
            _log(context, "worker_exited", worker_pid=child.pid, generation=generation, returncode=code)
            child = None
            if shutdown_requested:
                exit_state, exit_reason = "STOPPED", "shutdown-requested"
                return
            now = time.monotonic()
            restarts = [moment for moment in restarts if now - moment <= RESTART_WINDOW_SECONDS]
            restarts.append(now)
            if len(restarts) > MAX_RESTARTS:
                _write_status(context, "BACKOFF_EXHAUSTED", supervisor_pid=os.getpid(), restart_count=len(restarts), retry_after_seconds=RESTART_WINDOW_SECONDS)
                _log(context, "restart_backoff_exhausted", restart_count=len(restarts))
                time.sleep(RESTART_WINDOW_SECONDS)
                restarts.clear()
            else:
                delay = _restart_delay(restarts)
                _write_status(context, "RETRYING", supervisor_pid=os.getpid(), restart_count=len(restarts), retry_after_seconds=delay)
                time.sleep(delay)
    except BaseException as error:
        exit_state, exit_reason = "SUPERVISOR_FAILED", type(error).__name__
        _log(context, "supervisor_failed", error=str(error), traceback=traceback.format_exc())
        raise
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
        _write_status(context, exit_state, supervisor_pid=os.getpid(), generation=generation, reason=exit_reason)
        _log(context, "supervisor_exited", supervisor_pid=os.getpid(), generation=generation, state=exit_state, reason=exit_reason)
        mutex.release()


def _startup_path() -> Path:
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / STARTUP_FILENAME


def _task_name(context: ConsoleContext) -> str:
    return STABLE_TASK_NAME if context.role == "stable" else f"{DEV_TASK_PREFIX} {context.fingerprint}"


def _ps_literal(value: str) -> str:
    return value.replace("'", "''")


def _task_script(*, context: ConsoleContext, enabled: bool, start_now: bool = False, validate_helper: bool = True) -> str:
    task_name = _task_name(context)
    if not enabled:
        return f"Unregister-ScheduledTask -TaskName '{_ps_literal(task_name)}' -Confirm:$false -ErrorAction SilentlyContinue"
    runtime = Path(sys.executable).with_name("pythonw.exe")
    if not runtime.is_file():
        runtime = Path(sys.executable)
    if context.role == "stable":
        helper = context.install_root / "console_product.py"
        if validate_helper and not helper.is_file():
            raise RuntimeError("稳定安装缺少 console_product.py；请先完成与 stable artifact 绑定的本地安装修复。")
    else:
        helper = Path(__file__).resolve()
    arguments = f'"{helper}" --supervise --role {context.role} --port {context.port} --no-open --install-root "{context.install_root}"'
    if context.role == "dev":
        arguments += f' --program-root "{context.program}" --preview-state-root "{context.supervisor_root}"'
    lines = [
        "$ErrorActionPreference = 'Stop'",
        f"$action = New-ScheduledTaskAction -Execute '{_ps_literal(str(runtime))}' -Argument '{_ps_literal(arguments)}'",
        "$settings = New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew -StartWhenAvailable",
    ]
    if context.role == "stable":
        lines.append("$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME")
        lines.append(f"Register-ScheduledTask -TaskName '{_ps_literal(task_name)}' -Action $action -Trigger $trigger -Settings $settings -RunLevel Limited -Force | Out-Null")
    else:
        lines.append(f"Register-ScheduledTask -TaskName '{_ps_literal(task_name)}' -Action $action -Settings $settings -RunLevel Limited -Force | Out-Null")
    if start_now:
        lines.append(f"Start-ScheduledTask -TaskName '{_ps_literal(task_name)}'")
    return "\n".join(lines)


def _stable_task_script(*, enabled: bool, install_root: Path) -> str:
    """Compatibility wrapper retained for installer/unit-test callers."""
    context = ConsoleContext(role="stable", port=CONSOLE_PORT, install_root=install_root, program=install_root, data=install_root, supervisor_root=install_root, fingerprint="stable")
    return _task_script(context=context, enabled=enabled, validate_helper=False)


def _run_task_script(script: str) -> None:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded], capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode != 0:
        raise RuntimeError(f"无法更新 KnowledgeRadar stable Task Scheduler 任务：{result.stderr.strip() or result.stdout.strip()}")


def set_autostart(*, enabled: bool, context: ConsoleContext) -> str:
    if context.role != "stable":
        raise RuntimeError("开发预览不允许注册为 Windows 自启动任务。")
    _run_task_script(_task_script(context=context, enabled=enabled))
    _startup_path().unlink(missing_ok=True)
    return "enabled" if enabled else "disabled"


def status_snapshot(context: ConsoleContext) -> dict[str, Any]:
    try:
        status = json.loads(context.status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        status = {"state": "NO_SUPERVISOR_STATE"}
    health = _health_details(context.port)
    pid = status.get("supervisor_pid")
    if isinstance(pid, int) and pid > 0 and not _pid_is_alive(pid):
        prior_state = str(status.get("state") or "UNKNOWN")
        status = {**status, "state": "WORKER_WITHOUT_LIVE_SUPERVISOR" if health.get("state") == "ready" else "STALE_SUPERVISOR_STATE", "prior_state": prior_state, "stale_reason": "recorded supervisor PID is not running"}
    return {"schema": "knowledgeradar-console-status/v1", "role": context.role, "port": context.port, "expected_fingerprint": context.fingerprint, "health": health, "supervisor": status}


def _pid_is_alive(pid: int) -> bool:
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            kernel32.CloseHandle(handle)
            return True
        # ERROR_INVALID_PARAMETER and ERROR_NOT_FOUND both mean there is no
        # process to own this supervisor record. Access denied still means a
        # process exists, just not one this user may inspect.
        return ctypes.get_last_error() not in {87, 1168}
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open or supervise a role-isolated local KnowledgeRadar console.")
    parser.add_argument("--port", type=int, default=CONSOLE_PORT)
    parser.add_argument("--role", choices=("stable", "dev"), default="stable")
    parser.add_argument("--serve", action="store_true", help="Host the console worker in this process.")
    parser.add_argument("--supervise", action="store_true", help="Run the role-specific worker supervisor.")
    parser.add_argument("--restart", action="store_true", help="Ask the existing supervisor for a controlled worker restart.")
    parser.add_argument("--stop-supervisor", action="store_true", help="Request an intentional role-supervisor shutdown.")
    parser.add_argument("--status", action="store_true", help="Print redacted supervisor and health status.")
    parser.add_argument("--generation", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--no-open", action="store_true", help="Do not open the default browser.")
    parser.add_argument("--install-root", default="", help=argparse.SUPPRESS)
    parser.add_argument("--program-root", default="", help=argparse.SUPPRESS)
    parser.add_argument("--preview-state-root", default="", help=argparse.SUPPRESS)
    parser.add_argument("--legacy-healthless", action="store_true", help=argparse.SUPPRESS)
    autostart = parser.add_mutually_exclusive_group()
    autostart.add_argument("--enable-autostart", action="store_true")
    autostart.add_argument("--disable-autostart", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if sum(bool(value) for value in (args.serve, args.supervise, args.status, args.stop_supervisor, args.enable_autostart, args.disable_autostart)) > 1:
        parser.error("choose at most one lifecycle action")
    if args.legacy_healthless and (not args.supervise or args.role != "stable"):
        parser.error("--legacy-healthless is only valid for the stable supervisor recovery bridge")
    install_root = Path(args.install_root).resolve() if args.install_root else _install_root()
    program_override = Path(args.program_root).resolve() if args.program_root else None
    preview_state_root = Path(args.preview_state_root).resolve() if args.preview_state_root else None
    context = resolve_context(role=args.role, port=args.port, install_root=install_root, program_override=program_override, preview_state_root=preview_state_root)
    if args.enable_autostart:
        print(set_autostart(enabled=True, context=context)); return 0
    if args.disable_autostart:
        print(set_autostart(enabled=False, context=context)); return 0
    if args.status:
        print(json.dumps(status_snapshot(context), ensure_ascii=False, sort_keys=True)); return 0
    if args.stop_supervisor:
        request_supervisor_shutdown(context); return 0
    if args.serve:
        serve_console(context=context, generation=max(1, args.generation)); return 0
    if args.supervise:
        supervise_console(context, legacy_healthless=args.legacy_healthless); return 0
    open_console(context=context, restart=args.restart, open_browser=not args.no_open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
