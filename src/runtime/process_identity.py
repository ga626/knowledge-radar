"""PID 加创建代际，防止重启恢复把复用PID当成原进程。"""
import os
from pathlib import Path


def process_identity(pid=None):
    pid = int(pid or os.getpid())
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ""
        times = [wintypes.FILETIME() for _ in range(4)]
        try:
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
