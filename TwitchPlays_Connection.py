# DougDoug Note:
# This is the code that connects to Twitch / Youtube and checks for new messages.
# You should not need to modify anything in this file, just use as is.

# This code is based on Wituz's "Twitch Plays" tutorial, updated for Python 3.X
# Updated for Youtube by DDarknut, with help by Ottomated

import requests
import sys
import socket
import re
import random
import time
import os
import json
import concurrent.futures
import traceback
from typing import List, Dict, Any, Optional

MAX_TIME_TO_WAIT_FOR_LOGIN = 3
YOUTUBE_FETCH_INTERVAL = 1
TWITCH_HOST = "irc.chat.twitch.tv"
TWITCH_PORT = 6667

class Twitch:
    def __init__(self) -> None:
        self.re_prog: Optional[re.Pattern[bytes]] = None
        self.sock: Optional[socket.socket] = None
        self.partial: bytes = b""
        self.login_ok: bool = False
        self.channel: str = ""
        self.login_timestamp: float = 0.0
        self.reconnect_backoff: float = 1.0

    def twitch_connect(self, channel: str) -> None:
        # Clean up any previous socket
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass

        self.sock = None
        self.partial = b""
        self.login_ok = False
        self.channel = channel
        self.reconnect_backoff = 1.0

        # Compile regular expression for IRC message parsing (RFC-ish framing)
        self.re_prog = re.compile(
            b"^(?::(?:([^ !\r\n]+)![^ \r\n]*|[^ \r\n]*) )?([^ \r\n]+)(?: ([^:\r\n]*))?(?: :([^\r\n]*))?\r\n",
            re.MULTILINE,
        )

        # Create socket
        print("Connecting to Twitch...")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        # Attempt to connect socket
        self.sock.connect((TWITCH_HOST, TWITCH_PORT))

        # Log in read-only. PASS can be anything; NICK must be justinfan####. :contentReference[oaicite:2]{index=2}
        user = "justinfan%i" % random.randint(10000, 99999)
        print("Connected to Twitch. Logging in...")
        self.sock.sendall(("PASS asdf\r\nNICK %s\r\n" % user).encode())

        # small read timeout to keep loop responsive
        self.sock.settimeout(1.0 / 60.0)

        self.login_timestamp = time.time()

    # Attempt to reconnect after a delay
    def reconnect(self, delay):
        time.sleep(delay)
        self.twitch_connect(self.channel)

    # Returns a list of IRC messages received (already parsed)
    def receive_and_parse_data(self) -> List[Dict[str, Any]]:
        if not self.sock:
            return []

        buffer = b""
        while True:
            received = b""
            try:
                received = self.sock.recv(4096)
            except socket.timeout:
                break
            except Exception as e:
                print("Unexpected connection error. Reconnecting in 1s...", e)
                self.reconnect(1)
                return []
            if not received:
                print("Connection closed by Twitch. Reconnecting in 5s...")
                self.reconnect(5)
                return []
            buffer += received

        if not buffer:
            return []

        # Prepend unparsed data from previous iterations
        if self.partial:
            buffer = self.partial + buffer
            self.partial = b""

        res: List[Dict[str, Any]] = []

        # Parse IRC messages using compiled regex
        matches = list(self.re_prog.finditer(buffer)) if self.re_prog else []
        for match in matches:
            res.append(
                {
                    "name": (match.group(1) or b"").decode(errors="replace"),
                    "command": (match.group(2) or b"").decode(errors="replace"),
                    "params": list(
                        map(
                            lambda p: p.decode(errors="replace"),
                            (
                                (match.group(3) or b"").split(b" ")
                                if match.group(3)
                                else []
                            ),
                        )
                    ),
                    "trailing": (match.group(4) or b"").decode(errors="replace"),
                }
            )

        # Save any data that couldn't be parsed for the next iteration
        if not matches:
            self.partial += buffer
        else:
            end = matches[-1].end()
            if end < len(buffer):
                self.partial = buffer[end:]

            if matches[0].start() != 0:
                # Might have missed a message boundary
                print("Warning: possible partial IRC message at start of buffer.")

        return res

    def twitch_receive_messages(self) -> List[Dict[str, str]]:
        privmsgs: List[Dict[str, str]] = []
        for irc_message in self.receive_and_parse_data():
            cmd = irc_message["command"]
            if cmd == "PRIVMSG":
                privmsgs.append(
                    {
                        "username": irc_message["name"],
                        "message": irc_message["trailing"],
                    }
                )
            elif cmd == "PING":
                # Only send PONG in response to PING (per Twitch rules). :contentReference[oaicite:3]{index=3}
                if self.sock:
                    self.sock.send(b"PONG :tmi.twitch.tv\r\n")
            elif cmd == "001":
                print(f"Successfully logged in. Joining channel {self.channel}.")
                if self.sock:
                    self.sock.send(("JOIN #%s\r\n" % self.channel).encode())
                self.login_ok = True
            elif cmd in {
                "JOIN",
                "NOTICE",
                "002",
                "003",
                "004",
                "375",
                "372",
                "376",
                "353",
                "366",
            }:
                pass
            else:
                # Keep quiet unless debugging
                # print("Unhandled irc message:", irc_message)
                pass

        if not self.login_ok:
            # We are still waiting for the initial login message. If we've waited too long, reconnect.
            if time.time() - self.login_timestamp > MAX_TIME_TO_WAIT_FOR_LOGIN:
                print("No response from Twitch. Reconnecting...")
                self.reconnect(0)
                return []

        return privmsgs

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
