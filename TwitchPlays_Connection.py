# DougDoug Note:
# This is the code that connects to Twitch / Youtube and checks for new messages.
# You should not need to modify anything in this file, just use as is.

# This code is based on Wituz's "Twitch Plays" tutorial, updated for Python 3.X
# Updated for Youtube by DDarknut, with help by Ottomated

# Sol Note:
# Ignore the information above. Keeping it inside for nostalgia.

import concurrent.futures
import json
import queue
import re
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

import requests

try:
    import websocket
except Exception:
    websocket = None

API_TIMEOUT_SECONDS = 10
EVENTSUB_BACKOFF_MAX_SECONDS = 30
EVENTSUB_CHAT_MESSAGE_TYPE = "channel.chat.message"
EVENTSUB_KEEPALIVE_GRACE_SECONDS = 5
EVENTSUB_WEBSOCKET_URL = "wss://eventsub.wss.twitch.tv/ws"
MAX_STORED_MESSAGE_IDS = 2048
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


@dataclass
class TwitchSessionState:
    channel: str
    auth: Dict[str, str]
    chat_user_id: str
    broadcaster_user_id: str
    message_queue: "queue.Queue[Dict[str, str]]" = field(default_factory=queue.Queue)
    seen_message_ids: Deque[str] = field(default_factory=deque)
    seen_message_id_lookup: Set[str] = field(default_factory=set)
    stop_event: threading.Event = field(default_factory=threading.Event)
    ws: Any = None
    ws_lock: threading.Lock = field(default_factory=threading.Lock)


class TwitchRevocationError(RuntimeError):
    pass


class Twitch:
    def __init__(self) -> None:
        self.channel: str = ""
        self.session: Optional[TwitchSessionState] = None
        self.reader_thread: Optional[threading.Thread] = None

    def twitch_connect(self, channel: str) -> None:
        if websocket is None:
            raise RuntimeError(
                "Twitch EventSub requires websocket-client. Install it with `pip install -r requirements.txt`."
            )

        self.close()

        normalized_channel = self.normalize_channel(channel)
        if not normalized_channel:
            raise RuntimeError("Twitch channel name is required.")

        self.channel = normalized_channel
        auth = self.load_auth()

        print(f"Connecting to Twitch chat for {self.channel}...")
        chat_user_id, broadcaster_user_id = self.resolve_twitch_ids(auth, self.channel)
        session = TwitchSessionState(
            channel=self.channel,
            auth=auth,
            chat_user_id=chat_user_id,
            broadcaster_user_id=broadcaster_user_id,
        )
        ws, keepalive_timeout = self.establish_eventsub_session(
            session, EVENTSUB_WEBSOCKET_URL, create_subscription=True
        )

        with session.ws_lock:
            session.ws = ws

        self.session = session

        self.reader_thread = threading.Thread(
            target=self.reader_loop,
            name="twitchplays-eventsub",
            args=(session, ws, keepalive_timeout),
            daemon=True,
        )
        self.reader_thread.start()
        print(f"Twitch chat connected for {self.channel}.")

    def twitch_receive_messages(self) -> List[Dict[str, str]]:
        session = self.session
        if session is None:
            return []

        messages: List[Dict[str, str]] = []
        while True:
            try:
                messages.append(session.message_queue.get_nowait())
            except queue.Empty:
                return messages

    def close(self) -> None:
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

    def resolve_twitch_ids(self, auth: Dict[str, str], channel: str) -> Tuple[str, str]:
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
        return chat_user_id, broadcaster_user_id

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
        if not response.ok:
            raise RuntimeError(
                f"Twitch token validation failed. {self.describe_response_error(response)}"
            )

        data = response.json()
        validated_client_id = str(data.get("client_id") or "").strip()
        if validated_client_id != auth["client_id"]:
            raise RuntimeError(
                "TwitchPlays config client_id does not match the access token client_id."
            )

        scopes = data.get("scopes") or []
        if TWITCH_READ_CHAT_SCOPE not in scopes:
            raise RuntimeError(
                f"Twitch access token is missing the `{TWITCH_READ_CHAT_SCOPE}` scope."
            )

        return data

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
            raise RuntimeError(
                f"Twitch token refresh failed. {self.describe_response_error(response)}"
            )

        data = response.json()
        auth["access_token"] = str(data.get("access_token") or "").strip()
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
        return response

    def establish_eventsub_session(
        self,
        session: TwitchSessionState,
        ws_url: str,
        *,
        create_subscription: bool,
    ) -> Tuple[Any, int]:
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
            if create_subscription:
                self.create_chat_subscription(session, welcome_session_id)
            return ws, keepalive_timeout
        except Exception:
            self.safe_close_ws(ws)
            raise

    def open_websocket(self, ws_url: str) -> Any:
        try:
            ws = websocket.create_connection(
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
        deadline = time.time() + API_TIMEOUT_SECONDS
        while time.time() < deadline:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
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
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        if time.time() > keepalive_deadline:
                            raise RuntimeError(
                                "Timed out waiting for Twitch EventSub keepalive traffic."
                            )
                        continue
                    except websocket.WebSocketConnectionClosedException as exc:
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
                session.stop_event.set()
                print(f"Twitch chat transport stopped for {session.channel}. {exc}")
                break
            except Exception as exc:
                if session.stop_event.is_set():
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
                    ) = self.resolve_twitch_ids(session.auth, session.channel)
                    ws, keepalive_timeout = self.establish_eventsub_session(
                        session,
                        EVENTSUB_WEBSOCKET_URL,
                        create_subscription=True,
                    )
                except TwitchRevocationError as reconnect_exc:
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
                    session.ws = ws
                keepalive_deadline = self.next_keepalive_deadline(keepalive_timeout)
                reconnect_backoff = 1
                print(f"Twitch chat reconnected for {session.channel}.")

        with session.ws_lock:
            if session.ws is ws:
                session.ws = None
        self.safe_close_ws(ws)

    def next_keepalive_deadline(self, keepalive_timeout: int) -> float:
        return time.time() + keepalive_timeout + EVENTSUB_KEEPALIVE_GRACE_SECONDS

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

        session.message_queue.put({"username": username, "message": message_text})

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
        try:
            data = response.json()
        except Exception:
            data = None

        if isinstance(data, dict):
            for key in ("message", "error_description", "error"):
                value = str(data.get(key) or "").strip()
                if value:
                    return f"{response.status_code} {value}"

        return f"{response.status_code} {response.text.strip()}"

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


