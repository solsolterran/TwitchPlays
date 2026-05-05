"""
Twitch Plays Every Game
----------------------------------------------------------------

Run:
$ python TwitchPlays_Everything.py --game gta5

This runner implements:
    - 3s voting window with per-user dedupe
    - Soft kill toggle that stops inputs (Alt+Shift+P by default) and hard kill that stops program (Ctrl+Shift+Backspace)
    - Windows focus gate (via profile `target_process` / `window_title_contains`)
    - Immediate reset: commands are pulses, followed by release_all()
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from typing import Callable, Dict, List, Optional, Tuple
from pathlib import Path
import json
import threading
from collections import deque

try:
    import keyboard
except BaseException:
    keyboard = None

try:
    import pyautogui
except BaseException:
    pyautogui = None

try:
    import pydirectinput
except BaseException:
    pydirectinput = None

import TwitchPlays_Connection
from TwitchPlays_KeyCodes import *
from focus_gate import set_focus_target, is_target_focused

##################### STREAM / PLATFORM CONFIG #####################

STREAM_CONFIG = TwitchPlays_Connection.load_twitchplays_config()

TWITCH_CHANNEL = str(STREAM_CONFIG.get("twitch_channel") or "").strip().lower()

# Sources: default to both Twitch and YouTube; YouTube is skipped gracefully
# at runtime when not configured.
STREAM_SOURCES = ["twitch", "youtube"]

YOUTUBE_CHANNEL_ID = str(STREAM_CONFIG.get("youtube_channel_id") or "").strip() or None
YOUTUBE_API_KEY = str(STREAM_CONFIG.get("youtube_api_key") or "").strip() or None
YOUTUBE_STREAM_URL = str(STREAM_CONFIG.get("youtube_stream_url") or "").strip() or None

##################### MESSAGE QUEUE VARIABLES #####################

"""Core timing and safety caps."""
# Voting window length (seconds)
VOTE_WINDOW_SEC = 3.0

# Caps and limits
MAX_VOTES_PER_WINDOW = 200
MAX_MESSAGE_LENGTH = 64
MOUSE_COORD_LIMIT = 100.0
# Global minimum gap between executed winners (milliseconds)
MIN_EXECUTION_GAP_MS = 300
# Circuit breaker: on >=3 errors within 10s, auto soft-disable
ERROR_TRIP_THRESHOLD = 3
ERROR_WINDOW_SEC = 10.0

# Minimal idle sleep to avoid 100% CPU when chat is quiet
IDLE_SLEEP_SEC = float("0.005")

# Count down before starting, so you have time to load up the game
STARTUP_COUNTDOWN = int("5")

# Select game implementation
DEFAULT_GAME = "gta5"
PROFILE_DIR = Path(__file__).parent / "profiles"
PROFILE_TEMPLATE_PATH = PROFILE_DIR / "template.json"

if pyautogui is not None:
    pyautogui.FAILSAFE = False
if pydirectinput is not None and hasattr(pydirectinput, "FAILSAFE"):
    pydirectinput.FAILSAFE = False


HELD_KEYS: set[int] = set()


def press_and_release(keycode: int, seconds: float = 0.1) -> None:
    """Press and release a key for N seconds, tracking holds for cleanup."""
    try:
        HELD_KEYS.add(keycode)
        HoldKey(keycode)
        time.sleep(max(0.01, float(seconds)))
    finally:
        ReleaseKey(keycode)
        HELD_KEYS.discard(keycode)


def press_hold(keycode: int) -> None:
    """Hold a key (tracked). Intended for short pulses in this runner."""
    HELD_KEYS.add(keycode)
    HoldKey(keycode)


def release(keycode: int) -> None:
    ReleaseKey(keycode)
    HELD_KEYS.discard(keycode)


def load_mouse_backend():
    if sys.platform == "win32" and pydirectinput is not None:
        return ("pydirectinput", pydirectinput)
    if pyautogui is not None:
        return ("pyautogui", pyautogui)
    raise RuntimeError(
        "Mouse input requires pyautogui on Linux/macOS or pyautogui/pydirectinput on Windows."
    )


def mouse_down(btn: str = "left"):
    backend = load_mouse_backend()[1]
    backend.mouseDown(button=btn)


def mouse_up(btn: str = "left"):
    backend = load_mouse_backend()[1]
    backend.mouseUp(button=btn)


def mouse_click(btn: str = "left"):
    backend = load_mouse_backend()[1]
    backend.click(button=btn)


def mouse_move_to(x: int, y: int):
    backend = load_mouse_backend()[1]
    backend.moveTo(x, y)


def mouse_move(dx: int = 0, dy: int = 0):
    backend_name, backend = load_mouse_backend()
    if backend_name == "pydirectinput":
        backend.moveRel(dx, dy, relative=True)
        return
    backend.moveRel(dx, dy)


def screen_size() -> Tuple[int, int]:
    if pyautogui is None:
        raise RuntimeError("pyautogui is required for mouse_click_at screen coordinates.")
    size = pyautogui.size()
    return int(size.width), int(size.height)


def clamp_number(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def point_for_normalized_mouse_coordinate(x_value: float, y_value: float) -> Tuple[int, int]:
    width, height = screen_size()
    center_x = (width - 1) / 2.0
    center_y = (height - 1) / 2.0
    x = center_x + (clamp_number(x_value, -MOUSE_COORD_LIMIT, MOUSE_COORD_LIMIT) / MOUSE_COORD_LIMIT) * center_x
    y = center_y + (clamp_number(y_value, -MOUSE_COORD_LIMIT, MOUSE_COORD_LIMIT) / MOUSE_COORD_LIMIT) * center_y
    return (
        int(round(clamp_number(x, 0, width - 1))),
        int(round(clamp_number(y, 0, height - 1))),
    )


def release_all() -> None:
    """Release all tracked key holds and mouse buttons."""
    try:
        for k in list(HELD_KEYS):
            try:
                ReleaseKey(k)
            except Exception:
                pass
            HELD_KEYS.discard(k)
    except Exception:
        pass
    # Mouse buttons (best-effort)
    for b in ("left", "right", "middle"):
        try:
            mouse_up(b)
        except Exception:
            pass


##########################################################
# Game profile system
##########################################################


class ProfileGame:
    # mapping of chat message -> callable
    commands: Dict[str, Callable[[str], None]]

    def __init__(self, profile: dict):
        self.commands = {}
        aliases: Dict[str, str] = {
            (k or "").strip().lower(): (v or "").strip().lower()
            for k, v in (profile.get("aliases") or {}).items()
            if (k or "").strip() and (v or "").strip()
        }
        self.canonical_to_macros: Dict[str, list] = profile.get("macros") or {}

        def make_handler(canonical: str):
            def handler(user: str) -> None:
                execute_macro(self.canonical_to_macros.get(canonical) or [])

            return handler

        for alias, canonical in aliases.items():
            self.commands[alias] = make_handler(canonical)


class MultiChat:
    def __init__(self, twitch=None, youtube=None):
        self.t = twitch
        self.y = youtube

    def receive_messages(self):
        msgs = []
        if self.t:
            msgs.extend(self.t.twitch_receive_messages())
        if self.y:
            msgs.extend(self.y.twitch_receive_messages())
        # normalize once here and filter message length
        out = []
        for m in msgs:
            msg = (m.get("message") or "").strip()
            user = (m.get("username") or "").strip()
            if not msg or not user:
                continue
            if len(msg) > MAX_MESSAGE_LENGTH:
                continue
            out.append({"message": msg.lower(), "username": user.lower()})
        return out

    def close(self) -> None:
        if self.t:
            try:
                self.t.close()
            except Exception:
                pass
        if self.y:
            session = getattr(self.y, "session", None)
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
            thread_pool = getattr(self.y, "thread_pool", None)
            if thread_pool is not None:
                try:
                    thread_pool.shutdown(wait=False)
                except Exception:
                    pass


##########################################################
# Runtime
##########################################################


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Twitch Plays Every Game")
    p.add_argument("--game", default=DEFAULT_GAME, help="Game key ('gta5')")
    p.add_argument(
        "--countdown",
        type=int,
        default=STARTUP_COUNTDOWN,
        help="Startup countdown seconds",
    )
    p.add_argument(
        "--sources",
        default=",".join(STREAM_SOURCES),
        help="Separated chat sources: twitch,youtube",
    )
    return p.parse_args()


def parse_sources(raw_value: str) -> List[str]:
    sources = [
        source.strip().lower() for source in raw_value.split(",") if source.strip()
    ]
    if not sources:
        raise SystemExit("--sources must include twitch, youtube, or both.")
    for source in sources:
        if source not in {"twitch", "youtube"}:
            raise SystemExit("--sources must only include twitch or youtube.")
    return sources


def profile_path_for_game(game_key: str) -> Path:
    key = game_key.strip()
    if not key or "/" in key or "\\" in key:
        raise SystemExit("--game must be a profile name, not a path.")
    if key.endswith(".json"):
        raise SystemExit("Use --game without .json, for example --game minecraft.")
    return PROFILE_DIR / f"{key}.json"


def create_profile_from_template(profile_path: Path) -> None:
    if not PROFILE_TEMPLATE_PATH.exists():
        raise SystemExit(f"Profile template not found at {PROFILE_TEMPLATE_PATH}.")
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PROFILE_TEMPLATE_PATH, profile_path)
    print(f"Created starter profile: profiles/{profile_path.name}")


"""Mutable runtime state."""
injection_enabled = False
input_mode = "chat"  # "chat" or "external"
# Circuit breaker rolling window of error timestamps
error_times: deque[float] = deque()
# Global last execution time (seconds)
last_exec_ts: float = 0.0

# hotkeys (configurable)
TOGGLE_HOTKEY = "alt+shift+p"
KILL_HOTKEY = "ctrl+shift+backspace"

last_toggle_ts = 0.0
DEBOUNCE_SEC = 0.25


def toggle_injection():
    global injection_enabled, last_toggle_ts
    now = time.time()
    if now - last_toggle_ts < DEBOUNCE_SEC:
        return
    last_toggle_ts = now
    injection_enabled = not injection_enabled
    state = "ENABLED" if injection_enabled else "DISABLED"
    print(f"Injection {state}.")
    if not injection_enabled:
        release_all()


def main():
    args = parse_args()
    global last_exec_ts, injection_enabled

    profile_path = profile_path_for_game(args.game)
    if not profile_path.exists():
        create_profile_from_template(profile_path)

    game = select_profile_game(profile_path)
    print(f"Loaded profile: {profile_path.name} with {len(game.commands)} commands")
    # Configure focus target from profile
    try:
        with open(profile_path, "r", encoding="utf-8") as f:
            prof = json.load(f)
        proc = prof.get("target_process")
        title = prof.get("window_title_contains")
        set_focus_target(process_name=proc, title_contains=title)
    except Exception:
        set_focus_target(process_name=None, title_contains=None)

    # countdown so you can focus the game window, etc.
    countdown = args.countdown
    while countdown > 0:
        print(countdown)
        countdown -= 1
        time.sleep(1)

    sources = parse_sources(args.sources)
    t = None
    y = None

    try:
        if "twitch" in sources:
            if not TWITCH_CHANNEL:
                raise SystemExit(
                    "TWITCH_CHANNEL is required when Twitch chat is enabled."
                )
            t = TwitchPlays_Connection.Twitch()
            t.twitch_connect(TWITCH_CHANNEL)
        if "youtube" in sources:
            # Only connect to YouTube if configuration is present
            if YOUTUBE_CHANNEL_ID or YOUTUBE_STREAM_URL:
                y = TwitchPlays_Connection.YouTube(api_key=YOUTUBE_API_KEY)
                y.youtube_connect(
                    YOUTUBE_CHANNEL_ID, YOUTUBE_STREAM_URL, api_key=YOUTUBE_API_KEY
                )
            else:
                y = None
    except BaseException:
        MultiChat(t, y).close()
        raise

    if not t and not y:
        raise SystemExit(
            "No valid chat sources. Use --sources twitch,youtube or set STREAM_SOURCES."
        )

    client = MultiChat(t, y)

    # Hotkeys
    try:
        if keyboard is None:
            raise RuntimeError("keyboard package is not installed")
        keyboard.add_hotkey(TOGGLE_HOTKEY, toggle_injection, suppress=False)
        keyboard.add_hotkey(KILL_HOTKEY, lambda: os._exit(0), suppress=False)
        print(f"Soft toggle: {TOGGLE_HOTKEY} | Hard kill: {KILL_HOTKEY}")
    except Exception as e:
        print(f"Hotkeys unavailable: {e}")

    # Voting window state
    window_end = time.time() + VOTE_WINDOW_SEC
    last_vote_by_user: Dict[str, Tuple[str, float]] = {}
    counts: Dict[str, int] = {}
    last_ts_by_cmd: Dict[str, float] = {}
    unknown = 0

    # Precompute allowlist (commands)
    allow = set(game.commands.keys())

    # external messages input (P1)
    external_messages: List[dict] = []
    external_lock = threading.Lock()

    def drain_external() -> List[dict]:
        with external_lock:
            out = list(external_messages)
            external_messages.clear()
            return out

    # expose for API
    globals()["tp_external_sink"] = external_messages
    globals()["tp_external_lock"] = external_lock
    globals()["tp_allow_set"] = allow
    globals()["tp_input_mode_ref"] = lambda: input_mode

    print("Press soft toggle to enable listening.")

    try:
        while True:

            # Read messages
            incoming = []
            # Chat messages only if mode == chat
            if input_mode == "chat":
                msgs = client.receive_messages() or []
                incoming.extend(msgs)
            # If input is coming from API rather than chat
            if input_mode == "external":
                incoming.extend(drain_external())

            for m in incoming:
                msg = m.get("message") or ""
                user = m.get("username") or ""
                if not msg or not user:
                    continue
                if len(counts) + unknown >= MAX_VOTES_PER_WINDOW:
                    continue
                if msg not in allow:
                    unknown += 1
                    continue
                now = time.time()
                prev = last_vote_by_user.get(user)
                if prev:
                    prev_cmd, _ = prev
                    if prev_cmd == msg:
                        # no change
                        last_vote_by_user[user] = (msg, now)
                        last_ts_by_cmd[msg] = now
                        continue
                    # decrement previous
                    counts[prev_cmd] = max(0, counts.get(prev_cmd, 0) - 1)
                # add/update
                last_vote_by_user[user] = (msg, now)
                counts[msg] = counts.get(msg, 0) + 1
                last_ts_by_cmd[msg] = now

            now = time.time()
            if now < window_end:
                time.sleep(IDLE_SLEEP_SEC)
                continue

            # Select winner (max count; tie => latest last vote)
            winner = None
            if counts:
                max_count = max(counts.values())
                top = [cmd for cmd, c in counts.items() if c == max_count]
                if len(top) == 1:
                    winner = top[0]
                else:
                    # choose the tied command with most recent last ts
                    winner = max(top, key=lambda c: last_ts_by_cmd.get(c, 0.0))

            # Execute winner if enabled, focused, and not violating global min-gap
            executed = False
            reason = None
            if winner:
                if not injection_enabled:
                    reason = "disabled"
                    print("Injection disabled")
                elif not is_target_focused():
                    reason = "not_focused"
                else:
                    # Enforce global min execution gap
                    now_s = time.time()
                    if (now_s - last_exec_ts) * 1000.0 < MIN_EXECUTION_GAP_MS:
                        reason = "global_gap"
                    else:
                        try:
                            handler = game.commands.get(winner)
                            if handler:
                                print(f"Executing '{winner}'")
                                handler("vote")
                                # immediate reset
                                release_all()
                                executed = True
                                # record last execution time
                                last_exec_ts = time.time()
                        except Exception as e:
                            reason = f"error:{e}"
                            now_s = time.time()
                            error_times.append(now_s)
                            # prune
                            while (
                                error_times
                                and now_s - error_times[0] > ERROR_WINDOW_SEC
                            ):
                                error_times.popleft()
                            if len(error_times) >= ERROR_TRIP_THRESHOLD:
                                injection_enabled = False
                                release_all()
                                print(
                                    f"Circuit breaker tripped to prevent fatal issues. Too many errors triggered."
                                )

            # Summary line
            total_votes = sum(counts.values())
            payload = {
                "ts": time.time(),
                "winner": winner,
                "executed": executed,
                "total_votes": total_votes,
                "unknown": unknown,
                "reason": reason,
            }

            # Reset window
            window_end = now + VOTE_WINDOW_SEC
            last_vote_by_user.clear()
            counts.clear()
            last_ts_by_cmd.clear()
            unknown = 0
    except KeyboardInterrupt:
        print("Stopping Twitch Plays Every Game.")
    except Exception:
        print("fatal error in main loop")
        raise
    finally:
        release_all()
        client.close()


def keycode_from_name(name: str) -> Optional[int]:
    n = name.strip().upper()
    mapping = {
        "A": A,
        "B": B,
        "C": C,
        "D": D,
        "E": E,
        "F": F,
        "G": G,
        "H": H,
        "I": I,
        "J": J,
        "K": K,
        "L": L,
        "M": M,
        "N": N,
        "O": O,
        "P": P,
        "Q": Q,
        "R": R,
        "S": S,
        "T": T,
        "U": U,
        "V": V,
        "W": W,
        "X": X,
        "Y": Y,
        "Z": Z,
        "SPACE": SPACE,
        "SPACEBAR": SPACE,
        "ENTER": ENTER,
        "ESC": ESC,
        "TAB": TAB,
        "LEFT_SHIFT": LEFT_SHIFT,
        "LEFT_CTRL": LEFT_CONTROL,
        "LEFT_ALT": LEFT_ALT,
        "RIGHT_SHIFT": RIGHT_SHIFT,
        "RIGHT_CTRL": RIGHT_CONTROL,
        "RIGHT_ALT": RIGHT_ALT,
        "LEFT": LEFT_ARROW,
        "RIGHT": RIGHT_ARROW,
        "UP": UP_ARROW,
        "DOWN": DOWN_ARROW,
        "F1": F1,
        "F2": F2,
        "F3": F3,
        "F4": F4,
        "F5": F5,
        "F6": F6,
        "F7": F7,
        "F8": F8,
        "F9": F9,
        "F10": F10,
        "F11": F11,
        "F12": F12,
        "1": ONE,
        "2": TWO,
        "3": THREE,
        "4": FOUR,
        "5": FIVE,
        "6": SIX,
        "7": SEVEN,
        "8": EIGHT,
        "9": NINE,
        "0": ZERO,
        ".": PERIOD,
        "NUMPAD_4": NUMPAD_4,
        "NUMPAD_5": NUMPAD_5,
        "NUMPAD_6": NUMPAD_6,
        "NUMPAD_8": NUMPAD_8,
    }
    return mapping.get(n)


def execute_macro(steps: list) -> None:
    MAX_MS = 3000
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        t = str(step.get("type") or "").lower()
        if t == "parallel":
            execute_parallel_threads(step.get("threads") or [])
        elif t == "key_press":
            kc = keycode_from_name(str(step.get("key") or ""))
            ms = min(MAX_MS, max(0, int(step.get("duration_ms") or 0)))
            if kc and ms:
                press_and_release(kc, ms / 1000.0)
        elif t == "key_tap":
            kc = keycode_from_name(str(step.get("key") or ""))
            ms = min(MAX_MS, max(0, int(step.get("duration_ms") or 60)))
            if kc:
                press_and_release(kc, ms / 1000.0)
        elif t == "key_hold":
            kc = keycode_from_name(str(step.get("key") or ""))
            if kc:
                press_hold(kc)
        elif t == "key_release":
            key = step.get("key")
            keys = step.get("keys")
            names = keys if isinstance(keys, list) else ([key] if key else [])
            for name in names:
                kc = keycode_from_name(str(name or ""))
                if kc:
                    release(kc)
        elif t == "key_combo":
            keys = step.get("keys") or []
            ms = min(MAX_MS, max(0, int(step.get("duration_ms") or 0)))
            kcodes = [keycode_from_name(str(n or "")) for n in keys]
            kcodes = [k for k in kcodes if k]
            for kc in kcodes:
                press_hold(kc)
            time.sleep(ms / 1000.0 if ms else 0)
            for kc in kcodes:
                release(kc)
        elif t == "mouse_down":
            b = str(step.get("button") or "left").lower()
            mouse_down(b)
        elif t == "mouse_up":
            b = str(step.get("button") or "left").lower()
            mouse_up(b)
        elif t == "mouse_click":
            b = str(step.get("button") or "left").lower()
            mouse_click(b)
        elif t == "mouse_click_at":
            x, y = point_for_normalized_mouse_coordinate(
                float(step.get("x") or 0), float(step.get("y") or 0)
            )
            mouse_move_to(x, y)
            b = str(step.get("button") or "left").lower()
            mouse_click(b)
        elif t in {"mouse_hold", "mouse_pulse"}:
            b = str(step.get("button") or "left").lower()
            ms = min(MAX_MS, max(0, int(step.get("duration_ms") or 0)))
            mouse_down(b)
            if ms:
                time.sleep(ms / 1000.0)
            mouse_up(b)
        elif t == "mouse_move":
            dx = int(step.get("dx") or 0)
            dy = int(step.get("dy") or 0)
            mouse_move(dx, dy)


def normalize_parallel_thread(thread) -> list:
    if isinstance(thread, list):
        return thread
    if isinstance(thread, dict):
        return [thread]
    return []


def execute_parallel_threads(thread_specs: list) -> None:
    errors = []
    running_threads = []

    def run_thread(thread_steps: list) -> None:
        try:
            execute_macro(thread_steps)
        except Exception as exc:
            errors.append(exc)

    for thread_spec in thread_specs:
        thread_steps = normalize_parallel_thread(thread_spec)
        if not thread_steps:
            continue
        thread = threading.Thread(target=run_thread, args=(thread_steps,))
        thread.start()
        running_threads.append(thread)

    for thread in running_threads:
        thread.join()

    if errors:
        raise RuntimeError(f"Parallel macro thread failed: {errors[0]}")


def select_profile_game(path: Path) -> ProfileGame:
    with open(path, "r", encoding="utf-8") as f:
        prof = json.load(f)
    return ProfileGame(prof)


if __name__ == "__main__":
    main()
