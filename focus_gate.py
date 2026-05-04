"""
Foreground-window focus gate for Twitch Plays.

- set_focus_target(process_name?: str, title_contains?: str)
    Configure which window/process must be focused for input injection.

- is_target_focused() -> bool
    Returns True if the current foreground window matches the configured target.

Windows uses Win32 APIs. Linux uses X11 window metadata when available.
If a target is configured and the active window cannot be inspected, the gate
returns False instead of silently allowing input to the wrong window.
"""

from __future__ import annotations

import ast
import sys
import ctypes
import re
import shutil
import subprocess
from typing import Optional

import psutil


TARGET_PROCESS: Optional[str] = None
TITLE_CONTAINS: Optional[str] = None


def set_focus_target(process_name: Optional[str] = None, title_contains: Optional[str] = None) -> None:
    global TARGET_PROCESS, TITLE_CONTAINS
    # Use only the explicitly provided values; no environment fallbacks
    TARGET_PROCESS = (process_name or "").strip().lower() or None
    TITLE_CONTAINS = (title_contains or "").strip().lower() or None


def is_windows() -> bool:
    return sys.platform == "win32" and ctypes is not None


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def parse_xprop_window_id(output: str) -> Optional[str]:
    match = re.search(r"window id # (0x[0-9a-fA-F]+)", output or "")
    if not match:
        return None
    window_id = match.group(1).lower()
    return None if window_id == "0x0" else window_id


def parse_xprop_window_details(output: str) -> tuple[Optional[int], Optional[str]]:
    pid: Optional[int] = None
    title: Optional[str] = None
    for raw_line in (output or "").splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if key.startswith("_NET_WM_PID("):
            try:
                pid = int(value)
            except ValueError:
                pid = None
            continue
        if title is None and (key.startswith("_NET_WM_NAME(") or key.startswith("WM_NAME(")):
            if value.startswith('"'):
                try:
                    parsed = ast.literal_eval(value)
                except Exception:
                    parsed = value.strip('"')
            else:
                parsed = value
            title = str(parsed or "").strip() or None
    return pid, title


def read_linux_active_window() -> Optional[tuple[Optional[str], Optional[str]]]:
    xprop_path = shutil.which("xprop")
    if not xprop_path:
        return None

    try:
        root_result = subprocess.run(
            [xprop_path, "-root", "_NET_ACTIVE_WINDOW"],
            capture_output=True,
            text=True,
            check=False,
            timeout=0.5,
        )
    except Exception:
        return None
    if root_result.returncode != 0:
        return None

    window_id = parse_xprop_window_id(root_result.stdout)
    if not window_id:
        return None

    try:
        window_result = subprocess.run(
            [xprop_path, "-id", window_id, "_NET_WM_PID", "_NET_WM_NAME", "WM_NAME"],
            capture_output=True,
            text=True,
            check=False,
            timeout=0.5,
        )
    except Exception:
        return None
    if window_result.returncode != 0:
        return None

    pid, title = parse_xprop_window_details(window_result.stdout)
    process_name = None
    if pid and psutil is not None:
        try:
            process_name = psutil.Process(pid).name()
        except Exception:
            process_name = None
    return process_name, title


def matches_focus_target(process_name: Optional[str], title: Optional[str]) -> bool:
    title_ok = True
    if TITLE_CONTAINS:
        title_ok = TITLE_CONTAINS in ((title or "").lower())

    proc_ok = True
    if TARGET_PROCESS and psutil is not None:
        proc_ok = ((process_name or "").lower() == TARGET_PROCESS)

    return title_ok and proc_ok


def is_target_focused() -> bool:
    """Return True if the foreground window matches configured process/title.

    If no target configured: returns True.
    """
    if TARGET_PROCESS is None and TITLE_CONTAINS is None:
        return True

    if is_windows():
        try:
            user32 = ctypes.windll.user32
            GetForegroundWindow = user32.GetForegroundWindow
            GetWindowTextLengthW = user32.GetWindowTextLengthW
            GetWindowTextW = user32.GetWindowTextW
            GetWindowThreadProcessId = user32.GetWindowThreadProcessId

            hwnd = GetForegroundWindow()
            if not hwnd:
                return False

            length = GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value

            process_name = None
            if TARGET_PROCESS and psutil is not None:
                from ctypes import wintypes

                pid = wintypes.DWORD()
                GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                try:
                    process_name = psutil.Process(int(pid.value)).name()
                except Exception:
                    process_name = None

            return matches_focus_target(process_name, title)
        except Exception:
            return False

    if is_linux():
        active_window = read_linux_active_window()
        if active_window is None:
            return False
        process_name, title = active_window
        return matches_focus_target(process_name, title)

    return False