# Thanks to Ottomated for helping with the yt side of things!
class YouTube:
    """
    YouTube live chat reader with API-first, scrape-fallback behavior.

    When an API key is provided:
        1) Resolve active liveChatId via API (from stream URL's videoId or by searching the channel).
        2) Poll liveChatMessages.list using nextPageToken and pollingIntervalMillis.
        3) On quota errors (403 + reason quotaExceeded/dailyLimitExceeded), fall back to scraper.

    When no API key is provided: use the existing scraper immediately.
    """

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.session: Optional[requests.Session] = None
        self.config: Dict[str, Any] = {}
        self.payload: Dict[str, Any] = {}

        self.thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.fetch_job: Optional[concurrent.futures.Future] = None
        self.next_fetch_time = 0.0

        self.channel_id: Optional[str] = None
        self.stream_url: Optional[str] = None

        # scrape regexes
        self.re_initial_data = re.compile(
            r"(?:window\s*\[\s*[\"']ytInitialData[\"']\s*\]|ytInitialData)\s*=\s*({.+?})\s*;"
        )
        self.re_config = re.compile(r"(?:ytcfg\s*.set)\(({.+?})\)\s*;")

        # API mode state
        self.api_key: Optional[str] = api_key
        self.use_api: bool = bool(api_key)
        self.live_chat_id: Optional[str] = None
        self.next_page_token: Optional[str] = None

    # ---------------------------
    # Public connect entry point
    # ---------------------------
    def youtube_connect(
        self,
        channel_id: str,
        stream_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        if api_key is not None:
            self.api_key = api_key
            self.use_api = bool(api_key)

        self.channel_id = channel_id
        self.stream_url = stream_url

        if self.use_api:
            ok = self.api_connect()
            if ok:
                print("Connected to YouTube via API.")
                return
            print("Falling back to HTML scraping...")
        # scraper fallback / no key:
        self.scrape_connect()

    # ---------------------------
    # API path
    # ---------------------------
    def extract_video_id_from_url(self, url: str) -> Optional[str]:
        # supports youtu.be/<id>, youtube.com/watch?v=<id>, youtube.com/live/<id>
        m = re.search(r"(?:v=|/live/|youtu\.be/)([A-Za-z0-9_\-]{6,})", url)
        return m.group(1) if m else None

    def http(self) -> requests.Session:
        if not self.session:
            self.session = requests.Session()
            self.session.headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
            requests.utils.add_dict_to_cookiejar(
                self.session.cookies, {"CONSENT": "YES+"}
            )
        return self.session

    def api_connect(self) -> bool:
        try:
            sess = self.http()
            video_id = None

            # 1) Determine live videoId
            if self.stream_url:
                video_id = self.extract_video_id_from_url(self.stream_url)

            if not video_id and self.channel_id:
                # search.list: find the live video for this channel
                # eventType=live, type=video
                params = {
                    "part": "id",
                    "channelId": self.channel_id,
                    "eventType": "live",
                    "type": "video",
                    "maxResults": 1,
                    "key": self.api_key,
                }
                r = sess.get(
                    "https://www.googleapis.com/youtube/v3/search",
                    params=params,
                    timeout=10,
                )
                if r.status_code == 403 and self.is_quota_error(r):
                    self.switch_to_scrape_mode("quota while search.list")
                    return False
                r.raise_for_status()
                data = r.json()
                items = data.get("items") or []
                if items:
                    video_id = items[0]["id"]["videoId"]

            if not video_id:
                print("Could not resolve a live video for the channel.")
                return False

            # 2) videos.list: get liveStreamingDetails.activeLiveChatId
            params = {
                "part": "liveStreamingDetails",
                "id": video_id,
                "key": self.api_key,
            }
            r = sess.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params=params,
                timeout=10,
            )
            if r.status_code == 403 and self.is_quota_error(r):
                self.switch_to_scrape_mode("quota while videos.list")
                return False
            r.raise_for_status()
            data = r.json()
            items = data.get("items") or []
            if not items:
                print("Video not found or not live.")
                return False

            lcd = items[0].get("liveStreamingDetails", {})
            self.live_chat_id = lcd.get("activeLiveChatId")
            if not self.live_chat_id:
                print("Live chat is not active on this video.")
                return False

            # reset API poll state
            self.next_page_token = None
            self.next_fetch_time = 0.0
            return True

        except requests.HTTPError as e:
            if (
                e.response is not None
                and e.response.status_code == 403
                and self.is_quota_error(e.response)
            ):
                self.switch_to_scrape_mode("quota while connecting")
                return False
            traceback.print_exc()
            return False
        except Exception:
            traceback.print_exc()
            return False

    def is_quota_error(self, resp: requests.Response) -> bool:
        try:
            err = resp.json().get("error", {})
            for e in err.get("errors", []):
                if e.get("reason") in ("quotaExceeded", "dailyLimitExceeded"):
                    return True
        except Exception:
            pass
        return False

    def switch_to_scrape_mode(self, reason: str) -> None:
        print(f"API quota hit ({reason}). Switching to HTML scraping fallback.")
        self.use_api = False
        # tear down API state
        self.live_chat_id = None
        self.next_page_token = None
        self.fetch_job = None
        self.next_fetch_time = 0.0
        # re-init as scraper
        try:
            if self.session:
                self.session.close()
        except Exception:
            pass
        self.session = None
        self.scrape_connect()

    def api_fetch_messages(self) -> List[Dict[str, Any]]:
        if not (self.api_key and self.live_chat_id):
            return []

        # Respect server-provided pacing: we'll set next_fetch_time in the caller.
        params = {
            "part": "snippet,authorDetails",
            "liveChatId": self.live_chat_id,
            "maxResults": 2000,
            "key": self.api_key,
        }
        if self.next_page_token:
            params["pageToken"] = self.next_page_token

        try:
            r = self.http().get(
                "https://www.googleapis.com/youtube/v3/liveChat/messages",
                params=params,
                timeout=10,
            )
        except Exception as e:
            print(f"Failed to fetch API messages: {e}")
            return []

        if r.status_code == 403 and self.is_quota_error(r):
            self.switch_to_scrape_mode("quota on liveChatMessages.list")
            return []

        if not r.ok:
            print(f"API fetch failed. {r.status_code} {r.reason}")
            return []

        data = r.json()
        self.next_page_token = data.get("nextPageToken")
        # schedule next poll based on API hint
        poll_ms = int(data.get("pollingIntervalMillis", 1000))
        self.next_fetch_time = time.time() + (poll_ms / 1000.0)

        msgs: List[Dict[str, Any]] = []
        for item in data.get("items", []):
            s = item.get("snippet", {})
            a = item.get("authorDetails", {})
            # normalize to the template's expected shape
            msgs.append(
                {
                    "author": a.get("displayName", "") or a.get("channelId", ""),
                    "content": [{"text": s.get("displayMessage", "")}],
                }
            )
        return msgs

    # ---------------------------
    # Scraper path (unchanged logic, factored into methods)
    # ---------------------------
    def reconnect(self, delay: float) -> None:
        # Cancel pending fetch job if any
        if self.fetch_job and self.fetch_job.running():
            try:
                self.fetch_job.cancel()
            except Exception:
                pass
            else:
                try:
                    self.fetch_job.result(timeout=0.1)
                except Exception:
                    pass

        print(f"Retrying in {delay} seconds...")
        if self.session:
            try:
                self.session.close()
            except Exception:
                pass

        self.session = None
        self.config = {}
        self.payload = {}
        self.fetch_job = None
        self.next_fetch_time = 0
        time.sleep(max(0.0, delay))

        # Attempt reconnect
        if self.channel_id:
            # reconnect preserving mode
            if self.use_api:
                self.api_connect()
            else:
                self.scrape_connect()

    def scrape_connect(self) -> None:
        print("Connecting to YouTube (HTML scrape)...")

        # Create http client session
        self.session = requests.Session()
        self.session.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
        requests.utils.add_dict_to_cookiejar(self.session.cookies, {"CONSENT": "YES+"})

        # Connect using stream_url if provided, otherwise use the channel_id
        if self.stream_url is not None:
            live_url = self.stream_url
        else:
            live_url = f"https://youtube.com/channel/{self.channel_id}/live"

        res = self.session.get(live_url)
        if res.status_code == 404:
            live_url = f"https://youtube.com/c/{self.channel_id}/live"
            res = self.session.get(live_url)
        if not res.ok:
            if self.stream_url is not None:
                print(
                    f"Couldn't load the stream URL ({res.status_code} {res.reason}). Is the stream URL correct? {self.stream_url}"
                )
            else:
                print(
                    f"Couldn't load livestream page ({res.status_code} {res.reason}). Is the channel ID correct? {self.channel_id}"
                )
            time.sleep(5)
            sys.exit(1)
        livestream_page = res.text

        matches = list(self.re_initial_data.finditer(livestream_page))
        if len(matches) == 0:
            print("Couldn't find initial data in livestream page")
            time.sleep(5)
            sys.exit(1)
        initial_data = json.loads(matches[0].group(1))

        try:
            iframe_continuation = initial_data["contents"]["twoColumnWatchNextResults"][
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
        except Exception:
            print(
                f"Couldn't find the livestream chat. Is the channel not live? url: {live_url}"
            )
            time.sleep(5)
            sys.exit(1)

        res = self.session.get(
            f"https://youtube.com/live_chat?continuation={iframe_continuation}"
        )
        if not res.ok:
            print(f"Couldn't load live chat page ({res.status_code} {res.reason})")
            time.sleep(5)
            sys.exit(1)
        live_chat_page = res.text

        matches = list(self.re_initial_data.finditer(live_chat_page))
        if len(matches) == 0:
            print("Couldn't find initial data in live chat page")
            time.sleep(5)
            sys.exit(1)
        initial_data = json.loads(matches[0].group(1))

        matches = list(self.re_config.finditer(live_chat_page))
        if len(matches) == 0:
            print("Couldn't find config data in live chat page")
            time.sleep(5)
            sys.exit(1)
        self.config = json.loads(matches[0].group(1))

        token = self.get_continuation_token(initial_data)
        self.payload = {
            "context": self.config["INNERTUBE_CONTEXT"],
            "continuation": token,
            "webClientInfo": {"isDocumentHidden": False},
        }
        print("Connected (scrape).")

    def get_continuation_token(self, data: Dict[str, Any]) -> str:
        cont = data["continuationContents"]["liveChatContinuation"]["continuations"][0]
        if "timedContinuationData" in cont:
            return cont["timedContinuationData"]["continuation"]
        else:
            return cont["invalidationContinuationData"]["continuation"]

    def fetch_messages(self) -> List[Dict[str, Any]]:
        # scraper fetch (unchanged)
        try:
            payload_bytes = bytes(json.dumps(self.payload), "utf8")
            res = self.session.post(
                f"https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?key={self.config['INNERTUBE_API_KEY']}&prettyPrint=false",
                payload_bytes,
                timeout=10,
            )
        except Exception as e:
            print(f"Failed to fetch messages: {e}")
            return []

        if not res.ok:
            print(f"Failed to fetch messages. {res.status_code} {res.reason}")
            print("Body:", res.text[:500])
            print("Payload:", payload_bytes)
            try:
                self.session.close()
            except Exception:
                pass
            self.session = None
            return []

        try:
            data = json.loads(res.text)
            self.payload["continuation"] = self.get_continuation_token(data)
            cont = data["continuationContents"]["liveChatContinuation"]
            messages = []
            if "actions" in cont:
                for action in cont["actions"]:
                    item = (
                        action.get("addChatItemAction", {})
                        .get("item", {})
                        .get("liveChatTextMessageRenderer")
                    )
                    if item:
                        messages.append(
                            {
                                "author": item["authorName"]["simpleText"],
                                "content": item["message"]["runs"],
                            }
                        )
            return messages
        except Exception:
            print("Failed to parse messages.")
            print("Body (truncated):", res.text[:800])
            traceback.print_exc()
            return []

    # unified API for the template
    def twitch_receive_messages(self) -> List[Dict[str, str]]:
        if self.use_api:
            # API mode with background job + poll pacing
            messages: List[Dict[str, str]] = []

            if not self.fetch_job:
                # only schedule if we are past the advised poll time
                if time.time() >= self.next_fetch_time:
                    self.fetch_job = self.thread_pool.submit(self.api_fetch_messages)
                else:
                    # brief nap to avoid busy-spin
                    time.sleep(1.0 / 120.0)
            else:
                try:
                    res = self.fetch_job.result(timeout=1.0 / 60.0)
                except concurrent.futures.TimeoutError:
                    res = []
                except Exception:
                    traceback.print_exc()
                    try:
                        if self.session:
                            self.session.close()
                    except Exception:
                        pass
                    self.session = None
                    return []
                else:
                    self.fetch_job = None

                for item in res:
                    msg = {"username": item["author"], "message": ""}
                    for part in item["content"]:
                        if "text" in part:
                            msg["message"] += part["text"]
                        elif "emoji" in part:
                            msg["message"] += part["emoji"].get("emojiId", "")
                    messages.append(msg)

            return messages

        # scraper mode
        if self.session is None:
            self.reconnect(0)

        messages: List[Dict[str, str]] = []

        if not self.fetch_job:
            time.sleep(1.0 / 120.0)
            if time.time() > self.next_fetch_time:
                self.fetch_job = self.thread_pool.submit(self.fetch_messages)
        else:
            try:
                res = self.fetch_job.result(timeout=1.0 / 60.0)
            except concurrent.futures.TimeoutError:
                res = []
            except Exception:
                traceback.print_exc()
                try:
                    self.session.close()
                except Exception:
                    pass
                self.session = None
                return []
            else:
                self.fetch_job = None
                self.next_fetch_time = time.time() + YOUTUBE_FETCH_INTERVAL

            for item in res:
                msg = {"username": item["author"], "message": ""}
                for part in item["content"]:
                    if "text" in part:
                        msg["message"] += part["text"]
                    elif "emoji" in part:
                        msg["message"] += part["emoji"].get("emojiId", "")
                messages.append(msg)

        return messages
