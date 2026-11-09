"""PID 加创建代际，防止重启恢复把复用PID当成原进程。"""
import os
from pathlib import Path


def observe_process(pid):
    """只读、三态观测；访问被拒绝不等于进程已退出。"""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return {'status': 'unknown', 'identity': ''}
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return {'status': 'missing' if ctypes.get_last_error() == 87 else 'unknown', 'identity': ''}
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return {'status': 'unknown', 'identity': ''}
            if code.value != 259:
                return {'status': 'missing', 'identity': ''}
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return {'status': 'alive', 'identity': ''}
            return {'status': 'alive', 'identity': f'{pid}:{(times[0].dwHighDateTime << 32) | times[0].dwLowDateTime}'}
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return {'status': 'missing', 'identity': ''}
    except OSError:
        return {'status': 'unknown', 'identity': ''}
    return {'status': 'alive', 'identity': process_identity(pid)}


def process_identity(pid=None):
    pid = int(pid or os.getpid())
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ""
        times = [wintypes.FILETIME() for _ in range(4)]
        try:
            exit_code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
                return ""
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return ""
            return f"{pid}:{(times[0].dwHighDateTime << 32) | times[0].dwLowDateTime}"
        finally:
            kernel.CloseHandle(handle)
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return f"{pid}:{fields[19]}"
    except OSError:
        return ""
