Anyone is free to use my version of TwitchPlays. Credit of some kind is appreciated but not necessary.
  - https://www.twitch.tv/solsolterran
    - Haven't started streaming yet as of May 2026. Still making stuff and preparing.
  - https://x.com/SolSolTerran
  - https://www.youtube.com/@SolSolTerran
  - https://github.com/solsolterran/

I wanted to update DougDoug's TwitchPlays code to use Twitch's EventSub API and make it work with different games without editing Python every time. Each game gets a JSON profile with its chat commands, keys and macros. Pick the profile when you start the script.

## Quick Start

- Run these commands from the TwitchPlays folder with Python 3.9 or newer. I use `python3` in the examples; on Windows, you may need `python` instead.
- Install dependencies: `python3 -m pip install -r requirements.txt`.
- You need a Windows or Linux X11 desktop. Everything uses global hotkeys to enable and stop input, so those have to work before it will start. Single just uses Ctrl+C in the terminal.
- Everything checks the window against the target in your profile. Leave the target empty and it can send input to any window. Single doesn't check focus at all, so it can move the mouse, click or type into whatever is on screen.
- On Windows, if the game is running as administrator, the terminal needs to be running as administrator too. Otherwise, a normal terminal is fine.
- Copy `twitch_config.example.json` to `twitch_config.json` and fill it in. Keep your existing config if you already have one. Git ignores this file because it contains credentials. Don't share it.
  - `twitch_channel` - Your Twitch username. The code normalizes it to lowercase.
  - `client_id` - Your Twitch app's ID. Get it from https://dev.twitch.tv/console/apps.
  - `client_secret` - Your Twitch app's secret. Optional, but whether you can refresh without it depends on how you got your tokens.
  - `access_token` - lets this app read Twitch chat through EventSub. Get it from the Twitch OAuth token response.
  - `refresh_token` - gets a new `access_token` when the old one expires. It comes from the same Twitch OAuth token response.

  For YouTube:

  - `youtube_channel_id` - Your YouTube Channel ID, not your handle.
  - `youtube_stream_url` - Direct URL of the live video for unlisted or specific streams.
  - `youtube_api_key` - YouTube Data API v3 key. If absent or over quota, the reader falls back to the old scraping method.
- The commands below connect to live chat and can control your game. YouTube API requests use quota. Load the game first and check the profile's target window. Stop input before taking over yourself. If you use a new `--game` name, the script creates a starter profile in `profiles/`.
- Launch:
  - `python3 TwitchPlays_Everything.py --game minecraft`
  - Optional: `--sources twitch`, `--sources youtube`, or `--sources twitch,youtube`
- Turn chat control on or off: `Alt+Shift+P`. Turning it off cancels the current macro and clears the votes. Turning it back on starts a new vote; it won't pick up where the cancelled macro left off.
- Stop and exit: `Ctrl+Shift+Backspace` or Ctrl+C in the terminal. This releases held keys and mouse buttons. The program may take a little longer to exit if a chat request is still waiting on the network.

## Linux notes

- Keyboard and mouse input uses `pyautogui` on X11. Native Wayland isn't supported.
- Everything uses `xprop` to check the focused window. On Debian/Ubuntu, install it with `sudo apt install x11-utils`.
- The `keyboard` package needs root to read Linux input devices for Everything's global hotkeys. This means running Everything as root too. From your X11 desktop terminal, use `sudo -E "$(command -v python3)" TwitchPlays_Everything.py --game supertux`. This keeps the desktop environment variables and uses the Python you've selected, which needs the dependencies installed.
- Single uses Ctrl+C in its terminal and normally runs as your desktop user: `python3 TwitchPlays_Single.py --mode click --time 30s`.
- If Everything can't check a configured target, it won't send input. If the terminal says it skipped a command because of focus, check `DISPLAY`, `xprop`, and the game's actual window title/process. An empty target allows input to any foreground window.

## Profiles

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
- Use `parallel` to do things at the same time, like walking while clicking:
  - `"hit_while_walking": [ { "type": "parallel", "threads": [ [ { "type": "key_press", "key": "W", "duration_ms": 1000 } ], [ { "type": "key_combo", "keys": [], "duration_ms": 250 }, { "type": "mouse_click", "button": "left" } ] ] } ]`

## Macro types

