# CLI Traffic Light — desktop app

A no-terminal GUI for people who don't want to run `bridge.py` by hand. One small window: start/stop monitoring, USB or WiFi, watch Codex or Claude Code, one-click Claude Code hook setup, mute buzzer. USB mode auto-detects the ESP32's serial port.

## Requirements this app does NOT set up for you

1. The ESP32 already flashed with `firmware/traffic_light/traffic_light.ino` and wired up (see the main [README](../README.md); WiFi mode also needs the device already paired to a network).
2. Codex CLI or Claude Code CLI (the actual command-line tool, not a desktop chat app) installed.

For Claude Code, you no longer need to hand-edit JSON: `hooks/claude_light_hook.py` ships inside the app bundle (`Contents/Resources/hooks/`), and the app's **Configure Hooks** button merges the required entries into `~/.claude/settings.json` automatically, without touching anything else already in there. You still need to install Claude Code CLI yourself — the app won't do that for you.

Codex has no equivalent automation here — this repo doesn't include a Codex-side hook script. Codex's state file is expected to come from a separate external tool (`codex-traffic-light-mxp`) not included in this project; if you don't have it, the Codex option won't do anything.

## Building the standalone .app (one-time, on your machine)

Must run on macOS (py2app only builds Mac apps), with a Python that has a modern Tk (8.6) — not the system Python (its Tk 8.5 renders a blank/broken window on recent macOS):

```bash
brew install python-tk@3.12   # if not already installed
cd app
/opt/homebrew/bin/python3.12 -m pip install -r requirements.txt --break-system-packages
/opt/homebrew/bin/python3.12 setup.py py2app
```

Output is `app/dist/CLITrafficLight.app`. Ad-hoc sign it (no paid developer account needed — this also avoids some rendering glitches):

```bash
codesign --force --deep -s - "dist/CLITrafficLight.app"
```

## Giving it to someone else

1. Zip `CLITrafficLight.app` (Finder → right-click → Compress) and send it, along with the ESP32 device.
2. They unzip it, drag it into Applications, double-click.
3. **First launch gets blocked by Gatekeeper** ("can't be opened because the developer cannot be verified") since it isn't signed with a paid Apple developer certificate. Fix: right-click the app → Open → confirm Open. Only needed once; after that it opens normally.
4. In the app:
   - **Start/Stop Monitoring** — begin/stop reading the state file and driving the light.
   - **Connection** — USB (auto-detects the port) or WiFi (address field, defaults to `cli-light.local`).
   - **Watching** — Codex or Claude Code.
   - **Claude Code Setup** — install Claude Code CLI first (claude.ai/code), then click **Configure Hooks**.
   - **Mute buzzer** — buzzer off, light unaffected.
   - Closing the window stops monitoring (turns the light off first) and quits.

## Known limitations

- CLI tools only — no desktop chat app support (they don't expose the hook/event mechanism this relies on).
- No auto-start on login; add it under System Settings → General → Login Items if you want that.
- Unsigned build, so every new machine needs the right-click-Open step once.

## Deprecated: menu bar version

`traffic_light_menu.py` (a rumps-based menu bar icon) hit a macOS 26 system bug where third-party menu bar icons don't render. It's kept for reference but not wired into `setup.py` — use `traffic_light_window.py`.
