# DougDoug Note:
# This code contains key codes plus functions to press keys on Windows
# You should not need to modify anything in this file, just use as is.

import sys
import time
from typing import Any, Callable, Optional


IS_WINDOWS = sys.platform == "win32"

#############################################################
#################### DIRECT X KEY CODES #####################
#############################################################

# Key Codes found at: https://docs.microsoft.com/en-us/previous-versions/visualstudio/visual-studio-6.0/aa299374(v=vs.60)
Q = 0x10
W = 0x11
E = 0x12
R = 0x13
T = 0x14
Y = 0x15
U = 0x16
I = 0x17
O = 0x18
P = 0x19
A = 0x1E
S = 0x1F
D = 0x20
F = 0x21
G = 0x22
H = 0x23
J = 0x24
K = 0x25
L = 0x26
Z = 0x2C
X = 0x2D
C = 0x2E
V = 0x2F
B = 0x30
N = 0x31
M = 0x32

LEFT_ARROW = 0xCB
RIGHT_ARROW = 0xCD
UP_ARROW = 0xC8
DOWN_ARROW = 0xD0
ESC = 0x01
ONE = 0x02
TWO = 0x03
THREE = 0x04
FOUR = 0x05
FIVE = 0x06
SIX = 0x07
SEVEN = 0x08
EIGHT = 0x09
NINE = 0x0A
ZERO = 0x0B
MINUS = 0x0C
EQUALS = 0x0D
BACKSPACE = 0x0E
APOSTROPHE = 0x28
SEMICOLON = 0x27
TAB = 0x0F
CAPSLOCK = 0x3A
ENTER = 0x1C
LEFT_CONTROL = 0x1D
RIGHT_CONTROL = 0x9D
LEFT_ALT = 0x38
RIGHT_ALT = 0xB8
LEFT_SHIFT = 0x2A
RIGHT_SHIFT = 0x36
TILDE = 0x29
PRINTSCREEN = 0x37
NUM_LOCK = 0x45
SPACE = 0x39
DELETE = 0x53
COMMA = 0x33
PERIOD = 0x34
BACKSLASH = 0x35
FORWARDSLASH = 0x2B
LEFT_BRACKET = 0x1A
RIGHT_BRACKET = 0x1B

F1 = 0x3B
F2 = 0x3C
F3 = 0x3D
F4 = 0x3E
F5 = 0x3F
F6 = 0x40
F7 = 0x41
F8 = 0x42
F9 = 0x43
F10 = 0x44
F11 = 0x57
F12 = 0x58

NUMPAD_0 = 0x52
NUMPAD_1 = 0x4F
NUMPAD_2 = 0x50
NUMPAD_3 = 0x51
NUMPAD_4 = 0x4B
NUMPAD_5 = 0x4C
NUMPAD_6 = 0x4D
NUMPAD_7 = 0x47
NUMPAD_8 = 0x48
NUMPAD_9 = 0x49
NUMPAD_PLUS = 0x4E
NUMPAD_MINUS = 0x4A
NUMPAD_PERIOD = 0x53
NUMPAD_ENTER = 0x9C
NUMPAD_BACKSLASH = 0xB5

LEFT_MOUSE = 0x100
RIGHT_MOUSE = 0x101
MIDDLE_MOUSE = 0x102
MOUSE3 = 0x103
MOUSE4 = 0x104
MOUSE5 = 0x105
MOUSE6 = 0x106
MOUSE7 = 0x107
MOUSE_WHEEL_UP = 0x108
MOUSE_WHEEL_DOWN = 0x109

