Anyone is free to use my version of TwitchPlays and all I ask is I receive credit of some kind.
  - https://www.twitch.tv/solarterran
    - Haven't started streaming yet as of Oct. 2025. Still making stuff and preparing.
  - https://x.com/SolSolTerra
  - https://www.youtube.com/@SolSolTerran
  - https://github.com/solsolterran/TwitchPlays

This project is a game agnostic input automation framework built for Twitch Plays style integrations. The core logic is independent of any specific game, so users only need to create JSON profiles that map chat commands to input actions or macros. Profiles can be swapped dynamically using command line flags, allowing different games to run under the same system without changing the underlying code. Once a profile defines the necessary keys, combinations, or sequences, everything is ready to go, with no need to modify the core logic.

Quick Start
- Install deps (Run terminal as Administrator):
  - `pip install -r requirements.txt`
- Sets `.env` and set values you care about.
  - TWITCH_CHANNEL - Your Twitch username (lowercase).
  - STREAM_SOURCES - Chat sources to read, comma-separated (e.g., twitch,youtube).
  - YOUTUBE_CHANNEL_ID - Your YouTube Channel ID (not your handle).
  - YOUTUBE_STREAM_URL - Optional: direct URL of the live video (e.g., https://youtu.be/VIDEO_ID), useful for unlisted/specific streams.
  - YOUTUBE_API_KEY - Optional: YouTube Data API v3 key; falls back to scraping if absent or over quota.
 - Launch (Windows PowerShell, run as Administrator):
   - `sudo python -m TwitchPlays_Everything --game minecraft`
- Enable/disable injection: press `Alt+Shift+P`.
- Kills program immediately: `Ctrl+Shift+Backspace`.

Environment (common)
- `TWITCH_CHANNEL`: lowercase channel (e.g., `yourchannel`).
- YouTube (optional): `YOUTUBE_CHANNEL_ID`, `YOUTUBE_STREAM_URL`, `YOUTUBE_API_KEY`.
- Voting: fixed window `3s`, cap `200` messages per window, max message length `64`.
- Focus gate: configured via `profiles/<game>.json` (`target_process`, `window_title_contains`).
- Hotkeys: fixed  Etoggle `Alt+Shift+P`, hard kill `Ctrl+Shift+Backspace`.

Profiles
- Fields (minimal):
  - `target_process`, `window_title_contains`
  - `aliases`: `{ "chat phrase": "canonical_id" }`
  - `macros`: canonical id and list of steps
- Behavior:
  - If a profile exists for `--game <key>`, it is used.
  - If no profile exists, the runner attempts to load a plugin module named like `games.<key>:<Class>`.
- Supported macro steps:
  - `{"type":"key_tap","key":"E"}`
  - `{"type":"key_press","key":"W","duration_ms":1000}`
  - `{"type":"key_release","key":"W"}` or `{"type":"key_release","keys":["W","S"]}`
  - `{"type":"key_combo","keys":["W","D"],"duration_ms":2000}`
  - `{"type":"mouse_down","button":"left"}` / `{"type":"mouse_up","button":"left"}`
  - `{"type":"mouse_hold","button":"left","duration_ms":1500}`
  - `{"type":"mouse_click","button":"left"}`
  - `{"type":"mouse_move","dx":200,"dy":0}`
  - Wait inside a macro (no keys): `{"type":"key_combo","keys":[],"duration_ms":1000}`

Macro types
- `key_tap`: quick press-and-release of a key (default ~60 ms unless you set `duration_ms`).
- `key_press`: like a tap, but held for the specified `duration_ms` before release.
- `key_release`: releases one or more keys if they’re currently held.
- `key_combo`: holds multiple keys together for `duration_ms`, then releases them.
- `mouse_down` / `mouse_up`: press or release a mouse button (`left`/`right`/`middle`).
- `mouse_hold`: holds a mouse button for `duration_ms`, then releases.
- `mouse_click`: a single mouse click of the given button.
- `mouse_move`: move the mouse by `dx`,`dy` relative to its current position.
  - Tip: Use an empty `key_combo` with `duration_ms` to sleep within a macro.

Profile JSON example
```
{
  "target_process": "GTA5.exe",
  "window_title_contains": "Grand Theft Auto V",
  "aliases": {
    "drive": "drive",
    "left": "left",
    "right": "right",
    "brake": "brake"
  },
  "macros": {
    "drive": [ { "type": "key_release", "key": "S" }, { "type": "key_press", "key": "W", "duration_ms": 1000 } ],
    "left":  [ { "type": "key_press", "key": "A", "duration_ms": 2000 } ],
    "right": [ { "type": "key_press", "key": "D", "duration_ms": 2000 } ],
    "brake": [ { "type": "key_press", "key": "SPACE", "duration_ms": 700 } ]
  }
}
```

Notes
- Run as Administrator.

