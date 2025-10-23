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
import importlib
import inspect
import os
import time
from typing import Callable, Dict, List, Optional, Tuple
from pathlib import Path
import json
import threading
from collections import deque

import keyboard
import pyautogui
import pydirectinput

import TwitchPlays_Connection
from TwitchPlays_KeyCodes import *
from focus_gate import set_focus_target, is_target_focused

##################### STREAM / PLATFORM CONFIG #####################

# Replace this with your Twitch username. Must be all lowercase.
TWITCH_CHANNEL = os.environ.get("TWITCH_CHANNEL", "solarterran").lower()

# Sources: default to both Twitch and YouTube; YouTube is skipped gracefully
# at runtime when not configured.
STREAM_SOURCES = ["twitch", "youtube"]

# If you're streaming on Youtube, replace this with your Youtube's Channel ID
# Find this by clicking your Youtube profile pic -> Settings -> Advanced Settings
YOUTUBE_CHANNEL_ID = os.environ.get("YOUTUBE_CHANNEL_ID")

# YouTube Data API v3 key
# Automatically falls back to scraping when quota is reached.
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")

# If you're using an Unlisted stream to test on Youtube, replace "None" below with your stream's URL in quotes.
YOUTUBE_STREAM_URL = os.environ.get("YOUTUBE_STREAM_URL") or None

##################### MESSAGE QUEUE VARIABLES #####################

"""Core timing and safety caps."""
# Voting window length (seconds)
VOTE_WINDOW_SEC = 3.0

# Caps and limits
MAX_VOTES_PER_WINDOW = 200
MAX_MESSAGE_LENGTH = 64
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

pyautogui.FAILSAFE = False


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


def mouse_down(btn: str = "left"):
    pydirectinput.mouseDown(button=btn)


def mouse_up(btn: str = "left"):
    pydirectinput.mouseUp(button=btn)


def mouse_click(btn: str = "left"):
    pydirectinput.click(button=btn)


def mouse_move(dx: int = 0, dy: int = 0):
    pydirectinput.moveRel(dx, dy, relative=True)


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
            pydirectinput.mouseUp(button=b)
        except Exception:
            pass


##########################################################
# Game plugin system
##########################################################


class BaseGame:
    """Base class for games. Subclasses should register chat commands with @command.

    Each handler is a function (self, username: str) -> None and may inspect
    any additional context by reading instance variables.
    """

    # mapping of chat message -> callable
    commands: Dict[str, Callable[[str], None]]

    def __init__(self):
        self.commands = {}

    def handle_message(self, username: str, msg: str):
        key = msg.strip().lower()
        handler = self.commands.get(key)
        if handler:
            handler(username)
        else:
            # Unknown command; override if you want different behavior
            pass

    @classmethod
    def game_name(cls) -> str:
        # default: class name minus trailing "Game"
        n = cls.__name__
        return n[:-4].lower() if n.endswith("Game") else n.lower()


def command(*aliases: str):
    """Decorator to register a chat command on a BaseGame subclass method.

    Usage:
        class MyGame(BaseGame):
            @command("left", "l")
            def go_left(self, user):
                ...
    """

    def decorator(func: Callable[[BaseGame, str], None]):
        setattr(func, "twitch_aliases", [a.strip().lower() for a in aliases])
        return func

    return decorator


def build_command_table(game: BaseGame):
    for temp, method in inspect.getmembers(game, predicate=inspect.ismethod):
        aliases = getattr(method, "twitch_aliases", None)
        if aliases:
            for a in aliases:
                if a in game.commands:
                    raise ValueError(f"Duplicate command alias: {a}")
                game.commands[a] = method


#########################################
# Built-in games have been removed in favor of JSON profiles.
# Use profiles/<game>.json, or provide a plugin module (see select_game).
#########################################


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


def select_game(key: str) -> BaseGame:
    key = key.strip().lower()
    # Attempt dynamic import path like "games.gta5:GTA5Game" or "my_mod:CustomGame"
    if ":" in key:
        module_name, class_name = key.split(":", 1)
    else:
        module_name, class_name = key, None
    try:
        mod = importlib.import_module(module_name)
        cls = None
        if class_name:
            cls = getattr(mod, class_name)
        else:
            for temp, c in inspect.getmembers(mod, inspect.isclass):
                if issubclass(c, BaseGame) and c is not BaseGame:
                    cls = c
                    break
        if not cls:
            raise ImportError("No BaseGame subclass found in module")
        game = cls()
        if not isinstance(game, BaseGame):
            raise TypeError("Selected class is not a BaseGame subclass")
        build_command_table(game)
        return game
    except Exception as e:
        raise SystemExit(f"Failed to load game '{key}': {e}")


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

    # countdown so you can focus the game window, etc.
    countdown = args.countdown
    while countdown > 0:
        print(countdown)
        countdown -= 1
        time.sleep(1)

    sources = [s.strip().lower() for s in args.sources.split(",") if s.strip()]
    t = None
    y = None

    if "twitch" in sources:
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

    if not t and not y:
        raise SystemExit(
            "No valid chat sources. Use --sources twitch,youtube or set STREAM_SOURCES."
        )

    client = MultiChat(t, y)

    # Profiles-first: prefer JSON profile; fallback to dynamic import module.
    profile_path = Path(__file__).parent / "profiles" / f"{args.game}.json"
    if profile_path.exists():
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
    else:
        # Try dynamic plugin module as a fallback
        try:
            game = select_game(args.game)
            print(
                f"Loaded plugin: {game.__class__.__name__} (key='{args.game}') with {len(game.commands)} commands"
            )
            set_focus_target(process_name=None, title_contains=None)
        except SystemExit as e:
            raise SystemExit(
                f"No profile found at profiles/{args.game}.json and no plugin module available for '{args.game}'."
            )

    # Hotkeys
    try:
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
                        while error_times and now_s - error_times[0] > ERROR_WINDOW_SEC:
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
    except Exception:
        print("fatal error in main loop")
        raise


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
    }
    return mapping.get(n)


class ProfileGame(BaseGame):
    def __init__(self, profile: dict):
        super().__init__()
        self.profile = profile
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


def execute_macro(steps: list) -> None:
    MAX_MS = 3000
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        t = str(step.get("type") or "").lower()
        if t == "key_press":
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
        elif t == "mouse_pulse":
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


def select_profile_game(path: Path) -> BaseGame:
    with open(path, "r", encoding="utf-8") as f:
        prof = json.load(f)
    game = ProfileGame(prof)
    build_command_table(game)
    return game

if __name__ == "__main__":
    main()
