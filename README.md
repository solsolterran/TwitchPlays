I wanted to create a game agnostic program that allowed someone to basically only have to worry about creating profiles for individual games and make them interchangeable through different flags. Make the json profile with keys, key combos, or whatever, then you're good to go. No need to touch the actual logic.

Quick Start
- Install deps (Run terminal as Administrator):
  - `pip install -r requirements.txt`
- Sets `.env` and set values you care about.
  - TWITCH_CHANNEL
  - STREAM_SOURCES
  - YOUTUBE_CHANNEL_ID
  - YOUTUBE_STREAM_URL
  - YOUTUBE_API_KEY
- Launch:
  - `python -m stream_handler.TwitchPlays --game gta5`
- Enable/disable injection: press `Alt+Shift+P` (default).
- Kills program immediately: `Ctrl+Shift+Backspace` (default).

Environment (common)
- `TWITCH_CHANNEL`: lowercase channel (e.g., `yourchannel`).
- YouTube (optional): `YOUTUBE_CHANNEL_ID`, `YOUTUBE_STREAM_URL` (for unlisted tests), `YOUTUBE_API_KEY`.
- Voting: fixed window `3s`, cap `200` messages per window, max message length `64`.
- Focus gate: configured via `profiles/<game>.json` (`target_process`, `window_title_contains`).
- Hotkeys: fixed — toggle `Alt+Shift+P`, hard kill `Ctrl+Shift+Backspace`.

Profiles
- Fields (minimal):
  - `target_process`, `window_title_contains`
  - `aliases`: `{ "chat phrase": "canonical_id" }`
  - `macros`: canonical id and list of steps
- Behavior:
  - If a profile exists for `--game <key>`, it is used.
  - If no profile exists, the runner attempts to load a plugin module named like `games.<key>:<Class>`.
- Supported macro steps:
  - `{"type":"key_pulse","key":"W","duration_ms":1000}`
  - `{"type":"key_release","key":"W"}` or `{"type":"key_release","keys":["W","S"]}`
  - `{"type":"key_combo","keys":["W","D"],"duration_ms":2000}`
  - `{"type":"mouse_click","button":"left"}`
  - `{"type":"mouse_move","dx":200,"dy":0}`

Behavior & Safety
- Voting window: 3s by default; per-user dedupe; tie → last-of-top wins.
- Safety: executes only when focused; immediate reset via `release_all()` after each window.
- Exclusive source: `input_mode` is `chat` or `external`; the inactive source is ignored/blocked.
- Global min gap between executions (300 ms) and a circuit breaker disable injection after repeated errors.
- Sources: defaults to reading Twitch and YouTube; YouTube connection is skipped unless configured.

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
    "drive": [ { "type": "key_release", "key": "S" }, { "type": "key_pulse", "key": "W", "duration_ms": 1000 } ],
    "left":  [ { "type": "key_pulse", "key": "A", "duration_ms": 2000 } ],
    "right": [ { "type": "key_pulse", "key": "D", "duration_ms": 2000 } ],
    "brake": [ { "type": "key_pulse", "key": "SPACE", "duration_ms": 700 } ]
  }
}
```

Notes
- Run as Administrator for reliable keyboard/mouse injection.

