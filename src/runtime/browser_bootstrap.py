"""Windows日常Chrome的短命启动守护；只关带本次随机标记的空白窗口。"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid


def chrome_command(executable, marker, *, isolated_profile=None):
    if not re.fullmatch(r'KR-startup-[a-f0-9]{32}', marker):
        raise ValueError('invalid_browser_bootstrap_marker')
    command = [str(executable), '--no-first-run', '--no-default-browser-check']
    if isolated_profile is not None:
        command.append('--user-data-dir=' + str(isolated_profile))
    # No page script/network, remote debugging, authentication or extension flags.
    # An app window gives this blank bootstrap an unambiguous OS-visible title.
    return command + ['--app=data:text/html,<title>' + marker + '</title>']


def start(executable):
    from runtime.paths import runtime_state_dir
    from runtime.process import silent_subprocess_popen
    if os.name != 'nt':
        raise OSError('quiet_daily_chrome_start_requires_windows')
    marker = 'KR-startup-' + uuid.uuid4().hex
    receipt = runtime_state_dir() / 'browser_bootstrap' / (marker + '.json')
    receipt.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    # Pin the helper to this candidate, not an installed package on PATH.
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get('PYTHONPATH', '')
    return silent_subprocess_popen([sys.executable, str(Path(__file__).resolve()),
        '--chrome', str(executable), '--marker', marker, '--receipt', str(receipt)],
        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Windows:
    def __init__(self):
        self.api = ctypes.WinDLL('user32', use_last_error=True)
        self.api.GetForegroundWindow.restype = wintypes.HWND
        self.api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.api.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.api.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.api.IsWindowVisible.argtypes = [wintypes.HWND]
        self.api.IsIconic.argtypes = [wintypes.HWND]
        self.api.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.api.EnumWindows.argtypes = [self.callback_type, wintypes.LPARAM]

    def present(self, identity, foreground):
        self.api.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        self.api.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.api.ShowWindowAsync(identity["handle"], 9 if foreground else 7)
        if foreground:
            self.api.SetForegroundWindow(identity["handle"])

    def foreground(self):
        return int(self.api.GetForegroundWindow() or 0)

    def list(self):
        rows = []
        def collect(handle, unused):
            kind = ctypes.create_unicode_buffer(128)
            self.api.GetClassNameW(handle, kind, len(kind))
            if kind.value != 'Chrome_WidgetWin_1' or not self.api.IsWindowVisible(handle):
                return True
            title = ctypes.create_unicode_buffer(512)
            self.api.GetWindowTextW(handle, title, len(title))
            process = wintypes.DWORD()
            self.api.GetWindowThreadProcessId(handle, ctypes.byref(process))
            rows.append({'handle': int(handle), 'pid': process.value, 'title': title.value,
                         'minimized': bool(self.api.IsIconic(handle))})
            return True
        self.api.EnumWindows(self.callback_type(collect), 0)
        return rows

    def close_if_owned(self, identity, marker):
        current = next((row for row in self.list() if row['handle'] == identity['handle']), None)
        if not current or current['pid'] != identity['pid'] or current['title'] != marker:
            return False
        if self.foreground() == current['handle']:
            return False  # The user took this window; relinquish it.
        return bool(self.api.PostMessageW(current['handle'], 0x0010, 0, 0))


def supervise(executable, marker, receipt, *, isolated_profile=None, lifetime_s=45, windows=None):
    from runtime.state_file import write_state
    windows = windows or Windows()
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 7  # SW_SHOWMINNOACTIVE, not SW_HIDE/foreground minimize.
    state = {'schema': 'kr-browser-bootstrap/v1', 'marker': marker,
             'isolated_probe': isolated_profile is not None, 'may_close_browser_process': False,
             'owned_window_ever_foreground': False, 'bootstrap_window_closed': False}
    process = subprocess.Popen(chrome_command(executable, marker, isolated_profile=isolated_profile),
        startupinfo=startup, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    state['launcher_pid'] = process.pid
    deadline = time.monotonic() + lifetime_s
    identity = None
    try:
        while True:
            rows = windows.list()
            current = next((r for r in rows if r['title'] == marker), None)
            if current and identity is None:
                identity = current
                state.update(bootstrap_seen=True, minimized_at_discovery=current['minimized'])
            if identity:
                if not current or (current['handle'], current['pid']) != (identity['handle'], identity['pid']):
                    state['stop_reason'] = 'bootstrap_closed_or_identity_changed'
                    break
                if windows.foreground() == identity['handle']:
                    state['owned_window_ever_foreground'] = True
                    # Foreground alone does not establish user intent. Another
                    # Chrome window closing can activate this minimized blank.
                    # Never close while foreground; retry only until our deadline.
                    if time.monotonic() >= deadline:
                        state['stop_reason'] = 'foreground_at_cleanup_deadline'
                        break
                    time.sleep(.05)
                    continue
                # The executing driver has created a work window, or the user
                # has opened their own one. Closing just the bootstrap is safe.
                has_other = any(r['pid'] == identity['pid'] and r['handle'] != identity['handle'] for r in rows)
                if has_other or time.monotonic() >= deadline:
                    state['close_requested'] = windows.close_if_owned(identity, marker)
                    state['stop_reason'] = 'work_window_available' if has_other else 'startup_deadline'
                    break
            elif time.monotonic() >= deadline:
                state['stop_reason'] = 'bootstrap_not_observed'
                break
            time.sleep(.05)
    finally:
        # Even if the task owner exits, this short-lived helper has its own
        # deadline. A close timeout remains an explicit gap; never kill Chrome.
        if state.get('close_requested'):
            until = time.monotonic() + 3
            while time.monotonic() < until:
                if not any(r['handle'] == identity['handle'] and r['pid'] == identity['pid']
                           for r in windows.list()):
                    state['bootstrap_window_closed'] = True
                    break
                time.sleep(.05)
        write_state(Path(receipt), state)
    return state


def window_presentation(request, *, windows=None):
    """只管理以随机标题核验过的KR工作窗；不枚举后随意收起Chrome。"""
    windows = windows or Windows()
    rows = windows.list()
    if request.get('action') == 'bind':
        marker = request.get('marker', '')
        if not re.fullmatch(r'KR-work-[a-f0-9]{32}', marker):
            raise ValueError('invalid_work_window_marker')
        deadline = time.monotonic() + .6
        while True:
            matches = [r for r in rows if r['title'].lstrip('\u200e\u200f') == marker or r['title'].lstrip('\u200e\u200f').startswith(marker + ' - ')]
            if matches or time.monotonic() >= deadline:
                break
            time.sleep(.03)
            rows = windows.list()
        if len(matches) != 1:
            raise ValueError('work_window_not_unique')
        return {'handle': matches[0]['handle'], 'pid': matches[0]['pid']}
    identity = request.get('identity') or {}
    current = next((r for r in rows if r['handle'] == identity.get('handle') and r['pid'] == identity.get('pid')), None)
    title = request.get('title')
    if not current or not title or not (current['title'] == title or current['title'].startswith(title + ' - ')):
        raise ValueError('work_window_identity_changed')
    if request.get('action') != 'present' or not isinstance(request.get('foreground'), bool):
        raise ValueError('invalid_window_presentation')
    windows.present(identity, request['foreground'])
    return {'requested_foreground': request['foreground'], 'mechanism': 'kr_windows_presentation'}


def main():
    if sys.argv[1:] == ["--presentation"]:
        try:
            print(json.dumps(window_presentation(json.load(sys.stdin))))
            return 0
        except Exception as exc:
            print(json.dumps({"error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__}))
            return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chrome', required=True)
    parser.add_argument('--marker', required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--isolated-probe', action='store_true')
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('Windows only')
    profile = None
    if args.isolated_probe:
        profile = args.receipt.parent / 'empty-probe-profile'
        profile.mkdir(parents=True, exist_ok=False)
    try:
        supervise(args.chrome, args.marker, args.receipt, isolated_profile=profile,
                  lifetime_s=12 if args.isolated_probe else 45)
    except Exception as exc:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps({'error_type': type(exc).__name__,
            'may_close_browser_process': False}), encoding='utf-8')
        return 1
    return 0


if __name__ == '__main__':
    # The developer interpreter may have an older checkout in site-packages.
    # The helper must import the exact runtime tree that created this process.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
