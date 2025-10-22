"""
Windows foreground-window focus gate for Twitch Plays.

- set_focus_target(process_name?: str, title_contains?: str)
    Configure which window/process must be focused for input injection.

- is_target_focused() -> bool
    Returns True if the current foreground window matches the configured target.

Behavior on non-Windows: returns True (no-op), so development on Linux/macOS
does not block. Only enable real gating on Windows hosts.
"""

from __future__ import annotations

import os
from typing import Optional
import ctypes
from ctypes import wintypes
import psutil


TARGET_PROCESS: Optional[str] = None
TITLE_CONTAINS: Optional[str] = None


def set_focus_target(process_name: Optional[str] = None, title_contains: Optional[str] = None) -> None:
    global TARGET_PROCESS, TITLE_CONTAINS
    # Use only the explicitly provided values; no environment fallbacks
    TARGET_PROCESS = (process_name or "").strip().lower() or None
    TITLE_CONTAINS = (title_contains or "").strip().lower() or None


def is_windows() -> bool:
    return os.name == "nt" and ctypes is not None


def is_target_focused() -> bool:
    """Return True if the foreground window matches configured process/title.

    Non-Windows: returns True.
    If no target configured: returns True.
    """
    if not is_windows():
        return True

    if TARGET_PROCESS is None and TITLE_CONTAINS is None:
        return True

    if psutil is None:
        # without psutil we can only check title
        pass

    # Win32 API calls
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        GetForegroundWindow = user32.GetForegroundWindow
        GetWindowTextLengthW = user32.GetWindowTextLengthW
        GetWindowTextW = user32.GetWindowTextW
        GetWindowThreadProcessId = user32.GetWindowThreadProcessId

        hwnd = GetForegroundWindow()
        if not hwnd:
            return False

        # Title check (optional)
        title_ok = True
        if TITLE_CONTAINS:
            length = GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            GetWindowTextW(hwnd, buf, length + 1)
            title = (buf.value or "").lower()
            title_ok = TITLE_CONTAINS in title

        # Process name check (optional)
        proc_ok = True
        if TARGET_PROCESS and psutil is not None:
            pid = wintypes.DWORD()
            GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            name = None
            try:
                p = psutil.Process(int(pid.value))
                name = (p.name() or "").lower()
            except Exception:
                name = None
            proc_ok = (name == TARGET_PROCESS)

        return title_ok and proc_ok
    except Exception:
        return True