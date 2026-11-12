"""KR Browser Link：共享原生通道客户端。

Native Messaging 由 Chrome 扩展主动连接 Host；KR 通过 Host 暴露的本机
HTTP 控制面投递任务。这样 KR 不需要 debugger/CDP，也不拥有用户浏览器。
本模块只负责协议、超时、取消和断线分类，不包含 BOSS 选择器。
"""

from __future__ import annotations

from runtime.execution_context import execution_value
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

from runtime.paths import runtime_state_dir


def native_state_path() -> Path:
    explicit = os.environ.get("KR_BROWSER_LINK_STATE_FILE")
    if explicit:
        return Path(explicit)
    return runtime_state_dir() / "browser_link" / "host.json"


def _read_state(path: Path | None = None) -> dict[str, Any]:
    path = path or native_state_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class BrowserLink:
    """One shared protocol client; no browser process ownership or auto-replay."""

    def __init__(self, *, state_file: Path | None = None, client: httpx.Client | None = None):
        self.state_file = state_file or native_state_path()
        self.client = client
        self.explicit_state = state_file is not None

    def distribution_status(self):
        if self.explicit_state:
            return None  # 隔离协议测试显式传入自身state，不代表产品安装。
        path = Path(__file__).resolve().parents[2] / 'config/browser-link.json'
        try:
            metadata = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return 'browser_link_distribution_unavailable'
        if not metadata.get('extension_ids'):
            return 'browser_link_distribution_pending'
        if metadata.get('distribution')=='bundled_unpacked':
            from runtime.browser_components import verify_component
            try:verify_component('browser_link')
            except (OSError,ValueError):return 'browser_link_bundle_invalid'
        elif not metadata.get('store_url'):
            return 'browser_link_distribution_pending'
        return None

    def health(self) -> dict[str, Any]:
        pending = self.distribution_status()
        if pending:
            return {'ok': False, 'connected': False, 'code': pending, 'request_executed': False}
        state = _read_state(self.state_file)
        if not state.get("port") or not state.get("token"):
            return {"ok": False, "code": "native_host_unavailable", "connected": False}
        try:
            with self._session() as client:
                response = client.get(self._url(state, "/health"), headers=self._headers(state), timeout=2)
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError('invalid_health_result')
            if not self.explicit_state and payload.get('extension_version')!=json.loads((Path(__file__).resolve().parents[2]/'config/browser-link.json').read_text(encoding='utf-8')).get('extension_version'):
                return {**payload,'ok':False,'code':'browser_extension_reload_required'}
            return {**payload, "ok": response.is_success and bool(payload.get("connected")) and payload.get("protocol") == 1
                    and not payload.get('draining') and payload.get('generation') == state.get('generation')}
        except (httpx.HTTPError, ValueError):
            return {"ok": False, "code": "native_host_unreachable", "connected": False}

    def request(self, operation: str, payload: dict[str, Any], *, platform: str, timeout_s: float = 30.0) -> dict[str, Any]:
        pending = self.distribution_status()
        if pending:
            return self._failure(pending, str(payload.get('request_id') or ''), request_executed=False)
        if not self.explicit_state:
            current=_read_state(self.state_file)
            expected=json.loads((Path(__file__).resolve().parents[2]/'config/browser-link.json').read_text(encoding='utf-8')).get('extension_version')
            if current.get('extension_version') and current['extension_version']!=expected:
                return self._failure('browser_extension_reload_required',str(payload.get('request_id') or ''),request_executed=False)
        from runtime.browser_channel import BorrowedBrowser, browser_site_key, _HELD_SITES
        if browser_site_key(platform) in _HELD_SITES.get():
            return self._request(operation,payload,platform=platform,timeout_s=timeout_s)
        try:
            with BorrowedBrowser('daily',platform,'',driver='browser_link').lease(operation):
                return self._request(operation,payload,platform=platform,timeout_s=timeout_s)
        except RuntimeError as exc:
            if str(exc) != 'browser_session_busy':
                raise
            return self._failure('browser_busy',str(payload.get('request_id') or ''),request_executed=False)

    def _request(self, operation: str, payload: dict[str, Any], *, platform: str, timeout_s: float = 30.0) -> dict[str, Any]:
        request_id = str(payload.get("request_id") or uuid.uuid4().hex)
        payload = dict(payload)
        # Routine work stays quiet even if a previous diagnostic shell set OBSERVE.
        # Foreground handoff is a separate explicit login/attention operation.
        if operation not in {'login', 'attention'}:
            payload.pop('observe', None)
        task_id = execution_value('KR_DURABLE_TASK_ID', '')
        if task_id:
            from runtime.tasks import get_task_store
            task = get_task_store().get_task(task_id) or {}
            if task.get('status') in {'cancelled', 'interrupted'}:
                return self._failure('cancelled', request_id, request_executed=False)
            deadline = (task.get('metadata') or {}).get('deadline_at')
            if deadline:
                timeout_s = min(timeout_s, float(deadline) - time.time())
            payload['task_id'] = task_id
        if timeout_s <= 0:
            return self._failure('native_command_timeout', request_id, request_executed=False)
        request_deadline = time.monotonic() + timeout_s
        last_health = self.health() if operation in {'search', 'detail', 'comments', 'login', 'auth_verify', 'attention'} else {'ok': True}
        if not last_health.get('ok'):
            from runtime.browser_channel import ensure_borrowed_browser
            def connected():
                nonlocal last_health
                last_health = self.health()
                return bool(last_health.get('ok'))
            ready = ensure_borrowed_browser(connected, timeout_s=min(8, timeout_s))
            if not ready['ok']:
                return self._failure(ready['code'], request_id, request_executed=False,
                                     browser_readiness={**ready, 'connection_reason': last_health.get('code') or
                                         ('extension_disconnected' if last_health.get('connected') is False else 'channel_not_ready')})
            timeout_s = min(timeout_s, request_deadline - time.monotonic())
            if timeout_s <= 0:
                return self._failure('native_command_timeout', request_id, request_executed=False)
            if task_id:
                from runtime.execution_context import check_execution
                check_execution()
                timeout_s = min(timeout_s, float(deadline) - time.time()) if deadline else timeout_s
                if timeout_s <= 0:
                    return self._failure('native_command_timeout', request_id, request_executed=False)
        state = _read_state(self.state_file)
        if not state.get("port") or not state.get("token"):
            return self._failure("native_host_unavailable", request_id,request_executed=False)
        body = {"request_id": request_id, "platform": platform, "generation": state.get('generation',''), "operation": operation, "payload": payload,
                'timeout_ms': max(1, min(20000, round(timeout_s * 1000)))}
        try:
            with self._session() as client:
                response = client.post(self._url(state, "/command"), headers=self._headers(state),
                                       json=body, timeout=min(timeout_s, 20) + 2)
            if not response.is_success:
                # The host uses 502 for an extension-level failure; preserve its
                # typed cause instead of disguising it as a transport outage.
                try:
                    failure = response.json()
                except ValueError:
                    failure = None
                if isinstance(failure, dict) and failure.get("code"):
                    return {
                        **failure,
                        "ok": False,
                        "request_id": request_id,
                        "failure_type": str(failure["code"]),
                        "status": response.status_code,
                    }
                code = {409: 'browser_disconnected', 408: 'native_command_timeout'}.get(response.status_code, 'native_host_http_error')
                return self._failure(code, request_id, status=response.status_code)
            result = response.json()
            if not isinstance(result, dict):
                return self._failure("native_host_invalid_result", request_id)
            if result.get('generation') != state.get('generation') or result.get('request_id') != request_id:
                return self._failure('native_host_invalid_result',request_id)
            result.setdefault("request_id", request_id)
            return result
        except httpx.ConnectError:
            return self._failure('native_host_unreachable',request_id,request_executed=False)
        except httpx.TimeoutException:
            self.cancel(request_id)
            return self._failure("native_command_timeout", request_id)
        except (httpx.HTTPError, ValueError):
            return self._failure("native_host_unreachable", request_id)

    def cancel(self, request_id: str) -> dict[str, Any]:
        return self._cancel('/cancel', {'request_id': request_id}, request_id)

    def receipt(self, request_id: str) -> dict[str, Any]:
        """只读原请求结果；未知或Host换代都不触发重放。"""
        return self._cancel('/receipt', {'request_id': request_id}, request_id)

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        return self._cancel('/cancel_task', {'task_id': task_id}, task_id)

    def _cancel(self, path, body, request_id):
        state = _read_state(self.state_file)
        if not state.get("port") or not state.get("token"):
            return self._failure("native_host_unavailable", request_id)
        try:
            with self._session() as client:
                response = client.post(self._url(state, path), headers=self._headers(state), json=body, timeout=2)
            return response.json() if response.is_success else self._failure("native_cancel_failed", request_id)
        except (httpx.HTTPError, ValueError):
            return self._failure("native_cancel_failed", request_id)

    @contextmanager
    def _session(self):
        if self.client is not None:
            yield self.client
        else:
            with httpx.Client(trust_env=False) as client:
                yield client

    @staticmethod
    def _url(state: dict[str, Any], path: str) -> str:
        return f"http://127.0.0.1:{int(state['port'])}{path}"

    @staticmethod
    def _headers(state: dict[str, Any]) -> dict[str, str]:
        return {"X-KR-Native-Token": str(state["token"]), "Content-Type": "application/json"}

    @staticmethod
    def _failure(code: str, request_id: str, **extra: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "request_id": request_id,
            "code": code,
            "failure_type": code,
            "retryable": False,
            **extra,
        }


def wait_for_native_host(*, state_file: Path | None = None, timeout_s: float = 10.0) -> dict[str, Any]:
    bridge = BrowserLink(state_file=state_file)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        health = bridge.health()
        if health.get("ok"):
            return health
        time.sleep(0.1)
    return bridge.health()


def state_generation(path):
    return str(_read_state(path).get("generation") or "")