- `key_tap`: quick press-and-release of a key (default ~60 ms unless you set `duration_ms`).
- `key_press`: like a tap, but held for the specified `duration_ms` before release.
- `key_hold`: holds a key until a later `key_release` or the macro finishes or stops.
- `key_release`: releases one or more keys if they’re currently held.
- `key_combo`: holds multiple keys together for `duration_ms`, then releases them.
- `parallel`: runs each thread at the same time and waits for every thread to finish. Each thread is a list of normal macro steps.
  - You can put `parallel` between other steps in a macro. If two threads hold the same key or button, one finishing won't release it while the other still needs it. `key_hold` and `mouse_down` stay held until you release them or the macro finishes or stops. Holding the same key twice doesn't mean you have to release it twice. An explicit release also won't cut short another thread's timed hold.
- `mouse_down` / `mouse_up`: press or release a mouse button (`left`/`right`/`middle`).
- `mouse_hold`: holds a mouse button for `duration_ms`, then releases.
- `mouse_pulse`: alias of `mouse_hold`.
- `mouse_click`: a single mouse click of the given button.
- `mouse_click_at`: move to an `x`,`y` coordinate from `-100` to `100`, where `0,0` is the center of the screen, then click.
- `mouse_move`: move the mouse by `dx`,`dy` relative to its current position.

Profiles get checked before the script connects to chat or sends input. If something is wrong, the error points to the profile, macro and step where it can. Check for misspelled keys, aliases pointing to missing or empty macros, broken `parallel` lists, invalid mouse buttons, or numbers like `NaN` and infinity.

`duration_ms` is capped at 3,000 ms; anything higher gets cut down to that. Click coordinates go from -100 to 100 across the whole screen. An empty `key_combo` just waits for its duration. Use `LEFT`, `RIGHT`, `UP`, `DOWN` for arrows. Numpad keys are separate from the arrows and the numbers along the top of the keyboard.

A completed macro means the script finished sending its inputs. It doesn't read the game or know whether those inputs did what chat wanted.

## TwitchPlays Single

- `TwitchPlays_Single.py` gives chat time to vote, then does one thing when the timer runs out. It's meant for slower or turn-based games.
- Single doesn't check which window is focused. It clicks or runs macros wherever your mouse and active window are when the vote ends.
- Launch examples:
  - `python3 TwitchPlays_Single.py --game single --mode click --time 30s`
  - `python3 TwitchPlays_Single.py --game minecraft --mode command --time 30s`
- Defaults:
  - `--game single`
  - `--mode click`
  - `--time 30s`
- Click mode:
  - Chat votes with coordinates like `0 0`, `50 -25`, or `click -100 100`.
  - Coordinates go from `-100` to `100` across the screen, with `0 0` in the center.
  - The mouse snaps to the center at the start of each round, moves toward chat's weighted target during the vote, then clicks when time expires.
- Command mode:
  - Chat votes for aliases from the selected profile.
  - When time expires, the winning alias runs its macro.
- Single counts repeated votes from the same person. Every valid chat message counts, so viewers can spam coordinates or commands to pull the result toward what they want. I thought this would be more entertaining for viewers to see the mouse physically move towards a location.

## Notes

- Twitch chat uses EventSub over WebSockets. The old `twitch_connect(...)` and `twitch_receive_messages()` calls still work.
- Twitch tokens get checked on startup and every hour, refreshed shortly before they expire, and refreshed if Twitch returns `401`. You shouldn't need to log in again every four hours.
- Everything counts up to 200 messages per three-second vote, keeping each viewer's latest choice. Single counts up to 5,000 messages per vote, including repeats. Both ignore messages over 64 characters and messages the service sends again with the same ID. If commands tie, the most recent vote wins. For YouTube messages that arrive together, their order in the batch breaks the tie.
- Viewers are identified by platform and account ID. If the YouTube scraper only gives a display name, that's all there is to go on. Two people with the same name on that platform can end up counted as one person.
- A new vote starts after the previous macro has finished and released its inputs. Messages received while a macro is running, while control is off, or as old history when chat connects are thrown out. Message timestamps also let the script ignore anything sent before the round. Without timestamps, it drops results from requests started before the round, but it can't tell exactly how old a delayed message is.
- Chat queues have size limits so they can't keep growing if messages arrive faster than the script reads them. The terminal tells you when messages are dropped, commands finish or stop, or input is skipped because the wrong window is focused.
- If one chat source stops working, the other can keep voting. If no sources are connected, the current vote is dropped and Single stops moving the mouse. Voting starts fresh when a source comes back. If every source reports a permanent error, the script exits. Fix the reported login or ended-chat problem and restart. You can also select just one platform with `--sources twitch` or `--sources youtube`.
- YouTube tells the API reader how long to wait between requests. Failed requests wait before trying again; running out of quota switches to scraping. Permission errors stop that source, while rate limits and temporary connection errors get retried. The first batch after connecting is treated as old history. The scraper depends on YouTube's page format and can break when that changes.
- If a key or mouse button can't be released, the terminal names it and the script stops sending input. Stop the script, check the desktop permissions and make sure the key or button is released before restarting.

