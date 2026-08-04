# CLI Traffic Light

A physical desktop traffic light that mirrors what your AI coding CLI (Codex or Claude Code) is doing, in real time:

- 🔴 **red** — it's waiting on you (a permission prompt is up)
- 🟡 **yellow** — it's working
- 🟢 **green** — it just finished a turn
- ⚪ **off** — idle

Drive it over USB or WiFi, from the command line or a small desktop app.

## Repository layout

```
firmware/traffic_light/traffic_light.ino   ESP32 firmware
wokwi/diagram.json                         Wokwi simulation circuit (test without hardware)
bridge/bridge.py                           CLI bridge: reads state.json, sends commands over serial/WiFi
bridge/requirements.txt                    Python deps for bridge.py
hooks/claude_light_hook.py                 Claude Code hook script that writes claude-state.json
hooks/claude-settings-hooks-snippet.json   Hooks config to merge into ~/.claude/settings.json
app/traffic_light_window.py                Desktop GUI app (no command line needed)
app/setup.py                               py2app packaging config for the GUI app
```

## Hardware

| Item | Notes | Qty |
| --- | --- | --- |
| ESP32 dev board | CP2102, Type-C, 38-pin | 1 |
| 3-color LED traffic light module | red/yellow/green | 1 |
| Female-female jumper wires | ~20cm | 1 set |
| Buzzer module (optional) | active, 3-pin (with driver circuit) | 1 |
| Type-C USB cable | data-capable, not charge-only | 1 |

### Wiring

```
R (red)   -> GPIO25
Y (yellow)-> GPIO26
G (green) -> GPIO27
GND       -> GND
VCC       -> 3V3 (use 5V/VIN if the LEDs are too dim)
```

Buzzer (optional):

```
I/O (signal) -> GPIO14
VCC          -> 5V or 3V3 (per module spec)
GND          -> GND
```

Any GND pin on the board works — they're all tied together internally.

### Testing without hardware (Wokwi)

1. Open [wokwi.com](https://wokwi.com), create a new "ESP32" project.
2. Replace its `diagram.json` with `wokwi/diagram.json`.
3. Paste `firmware/traffic_light/traffic_light.ino` into `sketch.ino`.
4. Click Run, then type `R`, `Y`, `G`, `O` + Enter in the Serial Monitor and watch the LEDs.

## Firmware

Flash `firmware/traffic_light/traffic_light.ino` with the Arduino IDE (board: "ESP32 Dev Module"). It needs the [WiFiManager](https://github.com/tzapu/WiFiManager) library (Library Manager → search "WiFiManager" by tzapu).

### Commands

The firmware accepts single-character commands over USB serial (115200 baud) and WiFi at the same time:

| Command | Meaning |
| --- | --- |
| `R` | waiting — red blinks, buzzer beeps for 5s then goes quiet, but blinking continues until the next command |
| `Y` | working — yellow solid |
| `G` | done — green solid, two beeps |
| `O` | idle — all off |
| `M` / `U` | mute / unmute buzzer (persisted across reboots) |
| `W` | forget saved WiFi and restart into setup mode |

### WiFi setup

WiFi credentials aren't hardcoded — first boot (or after sending `W`) opens a temporary access point, **`CLITrafficLight-Setup`**, with a captive portal for entering your WiFi. Connect to it from a phone or laptop, pick your network (2.4GHz — ESP32 doesn't do 5GHz), enter the password, save. The device remembers it and reconnects automatically from then on.

Once connected it's reachable at `http://cli-light.local/cmd?c=R` (or the IP printed on the Serial Monitor). If no one completes setup within 3 minutes, the firmware gives up and USB serial still works as normal.

Before handing the device to someone else, send `W` to wipe the saved WiFi so they get a fresh setup screen.

## Driving the light

Either of these reads `aggregate_state` from a state file and turns it into a command. Only one needs to run.

### Option A — bridge.py (command line)

```bash
cd bridge
pip3 install -r requirements.txt --break-system-packages
python3 bridge.py --list-ports                              # find the ESP32's serial port
python3 bridge.py --port /dev/tty.usbserial-XXXX --source codex
python3 bridge.py --host cli-light.local --source claude     # or over WiFi
python3 bridge.py --dry-run --state-path /tmp/fake_state.json --once  # test without hardware
```

`--port` and `--host` are mutually exclusive. Add `--buzzer on/off` to set the mute state on connect.

### Option B — desktop app

`app/traffic_light_window.py` is a no-terminal GUI: pick USB or WiFi, pick Codex or Claude Code, click Start Monitoring. See [app/README.md](app/README.md) for building and distributing it as a standalone `.app`.

## Claude Code integration

`hooks/claude_light_hook.py` writes a separate state file from Codex's (they don't share one):

```
Codex   -> ~/Library/Application Support/CodexTrafficLight/state.json        (override: CODEX_TRAFFIC_LIGHT_STATE_PATH)
Claude  -> ~/Library/Application Support/CodexTrafficLight/claude-state.json (override: CLAUDE_TRAFFIC_LIGHT_STATE_PATH)
```

Event mapping: `UserPromptSubmit`/`PreToolUse`/`PostToolUse` → working, `PermissionRequest` → waiting (a real permission dialog), `Stop`/`SubagentStop` → done, `SessionEnd` → light off. Stale entries (session ended abnormally) expire automatically so the light never gets stuck.

**Easiest path:** use the desktop app's "Configure Hooks" button — it merges the required config into `~/.claude/settings.json` automatically, without touching anything else already there.

**Manual path:**

1. Merge the `hooks` object from `hooks/claude-settings-hooks-snippet.json` into `~/.claude/settings.json` (merge each event's array into any existing one — don't overwrite the whole file).
2. Fix the `command` paths to match where you put this repo.
3. Run `/hooks` inside Claude Code to confirm it loaded.
4. Have a normal conversation and watch `~/Library/Application Support/CodexTrafficLight/claude-state.json` update.

## Codex integration

This repo has no Codex-side hook script — Codex's state file is expected to be maintained by a separate, external tool (referred to here as `codex-traffic-light-mxp`) that isn't part of this project. If you have it, point `bridge.py --source codex` (or the app's "Watching: Codex" option) at it. Otherwise this half of the integration is up to you to build.

Neither integration talks to the desktop chat apps (Claude.ai desktop, ChatGPT desktop, etc.) — only their CLI tools expose the hook/event mechanism this relies on.

## Known limitations

- CLI tools only, not desktop chat apps.
- No code signing/notarization on the packaged app — first launch needs a right-click → Open on each new machine.
- Codex integration depends on external tooling not included here.
