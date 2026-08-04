#!/usr/bin/env python3
"""
CLI Traffic Light menu bar app (DEPRECATED)

Superseded by traffic_light_window.py -- rumps-based menu bar icons don't
render on macOS 26 (a system-level bug), so this file is kept for reference
only and isn't wired up in setup.py. See app/README.md.

Same logic as bridge/bridge.py (read state file -> send serial command),
wrapped in a menu bar UI with automatic ESP32 port detection.

Requirements this app does NOT set up for you:
    1. ESP32 already flashed with firmware/traffic_light/traffic_light.ino
    2. Codex or Claude Code CLI already installed and writing its state file
       - Codex: ~/Library/Application Support/CodexTrafficLight/state.json
       - Claude Code: needs the hooks from hooks/README merged into
         ~/.claude/settings.json
"""

import json
import os
import threading
import time

import rumps
import serial
import serial.tools.list_ports

APP_SUPPORT_DIR = os.path.expanduser("~/Library/Application Support/CodexTrafficLight")
CONFIG_PATH = os.path.join(APP_SUPPORT_DIR, "menu-app-config.json")

SOURCE_PATHS = {
    "codex": os.path.expanduser("~/Library/Application Support/CodexTrafficLight/state.json"),
    "claude": os.path.expanduser("~/Library/Application Support/CodexTrafficLight/claude-state.json"),
}

STATE_TO_CODE = {"waiting": "R", "working": "Y", "done": "G", "idle": "O"}
STATE_ICON = {"waiting": "\U0001F534", "working": "\U0001F7E1", "done": "\U0001F7E2", "idle": "⚪"}

# Description keywords for common ESP32 USB-serial chips, used to auto-pick
# the right port instead of asking for one.
ESP32_HINTS = (
    "CP210", "CH340", "CH9102", "USB Serial", "usbserial",
    "wchusbserial", "SLAB_USBtoUART", "FTDI", "UART",
)


def find_esp32_port():
    """Best-effort match against known chip descriptions; falls back to the
    first available port."""
    ports = list(serial.tools.list_ports.comports())
    for p in ports:
        text = f"{p.description or ''} {p.manufacturer or ''}"
        if any(hint.lower() in text.lower() for hint in ESP32_HINTS):
            return p.device
    return ports[0].device if ports else None


def load_config():
    default = {"source": "claude", "muted": False}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                default.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    return default


def save_config(cfg):
    os.makedirs(APP_SUPPORT_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def read_aggregate_state(path):
    if not os.path.exists(path):
        return "idle"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("aggregate_state", "idle")
    except (json.JSONDecodeError, OSError):
        return None


class TrafficLightApp(rumps.App):
    def __init__(self):
        super().__init__("⚪ Traffic Light", quit_button=None)
        self.cfg = load_config()
        self.ser = None
        self.running = False
        self.last_code = None
        self._lock = threading.Lock()

        self.item_toggle = rumps.MenuItem("Start Monitoring", callback=self.toggle_running)
        self.item_source_codex = rumps.MenuItem("Watch Codex", callback=self.set_source_codex)
        self.item_source_claude = rumps.MenuItem("Watch Claude Code", callback=self.set_source_claude)
        self.item_mute = rumps.MenuItem("Mute Buzzer", callback=self.toggle_mute)
        self.item_mute.state = self.cfg["muted"]
        self._refresh_source_check()

        self.menu = [
            self.item_toggle,
            None,
            "Watching",
            self.item_source_codex,
            self.item_source_claude,
            None,
            self.item_mute,
            None,
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]

        self.status_timer = rumps.Timer(self._tick, 1)

    def _refresh_source_check(self):
        self.item_source_codex.state = self.cfg["source"] == "codex"
        self.item_source_claude.state = self.cfg["source"] == "claude"

    def set_source_codex(self, _sender):
        self._switch_source("codex")

    def set_source_claude(self, _sender):
        self._switch_source("claude")

    def _switch_source(self, source):
        self.cfg["source"] = source
        save_config(self.cfg)
        self._refresh_source_check()
        self.last_code = None  # force re-sync on next poll

    def toggle_mute(self, sender):
        self.cfg["muted"] = not self.cfg["muted"]
        sender.state = self.cfg["muted"]
        save_config(self.cfg)
        self._send_raw("M" if self.cfg["muted"] else "U")

    def toggle_running(self, sender):
        if self.running:
            self.stop_monitor()
            sender.title = "Start Monitoring"
        else:
            if self.start_monitor():
                sender.title = "Stop Monitoring"
            else:
                rumps.alert(
                    "Device not found",
                    "Make sure the ESP32 is plugged in via USB, and that nothing "
                    "else (like the Arduino IDE Serial Monitor) is already using "
                    "the same port.",
                )

    def start_monitor(self):
        port = find_esp32_port()
        if not port:
            return False
        try:
            ser = serial.Serial(port, 115200, timeout=1)
            time.sleep(2)  # opening the port resets the ESP32; give it time to boot
        except Exception:
            return False

        with self._lock:
            self.ser = ser
        self._send_raw("M" if self.cfg["muted"] else "U")

        self.running = True
        self.last_code = None
        threading.Thread(target=self._monitor_loop, daemon=True).start()
        self.status_timer.start()
        return True

    def stop_monitor(self):
        self.running = False
        if self.status_timer.is_alive():
            self.status_timer.stop()
        with self._lock:
            if self.ser:
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser = None
        self.title = "⚪ Traffic Light"

    def _send_raw(self, code):
        with self._lock:
            if self.ser:
                try:
                    self.ser.write((code + "\n").encode("utf-8"))
                except Exception:
                    pass

    def _monitor_loop(self):
        while self.running:
            path = SOURCE_PATHS[self.cfg["source"]]
            state = read_aggregate_state(path)
            if state is not None:
                code = STATE_TO_CODE.get(state, "O")
                if code != self.last_code:
                    self._send_raw(code)
                    self.last_code = code
            time.sleep(2)

    def _tick(self, _timer):
        if not self.running:
            return
        path = SOURCE_PATHS[self.cfg["source"]]
        state = read_aggregate_state(path) or "idle"
        self.title = f"{STATE_ICON.get(state, chr(0x26AA))} Traffic Light"

    def quit_app(self, _sender):
        self.stop_monitor()
        rumps.quit_application()


if __name__ == "__main__":
    TrafficLightApp().run()
