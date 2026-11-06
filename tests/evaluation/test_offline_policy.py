"""隔离策略须能测自己创建的服务，同时拒绝访问日用服务和外网。"""
from pathlib import Path
import os
import subprocess
import sys
import time
import pytest


def test_only_fixture_owned_loopback_listener_is_allowed():
    root=Path(__file__).resolve().parents[2]
    code='''
import socket
from scripts.kr_eval.offline_guard import install_network_policy
install_network_policy()
with socket.socket() as listener:
    listener.bind(('127.0.0.1',0));listener.listen(1)
    with socket.create_connection(listener.getsockname(),timeout=1) as client:
        peer,_=listener.accept();peer.close()
for address in [('127.0.0.1',18882),('127.0.0.1',18883),('93.184.216.34',443),('example.org',443)]:
    try:
        socket.create_connection(address,timeout=.1)
    except RuntimeError as error:
        assert 'EVAL_OFFLINE_POLICY' in str(error)
    else:
        raise AssertionError('escaped isolation')
print('PASS')
'''
    bootstrap='import sys;sys.path.insert(0,'+repr(str(root))+');'+code
    result=subprocess.run([sys.executable,'-X','utf8','-c',bootstrap],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='PASS'


@pytest.mark.skipif(os.name != 'nt', reason='Windows Job子进程清理')
def test_terminated_probe_reclaims_its_regular_child(tmp_path):
    import ctypes
    from ctypes import wintypes
    root = Path(__file__).resolve().parents[2]
    child_pid_file = tmp_path / 'child-pid.txt'
    code = (
        'import runpy,subprocess,sys,time;from pathlib import Path;'
        f"probe=runpy.run_path({str(root/'scripts/kr_eval/pytest_entry.py')!r});"
        "job=probe['owned_probe_job'](allow_breakaway=False);"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        f"Path({str(child_pid_file)!r}).write_text(str(child.pid));time.sleep(60)"
    )
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    launcher = subprocess.Popen([sys.executable, '-X', 'utf8', '-c', code], cwd=root)
    child_handle = None
    try:
        deadline = time.monotonic() + 10
        while not child_pid_file.is_file() and time.monotonic() < deadline and launcher.poll() is None:
            time.sleep(.05)
        assert child_pid_file.is_file(), 'probe did not start its child'
        child_handle = kernel.OpenProcess(0x100001, False, int(child_pid_file.read_text()))
        assert child_handle, ctypes.WinError(ctypes.get_last_error())
        launcher.kill()
        launcher.wait(timeout=5)
        assert kernel.WaitForSingleObject(child_handle, 5000) == 0, 'probe child survived termination'
    finally:
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=5)
        if child_handle:
            if kernel.WaitForSingleObject(child_handle, 0) != 0:
                kernel.TerminateProcess(child_handle, 1)
            kernel.CloseHandle(child_handle)