## Offline testing

You can check the code without connecting to chat. No `twitch_config.json`, Twitch account or YouTube API key needed. Installing the dependencies needs a connection; the checks themselves don't. From the TwitchPlays folder:

```bash
cd ~/projects/TwitchPlays
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
python3 -m py_compile TwitchPlays_Everything.py TwitchPlays_Single.py TwitchPlays_Connection.py TwitchPlays_KeyCodes.py TwitchPlays_TEMPLATE.py focus_gate.py
python3 TwitchPlays_Everything.py --help
python3 TwitchPlays_Single.py --help
```

The tests use fake input and network calls, so they won't move your mouse, press keys or touch your credentials. They should finish with `OK`. Both help commands should print their options without connecting to chat or creating a profile.

To try actual game input, use a Windows or Linux X11 desktop with the dependencies installed. Open a game and get into a playable level. The included `supertux` profile looks for `SuperTux` in the window title and uses the arrows and spacebar. For the mouse examples, pick a local game or menu that uses the mouse.

The examples below send real input. They can move your character or click through menus, so keep unrelated windows out of the way and stop the script before taking over yourself.

To find a target on Linux, install `x11-utils` if needed, run `xprop _NET_WM_PID _NET_WM_NAME WM_NAME`, and click the game window. Use the displayed PID with `ps -p PID -o comm=` to find its process name. On Windows, use PowerShell: `Get-Process | Where-Object MainWindowTitle | Select-Object ProcessName, MainWindowTitle`. Use the shown title and the executable name, including `.exe` for a Windows `target_process`. Set those fields in your profile. They are optional, but leaving both empty permits input to any focused window.

Open an interactive Python prompt from the TwitchPlays folder. On Linux, Everything's hotkeys need `sudo -E "$(command -v python3)"`; on Windows, use `python3` (or `python`). Paste this to load the profile and set up the usual toggle and stop hotkeys without connecting to chat:

```python
from pathlib import Path
import time
import TwitchPlays_Everything as runner

game = runner.select_profile_game(Path("profiles/supertux.json"))
runner.set_focus_target(game.profile.get("target_process"), game.profile.get("window_title_contains"))
runner.register_hotkeys()
steps = game.canonical_to_macros[game.profile["aliases"]["forward"]]
```

This sets `steps` to the profile's forward action. To try a different macro type, pick an entry from the examples below before running the next block. Change the keys or buttons to ones your game uses. You're choosing the input here yourself; there's no chat vote:

```python
examples = {
    "key_press": [{"type": "key_press", "key": "RIGHT", "duration_ms": 1000}],
    "key_tap": [{"type": "key_tap", "key": "SPACE"}],
    "key_hold_release": [{"type": "key_hold", "key": "RIGHT"}, {"type": "key_combo", "keys": [], "duration_ms": 1000}, {"type": "key_release", "key": "RIGHT"}],
    "key_combo": [{"type": "key_combo", "keys": ["RIGHT", "SPACE"], "duration_ms": 500}],
    "wait": [{"type": "key_combo", "keys": [], "duration_ms": 1000}],
    "parallel": [{"type": "parallel", "threads": [[{"type": "key_press", "key": "RIGHT", "duration_ms": 3000}], [{"type": "key_press", "key": "SPACE", "duration_ms": 3000}]]}],
    "mouse_down_up": [{"type": "mouse_down", "button": "left"}, {"type": "key_combo", "keys": [], "duration_ms": 500}, {"type": "mouse_up", "button": "left"}],
    "mouse_hold": [{"type": "mouse_hold", "button": "left", "duration_ms": 500}],
    "mouse_pulse": [{"type": "mouse_pulse", "button": "left", "duration_ms": 500}],
    "mouse_click": [{"type": "mouse_click", "button": "left"}],
    "mouse_click_at": [{"type": "mouse_click_at", "x": 0, "y": 0, "button": "left"}],
    "mouse_move": [{"type": "mouse_move", "dx": 30, "dy": 0}],
}
steps = examples["parallel"]
```

