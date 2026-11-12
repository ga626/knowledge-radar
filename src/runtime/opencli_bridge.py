"""OpenCLI 共用连接与daemon生命周期；不包含站点选择器或登录判断。"""
from runtime.execution_context import execution_value
from pathlib import Path
import json
import sys
import os
import subprocess
import time
import uuid
import shutil
import hashlib
from runtime.paths import runtime_state_dir


def extension_supported(version):
    """复用已验证的用户扩展；包内分发版本不等于唯一运行版本。"""
    catalog = Path(__file__).resolve().parents[2] / "config/browser-components.json"
    compatible = json.loads(catalog.read_text(encoding="utf-8"))["components"]["opencli"]["runtime_compatibility"]
    return version in compatible["extension_versions"]


def write_state(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix("." + uuid.uuid4().hex + ".pending")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def connection_config():
    # 复用已有单一扩展绑定；旧配置键只迁移读取，不形成第二种浏览器模式。
    state = Path(os.environ.get("KR_OPENCLI_STATE_DIR") or os.environ.get("KR_XHS_OPENCLI_STATE_DIR") or runtime_state_dir() / "xhs_opencli")
    def read(name):
        path = state / name
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    dependency, binding = read("dependency.json"), read("binding.json")
    return {"root": os.environ.get("KR_OPENCLI_ROOT") or os.environ.get("KR_XHS_OPENCLI_ROOT") or dependency.get("root", ""),
        "context_id": os.environ.get("KR_OPENCLI_CONTEXT_ID") or os.environ.get("KR_XHS_OPENCLI_CONTEXT_ID") or binding.get("context_id", ""),
        "state_root": state}


def invoke_page(*, session, operation, expression="", url="", wait_selector="", login_expression="", idle_timeout_s=None,
                capture_pattern="", network_expression="", wait_text="", steps=None, manual_wait=False,
                passive_manual_login=False, isolated_work=False):
    from runtime.browser_channel import BorrowedBrowser, browser_site_key, _HELD_SITES
    from contextlib import nullcontext
    from runtime.process import silent_subprocess_run
    try:
        config = connection_config()
        root = Path(config["root"])
        if not config["root"] or not (root / "package.json").is_file() or json.loads((root / "package.json").read_text(encoding="utf-8")).get("version") != "2.0.2":
            return {"ok": False, "code": "opencli_pinned_dependency_missing"}
        if not config["context_id"]:
            return {"ok": False, "code": "opencli_daily_browser_binding_required"}
        node = os.environ.get("KR_NODE_EXE") or shutil.which("node")
        if not node:
            return {"ok": False, "code": "node_missing"}
        failure = ensure_daemon(root, node, state_root=config["state_root"])
        if failure:
            return {"ok": False, "code": failure}
        if operation in {'search','detail','comments','auth_verify'}:
            from runtime.browser_channel import ensure_borrowed_browser
            def connected():
                import httpx
                try:
                    with httpx.Client(timeout=1,trust_env=False) as client:
                        status=client.get('http://127.0.0.1:19825/status',headers={'X-OpenCLI':'1'},
                            params={'contextId':config['context_id']}).json()
                    return status.get('daemonVersion')=='2.0.2' and status.get('extensionConnected') is True and not status.get('profileDisconnected') and extension_supported(status.get('extensionVersion'))
                except (httpx.HTTPError,ValueError):
                    return False
            readiness=ensure_borrowed_browser(connected,timeout_s=18)
            if not readiness['ok']:
                return readiness
        runner = Path(__file__).with_name("opencli_page_runner.mjs")
        resource = BorrowedBrowser(config["context_id"], session, hashlib.sha256(runner.read_bytes()).hexdigest(), driver="opencli")
        payload = {"pythonExecutable": sys.executable, "root": str(root), "contextId": config["context_id"], "session": session,
            "operation": operation, "url": url, "expression": expression, "waitSelector": wait_selector,
            "loginExpression": login_expression, "idleTimeoutS": idle_timeout_s,
            "capturePattern": capture_pattern, "networkExpression": network_expression, "waitText": wait_text,
            "steps": steps or [], "manualWait": manual_wait, "passiveManualLogin": passive_manual_login,
            "isolatedWork": isolated_work}
        # Reuse the same execution context's outer site lease; other tasks still contend.
        run_id = 'kr-' + uuid.uuid4().hex
        payload['runId'] = run_id
        from runtime.browser_sessions import observe_borrowed_call
        from runtime.tool_trace import current_tool_trace
        def observed(result):
            return observe_borrowed_call(result, platform=browser_site_key(session).removeprefix('daily-chrome:'),
                context_id=config['context_id'], session=session, run_id=run_id, operation=operation,
                task_id=current_tool_trace().get('research_task_id', ''))
        lease = nullcontext() if browser_site_key(session) in _HELD_SITES.get() else resource.lease(operation)
        with lease:
            try:
                response = silent_subprocess_run([node, str(runner)], cwd=str(root), input=json.dumps(payload, ensure_ascii=False),
                    capture_output=True, encoding='utf-8', timeout=60)
            except subprocess.TimeoutExpired:
                return observed({'ok':False,'code':'opencli_caller_timeout','request_executed':True})
            except OSError:
                return observed({'ok':False,'code':'opencli_process_unavailable','request_executed':False})
            try:
                result = json.loads(response.stdout)
                if not isinstance(result, dict): raise ValueError('invalid_result')
            except (ValueError, TypeError):
                return observed({'ok':False,'code':'opencli_dependency_or_output_invalid','request_executed':True})
            result.setdefault('metadata', {}).update(transport='opencli', ownership='borrowed', browser_process_owned=False)
            # 回执落盘后才释放站点租约，防止迟到记录覆盖后续登录/关闭观察。
            return observed(result)
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": "opencli_caller_timeout", "request_executed": True}
    except (OSError, ValueError, TypeError):
        return {"ok": False, "code": "opencli_dependency_or_output_invalid"}
    except RuntimeError as exc:
        if str(exc) != "browser_session_busy":
            raise
        return {"ok": False, "code": "browser_session_busy", "request_executed": False}


def ensure_daemon(root, node, *, state_root):
    """按需启动固定上游daemon；不重启共享实例、不按裸PID终止进程。"""
    import httpx
    from runtime.leases import get_runtime_lease_coordinator, default_owner
    from runtime.process import silent_subprocess_popen
    def status():
        try:
            with httpx.Client(timeout=1, trust_env=False) as client:
                response = client.get("http://127.0.0.1:19825/status", headers={"X-OpenCLI":"1"})
            return response.json() if response.status_code == 200 else {"conflict":True}
        except httpx.RequestError:
            # On Windows an unused local port may time out instead of refusing.
            # A successful bind proves there is no listener, without proxy changes.
            import socket
            try:
                with socket.socket() as probe:
                    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    probe.bind(("127.0.0.1", 19825))
                return None
            except OSError:
                return {"probe_error": "opencli_daemon_status_unreachable"}
        except (ValueError, TypeError):
            return {"conflict":True}
    current = status()
    if current is not None:
        if current.get("probe_error"):
            return current["probe_error"]
        return "" if current.get("daemonVersion") == "2.0.2" else "opencli_daemon_identity_mismatch"
    if execution_value("KR_DURABLE_WORKER") == "1":
        # The task tree may be terminated on cancellation; a shared daemon cannot
        # be its descendant. Prepare it from the submission process instead.
        return "opencli_daemon_not_running"
    coordinator = get_runtime_lease_coordinator()
    grant = coordinator.acquire_exclusive("browser_bridge_daemon", "opencli:19825",
        owner=default_owner("start_opencli", request_id=uuid.uuid4().hex), ttl_s=15)
    if not grant.acquired:
        return "opencli_daemon_start_busy"
    try:
        current = status()
        if current is None:
            script = root / "dist" / "src" / "daemon.js"
            if not script.is_file():
                return "opencli_pinned_dependency_missing"
            state_root.mkdir(parents=True, exist_ok=True)
            with (state_root/"daemon.log").open("ab") as log:
                process = silent_subprocess_popen([node,str(script)], cwd=str(root),
                    stdin=subprocess.DEVNULL,stdout=log,stderr=log)
            from runtime.process_identity import process_identity
            write_state(state_root/"daemon.json", {"pid":process.pid,"identity":process_identity(process.pid),
                "script":str(script),"owned":"daemon_only; never_user_browser"})
            for _ in range(20):
                time.sleep(.1)
                current = status()
                if current is not None and not current.get("probe_error"):
                    break
        return "" if current and current.get("daemonVersion") == "2.0.2" else "opencli_daemon_unavailable"
    finally:
        coordinator.release(grant.lease_id)