key_name_by_code = {
    Q: "q",
    W: "w",
    E: "e",
    R: "r",
    T: "t",
    Y: "y",
    U: "u",
    I: "i",
    O: "o",
    P: "p",
    A: "a",
    S: "s",
    D: "d",
    F: "f",
    G: "g",
    H: "h",
    J: "j",
    K: "k",
    L: "l",
    Z: "z",
    X: "x",
    C: "c",
    V: "v",
    B: "b",
    N: "n",
    M: "m",
    LEFT_ARROW: "left",
    RIGHT_ARROW: "right",
    UP_ARROW: "up",
    DOWN_ARROW: "down",
    ESC: "esc",
    ONE: "1",
    TWO: "2",
    THREE: "3",
    FOUR: "4",
    FIVE: "5",
    SIX: "6",
    SEVEN: "7",
    EIGHT: "8",
    NINE: "9",
    ZERO: "0",
    BACKSPACE: "backspace",
    TAB: "tab",
    ENTER: "enter",
    LEFT_CONTROL: "ctrlleft",
    RIGHT_CONTROL: "ctrlright",
    LEFT_ALT: "altleft",
    RIGHT_ALT: "altright",
    LEFT_SHIFT: "shiftleft",
    RIGHT_SHIFT: "shiftright",
    SPACE: "space",
    PERIOD: ".",
    F1: "f1",
    F2: "f2",
    F3: "f3",
    F4: "f4",
    F5: "f5",
    F6: "f6",
    F7: "f7",
    F8: "f8",
    F9: "f9",
    F10: "f10",
    F11: "f11",
    F12: "f12",
}


def key_name_from_code(hexKeyCode: int) -> str:
    try:
        return key_name_by_code[hexKeyCode]
    except KeyError as exc:
        raise ValueError(f"Unsupported key code for non-Windows input: {hexKeyCode}") from exc


def load_pyautogui():
    try:
        import pyautogui
    except BaseException as exc:
        raise RuntimeError(
            "pyautogui is required for Twitch Plays input injection on Linux/macOS."
        ) from exc
    pyautogui.FAILSAFE = False
    return pyautogui


#############################################################
################## DIRECT INPUT FUNCTIONS ###################
#############################################################

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_SCANCODE = 0x0008

    # Structures for SendInput
    # Correct ULONG_PTR as an integer-sized pointer, not a pointer type
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        ULONG_PTR = ctypes.c_ulonglong
    else:
        ULONG_PTR = ctypes.c_ulong

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class INPUTUNION(ctypes.Union):
        _fields_ = [
            ("ki", KEYBDINPUT),
            ("mi", MOUSEINPUT),
            ("hi", HARDWAREINPUT),
        ]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [
            ("type", wintypes.DWORD),
            ("u", INPUTUNION),
        ]

    WinDLL: Any = getattr(ctypes, "WinDLL")
    get_last_error: Callable[[], Optional[int]] = getattr(
        ctypes, "get_last_error", lambda: None
    )

    user32 = WinDLL("user32", use_last_error=True)
    SendInput = user32.SendInput
    SendInput.restype = wintypes.UINT
    SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)

    def send_key(scan_code: int, flags: int) -> None:
        ki = KEYBDINPUT(
            wVk=0,
            wScan=scan_code & 0xFFFF,
            dwFlags=flags,
            time=0,
            dwExtraInfo=ULONG_PTR(0),
        )
        inp = INPUT(type=1, u=INPUTUNION(ki=ki))
        n = SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        if n != 1:
            err = get_last_error()
            # Print only on failure to avoid noisy logs
            print(f"SendInput failed (scan=0x{scan_code:X} flags=0x{flags:X}) err={err}")

    def HoldKey(hexKeyCode: int) -> None:
        send_key(hexKeyCode, KEYEVENTF_SCANCODE)

    def ReleaseKey(hexKeyCode: int) -> None:
        send_key(hexKeyCode, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP)
else:
    def HoldKey(hexKeyCode: int) -> None:
        load_pyautogui().keyDown(key_name_from_code(hexKeyCode))

    def ReleaseKey(hexKeyCode: int) -> None:
        load_pyautogui().keyUp(key_name_from_code(hexKeyCode))


# Holds down a key for the specified number of seconds
def HoldAndReleaseKey(hexKeyCode: int, seconds: float) -> None:
    HoldKey(hexKeyCode)
    time.sleep(seconds)
    ReleaseKey(hexKeyCode)
