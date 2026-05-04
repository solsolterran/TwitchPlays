Anyone is free to use my version of TwitchPlays. Credit of some kind is appreciated but not necessary.
  - https://www.twitch.tv/solarterran
    - Haven't started streaming yet as of May 2026. Still making stuff and preparing.
  - https://x.com/SolSolTerra
  - https://www.youtube.com/@SolSolTerran
  - https://github.com/solsolterran/TwitchPlays

I wanted to update DougDoug's TwitchPlays code to use the modern eventsub Twitch API.
I wanted to make this game agnostic. The core logic is independent of any specific game, so users only need to create JSON profiles that map chat commands to input actions or macros. Profiles can be swapped dynamically using command line flags, allowing different games to run under the same system without changing the underlying code. Once a profile defines the necessary keys, combinations, or sequences, everything is ready to go, with no need to modify the core program.

Quick Start
- Install deps (Run terminal as Administrator if you're using Windows):
  - `pip install -r requirements.txt`
- Change `twitch_config.example.json` to `twitch_config.json` and fill it in:
  - `twitch_channel` - Your Twitch username. The code normalizes it to lowercase.
  - `client_id` - identifies your Twitch app. Get it from https://dev.twitch.tv/console/apps.
  - `client_secret` - optional, but recommended if you want the standard long-lived Twitch refresh flow.
  - `access_token` - lets this app read Twitch chat through EventSub. Get it from the Twitch OAuth token response.
  - `refresh_token` - gets a new `access_token` when the old one expires. It comes from the same Twitch OAuth token response.
  Optional:
  - `youtube_channel_id` - Your YouTube Channel ID, not your handle.
  - `youtube_stream_url` - Direct URL of the live video for unlisted or specific streams.
  - `youtube_api_key` - YouTube Data API v3 key. If absent or over quota, the reader falls back to the old scraping method.
- Launch:
  - `python3 TwitchPlays_Everything.py --game minecraft`
  - Optional: `--sources twitch`, `--sources youtube`, or `--sources twitch,youtube`
- Enable/disable command injections: press `Alt+Shift+P`.
- Kills program immediately: `Ctrl+Shift+Backspace`.

Linux notes
- Linux input injection now falls back to `pyautogui` instead of Windows `SendInput`.
- Linux focus gating is supported on `X11` via `xprop` (`x11-utils` on Debian/Ubuntu).
- Run inside a graphical desktop session. X11 works best.
- Global hotkeys still depend on the `keyboard` package and may require higher permissions.

Profiles
- Fields:
  - `target_process`, `window_title_contains`
  - `aliases`: `{ "chat phrase": "canonical_id" }`
    - Wanted to make things customizable. Single words are easier for people to type in chat but may require more complex naming on the backend. Example is `{ "sniper": "weap_sniper" }` or `{ "sniper": "aim_sniper" }`. 
  - `macros`: canonical id and list of steps
- Behavior:
  - `--game` tells the runner which game profile to load.
    - Example: `--game minecraft` looks for `profiles/minecraft.json`.
  - If that profile does not exist yet, the runner copies `profiles/template.json` and renames the copy to match what you typed.
    - Example: `--game elden_ring` creates `profiles/elden_ring.json` from the template.
  - The template is a starter with basic movement and looking commands. After the runner creates the new copy, edit that new profile for the game you actually want to play.
- Macro examples:
  - `{"type":"key_tap","key":"E"}`
  - `{"type":"key_press","key":"W","duration_ms":1000}`
  - `{"type":"key_hold","key":"W"}`
  - `{"type":"key_release","key":"W"}` or `{"type":"key_release","keys":["W","S"]}`
  - `{"type":"key_combo","keys":["W","D"],"duration_ms":2000}`
  - `{"type":"mouse_down","button":"left"}` / `{"type":"mouse_up","button":"left"}`
  - `{"type":"mouse_hold","button":"left","duration_ms":1500}`
  - `{"type":"mouse_pulse","button":"left","duration_ms":1500}`
  - `{"type":"mouse_click","button":"left"}`
  - `{"type":"mouse_click_at","x":0,"y":0,"button":"left"}`
  - `{"type":"mouse_move","dx":200,"dy":0}`
  - Waiting inside a macro: `{"type":"key_combo","keys":[],"duration_ms":1000}`
- Macros can chain mouse and keyboard steps:
  - `"confirm": [ { "type": "mouse_click_at", "x": -40, "y": 20, "button": "left" }, { "type": "key_combo", "keys": [], "duration_ms": 500 }, { "type": "key_tap", "key": "E" }, { "type": "mouse_move", "dx": 200, "dy": 0 }, { "type": "mouse_click", "button": "left" } ]`
- Use `parallel` when threads should overlap:
  - `"hit_while_walking": [ { "type": "parallel", "threads": [ [ { "type": "key_press", "key": "W", "duration_ms": 1000 } ], [ { "type": "key_combo", "keys": [], "duration_ms": 250 }, { "type": "mouse_click", "button": "left" } ] ] } ]`

Macro types
- `key_tap`: quick press-and-release of a key (default ~60 ms unless you set `duration_ms`).
- `key_press`: like a tap, but held for the specified `duration_ms` before release.
- `key_hold`: holds a key until a later `key_release` or global cleanup.
- `key_release`: releases one or more keys if they’re currently held.
- `key_combo`: holds multiple keys together for `duration_ms`, then releases them.
- `parallel`: runs each thread at the same time and waits for every thread to finish. Each thread is a list of normal macro steps.
  - `parallel` can be placed between normal steps. For example, `key_hold`, then `parallel`, then another `key_hold` works; held keys stay held until a later `key_release` or cleanup.
- `mouse_down` / `mouse_up`: press or release a mouse button (`left`/`right`/`middle`).
- `mouse_hold`: holds a mouse button for `duration_ms`, then releases.
- `mouse_pulse`: alias of `mouse_hold`.
- `mouse_click`: a single mouse click of the given button.
- `mouse_click_at`: move to a normalized `x`,`y` coordinate from `-100` to `100`, where `0,0` is the center of the screen, then click.
- `mouse_move`: move the mouse by `dx`,`dy` relative to its current position.

TwitchPlays Single
- `TwitchPlays_Single.py` is for turn-based or single-action streams where chat votes during a timed window and the runner executes one result at the end.
- Launch examples:
  - `python3 TwitchPlays_Single.py --game single --mode click --time 30s`
  - `python3 TwitchPlays_Single.py --game minecraft --mode command --time 30s`
- Defaults:
  - `--game single`
  - `--mode click`
  - `--time 30s`
- Click mode:
  - Chat votes with coordinates like `0 0`, `50 -25`, or `click -100 100`.
  - Coordinates use a normalized `-100` to `100` field where `0 0` is the center of the screen.
  - The mouse snaps to the center at the start of each round, moves toward chat's weighted target during the vote, then clicks when time expires.
- Command mode:
  - Chat votes for aliases from the selected profile.
  - When time expires, the winning alias runs its macro.
- Single does not dedupe votes by user. Every valid chat message counts, so viewers can spam coordinates or commands to pull the result toward what they want. I thought this would be more entertaining for viewers to see the mouse physically move towards a location.
- Single does not use a focus gate. It clicks or runs macros wherever your mouse and active window are when the vote resolves.

Notes
- If you're using windows then you need to run this as Administrator. If you're on Linux then use `sudo`.
- Twitch chat reads use EventSub over WebSockets and keep the old `twitch_connect(...)` and `twitch_receive_messages()` interface.
- `twitch_config.json` is the one local config file for Twitch auth, Twitch channel, and optional YouTube settings.
- The app validates the saved Twitch token on startup and refreshes it when Twitch returns `401`, so users should not need to re-authorize every 4 hours.
- If `client_secret` is omitted, refresh behavior depends on the kind of Twitch token you originally created. For the least user friction, include `client_secret`.
- Voting: fixed window `3s`, cap `200` messages per window, max message length `64`.
- Focus gate: configured via `profiles/<game>.json` (`target_process`, `window_title_contains`).
- Sources: use `--sources twitch`, `--sources youtube`, or `--sources twitch,youtube`.
- Hotkeys: fixed toggle `Alt+Shift+P`, hard kill `Ctrl+Shift+Backspace`.

Twitch API docs I found while researching:
- Chat auth and EventSub setup: https://dev.twitch.tv/docs/chat/authenticating/
- `channel.chat.message` shape and required condition fields: https://dev.twitch.tv/docs/eventsub/eventsub-subscription-types/
- WebSocket welcome, keepalive, reconnect, and disconnect behavior: https://dev.twitch.tv/docs/eventsub/handling-websocket-events/
- WebSocket message reference: https://dev.twitch.tv/docs/eventsub/websocket-reference/
- Token refresh behavior: https://dev.twitch.tv/docs/authentication/refresh-tokens/
- OAuth flow details for public vs confidential clients: https://dev.twitch.tv/docs/authentication/getting-tokens-oauth


Changes I've made to DougDoug's original code:
1. Game profiles
  - Why:
    - I didn't like the idea of needing to hard code changes directly into the functions themselves. I'd originally thought of game specific functions but realized I ran into the same clutter and monolithic code Doug is famous for. This way games can be swapped easier with just a command line flag, commands/controls are easier to edit, more reliable and customizable, and it just looks nicer in my opinion.
    - I added aliases specifically to allow for more customization. Single words are easier for people to type in chat but may require more complex naming on the backend.
2. Twitch Chat transport
  - Why:
    - Twitch chat now reads through EventSub WebSockets instead of anonymous IRC parsing. `client_id`, `access_token`, and `refresh_token` replaced the old anonymous IRC login.
  - I didn't update youtube chat retrieval cause then I'd have to pay for queries. Still using scraping.
3. Macros
  - Why:
    - I liked the idea of chaining together commands/combos without chat needing to have complex commands they needed to type in chat. Better immersion imo.
  - Parallel command support so something like walking while shooting can be done together. This way mouse and key commands can be used together.
4. Linux compatibility
  - Why:
    - I use Linux. That's it.
    - Also gave me an opportunity to improve the focus gate and Windows OS usage.
5. TwitchPlays Single
  - Why:
    - I wanted a slower runner for turn-based games, browser games, menus, and click-heavy games where chat should vote on one action instead of constantly executing a stream of commands.
    - Click mode lets chat fight over a visible mouse target by spamming coordinates, while command mode reuses the same profile aliases and macros from the main runner for a single instance.
