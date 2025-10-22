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
  - `sudo python -m stream_handler.TwitchPlays --game gta5`
- Enable/disable injection: press `Alt+Shift+P`.
- Kills program immediately: `Ctrl+Shift+Backspace`.

Environment (common)
- `TWITCH_CHANNEL`: lowercase channel (e.g., `yourchannel`).
- YouTube (optional): `YOUTUBE_CHANNEL_ID`, `YOUTUBE_STREAM_URL`, `YOUTUBE_API_KEY`.
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
  - `{"type":"key_tap","key":"E"}`
  - `{"type":"key_pulse","key":"W","duration_ms":1000}`
  - `{"type":"key_release","key":"W"}` or `{"type":"key_release","keys":["W","S"]}`
  - `{"type":"key_combo","keys":["W","D"],"duration_ms":2000}`
  - `{"type":"mouse_down","button":"left"}` / `{"type":"mouse_up","button":"left"}`
  - `{"type":"mouse_click","button":"left"}`
  - `{"type":"mouse_move","dx":200,"dy":0}`

Macro types
- `key_tap`: quick press-and-release of a key (default ~60 ms unless you set `duration_ms`).
- `key_pulse`: like a tap, but held for the specified `duration_ms` before release.
- `key_release`: releases one or more keys if they’re currently held.
- `key_combo`: holds multiple keys together for `duration_ms`, then releases them.
- `mouse_down` / `mouse_up`: press or release a mouse button (`left`/`right`/`middle`).
- `mouse_click`: a single mouse click of the given button.
- `mouse_move`: move the mouse by `dx`,`dy` relative to its current position.

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
- Run as Administrator.

