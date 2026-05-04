"""
Twitch Plays Single
----------------------------------------------------------------

Run:
$ python TwitchPlays_Single.py --game <profile_name> --mode click --time 30s
$ python TwitchPlays_Single.py --game single --mode click --time 30s
$ python TwitchPlays_Single.py --game minecraft --mode command --time 30s

Defaults:
    "--game": single
    "--mode": click
    "--time": 30s

This runner implements:
    - Custom timed voting system for a single click, button press, or sequence.
    - Reads chat for commands during the voting period, and then executes the most voted command after the voting period is over.
    - You can either type in a wait time (e.g. 10s, 1m, etc.) or use the default time of 30 seconds.
        - After you can give it a new time, it will start a new cycle of voting and command execution.

This runner has two modes:
    - Click mode: Chat votes for a point on a normalized -100 to 100 coordinate field where 0,0 is the center of the click region. During the vote, the mouse moves toward chat's current target. When time expires, the runner clicks the winning point.
    - Command mode: Chat votes for aliases from the selected profile. When time expires, the winning alias runs its macro.

This is for those turn-based games where you don't need continuous input.
    - For example, in pokemon, you might want to vote on which move to use next. This will automatically click on the move after the voting time is up.
    - Another example is a game like Cookie Clicker, where you want to vote on which upgrade to buy next. This will automatically click on the upgrade after the voting time is up.

This is basically a more simplified version of TwitchPlays_Everything.py. It does not use a focus gate or anything like that so it can be used for browser games or whatever you have on your screen at the time.
Like TwitchPlays_Everything.py, this uses profiles for macros, specifically the single.json profile. But you can technically use any profile you want, as long as it has the correct format for the commands you want to use.

WARNING: This runner does not use a focus gate, so it will click or run a key sequence on whatever is on the screen at the time. Make sure to have the correct window focused when the voting period is over, or you might end up clicking on something you didn't intend to.
"""



from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

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


DEFAULT_GAME = "single"
DEFAULT_MODE = "click"
DEFAULT_TIME = "30s"
DEFAULT_SOURCES = "twitch,youtube"
PROFILE_DIR = Path(__file__).parent / "profiles"
PROFILE_TEMPLATE_PATH = PROFILE_DIR / "template.json"

COORD_LIMIT = 100.0
COORDINATE_PATTERN = re.compile(
    r"^(?:click\s+)?(-?\d+(?:\.\d+)?)\s*(?:,|\s)\s*(-?\d+(?:\.\d+)?)$"
)
CURSOR_UPDATE_SEC = 0.1
CURSOR_EASE = 0.25
IDLE_SLEEP_SEC = 0.01
MAX_MESSAGES_PER_WINDOW = 5000
MAX_MESSAGE_LENGTH = 64
STARTUP_COUNTDOWN = 5

if pyautogui is not None:
    pyautogui.FAILSAFE = False
if pydirectinput is not None and hasattr(pydirectinput, "FAILSAFE"):
    pydirectinput.FAILSAFE = False


HELD_KEYS: set[int] = set()


@dataclass(frozen=True)
class CoordinateVote:
    x: float
    y: float


@dataclass(frozen=True)
class ScreenPoint:
    x: int
    y: int


class ProfileGame:
    commands: Dict[str, Callable[[str], None]]

    def __init__(self, profile: dict):
        self.commands = {}
        aliases: Dict[str, str] = {
            str(alias or "").strip().lower(): str(canonical or "").strip().lower()
            for alias, canonical in (profile.get("aliases") or {}).items()
            if str(alias or "").strip() and str(canonical or "").strip()
        }
        self.canonical_to_macros: Dict[str, list] = profile.get("macros") or {}

        for alias, canonical in aliases.items():
            self.commands[alias] = self.make_handler(canonical)

    def make_handler(self, canonical: str) -> Callable[[str], None]:
        def handler(user: str) -> None:
            execute_macro(self.canonical_to_macros.get(canonical) or [])

        return handler


