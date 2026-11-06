"""显式设置工作树导入优先级，兼容忽略 PYTHONPATH 的 Windows 嵌入式 Python。"""
from pathlib import Path
import os
import sys
import tempfile

def owned_probe_job(*, allow_breakaway=True):
    """只给当前隔离探针创建可脱离的嵌套Job，不改变宿主Job。"""
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes
    class Basic(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                    ('flags', wintypes.DWORD), ('minimum', ctypes.c_size_t),
                    ('maximum', ctypes.c_size_t), ('active', wintypes.DWORD),
                    ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                    ('scheduling', wintypes.DWORD)]
    class Extended(ctypes.Structure):
        _fields_ = [('basic', Basic), ('io', ctypes.c_uint64 * 6),
                    ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                    ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    handle = kernel.CreateJobObjectW(None, None)
    limits = Extended()
    limits.basic.flags = 0x2000 | (0x0800 if allow_breakaway else 0)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if (not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess())):
        error = ctypes.get_last_error()
        kernel.CloseHandle(handle)
        raise ctypes.WinError(error)
    # 必须保留至进程退出；关闭会回收本Job成员，不能在活着的当前进程上提前close。
    return handle


if __name__ == "__main__":
    root = Path.cwd()
    isolated = Path(tempfile.mkdtemp(prefix="kr-eval-tests-"))
    os.environ.setdefault("KR_STATE_DIR", str(isolated / "runtime"))
    os.environ.setdefault("KR_DATA_ROOT", str(isolated / "data"))
    os.environ.setdefault("KR_DESKTOP_NOTIFICATIONS_DISABLED", "1")
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "src"))
    owned_job = owned_probe_job(allow_breakaway='--kr-owned-host-process' in sys.argv)
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    import pytest
    raise SystemExit(pytest.main(sys.argv[1:]))
