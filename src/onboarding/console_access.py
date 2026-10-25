"""当前安装用户的本地会话；秘密不进入网页、日志或固定地址。"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
from functools import lru_cache
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import threading
import time


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def _dpapi(value: bytes, *, decrypt: bool = False) -> bytes:
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    source, target = Blob(len(value), buffer), Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        function = crypt.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        args = (ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        function = crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        args = (ctypes.byref(source), 'KnowledgeRadar local console', None, None, None, 1, ctypes.byref(target))
    function.restype = wintypes.BOOL
    if not function(*args):
        raise RuntimeError('无法读取当前 Windows 用户的本地会话密钥；请使用安装该程序的账号。')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def _matching_tcp_pid(table: bytes, client: tuple[str, int], server: tuple[str, int]) -> int:
    """Match the client side of a live IPv4 connection, never the listener."""
    count = struct.unpack_from('<I', table)[0]
    row = struct.Struct('<6I')
    if count > (len(table) - 4) // row.size:
        raise ValueError('invalid_tcp_table')
    client_ip = struct.unpack('<I', socket.inet_aton(client[0]))[0]
    server_ip = struct.unpack('<I', socket.inet_aton(server[0]))[0]
    owners = []
    for index in range(count):
        state, local_ip, local_port, remote_ip, remote_port, process_id = row.unpack_from(table, 4 + index * row.size)
        if (state == 5 and local_ip == client_ip and remote_ip == server_ip
                and socket.ntohs(local_port & 0xffff) == client[1]
                and socket.ntohs(remote_port & 0xffff) == server[1]):
            owners.append(process_id)
    return owners[0] if len(owners) == 1 else 0


class _WindowsConnectionIdentity:
    """Read-only kernel identity checks; no admin rights or browser secrets."""

    def __init__(self) -> None:
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.security = ctypes.WinDLL('advapi32', use_last_error=True)
        self.iphelper = ctypes.WinDLL('iphlpapi', use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.GetCurrentProcess.argtypes = []
        self.kernel.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.GetExitCodeProcess.restype = wintypes.BOOL
        self.security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        self.security.OpenProcessToken.restype = wintypes.BOOL
        self.security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.security.GetTokenInformation.restype = wintypes.BOOL
        self.security.IsValidSid.argtypes = [ctypes.c_void_p]
        self.security.IsValidSid.restype = wintypes.BOOL
        self.security.GetLengthSid.argtypes = [ctypes.c_void_p]
        self.security.GetLengthSid.restype = wintypes.DWORD
        self.iphelper.GetExtendedTcpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL, wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
        self.iphelper.GetExtendedTcpTable.restype = wintypes.DWORD
        self.owner = self._user_sid(self.kernel.GetCurrentProcess())

    def _user_sid(self, process: int) -> bytes:
        token = wintypes.HANDLE()
        if not self.security.OpenProcessToken(process, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            size = wintypes.DWORD()
            self.security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
            if not 0 < size.value <= 65536:
                raise ValueError('invalid_token_size')
            buffer = ctypes.create_string_buffer(size.value)
            if not self.security.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            # TOKEN_USER begins with SID_AND_ATTRIBUTES; its first field is PSID.
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            if not sid or not self.security.IsValidSid(sid):
                raise ValueError('invalid_token_sid')
            length = self.security.GetLengthSid(sid)
            if not 0 < length <= 1024:
                raise ValueError('invalid_sid_length')
            return ctypes.string_at(sid, length)
        finally:
            self.kernel.CloseHandle(token)

    def _peer_pid(self, client: tuple[str, int], server: tuple[str, int]) -> int:
        size = wintypes.DWORD()
        result = self.iphelper.GetExtendedTcpTable(None, ctypes.byref(size), False, socket.AF_INET, 5, 0)
        for _ in range(3):
            if result != 122 or not 0 < size.value <= 16 * 1024 * 1024:
                raise OSError(result, 'tcp_identity_unavailable')
            buffer = ctypes.create_string_buffer(size.value)
            result = self.iphelper.GetExtendedTcpTable(buffer, ctypes.byref(size), False, socket.AF_INET, 5, 0)
            if result == 0:
                return _matching_tcp_pid(buffer.raw[:size.value], client, server)
        raise OSError(result, 'tcp_identity_unavailable')

    def allows(self, connection: socket.socket) -> bool:
        client, server = connection.getpeername(), connection.getsockname()
        if client[0] != '127.0.0.1' or server[0] != '127.0.0.1':
            return False
        process_id = self._peer_pid(client, server)
        if not process_id:
            return False
        process = self.kernel.OpenProcess(0x1000, False, process_id)  # QUERY_LIMITED_INFORMATION
        if not process:
            return False
        try:
            peer = self._user_sid(process)
            active = wintypes.DWORD()
            # Keep the process handle open and recheck the live tuple: PID reuse
            # or a stale table row must never authenticate another connection.
            return (hmac.compare_digest(peer, self.owner)
                    and self._peer_pid(client, server) == process_id
                    and bool(self.kernel.GetExitCodeProcess(process, ctypes.byref(active)))
                    and active.value == 259)  # STILL_ACTIVE
        finally:
            self.kernel.CloseHandle(process)


@lru_cache(maxsize=1)
def _windows_connection_identity() -> _WindowsConnectionIdentity:
    return _WindowsConnectionIdentity()


class ConsoleAccess:
    def connection_identity(self, connection: socket.socket) -> bool | None:
        if os.name != 'nt':
            return None  # Legacy ticket handoff on unsupported platforms only.
        try:
            return _windows_connection_identity().allows(connection)
        except (OSError, ValueError, struct.error):
            return False  # Fail closed; cookies never override a Windows denial.

    def __init__(self, root: Path, role: str):
        if role not in {'stable', 'dev'}:
            raise ValueError('invalid_console_role')
        self.role = role
        self.cookie_name = 'kr_console_' + role
        self.challenge = secrets.token_urlsafe(24)
        self._used: dict[str, int] = {}
        self._lock = threading.Lock()
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / (role + '.key')
        if not path.exists():
            key = secrets.token_bytes(32)
            sealed = b'dpapi:' + _dpapi(key) if os.name == 'nt' else b'posix:' + _b64(key).encode('ascii')
            # 原子发布：并发启动不会读到只写了一半的密钥。
            temporary = root / (role + '.' + secrets.token_hex(8) + '.tmp')
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(sealed)
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
            finally:
                temporary.unlink(missing_ok=True)
        sealed = path.read_bytes()
        if os.name == 'nt':
            if not sealed.startswith(b'dpapi:'):
                raise RuntimeError('本地会话密钥格式错误。')
            self.key = _dpapi(sealed[6:], decrypt=True)
        else:
            if not sealed.startswith(b'posix:') or path.stat().st_mode & 0o077:
                raise RuntimeError('本地会话密钥权限错误。')
            self.key = _unb64(sealed[6:].decode('ascii'))
        if len(self.key) != 32:
            raise RuntimeError('本地会话密钥无效。')
        self.csrf_token = self._sign('csrf:' + role)

    def _sign(self, message: str) -> str:
        return _b64(hmac.digest(self.key, message.encode('ascii'), hashlib.sha256))

    def _encode(self, purpose: str, payload: dict) -> str:
        body = _b64(json.dumps(payload, separators=(',', ':')).encode('utf-8'))
        return body + '.' + self._sign(purpose + ':' + body)

    def _decode(self, purpose: str, value: str) -> dict:
        try:
            if len(value) > 2048:
                return {}
            body, signature = value.split('.')
            if not hmac.compare_digest(signature, self._sign(purpose + ':' + body)):
                return {}
            payload = json.loads(_unb64(body))
            return payload if isinstance(payload, dict) and payload.get('role') == self.role else {}
        except (ValueError, TypeError, UnicodeError):
            return {}

    def issue_ticket(self, challenge: str, *, now: float | None = None) -> str:
        return self._encode('launch', {'role': self.role, 'challenge': challenge, 'at': int(time.time() if now is None else now), 'nonce': secrets.token_hex(16)})

    def redeem(self, ticket: str, *, now: float | None = None) -> bool:
        stamp = int(time.time() if now is None else now)
        row = self._decode('launch', ticket)
        at, nonce = row.get('at'), row.get('nonce')
        if not isinstance(at, int) or not isinstance(nonce, str) or row.get('challenge') != self.challenge or not 0 <= stamp - at <= 60:
            return False
        with self._lock:
            self._used = {key: expiry for key, expiry in self._used.items() if expiry >= stamp}
            if nonce in self._used:
                return False
            self._used[nonce] = at + 60
            return True

    def session_cookie(self, *, now: float | None = None) -> str:
        stamp = int(time.time() if now is None else now)
        value = self._encode('session', {'role': self.role, 'at': stamp, 'until': stamp + 30 * 86400})
        return f'{self.cookie_name}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={30 * 86400}'

    def valid_cookie(self, header: str, *, now: float | None = None) -> bool:
        from http.cookies import SimpleCookie, CookieError
        try:
            jar = SimpleCookie()
            jar.load(header)
            value = jar[self.cookie_name].value if self.cookie_name in jar else ''
            row = self._decode('session', value)
            stamp = int(time.time() if now is None else now)
            return isinstance(row.get('at'), int) and isinstance(row.get('until'), int) and row['at'] <= stamp < row['until']
        except (CookieError, ValueError):
            return False

    def control_proof(self, challenge: str, *, now: float | None = None) -> str:
        return self._encode('control', {'role': self.role, 'challenge': challenge, 'at': int(time.time() if now is None else now)})

    def valid_control(self, proof: str) -> bool:
        row = self._decode('control', proof)
        return isinstance(row.get('at'), int) and row.get('challenge') == self.challenge and 0 <= time.time() - row['at'] <= 60

    def gate_page(self, nonce: str) -> str:
        # fragment 不发送给 HTTP 服务；读取后立即从地址栏及当前历史记录移除。
        return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>KnowledgeRadar</title><body><main><h1>KnowledgeRadar</h1><p id="state">请从本机 KnowledgeRadar 启动入口打开。</p></main><script nonce="''' + nonce + '''">(async()=>{const ticket=new URLSearchParams(location.hash.slice(1)).get('launch');if(!ticket)return;history.replaceState(null,'',location.pathname);try{const r=await fetch('/api/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ticket})});if(!r.ok)throw Error();location.replace('/');}catch{document.getElementById('state').textContent='启动凭证已过期，请重新点击本机启动入口。';}})();</script></body></html>'''
