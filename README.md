# LeageStatusLight

Connect League of Legends to your Hubspace lights: 100% red while dead,
20% brightness at 2700 K while alive or outside a game. Built with
[hubspace-ng](https://github.com/shawngmc/hubspace-ng), with standalone light
commands included.

**[Download the Windows package](https://github.com/nmandorf/LeageStatusLight/releases/latest).**
Choose `hubspace-lights-windows.zip` under Assets, extract it, then run
`setup_windows.cmd`. Sign into GitHub to access this private repository.

## League of Legends lighting

For your Windows gaming PC, extract the Windows ZIP, run **setup_windows.cmd**
once, then run **start_league.cmd** before playing. Setup requires Python 3.10
or newer (tested with 3.12), installs the dependencies, and asks for your
Hubspace email and hidden password. Get Python from the
[official Windows downloads page](https://www.python.org/downloads/windows/)
if it is not installed. Use **status_league.cmd** to check it and
**stop_league.cmd** to stop and restore warm white.

The League watcher runs on the **same computer as the game** and reads your
player's `isDead` flag through Riot's
[Live Client Data API](https://developer.riotgames.com/docs/lol#game-client-api).
No Riot password or developer API key is required.

| Situation | Lights |
| --- | --- |
| Alive, waiting for a game, or outside a game | On, 20% brightness, 2700 K warm white |
| Your champion is dead | On, 100% brightness, red |
| Respawn | Return to 20%, 2700 K warm white |
| Stop the watcher normally | Attempt to restore the normal warm-white scene |

In the project's activated terminal environment:

```sh
light league start
light league status
light league stop
```

`start` runs in the background, so you can close that terminal and play.
Start it again after rebooting the computer. In Windows PowerShell use
`.\light.cmd league start` from this folder. You can also select the **League
lights** run configuration in PyCharm, or use `light league run` to keep the
watcher in the foreground and stop with Ctrl+C. In PyCharm, select this
project's virtual-environment interpreter first (`.venv\Scripts\python.exe`
on Windows). Use `stop_league.cmd` for graceful shutdown; a forced process
termination may leave the last scene on. Only one watcher can run in
this project at a time. Use `stop` before starting another copy or testing
different settings.

Edit **league_settings.py** to change the standard brightness/temperature,
death brightness/color, or polling interval, then stop and start the watcher.
The default checks game state twice per second. It sends light commands on
scene changes and retries failed bulbs after a delay, avoiding constant cloud
writes. A brief missing game response retains the current scene; after three
seconds without valid game data it returns to standard lighting. Starting
while already dead immediately selects the death scene.

Both bulbs need working Wi-Fi for operation with the phone app closed. A
failed bulb is reported separately and retried, while the other can continue.
Status reports cloud acceptance, which does not prove physical delivery to an
offline bulb. Hubspace cloud latency still adds to the local polling delay.

To inspect game detection without accessing Hubspace or changing lights:

```sh
light league run --dry-run
light league run --dry-run --once
```

League exposes live game data during a match, including Practice Tool; the
watcher waits during the launcher/lobby or when no game is available. A live
death/respawn check requires a match on this computer. Spectator sessions with
no identifiable local player use standard lighting. Logs and status are saved
under the ignored `.cache/league/` folder.

On another computer, copy the source files (not `.venv` or `.cache`), create a
Python environment, install `requirements.txt`, configure the Hubspace email,
and run the login command there. The saved password stays in that computer's
credential store. On Windows, the direct equivalent is:

```powershell
.venv\Scripts\python.exe main.py login
.venv\Scripts\python.exe light_cli.py league start
.venv\Scripts\python.exe light_cli.py league status
.venv\Scripts\python.exe light_cli.py league stop
```

## Quick terminal commands

The `light` command is installed in this project's virtual environment. In
PyCharm's Terminal, or after running `source .venv/bin/activate` from this
folder, use:

```sh
light 100 red
light 40
light 50 blue --light "Left bed"
light off
light 0
light list
light colors
```

`light 100 red` sets all lights to red at 100% brightness and turns them on.
`light 40` sets 40% brightness and turns them on, keeping their color.
`light 0` and `light off` turn them off. Use `--light` to target one light;
otherwise all supported account lights are included. Color accepts names,
quoted hex values such as `'#FF6600'`, or RGB such as `255,100,30`.

The shortcut reuses your session and checks all lights before any changes.
It sends color mode, RGB, brightness, and power together in one request per
light. If a cloud request fails partway through, earlier changes may have
taken effect.

Run `light --help` for usage. If a terminal says `command not found`, activate
the environment as above. To install the shortcut after recreating the
environment, run `.venv/bin/python install_shortcut.py` from this folder.
The launcher runs your source files directly, so code edits take effect
immediately without reinstalling. Re-run the installer if you move the project.

## Faster commands

Repeat commands reuse a login token stored in macOS Keychain and light
definitions cached for five minutes. With both caches available, a command
goes straight to the light update requests. Tokens refresh automatically;
the first command after expiry can take longer. Internet and Hubspace cloud
latency still apply, and lights receive separate requests in sequence.

`light list` always fetches current state and refreshes the device list. Run it
after adding or renaming lights if you want those changes picked up immediately
by commands targeting all lights. A missing named target triggers a fresh
discovery automatically. Cached definitions are never used as current state.

Login tokens use credential-store service `hubspace-lights-session`;
passwords use `hubspace-lights` (macOS Keychain or Windows Credential Manager).
The ignored `.cache/devices.json`
contains device information only. Optional cache failures fall back to normal
sign-in/discovery. `light login` always verifies the entered password.

## Where to edit

| File | Responsibility |
| --- | --- |
| `light_strength.py` | Brightness parsing, limits, and power rules; reusable individual setters. |
| `light_color.py` | Editable `COLOR_PRESETS`, RGB validation, mode selection, white temperatures, and individual setters. |
| `main.py` | Login, light selection, command-line arguments, and running commands for one or all lights. |
| `light_cli.py` | The short `light 100 red` syntax and shortcut names. |
| `light_transport.py` | `build_updates()` combines settings; `apply_updates()` sends one request per light. |
| `hubspace_connection.py` | Fast discovery and automatic session refresh. |
| `session_cache.py` | Token storage in Keychain and the five-minute device cache. |
| `league_settings.py` | Editable alive/dead lighting settings and timing. |
| `league_client.py` | Read your current life state from League's local API. |
| `league_watcher.py` | Death/respawn transitions and per-light cloud updates. |
| `league_service.py` | Start, stop, status, and protection against duplicate watchers. |
| `install_shortcut.py` | Installs the `light` launcher into this project's `.venv/bin`. |
| `errors.py` | Shared user-facing error type. |

The control files receive a light object and the desired value. They do not
contain passwords or login code. The CLI uses their validators and color
helpers to prepare combined updates for every light before sending any changes.
Edit presets and validation in those files; edit `build_updates()` to change
which settings a terminal command sends. Their individual async setters remain
available when writing your own Python scripts.

## Run in PyCharm

Open this folder as a project. Select `.venv/bin/python` on macOS or
`.venv\Scripts\python.exe` on Windows as its interpreter (tested with Python 3.12).
Run `main.py` with no arguments to list lights and their current state.
The supplied **List Hubspace lights** run configuration does the same thing.

Your email is in the local, ignored `config.json`. A successful login saves the
password in the system credential store under service `hubspace-lights`, outside
this project (macOS Keychain or Windows Credential Manager).
To enter or replace the saved password, run the **Hubspace login** configuration
or use the terminal command below. Password entry is hidden.

## Terminal commands

From the project directory:

```sh
.venv/bin/python main.py login
.venv/bin/python main.py list
.venv/bin/python main.py on "Left bed"
.venv/bin/python main.py off "Left bed"
.venv/bin/python main.py brightness "Left bed" 50
.venv/bin/python main.py color "Left bed" blue
.venv/bin/python main.py color "Right bed" '#FF6600'
.venv/bin/python main.py color "Left bed" 255,100,30
.venv/bin/python main.py temperature "Left bed" 2700
```

Use an exact name from the list, or supply the device ID.
Listing and logging in do not change the lights. The other commands target one
light unless you use `--all`. Brightness and white temperature are validated
against each device's supported range.

To control all lights in your account with one command:

```sh
.venv/bin/python main.py on --all
.venv/bin/python main.py off --all
.venv/bin/python main.py brightness --all 50
.venv/bin/python main.py color --all blue
.venv/bin/python main.py temperature --all 2700
```

This currently includes `Left bed` and `Right bed`. Any supported lights added
to the account later will also be included. The program checks every light's
capabilities before making changes, then sends commands one after the other.
They may change slightly apart; this is not an exactly synchronized update.
If a cloud request fails partway through, earlier commands may already have
taken effect. Each successfully sent command is printed in the output.

## Editing colors

Run `.venv/bin/python main.py colors` to see the named presets without logging
in. To add one, edit `COLOR_PRESETS` near the top of `light_color.py`, for example:

```python
"relax": (255, 100, 30),
```

Then use `.venv/bin/python main.py color --all relax`. Each RGB channel is a
whole number from 0 to 255. Quote hexadecimal values so `#` is not interpreted
as a shell comment.

`Left bed` and `Right bed` both advertise RGB support and white temperatures of
**2200–6500 K in steps of 100 K**. Lower temperatures look warmer; higher ones
look cooler. Use `temperature` for white-light settings. Color commands select
the device's `color` mode; temperature commands select its `white` mode. They
do not send brightness or power commands. If a light is off, turn it on to see
the setting. Terminal commands send mode and value together in one cloud request.
The individual Python setters send them separately.

For another machine, use Python 3.10 or newer, create a virtual environment,
install `requirements.txt`, copy `config.example.json` to `config.json`, and run
the login command. `HUBSPACE_USERNAME` and `HUBSPACE_PASSWORD` environment
variables are also supported for a single session.

## Limitations

An internet connection and lights registered to your Hubspace account are
required. Upstream hubspace-ng 0.0.4 is an older unofficial library; login or
device compatibility may require updates. The commands here cover power and
brightness, RGB colors, and white temperatures. This project's color module
also reads the device's advertised temperature function when the older
library does not expose it directly. Unsupported features are reported as errors.

Authentication errors are summarized without printing sensitive server
responses. Do not enable upstream debug logging: it can expose tokens.

## Offline checks

Run `.venv/bin/python -m unittest -v`. These tests use fake devices/cloud
responses and never change your real lights.
