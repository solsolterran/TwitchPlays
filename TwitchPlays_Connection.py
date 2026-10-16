# DougDoug Note:
# This is the code that connects to Twitch / Youtube and checks for new messages.
# You should not need to modify anything in this file, just use as is.

# This code is based on Wituz's "Twitch Plays" tutorial, updated for Python 3.X
# Updated for Youtube by DDarknut, with help by Ottomated

# Sol Note:
# Ignore the information above. Keeping it inside for nostalgia.

import concurrent.futures
import importlib
import json
import math
from datetime import datetime
import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set, Tuple, Type

import requests


class FallbackWebSocketTimeoutException(Exception):
    pass


class FallbackWebSocketConnectionClosedException(Exception):
    pass


try:
    websocket_module: Any = importlib.import_module("websocket")
except Exception:
    websocket_module = None

websocket_create_connection = getattr(websocket_module, "create_connection", None)
WebSocketTimeoutException: Type[BaseException] = getattr(
    websocket_module,
    "WebSocketTimeoutException",
    FallbackWebSocketTimeoutException,
)
WebSocketConnectionClosedException: Type[BaseException] = getattr(
    websocket_module,
    "WebSocketConnectionClosedException",
    FallbackWebSocketConnectionClosedException,
)

if not callable(websocket_create_connection):
    try:
        core_module: Any = importlib.import_module("websocket._core")
        exceptions_module: Any = importlib.import_module("websocket._exceptions")
    except Exception:
        websocket_create_connection = None
    else:
        websocket_create_connection = getattr(core_module, "create_connection", None)
        WebSocketTimeoutException = getattr(
            exceptions_module,
            "WebSocketTimeoutException",
            FallbackWebSocketTimeoutException,
        )
        WebSocketConnectionClosedException = getattr(
            exceptions_module,
            "WebSocketConnectionClosedException",
            FallbackWebSocketConnectionClosedException,
        )
        if not callable(websocket_create_connection):
            websocket_create_connection = None

API_TIMEOUT_SECONDS = 10
EVENTSUB_BACKOFF_MAX_SECONDS = 30
EVENTSUB_CHAT_MESSAGE_TYPE = "channel.chat.message"
EVENTSUB_KEEPALIVE_GRACE_SECONDS = 5
EVENTSUB_WEBSOCKET_URL = "wss://eventsub.wss.twitch.tv/ws"
MAX_STORED_MESSAGE_IDS = 2048
TOKEN_REFRESH_MARGIN_SECONDS = 300
TOKEN_VALIDATION_INTERVAL_SECONDS = 3600
MAX_CHAT_MESSAGE_LENGTH = 64
TWITCH_CONFIG_EXAMPLE_FILE_NAME = "twitch_config.example.json"
TWITCH_CONFIG_FILE_NAME = "twitch_config.json"
TWITCH_HELIX_SUBSCRIPTIONS_URL = "https://api.twitch.tv/helix/eventsub/subscriptions"
TWITCH_HELIX_USERS_URL = "https://api.twitch.tv/helix/users"
TWITCH_OAUTH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_OAUTH_VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
TWITCH_READ_CHAT_SCOPE = "user:read:chat"
YOUTUBE_FETCH_INTERVAL = 1


def twitchplays_config_path() -> Path:
    return Path(__file__).with_name(TWITCH_CONFIG_FILE_NAME)


def load_twitchplays_config() -> Dict[str, Any]:
    path = twitchplays_config_path()
    if not path.exists():
        raise FileNotFoundError(
            f"Missing TwitchPlays config file: {path}. Copy {TWITCH_CONFIG_EXAMPLE_FILE_NAME} to {TWITCH_CONFIG_FILE_NAME} and fill in the values."
        )

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"TwitchPlays config file is not valid JSON: {path}"
        ) from exc

    if not isinstance(raw, dict):
        raise RuntimeError(
            f"TwitchPlays config file must contain a JSON object: {path}"
        )

    return raw


def message_timestamp(value) -> Optional[float]:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def message_in_window(
    message: dict, start: float, end: float, start_wall: float
) -> bool:
    received = message.get("received_at", time.monotonic())
    if not start <= received < end:
        return False
    published = message.get("published_at")
    if published is not None:
        return published >= start_wall
    return message.get("fetch_started_at", start) >= start


@dataclass
class TwitchSessionState:
    channel: str
    auth: Dict[str, str]
    chat_user_id: str
    broadcaster_user_id: str
    token_refresh_at: Optional[float] = None
    token_validation_at: float = field(
        default_factory=lambda: time.monotonic() + TOKEN_VALIDATION_INTERVAL_SECONDS
    )
    message_queue: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=200))
    dropped_messages: int = 0
    seen_message_ids: Deque[str] = field(default_factory=deque)
    seen_message_id_lookup: Set[str] = field(default_factory=set)
    stop_event: threading.Event = field(default_factory=threading.Event)
    ws: Any = None
    ws_lock: threading.Lock = field(default_factory=threading.Lock)


class TwitchRevocationError(RuntimeError):
    pass