class MultiChat:
    def __init__(self, twitch_client=None, youtube_client=None):
        self.twitch_client = twitch_client
        self.youtube_client = youtube_client

    def receive_messages(self) -> List[dict]:
        messages = []
        if self.twitch_client:
            messages.extend(self.twitch_client.twitch_receive_messages())
        if self.youtube_client:
            messages.extend(self.youtube_client.twitch_receive_messages())

        normalized = []
        for message in messages:
            text = str(message.get("message") or "").strip()
            username = str(message.get("username") or "").strip()
            if not text or not username:
                continue
            if len(text) > MAX_MESSAGE_LENGTH:
                continue
            normalized.append({"message": text.lower(), "username": username.lower()})
        return normalized

    def close(self) -> None:
        if self.twitch_client:
            try:
                self.twitch_client.close()
            except Exception:
                pass
        if self.youtube_client:
            close_session(getattr(self.youtube_client, "session", None))
            close_thread_pool(getattr(self.youtube_client, "thread_pool", None))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Twitch Plays Single")
    parser.add_argument("--game", default=DEFAULT_GAME, help="Profile name to load")
    parser.add_argument(
        "--mode",
        default=DEFAULT_MODE,
        choices=("click", "command"),
        help="Vote mode",
    )
    parser.add_argument("--time", default=DEFAULT_TIME, help="Voting time, like 10s or 1m")
    parser.add_argument(
        "--sources",
        default=DEFAULT_SOURCES,
        help="Comma-separated chat sources: twitch,youtube",
    )
    return parser.parse_args()


def parse_vote_seconds(raw_value: str) -> float:
    value = str(raw_value or "").strip().lower()
    if not value:
        raise SystemExit("--time must not be empty.")

    multiplier = 1.0
    if value.endswith("ms"):
        multiplier = 0.001
        value = value[:-2]
    elif value.endswith("s"):
        value = value[:-1]
    elif value.endswith("m"):
        multiplier = 60.0
        value = value[:-1]

    try:
        seconds = float(value) * multiplier
    except ValueError as exc:
        raise SystemExit("--time must be a number followed by ms, s, or m.") from exc

    if seconds <= 0:
        raise SystemExit("--time must be greater than zero.")
    return seconds


def parse_sources(raw_value: str) -> List[str]:
    sources = [source.strip().lower() for source in raw_value.split(",") if source.strip()]
    if not sources:
        raise SystemExit("--sources must include twitch, youtube, or both.")
    for source in sources:
        if source not in {"twitch", "youtube"}:
            raise SystemExit("--sources must only include twitch or youtube.")
    return sources


def validate_mode_requirements(mode: str) -> None:
    if mode != "click":
        return

    try:
        load_mouse_backend()
        screen_size()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


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


def select_profile_game(path: Path) -> ProfileGame:
    with open(path, "r", encoding="utf-8") as profile_file:
        profile = json.load(profile_file)
    return ProfileGame(profile)


def connect_chat(sources: List[str]) -> MultiChat:
    try:
        stream_config = TwitchPlays_Connection.load_twitchplays_config()
    except (FileNotFoundError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc

    twitch_channel = str(stream_config.get("twitch_channel") or "").strip().lower()
    youtube_channel_id = str(stream_config.get("youtube_channel_id") or "").strip() or None
    youtube_api_key = str(stream_config.get("youtube_api_key") or "").strip() or None
    youtube_stream_url = str(stream_config.get("youtube_stream_url") or "").strip() or None

    twitch_client = None
    youtube_client = None

    try:
        if "twitch" in sources:
            if not twitch_channel:
                raise SystemExit("twitch_channel is required when Twitch chat is enabled.")
            twitch_client = TwitchPlays_Connection.Twitch()
            twitch_client.twitch_connect(twitch_channel)

        if "youtube" in sources:
            if youtube_channel_id or youtube_stream_url:
                youtube_client = TwitchPlays_Connection.YouTube(api_key=youtube_api_key)
                youtube_client.youtube_connect(
                    youtube_channel_id, youtube_stream_url, api_key=youtube_api_key
                )
    except BaseException:
        MultiChat(twitch_client=twitch_client, youtube_client=youtube_client).close()
        raise

    if not twitch_client and not youtube_client:
        raise SystemExit(
            "No valid chat sources. Configure Twitch or YouTube, or use --sources twitch."
        )

    return MultiChat(twitch_client=twitch_client, youtube_client=youtube_client)


def close_session(session) -> None:
    if session is None:
        return
    try:
        session.close()
    except Exception:
        pass


def close_thread_pool(thread_pool) -> None:
    if thread_pool is None:
        return
    try:
        thread_pool.shutdown(wait=False)
    except Exception:
        pass


def keycode_from_name(name: str) -> Optional[int]:
    normalized = name.strip().upper()
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
        "NUMPAD_0": NUMPAD_0,
        "NUMPAD_1": NUMPAD_1,
        "NUMPAD_2": NUMPAD_2,
        "NUMPAD_3": NUMPAD_3,
        "NUMPAD_4": NUMPAD_4,
        "NUMPAD_5": NUMPAD_5,
        "NUMPAD_6": NUMPAD_6,
        "NUMPAD_7": NUMPAD_7,
        "NUMPAD_8": NUMPAD_8,
        "NUMPAD_9": NUMPAD_9,
    }
    return mapping.get(normalized)