Paste this block, switch to the game, then press Alt+Shift+P. The action should run once and release its keys/buttons. Press Alt+Shift+P during a hold or parallel action to cancel it. Switching away from the configured game window should cancel it too. Ctrl+Shift+Backspace stops the script. Once cancelled, it shouldn't send any more presses. The `finally` block releases input and removes the hotkeys from this Python session:

```python
try:
    while not runner.injection_enabled and not runner.stop_event.is_set():
        time.sleep(0.05)
    if not runner.stop_event.is_set():
        runner.run_macro(steps)
except runner.MacroCancelled as error:
    print(error)
finally:
    runner.request_stop()
    runner.release_all()
    runner.unregister_hotkeys()

```

Exit Python with `quit()` and start it again for another check. If nothing happens, check the target window settings and the terminal error before changing the macro. You can take over once the keys/buttons are released. If a release failed, sort that out before continuing.

For Single, open a fresh Python prompt with `python3` from `~/projects/TwitchPlays`, as your normal desktop user. Single doesn't check focus, so make sure the right window is in front. This example runs a three-second click vote with three local messages instead of live chat. They pull the target one-third of the way from the screen center to the right edge. You have five seconds to switch to the game before the mouse starts moving, then it clicks when the vote ends. Ctrl+C in the terminal cancels the round and releases input:

```python
import time
from types import SimpleNamespace
import TwitchPlays_Single as runner

batches = iter([[{"username": "operator", "message": text} for text in ("0 0", "0 0", "100 0")]])
local_chat = SimpleNamespace(available=True, receive_messages=lambda: next(batches, []))
try:
    time.sleep(5)
    runner.run_click_round(local_chat, 3.0)
finally:
    runner.request_stop()
    runner.release_all()

```

Exit with `quit()`. To try Single's command mode, open a fresh Python prompt with `python3` in the same folder and paste this. You have five seconds to switch to SuperTux:

```python
from pathlib import Path
import time
import TwitchPlays_Single as runner

game = runner.select_profile_game(Path("profiles/supertux.json"))
try:
    time.sleep(5)
    game.commands["forward"]("operator")
finally:
    runner.request_stop()
    runner.release_all()

```

The character should move right and stop. Ctrl+C should cancel early. Single ignores the profile's focus fields, so it will still send input if another window is in front.

These checks cover local input and releasing keys/buttons. Live Twitch and YouTube chat still need a separate try, and each game needs checking to see whether it accepts the inputs. `TwitchPlays_TEMPLATE.py` is the original standalone example. It doesn't have Everything and Single's cancellation controls.

## Twitch API docs I found while researching

- Chat auth and EventSub setup: https://dev.twitch.tv/docs/chat/authenticating/
- `channel.chat.message` shape and required condition fields: https://dev.twitch.tv/docs/eventsub/eventsub-subscription-types/
- WebSocket welcome, keepalive, reconnect, and disconnect behavior: https://dev.twitch.tv/docs/eventsub/handling-websocket-events/
- WebSocket message reference: https://dev.twitch.tv/docs/eventsub/websocket-reference/
- Token refresh behavior: https://dev.twitch.tv/docs/authentication/refresh-tokens/
- OAuth flow details for public vs confidential clients: https://dev.twitch.tv/docs/authentication/getting-tokens-oauth


## Changes I've made to DougDoug's original code

1. Game profiles
  - Why:
    - I didn't like the idea of needing to hard code changes directly into the functions themselves. I'd originally thought of game specific functions but realized I ran into the same clutter and monolithic code Doug is famous for. This way games can be swapped easier with just a command line flag, commands/controls are easier to edit, more reliable and customizable, and it just looks nicer in my opinion.
    - I added aliases specifically to allow for more customization. Single words are easier for people to type in chat but may require more complex naming on the backend.
2. Twitch chat
  - Why:
    - Switched from anonymous IRC to EventSub WebSockets. It needs `client_id`, `access_token`, and `refresh_token` for the Twitch login.
  - YouTube uses the Data API if you give it a key, or scrapes the page if there's no key or the quota runs out. Scraping can break when YouTube changes the page.
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
    - I wanted something slower for turn-based games, browser games, menus, and games with a lot of clicking. Chat gets time to vote before the command or final click runs.
    - In click mode, chat can fight over where the mouse goes by spamming coordinates. Command mode uses the same profile aliases and macros as Everything, but only runs the winning command once per vote.