class Twitch:
    def __init__(self, message_limit: int = 200) -> None:
        self.message_limit = message_limit
        self.connected = False
        self.stopped = False
        self.channel: str = ""
        self.session: Optional[TwitchSessionState] = None
        self.reader_thread: Optional[threading.Thread] = None

    def twitch_connect(self, channel: str) -> None:
        if websocket_create_connection is None:
            raise RuntimeError(
                "Twitch EventSub requires websocket-client. Install it with `pip install -r requirements.txt`. If that is already installed, repair it with `python3 -m pip install --force-reinstall --no-cache-dir websocket-client`."
            )

        self.close()
        self.stopped = False

        normalized_channel = self.normalize_channel(channel)
        if not normalized_channel:
            raise RuntimeError("Twitch channel name is required.")

        self.channel = normalized_channel
        auth = self.load_auth()

        print(f"Connecting to Twitch chat for {self.channel}...")
        session = TwitchSessionState(
            channel=self.channel,
            auth=auth,
            chat_user_id="",
            broadcaster_user_id="",
            message_queue=queue.Queue(maxsize=self.message_limit),
        )
        ws, keepalive_timeout = None, 10
        try:
            (
                session.chat_user_id,
                session.broadcaster_user_id,
                session.token_refresh_at,
            ) = self.resolve_twitch_ids(auth, self.channel)
            ws, keepalive_timeout = self.establish_eventsub_session(
                session, EVENTSUB_WEBSOCKET_URL, create_subscription=True
            )
        except TwitchRevocationError:
            self.stopped = True
            raise
        except (RuntimeError, requests.RequestException) as exc:
            print(
                f"Twitch initial connection failed ({type(exc).__name__}); reconnecting in the reader."
            )
        with session.ws_lock:
            session.ws = ws
        self.session = session
        self.connected = ws is not None

        self.reader_thread = threading.Thread(
            target=self.reader_loop,
            name="twitchplays-eventsub",
            args=(session, ws, keepalive_timeout),
            daemon=True,
        )
        self.reader_thread.start()
        if self.connected:
            print(f"Twitch chat connected for {self.channel}.")

    def twitch_receive_messages(self) -> List[Dict[str, Any]]:
        session = self.session
        if session is None:
            return []

        with session.ws_lock:
            dropped = session.dropped_messages
            session.dropped_messages = 0
        if dropped:
            print(
                f"Discarded {dropped} Twitch messages because the chat queue was full."
            )
        messages = []
        for index in range(self.message_limit):
            try:
                messages.append(session.message_queue.get_nowait())
            except queue.Empty:
                break
        return messages

    def close(self) -> None:
        self.stopped = True
        self.connected = False
        session = self.session
        reader_thread = self.reader_thread
        self.session = None
        self.reader_thread = None

        if session is not None:
            session.stop_event.set()
            with session.ws_lock:
                ws = session.ws
                session.ws = None
            self.safe_close_ws(ws)

        if (
            reader_thread
            and reader_thread.is_alive()
            and reader_thread is not threading.current_thread()
        ):
            reader_thread.join()

    def normalize_channel(self, channel: str) -> str:
        return str(channel or "").strip().lstrip("#").lower()

    def load_auth(self) -> Dict[str, str]:
        raw = load_twitchplays_config()
        auth: Dict[str, str] = {}
        for field in ("client_id", "access_token", "refresh_token"):
            value = str(raw.get(field) or "").strip()
            if not value:
                raise RuntimeError(
                    f"TwitchPlays config is missing `{field}`: {twitchplays_config_path()}"
                )
            auth[field] = value
        client_secret = str(raw.get("client_secret") or "").strip()
        if client_secret:
            auth["client_secret"] = client_secret

        return auth

    def save_auth(self, auth: Dict[str, str]) -> None:
        config = load_twitchplays_config()
        for field in ("client_id", "access_token", "refresh_token"):
            config[field] = auth[field]

        twitchplays_config_path().write_text(
            json.dumps(config, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def resolve_twitch_ids(
        self, auth: Dict[str, str], channel: str
    ) -> Tuple[str, str, Optional[float]]:
        token_info = self.validate_user_token(auth)
        chat_user_id = str(token_info.get("user_id") or "").strip()
        if not chat_user_id:
            raise RuntimeError("Twitch token validation did not return a user_id.")

        broadcaster = self.fetch_user_by_login(auth, channel)
        broadcaster_user_id = str(broadcaster.get("id") or "").strip()
        if not broadcaster_user_id:
            raise RuntimeError(
                f"Could not resolve a Twitch user ID for channel `{channel}`."
            )
        return chat_user_id, broadcaster_user_id, self.next_token_refresh_at(token_info)

    def validate_user_token(self, auth: Dict[str, str]) -> Dict[str, Any]:
        response = requests.get(
            TWITCH_OAUTH_VALIDATE_URL,
            headers={"Authorization": f"OAuth {auth['access_token']}"},
            timeout=API_TIMEOUT_SECONDS,
        )
        if response.status_code == 401:
            self.refresh_access_token(auth)
            response = requests.get(
                TWITCH_OAUTH_VALIDATE_URL,
                headers={"Authorization": f"OAuth {auth['access_token']}"},
                timeout=API_TIMEOUT_SECONDS,
            )
        data = self.parse_validated_token_response(auth, response)

        if self.token_expires_soon(data):
            self.refresh_access_token(auth)
            response = requests.get(
                TWITCH_OAUTH_VALIDATE_URL,
                headers={"Authorization": f"OAuth {auth['access_token']}"},
                timeout=API_TIMEOUT_SECONDS,
            )
            data = self.parse_validated_token_response(auth, response)

        return data

    def parse_validated_token_response(
        self, auth: Dict[str, str], response: requests.Response
    ) -> Dict[str, Any]:
        if not response.ok:
            error_type = (
                TwitchRevocationError
                if response.status_code in (401, 403)
                else RuntimeError
            )
            raise error_type(
                f"Twitch token validation failed. {self.describe_response_error(response)}"
            )

        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("Twitch token validation returned an invalid response.")
        validated_client_id = str(data.get("client_id") or "").strip()
        if validated_client_id != auth["client_id"]:
            raise TwitchRevocationError(
                "TwitchPlays config client_id does not match the access token client_id."
            )

        scopes = data.get("scopes") or []
        if TWITCH_READ_CHAT_SCOPE not in scopes:
            raise TwitchRevocationError(
                f"Twitch access token is missing the `{TWITCH_READ_CHAT_SCOPE}` scope."
            )
        return data

    def token_expires_soon(self, data: Dict[str, Any]) -> bool:
        raw_expires_in = data.get("expires_in")
        if raw_expires_in is None:
            return False

        try:
            expires_in = int(raw_expires_in)
        except (TypeError, ValueError):
            return False

        return expires_in <= TOKEN_REFRESH_MARGIN_SECONDS

    def next_token_refresh_at(self, data: Dict[str, Any]) -> Optional[float]:
        raw_expires_in = data.get("expires_in")
        if raw_expires_in is None:
            return None

        try:
            expires_in = int(raw_expires_in)
        except (TypeError, ValueError):
            return None

        return time.monotonic() + max(0, expires_in - TOKEN_REFRESH_MARGIN_SECONDS)

    def refresh_token_if_due(self, session: TwitchSessionState) -> None:
        now = time.monotonic()
        refresh_due = (
            session.token_refresh_at is not None and now >= session.token_refresh_at
        )
        if not refresh_due and now < session.token_validation_at:
            return
        if refresh_due:
            self.refresh_access_token(session.auth)
        token_info = self.validate_user_token(session.auth)
        if str(token_info.get("user_id") or "") != session.chat_user_id:
            raise TwitchRevocationError(
                "Twitch token no longer belongs to the connected chat account."
            )
        session.token_refresh_at = self.next_token_refresh_at(token_info)
        session.token_validation_at = (
            time.monotonic() + TOKEN_VALIDATION_INTERVAL_SECONDS
        )

    def refresh_access_token(self, auth: Dict[str, str]) -> None:
        body = {
            "client_id": auth["client_id"],
            "grant_type": "refresh_token",
            "refresh_token": auth["refresh_token"],
        }
        client_secret = str(auth.get("client_secret") or "").strip()
        if client_secret:
            body["client_secret"] = client_secret

        response = requests.post(
            TWITCH_OAUTH_TOKEN_URL,
            data=body,
            timeout=API_TIMEOUT_SECONDS,
        )
        if not response.ok:
            error_type = (
                TwitchRevocationError
                if response.status_code in (400, 401, 403)
                else RuntimeError
            )
            raise error_type(
                f"Twitch token refresh failed. {self.describe_response_error(response)}"
            )

        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("Twitch token refresh returned an invalid response.")

        new_access_token = str(data.get("access_token") or "").strip()
        if not new_access_token:
            raise RuntimeError(
                "Twitch token refresh response did not include an access_token."
            )

        auth["access_token"] = new_access_token
        new_refresh_token = str(data.get("refresh_token") or "").strip()
        if new_refresh_token:
            auth["refresh_token"] = new_refresh_token
        self.save_auth(auth)
        print("Refreshed Twitch access token.")

    def fetch_user_by_login(self, auth: Dict[str, str], login: str) -> Dict[str, Any]:
        response = self.request_helix(
            auth,
            "GET",
            TWITCH_HELIX_USERS_URL,
            params={"login": login},
        )
        if not response.ok:
            raise RuntimeError(
                f"Could not look up Twitch user `{login}`. {self.describe_response_error(response)}"
            )

        data = response.json().get("data") or []
        if not data:
            raise RuntimeError(f"Could not find Twitch user `{login}`.")
        return data[0]

    def request_helix(
        self,
        auth: Dict[str, str],
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        allow_refresh: bool = True,
    ) -> requests.Response:
        response = requests.request(
            method,
            url,
            params=params,
            json=json_body,
            headers={
                "Authorization": f"Bearer {auth['access_token']}",
                "Client-Id": auth["client_id"],
                "Content-Type": "application/json",
            },
            timeout=API_TIMEOUT_SECONDS,
        )
        if response.status_code == 401 and allow_refresh:
            self.refresh_access_token(auth)
            return self.request_helix(
                auth,
                method,
                url,
                params=params,
                json_body=json_body,
                allow_refresh=False,
            )
        if response.status_code in (401, 403):
            raise TwitchRevocationError(
                f"Twitch rejected authorization for the Helix request (HTTP {response.status_code})."
            )
        return response

    def establish_eventsub_session(
        self,
        session: TwitchSessionState,
        ws_url: str,
        *,
        create_subscription: bool,
    ) -> Tuple[Any, int]:
        if session.stop_event.is_set():
            raise RuntimeError("Twitch connection was stopped.")
        ws = self.open_websocket(ws_url)
        try:
            welcome = self.wait_for_welcome(ws, session.channel)
            welcome_session = welcome.get("payload", {}).get("session", {})
            keepalive_timeout = int(
                welcome_session.get("keepalive_timeout_seconds") or 10
            )
            welcome_session_id = str(welcome_session.get("id") or "").strip()
            if not welcome_session_id:
                raise RuntimeError(
                    "Twitch EventSub welcome message did not include a session ID."
                )
            if session.stop_event.is_set():
                raise RuntimeError("Twitch connection was stopped.")
            if create_subscription:
                self.create_chat_subscription(session, welcome_session_id)
            return ws, keepalive_timeout
        except Exception:
            self.safe_close_ws(ws)
            raise

    def open_websocket(self, ws_url: str) -> Any:
        try:
            if websocket_create_connection is None:
                raise RuntimeError("websocket-client is not available.")
            ws = websocket_create_connection(
                ws_url,
                timeout=API_TIMEOUT_SECONDS,
                enable_multithread=True,
            )
            ws.settimeout(1.0)
            return ws
        except Exception as exc:
            raise RuntimeError(
                f"Could not connect to the Twitch EventSub websocket. {exc}"
            ) from exc

    def wait_for_welcome(self, ws: Any, channel: str) -> Dict[str, Any]:
        deadline = time.monotonic() + API_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                raw = ws.recv()
            except WebSocketTimeoutException:
                continue
            except Exception as exc:
                raise RuntimeError(
                    f"Failed while waiting for the Twitch EventSub welcome message. {exc}"
                ) from exc

            if raw is None:
                raise RuntimeError(
                    "Twitch EventSub closed the websocket before sending the welcome message."
                )

            message = self.parse_eventsub_message(raw)
            message_type = self.message_type(message)
            if message_type == "session_welcome":
                return message
            if message_type == "revocation":
                raise TwitchRevocationError(
                    self.format_revocation_message(message, channel)
                )
        raise RuntimeError(
            "Timed out while waiting for the Twitch EventSub welcome message."
        )

    def create_chat_subscription(
        self, session: TwitchSessionState, session_id: str
    ) -> None:
        body = {
            "type": EVENTSUB_CHAT_MESSAGE_TYPE,
            "version": "1",
            "condition": {
                "broadcaster_user_id": session.broadcaster_user_id,
                "user_id": session.chat_user_id,
            },
            "transport": {"method": "websocket", "session_id": session_id},
        }
        response = self.request_helix(
            session.auth,
            "POST",
            TWITCH_HELIX_SUBSCRIPTIONS_URL,
            json_body=body,
        )
        if response.status_code not in (200, 202):
            raise RuntimeError(
                f"Could not create the Twitch chat subscription. {self.describe_response_error(response)}"
            )

    def reader_loop(
        self,
        session: TwitchSessionState,
        initial_ws: Any,
        initial_keepalive_timeout: int,
    ) -> None:
        ws = initial_ws
        keepalive_timeout = initial_keepalive_timeout
        keepalive_deadline = self.next_keepalive_deadline(keepalive_timeout)
        reconnect_backoff = 1

        while not session.stop_event.is_set():
            try:
                while not session.stop_event.is_set():
                    if ws is None:
                        raise RuntimeError("Twitch has no active socket.")
                    self.refresh_token_if_due(session)
                    try:
                        raw = ws.recv()
                    except WebSocketTimeoutException:
                        if time.monotonic() > keepalive_deadline:
                            raise RuntimeError(
                                "Timed out waiting for Twitch EventSub keepalive traffic."
                            )
                        continue
                    except WebSocketConnectionClosedException as exc:
                        raise RuntimeError("Twitch EventSub websocket closed.") from exc
                    except Exception as exc:
                        raise RuntimeError(
                            f"Twitch EventSub websocket read failed. {exc}"
                        ) from exc

                    if raw is None:
                        raise RuntimeError("Twitch EventSub websocket closed.")

                    message = self.parse_eventsub_message(raw)
                    message_type = self.message_type(message)
                    keepalive_deadline = self.next_keepalive_deadline(keepalive_timeout)

                    if message_type == "session_keepalive":
                        continue
                    if message_type == "notification":
                        self.handle_notification(session, message)
                        continue
                    if message_type == "session_reconnect":
                        reconnect_url = (
                            message.get("payload", {})
                            .get("session", {})
                            .get("reconnect_url")
                        )
                        if not reconnect_url:
                            raise RuntimeError(
                                "Twitch asked for a reconnect without a reconnect_url."
                            )
                        new_ws, keepalive_timeout = self.establish_eventsub_session(
                            session, reconnect_url, create_subscription=False
                        )
                        with session.ws_lock:
                            if session.ws is ws:
                                session.ws = new_ws
                        self.safe_close_ws(ws)
                        ws = new_ws
                        keepalive_deadline = self.next_keepalive_deadline(
                            keepalive_timeout
                        )
                        print(
                            f"Twitch requested a websocket reconnect for {session.channel}."
                        )
                        continue
                    if message_type == "revocation":
                        raise TwitchRevocationError(
                            self.format_revocation_message(message, session.channel)
                        )
                    if message_type == "session_welcome":
                        welcome_session = message.get("payload", {}).get("session", {})
                        keepalive_timeout = int(
                            welcome_session.get("keepalive_timeout_seconds")
                            or keepalive_timeout
                        )
                        keepalive_deadline = self.next_keepalive_deadline(
                            keepalive_timeout
                        )
            except TwitchRevocationError as exc:
                self.connected = False
                self.stopped = True
                session.stop_event.set()
                print(f"Twitch chat transport stopped for {session.channel}. {exc}")
                break
            except Exception as exc:
                if session.stop_event.is_set():
                    break

                self.connected = False
                for index in range(self.message_limit):
                    try:
                        session.message_queue.get_nowait()
                    except queue.Empty:
                        break
                print(
                    f"Twitch chat transport failed for {session.channel}. Reconnecting in {reconnect_backoff} seconds. {exc}"
                )
                self.safe_close_ws(ws)
                with session.ws_lock:
                    if session.ws is ws:
                        session.ws = None

                if session.stop_event.wait(reconnect_backoff):
                    break

                try:
                    (
                        session.chat_user_id,
                        session.broadcaster_user_id,
                        session.token_refresh_at,
                    ) = self.resolve_twitch_ids(session.auth, session.channel)
                    ws, keepalive_timeout = self.establish_eventsub_session(
                        session,
                        EVENTSUB_WEBSOCKET_URL,
                        create_subscription=True,
                    )
                except TwitchRevocationError as reconnect_exc:
                    self.stopped = True
                    session.stop_event.set()
                    print(
                        f"Twitch chat transport stopped for {session.channel}. {reconnect_exc}"
                    )
                    break
                except Exception as reconnect_exc:
                    print(
                        f"Twitch chat reconnect failed for {session.channel}. {reconnect_exc}"
                    )
                    reconnect_backoff = min(
                        EVENTSUB_BACKOFF_MAX_SECONDS, reconnect_backoff * 2
                    )
                    continue

                with session.ws_lock:
                    if session.stop_event.is_set():
                        self.safe_close_ws(ws)
                        break
                    session.ws = ws
                    self.connected = True
                session.token_validation_at = (
                    time.monotonic() + TOKEN_VALIDATION_INTERVAL_SECONDS
                )
                keepalive_deadline = self.next_keepalive_deadline(keepalive_timeout)
                reconnect_backoff = 1
                print(f"Twitch chat reconnected for {session.channel}.")

        self.connected = False
        with session.ws_lock:
            if session.ws is ws:
                session.ws = None
        self.safe_close_ws(ws)

    def next_keepalive_deadline(self, keepalive_timeout: int) -> float:
        return time.monotonic() + keepalive_timeout + EVENTSUB_KEEPALIVE_GRACE_SECONDS

    def parse_eventsub_message(self, raw: Any) -> Dict[str, Any]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Received malformed JSON from Twitch EventSub.") from exc
        if not isinstance(data, dict):
            raise RuntimeError("Received an invalid payload from Twitch EventSub.")
        return data

    def message_type(self, message: Dict[str, Any]) -> str:
        metadata = message.get("metadata") or {}
        return str(metadata.get("message_type") or "").strip()

    def handle_notification(
        self, session: TwitchSessionState, message: Dict[str, Any]
    ) -> None:
        payload = message.get("payload") or {}
        subscription = payload.get("subscription") or {}
        event = payload.get("event") or {}
        subscription_type = str(
            subscription.get("type")
            or (message.get("metadata") or {}).get("subscription_type")
            or ""
        ).strip()
        if subscription_type != EVENTSUB_CHAT_MESSAGE_TYPE:
            return

        message_id = str(event.get("message_id") or "").strip()
        if message_id and not self.track_message_id(session, message_id):
            return

        username = str(
            event.get("chatter_user_name") or event.get("chatter_user_login") or ""
        ).strip()
        message_text = str((event.get("message") or {}).get("text") or "").strip()
        if not username or not message_text:
            return

        if len(message_text) > MAX_CHAT_MESSAGE_LENGTH or session.stop_event.is_set():
            return
        item = {
            "username": username,
            "message": message_text,
            "platform": "twitch",
            "user_id": str(event.get("chatter_user_id") or ""),
            "message_id": message_id,
            "received_at": time.monotonic(),
            "published_at": message_timestamp(
                (message.get("metadata") or {}).get("message_timestamp")
            ),
        }
        try:
            session.message_queue.put_nowait(item)
        except queue.Full:
            with session.ws_lock:
                session.dropped_messages += 1

    def track_message_id(self, session: TwitchSessionState, message_id: str) -> bool:
        if message_id in session.seen_message_id_lookup:
            return False

        session.seen_message_ids.append(message_id)
        session.seen_message_id_lookup.add(message_id)

        while len(session.seen_message_ids) > MAX_STORED_MESSAGE_IDS:
            oldest = session.seen_message_ids.popleft()
            session.seen_message_id_lookup.discard(oldest)

        return True

    def format_revocation_message(self, message: Dict[str, Any], channel: str) -> str:
        payload = message.get("payload") or {}
        subscription = payload.get("subscription") or {}
        subscription_type = str(subscription.get("type") or "").strip() or "unknown"
        status = str(subscription.get("status") or "").strip() or "unknown"
        return f"Twitch revoked the `{subscription_type}` subscription for `{channel}` with status `{status}`."

    def describe_response_error(self, response: requests.Response) -> str:
        return f"HTTP {response.status_code}"

    def safe_close_ws(self, ws_obj: Any) -> None:
        if ws_obj is None:
            return
        try:
            ws_obj.close()
        except Exception:
            pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class YouTubeFallbackError(RuntimeError):
    pass


class ChatEndedError(RuntimeError):
    pass


class YouTubeAuthorizationError(RuntimeError):
    pass


# Thanks to Ottomated for helping with the yt side of things!
class YouTube:
    """Poll the API when configured, otherwise use the existing HTML reader."""

    def __init__(self, api_key: Optional[str] = None, message_limit: int = 200) -> None:
        self.session = None
        self.config = {}
        self.payload = {}
        self.message_limit = message_limit
        self.thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.fetch_job = None
        self.next_fetch_time = 0.0
        self.poll_interval = YOUTUBE_FETCH_INTERVAL
        self.channel_id = None
        self.stream_url = None
        self.api_key = api_key
        self.use_api = bool(api_key)
        self.live_chat_id = None
        self.next_page_token = None
        self.connected = False
        self.stopped = False
        self.stop_event = threading.Event()
        self.needs_connect = True
        self.bootstrap_pending = True
        self.seen_message_ids = deque()
        self.seen_message_id_lookup = set()
        self.re_initial_data = re.compile(
            r"(?:window\s*\[\s*[\"']ytInitialData[\"']\s*\]|ytInitialData)\s*=\s*({.+?})\s*;"
        )
        self.re_config = re.compile(r"(?:ytcfg\s*.set)\(({.+?})\)\s*;")

    def youtube_connect(self, channel_id, stream_url=None, api_key=None) -> None:
        if self.stop_event.is_set():
            raise RuntimeError("The YouTube reader is closed.")
        self.channel_id, self.stream_url = channel_id, stream_url
        if api_key is not None:
            self.api_key = api_key
            self.use_api = bool(api_key)
        if not channel_id and not stream_url:
            raise RuntimeError("YouTube requires either a channel ID or a stream URL.")
        try:
            if self.use_api:
                try:
                    if self.api_connect():
                        self.connected = True
                        self.needs_connect = False
                        print("Connected to YouTube via API.")
                        return
                except (YouTubeFallbackError, requests.RequestException):
                    pass
                self.switch_to_scrape_mode("The API connection could not be used.")
            self.scrape_connect()
            self.connected = True
            self.needs_connect = False
        except (ChatEndedError, YouTubeAuthorizationError):
            self.connected = False
            self.stopped = True
            raise

    def extract_video_id_from_url(self, url: str) -> Optional[str]:
        match = re.search(r"(?:v=|/live/|youtu\.be/)([A-Za-z0-9_\-]{6,})", url)
        return match.group(1) if match else None

    def http(self) -> requests.Session:
        if self.stop_event.is_set():
            raise RuntimeError("The YouTube reader is closed.")
        if self.session is None:
            self.session = requests.Session()
            self.session.headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
            requests.utils.add_dict_to_cookiejar(
                self.session.cookies, {"CONSENT": "YES+"}
            )
        return self.session

    def response_reasons(self, response) -> set:
        try:
            data = response.json()
        except ValueError:
            return set()
        return {item.get("reason") for item in data.get("error", {}).get("errors", [])}

    def is_quota_error(self, response) -> bool:
        return bool(
            self.response_reasons(response) & {"quotaExceeded", "dailyLimitExceeded"}
        )

    def check_response(self, response, operation: str) -> None:
        if response.ok:
            return
        reasons = self.response_reasons(response)
        if reasons & {"quotaExceeded", "dailyLimitExceeded"}:
            raise YouTubeFallbackError(
                f"YouTube quota was exhausted during {operation}."
            )
        if reasons & {"liveChatEnded", "liveChatDisabled", "liveChatNotFound"}:
            raise ChatEndedError(
                f"YouTube chat is unavailable during {operation}: {', '.join(sorted(reasons))}."
            )
        # Google also uses 403 for temporary request limits.
        if response.status_code == 401 or (
            response.status_code == 403
            and (
                not reasons
                or reasons
                & {"forbidden", "insufficientPermissions", "accessNotConfigured"}
            )
        ):
            raise YouTubeAuthorizationError(
                f"YouTube denied access during {operation} (HTTP {response.status_code}). Check video access and the configured credentials."
            )
        # Request exceptions can include URLs containing API keys; report only status.
        raise requests.HTTPError(
            f"YouTube {operation} failed (HTTP {response.status_code}).",
            response=response,
        )

    def api_connect(self) -> bool:
        video_id = (
            self.extract_video_id_from_url(self.stream_url) if self.stream_url else None
        )
        if not video_id and self.channel_id:
            response = self.http().get(
                "https://www.googleapis.com/youtube/v3/search",
                params={
                    "part": "id",
                    "channelId": self.channel_id,
                    "eventType": "live",
                    "type": "video",
                    "maxResults": 1,
                    "key": self.api_key,
                },
                timeout=API_TIMEOUT_SECONDS,
            )
            if not response.ok and not self.is_quota_error(response):
                print(f"YouTube video search failed (HTTP {response.status_code}).")
                return False
            self.check_response(response, "video search")
            items = response.json().get("items") or []
            video_id = items[0]["id"]["videoId"] if items else None
        if not video_id:
            raise ChatEndedError(
                "YouTube has no live video for the configured channel or URL."
            )
        response = self.http().get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={
                "part": "liveStreamingDetails",
                "id": video_id,
                "key": self.api_key,
            },
            timeout=API_TIMEOUT_SECONDS,
        )
        if not response.ok and not self.is_quota_error(response):
            print(f"YouTube live chat lookup failed (HTTP {response.status_code}).")
            return False
        self.check_response(response, "live chat lookup")
        items = response.json().get("items") or []
        self.live_chat_id = (
            items[0].get("liveStreamingDetails", {}).get("activeLiveChatId")
            if items
            else None
        )
        if not self.live_chat_id:
            raise ChatEndedError(
                "YouTube live chat is not active on the configured video."
            )
        self.next_page_token = None
        self.bootstrap_pending = True
        return True

    def switch_to_scrape_mode(self, reason: str) -> None:
        # Called by the receiving thread after the pending job has finished.
        print(f"Switching YouTube to HTML scraping. {reason}")
        self.use_api = False
        self.live_chat_id = None
        self.next_page_token = None
        self.needs_connect = True
        self.bootstrap_pending = True
        if self.session is not None:
            self.session.close()
            self.session = None

    def api_fetch_messages(self) -> list:
        params = {
            "part": "snippet,authorDetails",
            "liveChatId": self.live_chat_id,
            "maxResults": min(2000, max(200, self.message_limit)),
            "key": self.api_key,
        }
        if self.next_page_token:
            params["pageToken"] = self.next_page_token
        response = self.http().get(
            "https://www.googleapis.com/youtube/v3/liveChat/messages",
            params=params,
            timeout=API_TIMEOUT_SECONDS,
        )
        self.check_response(response, "chat polling")
        data = response.json()
        self.next_page_token = data.get("nextPageToken")
        self.poll_interval = max(
            YOUTUBE_FETCH_INTERVAL,
            int(data.get("pollingIntervalMillis", 1000)) / 1000.0,
        )
        messages = []
        for item in data.get("items", []):
            snippet, author = item.get("snippet", {}), item.get("authorDetails", {})
            messages.append(
                {
                    "username": author.get("displayName")
                    or author.get("channelId", ""),
                    "message": snippet.get("displayMessage", ""),
                    "user_id": author.get("channelId", ""),
                    "message_id": item.get("id", ""),
                    "published_at": message_timestamp(snippet.get("publishedAt")),
                }
            )
        if data.get("offlineAt"):
            raise ChatEndedError("YouTube reported that the live chat has ended.")
        return messages

    def scrape_connect(self) -> None:
        print("Connecting to YouTube using HTML scraping...")
        live_url = (
            self.stream_url or f"https://youtube.com/channel/{self.channel_id}/live"
        )
        response = self.http().get(live_url, timeout=API_TIMEOUT_SECONDS)
        if response.status_code == 404 and not self.stream_url:
            response = self.http().get(
                f"https://youtube.com/c/{self.channel_id}/live",
                timeout=API_TIMEOUT_SECONDS,
            )
        self.check_response(response, "livestream page loading")
        match = self.re_initial_data.search(response.text)
        if not match:
            raise RuntimeError(
                "YouTube livestream page contained no initial chat data."
            )
        initial = json.loads(match.group(1))
        try:
            continuation = initial["contents"]["twoColumnWatchNextResults"][
                "conversationBar"
            ]["liveChatRenderer"]["header"]["liveChatHeaderRenderer"]["viewSelector"][
                "sortFilterSubMenuRenderer"
            ][
                "subMenuItems"
            ][
                1
            ][
                "continuation"
            ][
                "reloadContinuationData"
            ][
                "continuation"
            ]
        except (KeyError, IndexError, TypeError) as exc:
            raise ChatEndedError(
                "YouTube livestream page has no active live chat."
            ) from exc
        response = self.http().get(
            "https://youtube.com/live_chat",
            params={"continuation": continuation},
            timeout=API_TIMEOUT_SECONDS,
        )
        self.check_response(response, "live chat page loading")
        initial_match = self.re_initial_data.search(response.text)
        config_match = self.re_config.search(response.text)
        if not initial_match or not config_match:
            raise RuntimeError(
                "YouTube live chat page is missing its data or configuration."
            )
        initial, self.config = json.loads(initial_match.group(1)), json.loads(
            config_match.group(1)
        )
        self.payload = {
            "context": self.config["INNERTUBE_CONTEXT"],
            "continuation": self.get_continuation_token(initial),
            "webClientInfo": {"isDocumentHidden": False},
        }
        self.bootstrap_pending = True
        self.poll_interval = YOUTUBE_FETCH_INTERVAL
        print("Connected to YouTube using HTML scraping.")

    def get_continuation_token(self, data: dict) -> str:
        continuations = (
            data["continuationContents"]["liveChatContinuation"].get("continuations")
            or []
        )
        if not continuations:
            raise ChatEndedError("YouTube chat no longer has a live continuation.")
        continuation = continuations[0]
        for name in (
            "timedContinuationData",
            "invalidationContinuationData",
            "reloadContinuationData",
        ):
            if name in continuation:
                return continuation[name]["continuation"]
        raise RuntimeError("YouTube returned an unsupported chat continuation.")

    def fetch_messages(self) -> list:
        response = self.http().post(
            "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat",
            params={"key": self.config["INNERTUBE_API_KEY"], "prettyPrint": "false"},
            data=json.dumps(self.payload).encode("utf-8"),
            timeout=API_TIMEOUT_SECONDS,
        )
        self.check_response(response, "HTML chat polling")
        data = response.json()
        self.payload["continuation"] = self.get_continuation_token(data)
        messages = []
        for action in data["continuationContents"]["liveChatContinuation"].get(
            "actions", []
        ):
            item = (
                action.get("addChatItemAction", {})
                .get("item", {})
                .get("liveChatTextMessageRenderer")
            )
            if not item:
                continue
            timestamp = item.get("timestampUsec")
            messages.append(
                {
                    "username": item.get("authorName", {}).get("simpleText", ""),
                    "message": "".join(
                        part.get("text", part.get("emoji", {}).get("emojiId", ""))
                        for part in item.get("message", {}).get("runs", [])
                    ),
                    "user_id": item.get("authorExternalChannelId", ""),
                    "message_id": item.get("id", ""),
                    "published_at": int(timestamp) / 1_000_000 if timestamp else None,
                }
            )
        return messages

    def fetch_batch(self) -> list:
        started = time.monotonic()
        if self.needs_connect:
            if self.use_api:
                if not self.api_connect():
                    raise YouTubeFallbackError("The API connection could not be used.")
            else:
                self.scrape_connect()
            self.needs_connect = False
        messages = self.api_fetch_messages() if self.use_api else self.fetch_messages()
        received = time.monotonic()
        bootstrap = self.bootstrap_pending
        self.bootstrap_pending = False
        output = []
        for message in messages:
            message_id = message.get("message_id")
            if message_id:
                if message_id in self.seen_message_id_lookup:
                    continue
                self.seen_message_id_lookup.add(message_id)
                self.seen_message_ids.append(message_id)
                while len(self.seen_message_ids) > max(
                    MAX_STORED_MESSAGE_IDS, self.message_limit
                ):
                    self.seen_message_id_lookup.discard(self.seen_message_ids.popleft())
            if (
                not bootstrap
                and message.get("username")
                and 0 < len(message.get("message", "")) <= MAX_CHAT_MESSAGE_LENGTH
            ):
                output.append(
                    {
                        **message,
                        "platform": "youtube",
                        "received_at": received,
                        "fetch_started_at": started,
                    }
                )
        if len(output) > self.message_limit:
            print(
                f"Discarded {len(output) - self.message_limit} YouTube messages because the chat batch was full."
            )
        return output[: self.message_limit]

    def twitch_receive_messages(self) -> list:
        if self.stopped or self.stop_event.is_set():
            return []
        if self.fetch_job is None:
            if time.monotonic() >= self.next_fetch_time:
                self.fetch_job = self.thread_pool.submit(self.fetch_batch)
            return []
        if not self.fetch_job.done():
            return []
        job, self.fetch_job = self.fetch_job, None
        try:
            messages = job.result()
        except (ChatEndedError, YouTubeAuthorizationError) as exc:
            self.connected = False
            self.stopped = True
            print(f"YouTube chat stopped. {exc}")
        except YouTubeFallbackError as exc:
            self.connected = False
            self.switch_to_scrape_mode(str(exc))
            self.next_fetch_time = time.monotonic() + max(5.0, self.poll_interval)
        except Exception as exc:
            self.connected = False
            self.needs_connect = True
            self.bootstrap_pending = True
            self.next_fetch_time = time.monotonic() + max(5.0, self.poll_interval)
            # Do not print exception URLs or response bodies containing credentials.
            detail = (
                f"HTTP {exc.response.status_code}"
                if isinstance(exc, requests.HTTPError) and exc.response is not None
                else type(exc).__name__
            )
            print(
                f"YouTube chat polling failed ({detail}); retrying after the polling delay."
            )
        else:
            if not self.connected:
                print("YouTube chat recovered.")
            self.connected = True
            self.next_fetch_time = time.monotonic() + self.poll_interval
            return messages
        return []

    def close(self) -> None:
        self.stop_event.set()
        self.stopped = True
        self.connected = False
        if self.fetch_job is not None:
            self.fetch_job.cancel()
        self.thread_pool.shutdown(wait=True, cancel_futures=True)
        self.fetch_job = None
        if self.session is not None:
            self.session.close()
            self.session = None