def press_and_release(keycode: int, seconds: float = 0.1) -> None:
    try:
        HELD_KEYS.add(keycode)
        HoldKey(keycode)
        time.sleep(max(0.01, float(seconds)))
    finally:
        ReleaseKey(keycode)
        HELD_KEYS.discard(keycode)


def press_hold(keycode: int) -> None:
    HELD_KEYS.add(keycode)
    HoldKey(keycode)


def release(keycode: int) -> None:
    ReleaseKey(keycode)
    HELD_KEYS.discard(keycode)


def load_mouse_backend() -> Tuple[str, object]:
    if sys.platform == "win32" and pydirectinput is not None:
        return ("pydirectinput", pydirectinput)
    if pyautogui is not None:
        return ("pyautogui", pyautogui)
    raise RuntimeError(
        "Mouse input requires pyautogui on Linux/macOS or pyautogui/pydirectinput on Windows."
    )


def mouse_down(button: str = "left") -> None:
    backend = load_mouse_backend()[1]
    backend.mouseDown(button=button)


def mouse_up(button: str = "left") -> None:
    backend = load_mouse_backend()[1]
    backend.mouseUp(button=button)


def mouse_click(button: str = "left") -> None:
    backend = load_mouse_backend()[1]
    backend.click(button=button)


def mouse_move(dx: int = 0, dy: int = 0) -> None:
    backend_name, backend = load_mouse_backend()
    if backend_name == "pydirectinput":
        backend.moveRel(dx, dy, relative=True)
        return
    backend.moveRel(dx, dy)


def mouse_move_to(point: ScreenPoint) -> None:
    backend = load_mouse_backend()[1]
    backend.moveTo(point.x, point.y)


def screen_size() -> Tuple[int, int]:
    if pyautogui is None:
        raise RuntimeError("pyautogui is required for click mode screen coordinates.")
    size = pyautogui.size()
    return int(size.width), int(size.height)


def current_mouse_position() -> ScreenPoint:
    if pyautogui is None:
        raise RuntimeError("pyautogui is required for click mode mouse position.")
    position = pyautogui.position()
    return ScreenPoint(int(position.x), int(position.y))


def release_all() -> None:
    for keycode in list(HELD_KEYS):
        try:
            ReleaseKey(keycode)
        except Exception:
            pass
        HELD_KEYS.discard(keycode)

    for button in ("left", "right", "middle"):
        try:
            mouse_up(button)
        except Exception:
            pass


