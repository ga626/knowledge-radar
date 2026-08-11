"""Deterministic runtime executable discovery helpers.

Managed login/CDP sessions require Google Chrome.  Other Chromium-family
browsers must never become an implicit substitute merely because Chrome was
not discoverable from a transient process environment.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Optional

@dataclass(frozen=True)
class ManagedChromeSelection:
    """An existing Google Chrome executable and the stable source that found it."""

    path: str
    family: str
    selection_source: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def find_node_exe() -> str:
    return _first_existing(
        [
            os.environ.get("KR_NODE_EXE", ""),
            shutil.which("node") or "",
            r"C:\Program Files\nodejs\node.exe",
            r"C:\Program Files (x86)\nodejs\node.exe",
        ]
    )


def resolve_managed_chrome() -> Optional[ManagedChromeSelection]:
    """Resolve Google Chrome for persistent managed browser sessions.

    The lookup deliberately does not contain Edge or a generic Chromium
    fallback.  A missing Chrome is an actionable configuration state, not a
    reason to change the browser family and risk a different session/fingerprint.
    """

    for path, source in _managed_chrome_candidates():
        normalized = _existing_google_chrome(path)
        if normalized:
            return ManagedChromeSelection(
                path=normalized,
                family="google_chrome",
                selection_source=source,
            )
    return None


def managed_chrome_resolution_summary() -> dict[str, object]:
    """Return a non-launching, safe-to-log view of the managed browser policy."""

    selection = resolve_managed_chrome()
    return {
        "policy": "google_chrome_only_no_implicit_edge_fallback",
        "available": bool(selection),
        "selection": selection.to_dict() if selection else None,
        "required_action": "install_or_configure_google_chrome" if selection is None else "",
    }


def find_chrome_exe_candidates() -> list[str]:
    """Compatibility view for existing callers: only existing Google Chrome paths.

    New code should use :func:`resolve_managed_chrome` so its logs and health
    data retain the browser family and discovery source.
    """

    selection = resolve_managed_chrome()
    return [selection.path] if selection else []


def _managed_chrome_candidates() -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = [
        (os.environ.get("KR_CHROME_EXE", ""), "KR_CHROME_EXE"),
        (os.environ.get("CHROME_PATH", ""), "CHROME_PATH_compatibility"),
    ]
    if os.name == "nt":
        candidates.extend(_windows_app_paths_chrome_candidates())
        home = Path.home()
        candidates.extend(
            [
                (str(home / "AppData" / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe"), "user_home_install"),
                (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "program_files_install"),
                (r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe", "program_files_x86_install"),
            ]
        )
    candidates.extend(
        [
            (shutil.which("chrome.exe") or "", "PATH_chrome_exe"),
            (shutil.which("chrome") or "", "PATH_chrome"),
        ]
    )
    return candidates


def _windows_app_paths_chrome_candidates() -> list[tuple[str, str]]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []

    keys = [
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe", "windows_app_paths_hkcu"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe", "windows_app_paths_hklm"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe", "windows_app_paths_hklm_wow6432"),
    ]
    candidates: list[tuple[str, str]] = []
    for hive, key_path, source in keys:
        try:
            with winreg.OpenKey(hive, key_path) as key:
                value, _value_type = winreg.QueryValueEx(key, "")
            candidates.append((str(value or ""), source))
        except OSError:
            continue
    return candidates


def _existing_google_chrome(candidate: str) -> str:
    path = str(candidate or "").strip().strip('"')
    if not path or not os.path.isfile(path):
        return ""
    name = os.path.basename(path).lower()
    if name not in {"chrome.exe", "chrome"}:
        return ""
    return os.path.abspath(path)


def _first_existing(candidates: Iterable[str]) -> str:
    for candidate in candidates:
        path = str(candidate or "").strip()
        if path and os.path.isfile(path):
            return path
    raise FileNotFoundError("No executable found from configured candidates")
