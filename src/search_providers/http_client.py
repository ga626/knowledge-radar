"""Shared HTTP client helpers for search providers."""

from __future__ import annotations

import atexit
import threading
from typing import Any, Dict, Tuple

import httpx


_CLIENTS: Dict[Tuple[float, Tuple[Tuple[str, str], ...]], httpx.Client] = {}
_LOCK = threading.RLock()


def _headers_key(headers: Dict[str, str] | None) -> Tuple[Tuple[str, str], ...]:
    return tuple(sorted((str(k), str(v)) for k, v in (headers or {}).items()))


def shared_client(timeout: float, headers: Dict[str, str] | None = None) -> httpx.Client:
    """Return a process-shared client keyed by timeout and default headers."""

    key = (float(timeout), _headers_key(headers))
    with _LOCK:
        client = _CLIENTS.get(key)
        if client is None:
            client = httpx.Client(timeout=float(timeout), headers=dict(headers or {}))
            _CLIENTS[key] = client
        return client


def request_json(method: str, url: str, *, timeout: float, headers: Dict[str, str] | None = None, **kwargs: Any) -> Any:
    response = shared_client(timeout, headers=headers).request(method.upper(), url, **kwargs)
    response.raise_for_status()
    return response.json()


def close_shared_clients() -> None:
    with _LOCK:
        clients = list(_CLIENTS.values())
        _CLIENTS.clear()
    for client in clients:
        try:
            client.close()
        except Exception:
            pass


def shared_client_summary() -> Dict[str, Any]:
    with _LOCK:
        return {
            "schema": "knowledgeradar-shared-http-clients/v1",
            "status": "ok",
            "client_count": len(_CLIENTS),
            "keys": [{"timeout_s": key[0], "header_count": len(key[1])} for key in _CLIENTS],
        }


atexit.register(close_shared_clients)