def execute_macro(steps: list) -> None:
    max_ms = 3000
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        step_type = str(step.get("type") or "").lower()
        if step_type == "parallel":
            execute_parallel_threads(step.get("threads") or [])
        elif step_type == "key_press":
            keycode = keycode_from_name(str(step.get("key") or ""))
            duration_ms = min(max_ms, max(0, int(step.get("duration_ms") or 0)))
            if keycode and duration_ms:
                press_and_release(keycode, duration_ms / 1000.0)
        elif step_type == "key_tap":
            keycode = keycode_from_name(str(step.get("key") or ""))
            duration_ms = min(max_ms, max(0, int(step.get("duration_ms") or 60)))
            if keycode:
                press_and_release(keycode, duration_ms / 1000.0)
        elif step_type == "key_hold":
            keycode = keycode_from_name(str(step.get("key") or ""))
            if keycode:
                press_hold(keycode)
        elif step_type == "key_release":
            key = step.get("key")
            keys = step.get("keys")
            names = keys if isinstance(keys, list) else ([key] if key else [])
            for name in names:
                keycode = keycode_from_name(str(name or ""))
                if keycode:
                    release(keycode)
        elif step_type == "key_combo":
            keys = step.get("keys") or []
            duration_ms = min(max_ms, max(0, int(step.get("duration_ms") or 0)))
            keycodes = [keycode_from_name(str(name or "")) for name in keys]
            keycodes = [keycode for keycode in keycodes if keycode]
            for keycode in keycodes:
                press_hold(keycode)
            time.sleep(duration_ms / 1000.0 if duration_ms else 0)
            for keycode in keycodes:
                release(keycode)
        elif step_type == "mouse_down":
            mouse_down(str(step.get("button") or "left").lower())
        elif step_type == "mouse_up":
            mouse_up(str(step.get("button") or "left").lower())
        elif step_type == "mouse_click":
            mouse_click(str(step.get("button") or "left").lower())
        elif step_type == "mouse_click_at":
            width, height = screen_size()
            vote = CoordinateVote(
                x=clamp_number(float(step.get("x") or 0), -COORD_LIMIT, COORD_LIMIT),
                y=clamp_number(float(step.get("y") or 0), -COORD_LIMIT, COORD_LIMIT),
            )
            mouse_move_to(point_for_coordinate(vote, width, height))
            mouse_click(str(step.get("button") or "left").lower())
        elif step_type in {"mouse_hold", "mouse_pulse"}:
            button = str(step.get("button") or "left").lower()
            duration_ms = min(max_ms, max(0, int(step.get("duration_ms") or 0)))
            mouse_down(button)
            if duration_ms:
                time.sleep(duration_ms / 1000.0)
            mouse_up(button)
        elif step_type == "mouse_move":
            mouse_move(int(step.get("dx") or 0), int(step.get("dy") or 0))


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


