"""
Twitch Plays Every Game
----------------------------------------------------------------

Run:
$ python TwitchPlays_Everything.py --game gta5

This runner implements:
    - 3s voting window with per-user dedupe
    - Input toggle (Alt+Shift+P) and stop with cleanup (Ctrl+Shift+Backspace)
    - Windows/Linux X11 focus gate (via profile `target_process` / `window_title_contains`)
    - Immediate reset: commands are pulses, followed by release_all()
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from typing import Callable, Dict, List, Optional, Tuple
from pathlib import Path
import json
import math
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

STREAM_SOURCES = ["twitch", "youtube"]

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


def load_mouse_backend():
    if sys.platform == "win32" and pydirectinput is not None:
        return ("pydirectinput", pydirectinput)
    if pyautogui is not None:
        return ("pyautogui", pyautogui)
    raise RuntimeError(
        "Mouse input requires pyautogui on Linux/macOS or pyautogui/pydirectinput on Windows."
    )


def mouse_move_to(x: int, y: int):
    with input_lock:
        check_input_allowed()
        backend = load_mouse_backend()[1]
        backend.moveTo(x, y)


def mouse_move(dx: int = 0, dy: int = 0):
    with input_lock:
        check_input_allowed()
        backend_name, backend = load_mouse_backend()
        if backend_name == "pydirectinput":
            backend.moveRel(dx, dy, relative=True)
            return
        backend.moveRel(dx, dy)


def screen_size() -> Tuple[int, int]:
    if pyautogui is None:
        raise RuntimeError(
            "pyautogui is required for mouse_click_at screen coordinates."
        )
    size = pyautogui.size()
    return int(size.width), int(size.height)


def clamp_number(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def point_for_normalized_mouse_coordinate(
    x_value: float, y_value: float
) -> Tuple[int, int]:
    width, height = screen_size()
    center_x = (width - 1) / 2.0
    center_y = (height - 1) / 2.0
    x = (
        center_x
        + (
            clamp_number(x_value, -MOUSE_COORD_LIMIT, MOUSE_COORD_LIMIT)
            / MOUSE_COORD_LIMIT
        )
        * center_x
    )
    y = (
        center_y
        + (
            clamp_number(y_value, -MOUSE_COORD_LIMIT, MOUSE_COORD_LIMIT)
            / MOUSE_COORD_LIMIT
        )
        * center_y
    )
    return (
        int(round(clamp_number(x, 0, width - 1))),
        int(round(clamp_number(y, 0, height - 1))),
    )


##########################################################
# Game profile system
##########################################################


input_lock = threading.RLock()
cancel_event = threading.Event()
stop_event = threading.Event()
held_inputs: set[tuple] = set()
persistent_inputs: set[tuple] = set()
temporary_inputs: dict[tuple, int] = {}
input_active = False
cleanup_failed = False


class MacroCancelled(RuntimeError):
    pass


def cancel_input() -> None:
    with input_lock:
        cancel_event.set()


def request_stop() -> None:
    with input_lock:
        stop_event.set()
        cancel_event.set()


def check_input_allowed() -> None:
    if stop_event.is_set() or cancel_event.is_set():
        raise MacroCancelled("Input was cancelled.")
    if cleanup_failed:
        raise RuntimeError("Input remains stopped because a previous release failed.")
    if not injection_enabled:
        raise MacroCancelled("Input is disabled.")
    if not is_target_focused():
        cancel_event.set()
        raise MacroCancelled("The configured game window lost focus.")


def begin_input() -> None:
    global input_active
    with input_lock:
        if input_active or held_inputs:
            raise RuntimeError("The previous input sequence has not finished cleanup.")
        cancel_event.clear()
        check_input_allowed()
        input_active = True


def send_input(kind: str, value, pressed: bool) -> None:
    if kind == "key":
        (HoldKey if pressed else ReleaseKey)(value)
    else:
        backend = load_mouse_backend()[1]
        (backend.mouseDown if pressed else backend.mouseUp)(button=value)


def acquire_input(kind: str, value, persistent: bool = False) -> None:
    item = (kind, value)
    with input_lock:
        check_input_allowed()
        if persistent:
            persistent_inputs.add(item)
        else:
            temporary_inputs[item] = temporary_inputs.get(item, 0) + 1
        if item not in held_inputs:
            # Track before sending: a failed backend call may still have pressed it.
            held_inputs.add(item)
            send_input(kind, value, True)


def release_input(kind: str, value, persistent: bool = False) -> None:
    global cleanup_failed
    item = (kind, value)
    with input_lock:
        if persistent:
            persistent_inputs.discard(item)
        elif temporary_inputs.get(item, 0) > 0:
            temporary_inputs[item] -= 1
            if not temporary_inputs[item]:
                del temporary_inputs[item]
        if (
            item in persistent_inputs
            or item in temporary_inputs
            or item not in held_inputs
        ):
            return
        try:
            send_input(kind, value, False)
        except Exception:
            cleanup_failed = True
            cancel_event.set()
            raise
        held_inputs.remove(item)


def wait_for_input(seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        with input_lock:
            check_input_allowed()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        cancel_event.wait(min(remaining, 0.05))


def hold_inputs(items: list[tuple], seconds: float) -> None:
    acquired = []
    try:
        for kind, value in items:
            acquire_input(kind, value)
            acquired.append((kind, value))
        wait_for_input(seconds)
    finally:
        for kind, value in acquired:
            release_input(kind, value)


def press_and_release(keycode: int, seconds: float = 0.1) -> None:
    hold_inputs([("key", keycode)], max(0.01, seconds))


def press_hold(keycode: int) -> None:
    acquire_input("key", keycode, persistent=True)


def release(keycode: int) -> None:
    release_input("key", keycode, persistent=True)


def mouse_down(button: str = "left") -> None:
    acquire_input("mouse", button, persistent=True)


def mouse_up(button: str = "left") -> None:
    release_input("mouse", button, persistent=True)


def mouse_click(button: str = "left") -> None:
    hold_inputs([("mouse", button)], 0.01)


def release_all() -> bool:
    global cleanup_failed
    with input_lock:
        for kind, value in list(held_inputs):
            try:
                send_input(kind, value, False)
            except Exception as exc:
                print(f"Could not release {kind} {value}: {exc}")
            else:
                held_inputs.remove((kind, value))
        persistent_inputs.clear()
        temporary_inputs.clear()
        cleanup_failed = bool(held_inputs)
        if cleanup_failed:
            cancel_event.set()
        return not cleanup_failed


def run_macro(steps: list, expected_vote_generation: Optional[int] = None) -> None:
    global input_active
    validate_steps(steps, "macro")
    with input_lock:
        if (
            expected_vote_generation is not None
            and expected_vote_generation != vote_generation
        ):
            raise MacroCancelled("The vote was discarded when input was toggled.")
        begin_input()
    try:
        execute_macro(steps)
    finally:
        cancel_input()
        released = release_all()
        input_active = False
        if not released:
            raise RuntimeError(
                "Input remains stopped because cleanup could not release every input."
            )


def validate_steps(steps: list, location: str) -> None:
    if not isinstance(steps, list):
        raise ValueError(f"{location}: macro steps must be a list.")
    for index, step in enumerate(steps):
        where = f"{location}, step {index + 1}"
        if not isinstance(step, dict) or not isinstance(step.get("type"), str):
            raise ValueError(f"{where}: each step needs a type.")
        kind = step["type"].lower()
        if kind not in {
            "parallel",
            "key_press",
            "key_tap",
            "key_hold",
            "key_release",
            "key_combo",
            "mouse_down",
            "mouse_up",
            "mouse_click",
            "mouse_click_at",
            "mouse_hold",
            "mouse_pulse",
            "mouse_move",
        }:
            raise ValueError(f"{where}: unknown step type {kind!r}.")
        if kind == "parallel":
            threads = step.get("threads")
            if not isinstance(threads, list) or not threads:
                raise ValueError(f"{where}: parallel needs a nonempty threads list.")
            for branch, thread in enumerate(threads):
                validate_steps(
                    [thread] if isinstance(thread, dict) else thread,
                    f"{where}, branch {branch + 1}",
                )
        elif kind.startswith("key_"):
            if kind == "key_combo" or (kind == "key_release" and "keys" in step):
                names = step.get("keys")
                if not isinstance(names, list):
                    raise ValueError(f"{where}: keys must be a list.")
            else:
                names = [step.get("key")]
            for name in names:
                code = keycode_from_name(name) if isinstance(name, str) else None
                if code is None:
                    raise ValueError(f"{where}: unsupported key {name!r}.")
                if not IS_WINDOWS:
                    try:
                        key_name_from_code(code)
                    except ValueError as exc:
                        raise ValueError(
                            f"{where}: key {name!r} is unsupported on this platform."
                        ) from exc
        elif kind != "mouse_move":
            button = step.get("button", "left")
            if not isinstance(button, str) or button.lower() not in {
                "left",
                "right",
                "middle",
            }:
                raise ValueError(f"{where}: unsupported mouse button {button!r}.")
        for field in ("duration_ms", "x", "y", "dx", "dy"):
            if field in step:
                value = step[field]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                ):
                    raise ValueError(f"{where}: {field} must be a finite number.")


def validate_profile(profile: dict, location: str) -> dict:
    if not isinstance(profile, dict):
        raise ValueError(f"{location}: profile must be a JSON object.")
    for field in ("target_process", "window_title_contains"):
        if profile.get(field) is not None and not isinstance(profile[field], str):
            raise ValueError(f"{location}: {field} must be a string or null.")
    aliases = profile.get("aliases", {})
    macros = profile.get("macros", {})
    if not isinstance(aliases, dict) or not isinstance(macros, dict):
        raise ValueError(f"{location}: aliases and macros must be objects.")
    normalized_macros = {}
    for name, steps in macros.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{location}: macro names must be nonempty strings.")
        canonical = name.strip().lower()
        if canonical in normalized_macros:
            raise ValueError(f"{location}: duplicate normalized macro {canonical!r}.")
        validate_steps(steps, f"{location}, macro {name!r}")
        normalized_macros[canonical] = steps
    normalized_aliases = {}
    for alias, canonical in aliases.items():
        if (
            not isinstance(alias, str)
            or not alias.strip()
            or not isinstance(canonical, str)
        ):
            raise ValueError(
                f"{location}: aliases must map nonempty strings to macro names."
            )
        alias, canonical = alias.strip().lower(), canonical.strip().lower()
        if alias in normalized_aliases:
            raise ValueError(f"{location}: duplicate normalized alias {alias!r}.")
        if not normalized_macros.get(canonical):
            raise ValueError(
                f"{location}: alias {alias!r} refers to missing or empty macro {canonical!r}."
            )
        normalized_aliases[alias] = canonical
    return {**profile, "aliases": normalized_aliases, "macros": normalized_macros}


class ProfileGame:
    # mapping of chat message -> callable
    commands: Dict[str, Callable[[str], None]]

    def __init__(self, profile: dict):
        profile = validate_profile(profile, "profile")
        self.profile = profile
        self.commands = {}
        aliases: Dict[str, str] = {
            (k or "").strip().lower(): (v or "").strip().lower()
            for k, v in (profile.get("aliases") or {}).items()
            if (k or "").strip() and (v or "").strip()
        }
        self.canonical_to_macros: Dict[str, list] = profile.get("macros") or {}

        def make_handler(canonical: str):
            def handler(user: str) -> None:
                run_macro(self.canonical_to_macros[canonical])

            return handler

        for alias, canonical in aliases.items():
            self.commands[alias] = make_handler(canonical)


class MultiChat:
    def __init__(self, twitch_client=None, youtube_client=None):
        self.twitch_client = twitch_client
        self.youtube_client = youtube_client

    @property
    def available(self) -> bool:
        return any(
            client.connected and not client.stopped
            for client in (self.twitch_client, self.youtube_client)
            if client
        )

    @property
    def finished(self) -> bool:
        return all(
            client.stopped
            for client in (self.twitch_client, self.youtube_client)
            if client
        )

    def receive_messages(self) -> List[dict]:
        messages = []
        for platform, client in (
            ("twitch", self.twitch_client),
            ("youtube", self.youtube_client),
        ):
            if client is None:
                continue
            for message in client.twitch_receive_messages():
                text = str(message.get("message") or "").strip()
                username = str(message.get("username") or "").strip()
                if username and 0 < len(text) <= MAX_MESSAGE_LENGTH:
                    messages.append(
                        {
                            **message,
                            "platform": platform,
                            "message": text.lower(),
                            "username": username,
                        }
                    )
        messages.sort(key=lambda message: message.get("received_at", 0))
        if len(messages) > MAX_VOTES_PER_WINDOW:
            print(
                f"Discarded {len(messages) - MAX_VOTES_PER_WINDOW} messages because the combined chat batch was full."
            )
        return messages[:MAX_VOTES_PER_WINDOW]

    def close(self) -> None:
        for platform, client in (
            ("Twitch", self.twitch_client),
            ("YouTube", self.youtube_client),
        ):
            if client is not None:
                try:
                    client.close()
                except Exception as exc:
                    print(
                        f"Could not close the {platform} chat reader ({type(exc).__name__})."
                    )


def connect_chat(sources: List[str]) -> MultiChat:
    stream_config = TwitchPlays_Connection.load_twitchplays_config()
    twitch_client = None
    youtube_client = None
    try:
        if "twitch" in sources:
            twitch_channel = str(stream_config.get("twitch_channel") or "").strip()
            if not twitch_channel:
                raise ValueError(
                    "twitch_channel is required when Twitch chat is selected."
                )
            twitch_client = TwitchPlays_Connection.Twitch(
                message_limit=MAX_VOTES_PER_WINDOW
            )
            try:
                twitch_client.twitch_connect(twitch_channel)
            except (RuntimeError, OSError) as exc:
                twitch_client.stopped = True
                detail = (
                    str(exc)
                    if isinstance(exc, TwitchPlays_Connection.TwitchRevocationError)
                    else type(exc).__name__
                )
                print(
                    f"Twitch chat could not start: {detail}. Check the channel and Twitch authorization in twitch_config.json."
                )
        if "youtube" in sources:
            channel_id = (
                str(stream_config.get("youtube_channel_id") or "").strip() or None
            )
            stream_url = (
                str(stream_config.get("youtube_stream_url") or "").strip() or None
            )
            api_key = str(stream_config.get("youtube_api_key") or "").strip() or None
            if channel_id or stream_url:
                youtube_client = TwitchPlays_Connection.YouTube(
                    api_key=api_key, message_limit=MAX_VOTES_PER_WINDOW
                )
                try:
                    youtube_client.youtube_connect(
                        channel_id, stream_url, api_key=api_key
                    )
                except (
                    TwitchPlays_Connection.ChatEndedError,
                    TwitchPlays_Connection.YouTubeAuthorizationError,
                ) as exc:
                    print(f"YouTube chat could not start. {exc}")
                except (RuntimeError, OSError, ValueError, KeyError) as exc:
                    print(
                        f"YouTube chat could not connect ({type(exc).__name__}); the reader will retry."
                    )
        chat = MultiChat(twitch_client, youtube_client)
        if chat.finished:
            raise RuntimeError(
                "No chat source can run. Check twitch_config.json and --sources."
            )
        return chat
    except BaseException:
        MultiChat(twitch_client, youtube_client).close()
        raise


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


injection_enabled = False
input_mode = "chat"
error_times: deque[float] = deque()
last_exec_ts = 0.0
TOGGLE_HOTKEY = "alt+shift+p"
KILL_HOTKEY = "ctrl+shift+backspace"
last_toggle_ts = 0.0
DEBOUNCE_SEC = 0.25
hotkey_handles = []
hotkeys_ready = False
vote_generation = 0

# Preserve the existing in-process external-message hook.
tp_external_sink = deque(maxlen=MAX_VOTES_PER_WINDOW)
tp_external_lock = threading.Lock()
tp_allow_set = set()
tp_input_mode_ref = lambda: input_mode


def receive_votes(client: MultiChat) -> list:
    if input_mode == "chat":
        return client.receive_messages()
    with tp_external_lock:
        incoming = list(tp_external_sink)
        tp_external_sink.clear()
    now = time.monotonic()
    return [
        {**item, "platform": "external", "received_at": item.get("received_at", now)}
        for item in incoming
    ]


def toggle_injection() -> None:
    global injection_enabled, last_toggle_ts, vote_generation
    with input_lock:
        now = time.monotonic()
        if now - last_toggle_ts < DEBOUNCE_SEC:
            return
        last_toggle_ts = now
        if not hotkeys_ready or stop_event.is_set() or cleanup_failed:
            print(
                "Input cannot be enabled until hotkeys and input cleanup are working."
            )
            return
        injection_enabled = not injection_enabled
        vote_generation += 1
        if not injection_enabled:
            cancel_event.set()
        print(f"Input {'enabled' if injection_enabled else 'disabled'}.")


def register_hotkeys() -> None:
    global hotkeys_ready, injection_enabled
    try:
        if keyboard is None:
            raise RuntimeError("The keyboard package is not installed.")
        hotkey_handles.append(
            keyboard.add_hotkey(TOGGLE_HOTKEY, toggle_injection, suppress=False)
        )
        hotkey_handles.append(
            keyboard.add_hotkey(KILL_HOTKEY, request_stop, suppress=False)
        )
    except Exception as exc:
        injection_enabled = False
        cancel_input()
        unregister_hotkeys()
        raise RuntimeError(
            "Hotkeys could not be registered. Check keyboard installation and desktop permissions; input remains disabled."
        ) from exc
    hotkeys_ready = True
    print(f"Toggle input: {TOGGLE_HOTKEY}. Stop and release input: {KILL_HOTKEY}.")


def unregister_hotkeys() -> None:
    global hotkeys_ready
    hotkeys_ready = False
    for handle in hotkey_handles:
        try:
            keyboard.remove_hotkey(handle)
        except Exception as exc:
            print(f"Could not remove a TwitchPlays hotkey ({type(exc).__name__}).")
    hotkey_handles.clear()


def collect_votes(client: MultiChat, game: ProfileGame):
    start, start_wall = time.monotonic(), time.time()
    end = start + VOTE_WINDOW_SEC
    generation = vote_generation
    votes = {}
    seen_messages = set()
    processed = unknown = discarded = 0
    while time.monotonic() < end and not stop_event.is_set():
        incoming = receive_votes(client)
        if (
            not injection_enabled
            or vote_generation != generation
            or (input_mode == "chat" and not client.available)
        ):
            return None
        for message in incoming:
            if not TwitchPlays_Connection.message_in_window(
                message, start, end, start_wall
            ):
                continue
            text = str(message.get("message") or "").strip().lower()
            username = str(message.get("username") or "").strip()
            if not username or not text or len(text) > MAX_MESSAGE_LENGTH:
                continue
            platform = message.get("platform", "external")
            message_id = message.get("message_id")
            identity = (platform, message.get("user_id") or username.lower())
            if message_id and (platform, message_id) in seen_messages:
                continue
            if processed >= MAX_VOTES_PER_WINDOW:
                discarded += 1
                continue
            processed += 1
            if message_id:
                seen_messages.add((platform, message_id))
            if text not in game.commands:
                unknown += 1
                continue
            # Messages in a YouTube batch share a receipt timestamp.
            recency = (message.get("received_at", time.monotonic()), processed)
            votes[identity] = (text, recency)
        stop_event.wait(IDLE_SLEEP_SEC)
    if (
        stop_event.is_set()
        or not injection_enabled
        or vote_generation != generation
        or (input_mode == "chat" and not client.available)
    ):
        return None
    counts, latest = {}, {}
    for command, recency in votes.values():
        counts[command] = counts.get(command, 0) + 1
        latest[command] = max(recency, latest.get(command, recency))
    if discarded:
        print(
            f"Discarded {discarded} messages after the {MAX_VOTES_PER_WINDOW}-message vote limit."
        )
    winner = (
        max(counts, key=lambda command: (counts[command], latest[command]))
        if counts
        else None
    )
    return winner, len(votes), unknown, generation


def main() -> None:
    global injection_enabled, last_exec_ts, tp_allow_set
    args = parse_args()
    sources = parse_sources(args.sources)
    profile_path = profile_path_for_game(args.game)
    if not profile_path.exists():
        create_profile_from_template(profile_path)
    game = select_profile_game(profile_path)
    set_focus_target(
        game.profile.get("target_process"), game.profile.get("window_title_contains")
    )
    tp_allow_set = set(game.commands)
    print(f"Loaded profile: {profile_path.name} with {len(game.commands)} commands.")
    client = None
    try:
        register_hotkeys()
        for remaining in range(max(0, args.countdown), 0, -1):
            print(remaining)
            if stop_event.wait(1):
                return
        client = connect_chat(sources)
        print("Press the input toggle to start voting.")
        while not stop_event.is_set():
            # Discard anything received outside a voting window; keep polling YouTube.
            receive_votes(client)
            if input_mode == "chat" and client.finished:
                print("All configured chat sources have stopped.")
                break
            if not injection_enabled or (input_mode == "chat" and not client.available):
                stop_event.wait(0.05)
                continue
            result = collect_votes(client, game)
            if result is None:
                continue
            winner, total_votes, unknown, generation = result
            if winner is None:
                continue
            if (time.monotonic() - last_exec_ts) * 1000 < MIN_EXECUTION_GAP_MS:
                print(f"Skipped {winner!r}: the minimum input gap has not elapsed.")
                continue
            try:
                canonical = game.profile["aliases"][winner]
                run_macro(
                    game.canonical_to_macros[canonical],
                    expected_vote_generation=generation,
                )
            except MacroCancelled as exc:
                print(f"Cancelled {winner!r}: {exc}")
            except Exception as exc:
                print(f"Failed to execute {winner!r}: {exc}")
                now = time.monotonic()
                error_times.append(now)
                while error_times and now - error_times[0] > ERROR_WINDOW_SEC:
                    error_times.popleft()
                if cleanup_failed or len(error_times) >= ERROR_TRIP_THRESHOLD:
                    injection_enabled = False
                    print(
                        "Input disabled after input errors. Check the reported failure before re-enabling."
                    )
            else:
                last_exec_ts = time.monotonic()
                print(
                    f"Completed {winner!r} from {total_votes} viewer votes; ignored {unknown} unknown commands."
                )
    except KeyboardInterrupt:
        print("Stopping TwitchPlays Everything.")
    finally:
        injection_enabled = False
        request_stop()
        release_all()
        unregister_hotkeys()
        if client is not None:
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
        with input_lock:
            check_input_allowed()
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
            hold_inputs([("key", code) for code in kcodes], ms / 1000.0)
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
            hold_inputs([("mouse", b)], ms / 1000.0)
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
    workers = []
    cancellation = None

    def run_thread(thread_steps: list, finished: threading.Event) -> None:
        try:
            execute_macro(thread_steps)
        except Exception as exc:
            errors.append(exc)
            cancel_input()
        finally:
            finished.set()

    try:
        for thread_spec in thread_specs:
            finished = threading.Event()
            thread = threading.Thread(
                target=run_thread,
                args=(normalize_parallel_thread(thread_spec), finished),
            )
            thread.start()
            workers.append((thread, finished))
        for thread, finished in workers:
            while not finished.wait(0.05):
                with input_lock:
                    check_input_allowed()
    except MacroCancelled as exc:
        cancel_input()
        cancellation = exc
    except BaseException:
        cancel_input()
        raise
    finally:
        # A KeyboardInterrupt can interrupt join before a worker has exited.
        for thread, finished in workers:
            while True:
                try:
                    if finished.wait(0.05):
                        thread.join()
                        break
                except KeyboardInterrupt:
                    request_stop()
    if errors:
        # Preserve the failure that cancelled the other branches.
        raise next(
            (error for error in errors if not isinstance(error, MacroCancelled)),
            errors[0],
        )
    if cancellation is not None:
        raise cancellation


def select_profile_game(path: Path) -> ProfileGame:
    with open(path, "r", encoding="utf-8") as f:
        prof = json.load(f)
    return ProfileGame(validate_profile(prof, str(path)))


if __name__ == "__main__":
    main()
