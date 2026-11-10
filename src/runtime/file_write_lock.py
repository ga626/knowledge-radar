"""小型跨进程文件写锁；锁文件保持稳定，不随被保护文件的原子替换而变化。"""
from contextlib import contextmanager
import os
from pathlib import Path
import threading
import time
import weakref

_LOCK = threading.RLock()
_PATH_LOCKS = weakref.WeakValueDictionary()


def read_observed(path, *, max_bytes=-1):
    """读取完整文件快照，并允许Windows写入方原子替换路径。"""
    if os.name!='nt':
        with path.open('rb') as stream:return stream.read(max_bytes)
    import ctypes,msvcrt
    from ctypes import wintypes
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    api.CreateFileW.restype=wintypes.HANDLE
    api.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=api.CreateFileW(str(path),0x80000000,7,None,3,0x00200000,None)
    if handle==wintypes.HANDLE(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    try:descriptor=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
    except BaseException:
        api.CloseHandle(handle)
        raise
    with os.fdopen(descriptor,'rb') as stream:return stream.read(max_bytes)


@contextmanager
def file_write_lock(path):
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        key = str(lock_path.resolve()).casefold() if os.name == 'nt' else str(lock_path.resolve())
        local_lock = _PATH_LOCKS.setdefault(key, threading.RLock())
    if not local_lock.acquire(timeout=30):
        raise TimeoutError('file_write_lock_timeout')
    try:
        with _process_lock(lock_path):
            yield
    finally:
        local_lock.release()


@contextmanager
def _process_lock(lock_path):
    with lock_path.open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            until = time.monotonic() + 30
            while True:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= until:
                        raise TimeoutError('file_write_lock_timeout')
                    time.sleep(.05)
        else:
            import fcntl
            until = time.monotonic() + 30
            while True:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= until:
                        raise TimeoutError('file_write_lock_timeout')
                    time.sleep(.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