def clamp_number(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def parse_coordinate_vote(message: str) -> Optional[CoordinateVote]:
    match = COORDINATE_PATTERN.match(message.strip().lower())
    if not match:
        return None

    x = clamp_number(float(match.group(1)), -COORD_LIMIT, COORD_LIMIT)
    y = clamp_number(float(match.group(2)), -COORD_LIMIT, COORD_LIMIT)
    return CoordinateVote(x=x, y=y)


def point_for_coordinate(vote: CoordinateVote, width: int, height: int) -> ScreenPoint:
    center_x = (width - 1) / 2.0
    center_y = (height - 1) / 2.0
    x = center_x + (vote.x / COORD_LIMIT) * center_x
    y = center_y + (vote.y / COORD_LIMIT) * center_y
    return ScreenPoint(
        x=int(round(clamp_number(x, 0, width - 1))),
        y=int(round(clamp_number(y, 0, height - 1))),
    )


def center_point(width: int, height: int) -> ScreenPoint:
    return ScreenPoint(x=int(round((width - 1) / 2.0)), y=int(round((height - 1) / 2.0)))


def move_toward(target: ScreenPoint) -> None:
    current = current_mouse_position()
    next_point = ScreenPoint(
        x=int(round(current.x + (target.x - current.x) * CURSOR_EASE)),
        y=int(round(current.y + (target.y - current.y) * CURSOR_EASE)),
    )
    mouse_move_to(next_point)


def format_seconds(seconds: float) -> str:
    if seconds.is_integer():
        return f"{int(seconds)}s"
    return f"{seconds:.2f}s"


def run_click_round(chat: MultiChat, vote_seconds: float) -> None:
    width, height = screen_size()
    mouse_move_to(center_point(width, height))
    print(f"Click vote started for {format_seconds(vote_seconds)}. Mouse centered.")

    end_time = time.monotonic() + vote_seconds
    next_cursor_update = time.monotonic()
    processed_count = 0
    vote_count = 0
    x_total = 0.0
    y_total = 0.0

    while time.monotonic() < end_time:
        for message in chat.receive_messages():
            if processed_count >= MAX_MESSAGES_PER_WINDOW:
                continue
            processed_count += 1
            vote = parse_coordinate_vote(message["message"])
            if vote is None:
                continue
            vote_count += 1
            x_total += vote.x
            y_total += vote.y

        now = time.monotonic()
        if now >= next_cursor_update:
            if vote_count:
                current_vote = CoordinateVote(x=x_total / vote_count, y=y_total / vote_count)
                move_toward(point_for_coordinate(current_vote, width, height))
            next_cursor_update = now + CURSOR_UPDATE_SEC

        time.sleep(IDLE_SLEEP_SEC)

    if not vote_count:
        print("No valid click votes this round.")
        return

    winning_vote = CoordinateVote(x=x_total / vote_count, y=y_total / vote_count)
    winning_point = point_for_coordinate(winning_vote, width, height)
    mouse_move_to(winning_point)
    mouse_click("left")
    print(
        f"Clicked {winning_vote.x:.1f}, {winning_vote.y:.1f} "
        f"from {vote_count} vote messages."
    )


def run_command_round(chat: MultiChat, game: ProfileGame, vote_seconds: float) -> None:
    print(f"Command vote started for {format_seconds(vote_seconds)}.")

    end_time = time.monotonic() + vote_seconds
    counts: Dict[str, int] = {}
    last_seen: Dict[str, float] = {}
    processed_count = 0
    vote_count = 0

    while time.monotonic() < end_time:
        for message in chat.receive_messages():
            if processed_count >= MAX_MESSAGES_PER_WINDOW:
                continue
            processed_count += 1
            command = message["message"]
            if command not in game.commands:
                continue
            vote_count += 1
            counts[command] = counts.get(command, 0) + 1
            last_seen[command] = time.monotonic()

        time.sleep(IDLE_SLEEP_SEC)

    if not counts:
        print("No valid command votes this round.")
        return

    winner = max(counts, key=lambda command: (counts[command], last_seen.get(command, 0.0)))
    print(f"Executing '{winner}' with {counts[winner]} vote messages.")
    try:
        game.commands[winner]("vote")
    except Exception as exc:
        print(f"Failed to execute '{winner}': {exc}")
    finally:
        release_all()


def run_startup_countdown() -> None:
    countdown = STARTUP_COUNTDOWN
    print(f"Starting in {countdown} seconds. Focus the target window now.")
    while countdown > 0:
        print(countdown)
        time.sleep(1)
        countdown -= 1


def drain_stale_messages(chat: MultiChat) -> None:
    stale_count = 0
    quiet_passes = 0
    while quiet_passes < 2:
        messages = chat.receive_messages()
        if messages:
            stale_count += len(messages)
            quiet_passes = 0
            continue
        quiet_passes += 1
        if quiet_passes < 2:
            time.sleep(IDLE_SLEEP_SEC)

    if stale_count:
        print(f"Ignored {stale_count} chat messages received before voting started.")


def main() -> None:
    args = parse_args()
    vote_seconds = parse_vote_seconds(args.time)
    sources = parse_sources(args.sources)
    validate_mode_requirements(args.mode)

    profile_path = profile_path_for_game(args.game)
    if not profile_path.exists():
        create_profile_from_template(profile_path)
    game = select_profile_game(profile_path)
    print(f"Loaded profile: {profile_path.name} with {len(game.commands)} commands.")

    chat = connect_chat(sources)

    try:
        run_startup_countdown()
        print(
            f"Running {args.mode} mode. Every valid chat message counts; votes are not deduped by user."
        )
        while True:
            drain_stale_messages(chat)
            if args.mode == "click":
                run_click_round(chat, vote_seconds)
            else:
                run_command_round(chat, game, vote_seconds)
    except KeyboardInterrupt:
        print("Stopping Twitch Plays Single.")
    finally:
        release_all()
        chat.close()


if __name__ == "__main__":
    main()
