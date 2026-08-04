#!/usr/bin/env python3
"""
CLI Traffic Light -- desktop window app

The original menu-bar icon version (traffic_light_menu.py, built on rumps) hit a
system-level bug on macOS 26 where third-party menu bar icons don't render, so
this is a plain window app instead. No dependency on NSStatusItem, more portable.

Requirements this app does NOT set up for you:
    1. ESP32 already flashed with firmware/traffic_light/traffic_light.ino
    2. Codex CLI or Claude Code CLI itself installed (the "Configure Hooks"
       buttons wire up the hook config automatically, but won't install the
       CLI tools)

Closing the window stops monitoring and quits the app.
"""

import json
import os
import shutil
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from tkinter import font as tkfont
from tkinter import messagebox, ttk

import serial
import serial.tools.list_ports

APP_TITLE = "CLI Traffic Light"

APP_SUPPORT_DIR = os.path.expanduser("~/Library/Application Support/CodexTrafficLight")
CONFIG_PATH = os.path.join(APP_SUPPORT_DIR, "window-app-config.json")

SOURCE_PATHS = {
    "codex": os.path.expanduser("~/Library/Application Support/CodexTrafficLight/state.json"),
    "claude": os.path.expanduser("~/Library/Application Support/CodexTrafficLight/claude-state.json"),
}

STATE_TO_CODE = {"waiting": "R", "working": "Y", "done": "G", "idle": "O"}
STATE_LABEL = {
    "waiting": "Waiting for you",
    "working": "Working",
    "done": "Done",
    "idle": "Idle",
}
STATE_COLOR = {
    "waiting": "#ff5c5c",
    "working": "#ffc233",
    "done": "#34c759",
    "idle": "#c7c7cc",
}

BG = "#f2f2f5"
CARD_BG = "#ffffff"
TEXT_PRIMARY = "#1d1d1f"
TEXT_SECONDARY = "#86868b"
ACCENT = "#0071e3"
ACCENT_HOVER = "#0077ed"
DANGER = "#ff3b30"
DANGER_HOVER = "#ff5147"
BORDER = "#e3e3e8"

ESP32_HINTS = (
    "CP210", "CH340", "CH9102", "USB Serial", "usbserial",
    "wchusbserial", "SLAB_USBtoUART", "FTDI", "UART",
)


def find_esp32_port():
    ports = list(serial.tools.list_ports.comports())
    for p in ports:
        text = f"{p.description or ''} {p.manufacturer or ''}"
        if any(hint.lower() in text.lower() for hint in ESP32_HINTS):
            return p.device
    return ports[0].device if ports else None


DEFAULT_WIFI_HOST = "cli-light.local"


def load_config():
    default = {"source": "claude", "muted": False, "conn_mode": "usb", "wifi_host": DEFAULT_WIFI_HOST}
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


# ---- Hook auto-setup (Claude Code + Codex) ----
#
# Both tools use the same {"hooks": {EventName: [{matcher?, hooks: [{type,
# command}]}]}} shape, just in different files. Claude Code passes the event
# name as argv[1]; Codex only ever sends it via the stdin JSON payload.
HOOK_INTEGRATIONS = {
    "claude": {
        "label": "Claude Code",
        "script_name": "claude_light_hook.py",
        "settings_path": os.path.expanduser("~/.claude/settings.json"),
        "command_includes_event": True,
        "cli_name": "claude",
        "cli_hint": "claude.ai/code",
        "events": {
            "UserPromptSubmit": None,
            "PreToolUse": "*",
            "PostToolUse": "*",
            "PermissionRequest": "*",
            "Stop": None,
            "SubagentStop": None,
            "SessionEnd": None,
        },
    },
    "codex": {
        "label": "Codex",
        "script_name": "codex_light_hook.py",
        "settings_path": os.path.expanduser("~/.codex/hooks.json"),
        "command_includes_event": False,
        "cli_name": "codex",
        "cli_hint": "developers.openai.com/codex",
        "events": {
            "UserPromptSubmit": None,
            "PreToolUse": "*",
            "PostToolUse": "*",
            "PermissionRequest": "*",
            "Stop": None,
            "SubagentStop": None,
            "SessionEnd": None,
        },
    },
}


def get_hook_script_path(script_name):
    """Find a hook script, whether running as a plain script (hooks/ is a
    sibling of app/) or bundled into a py2app .app (hooks/ is copied into
    Contents/Resources alongside this script)."""
    candidates = []
    resource_path = os.environ.get("RESOURCEPATH")
    if resource_path:
        candidates.append(os.path.join(resource_path, "hooks", script_name))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "hooks", script_name))
    candidates.append(os.path.abspath(os.path.join(here, "..", "hooks", script_name)))
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _hook_command(hook_path, event, include_event):
    if include_event:
        return f'python3 "{hook_path}" {event}'
    return f'python3 "{hook_path}"'


def _command_present(bucket, command):
    return any(
        isinstance(group, dict)
        and any(
            isinstance(h, dict) and h.get("command", "").strip() == command
            for h in group.get("hooks", [])
            if isinstance(group.get("hooks", []), list)
        )
        for group in bucket
    )


def hooks_configured(integration, hook_path):
    """True only if every event this integration cares about already has our
    exact command somewhere in its hook list."""
    settings_path = integration["settings_path"]
    if not hook_path or not os.path.exists(settings_path):
        return False
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(settings, dict):
        return False
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return False
    for event in integration["events"]:
        command = _hook_command(hook_path, event, integration["command_includes_event"])
        bucket = hooks.get(event, [])
        if not isinstance(bucket, list) or not _command_present(bucket, command):
            return False
    return True


def configure_hooks(integration, hook_path):
    """Merge our hook commands into the integration's settings file without
    touching anything else already configured there. Returns the list of
    event names newly added (empty if already up to date)."""
    settings_path = integration["settings_path"]
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)

    settings = {}
    if os.path.exists(settings_path):
        with open(settings_path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if raw:
            try:
                settings = json.loads(raw)
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"{settings_path} isn't valid JSON ({e}). Fix or back it "
                    "up by hand first, then try again."
                )
        if not isinstance(settings, dict):
            raise RuntimeError(f"{settings_path} doesn't contain a JSON object at the top level.")

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise RuntimeError(f'The "hooks" key in {settings_path} isn\'t a JSON object.')

    added = []
    for event, matcher in integration["events"].items():
        command = _hook_command(hook_path, event, integration["command_includes_event"])
        bucket = hooks.setdefault(event, [])
        if not isinstance(bucket, list):
            continue  # unexpected shape written by something else; leave it alone
        if _command_present(bucket, command):
            continue
        entry = {"hooks": [{"type": "command", "command": command}]}
        if matcher is not None:
            entry["matcher"] = matcher
        bucket.append(entry)
        added.append(event)

    tmp_path = settings_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, settings_path)
    return added


class RoundedButton(tk.Canvas):
    """A flat, rounded, custom-colored button.

    macOS's native Aqua theme ignores the 'bg'/'fg' options on a plain
    tk.Button, so a colored accent button just renders as a stock gray
    system button. Drawing it on a Canvas instead gives full control over
    color and shape.
    """

    def __init__(self, parent, text, command, bg_color, hover_color=None,
                 fg_color="white", font=None, width=260, height=42, radius=10):
        super().__init__(parent, width=width, height=height, bg=parent["bg"],
                          highlightthickness=0, cursor="pointinghand")
        self.command = command
        self.bg_color = bg_color
        self.hover_color = hover_color or bg_color
        self.fg_color = fg_color
        self.font = font
        self.radius = radius
        self._btn_width = width
        self._btn_height = height
        self._text = text
        self._draw(self.bg_color)
        self.bind("<Button-1>", lambda _e: self.command())
        self.bind("<Enter>", lambda _e: self._draw(self.hover_color))
        self.bind("<Leave>", lambda _e: self._draw(self.bg_color))

    def _points(self, x1, y1, x2, y2, r):
        return [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]

    def _draw(self, color):
        self.delete("all")
        w, h = self._btn_width, self._btn_height
        self.create_polygon(self._points(1, 1, w - 1, h - 1, self.radius),
                             smooth=True, fill=color, outline=color)
        self.create_text(w / 2, h / 2, text=self._text, fill=self.fg_color, font=self.font)

    def set_text(self, text):
        self._text = text
        self._draw(self.bg_color)

    def set_colors(self, bg_color, hover_color=None):
        self.bg_color = bg_color
        self.hover_color = hover_color or bg_color
        self._draw(self.bg_color)


class TrafficLightWindow:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.ser = None
        self.running = False
        self.last_code = None
        self._lock = threading.Lock()

        self.hook_script_paths = {}
        self.hooks_status_labels = {}
        self.configure_hooks_btns = {}

        root.title(APP_TITLE)
        root.configure(bg=BG)
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._setup_fonts()
        self._setup_style()

        outer = tk.Frame(root, bg=BG, padx=20, pady=20)
        outer.pack(fill="both", expand=True)

        tk.Label(outer, text=APP_TITLE, font=self.font_app_title, fg=TEXT_PRIMARY,
                 bg=BG).pack(anchor="w", pady=(0, 12))

        card = tk.Frame(outer, bg=CARD_BG, highlightbackground=BORDER,
                         highlightthickness=1, bd=0)
        card.pack(fill="both", expand=True)

        # ---- Status row ----
        status_row = tk.Frame(card, bg=CARD_BG, padx=22, pady=22)
        status_row.pack(fill="x")

        self.canvas = tk.Canvas(status_row, width=16, height=16, bg=CARD_BG,
                                 highlightthickness=0)
        self.dot = self.canvas.create_oval(1, 1, 15, 15, fill=STATE_COLOR["idle"], outline="")
        self.canvas.pack(side="left", pady=(3, 0))

        text_col = tk.Frame(status_row, bg=CARD_BG)
        text_col.pack(side="left", padx=(12, 0))
        self.status_label = tk.Label(text_col, text="Not connected", font=self.font_title,
                                      fg=TEXT_PRIMARY, bg=CARD_BG, anchor="w")
        self.status_label.pack(anchor="w")
        self.sub_label = tk.Label(text_col, text="Click Start Monitoring to connect",
                                   font=self.font_small, fg=TEXT_SECONDARY, bg=CARD_BG, anchor="w")
        self.sub_label.pack(anchor="w", pady=(2, 0))

        self._divider(card)

        # ---- Toggle button ----
        toggle_row = tk.Frame(card, bg=CARD_BG, padx=22, pady=18)
        toggle_row.pack(fill="x")
        self.toggle_btn = RoundedButton(
            toggle_row, text="Start Monitoring", command=self.toggle_running,
            bg_color=ACCENT, hover_color=ACCENT_HOVER, font=self.font_button,
            width=276, height=42,
        )
        self.toggle_btn.pack(fill="x")

        self._divider(card)

        # ---- Connection ----
        conn_row = tk.Frame(card, bg=CARD_BG, padx=22, pady=16)
        conn_row.pack(fill="x")
        tk.Label(conn_row, text="CONNECTION", font=self.font_label,
                 fg=TEXT_SECONDARY, bg=CARD_BG).pack(anchor="w", pady=(0, 8))

        self.conn_var = tk.StringVar(value=self.cfg["conn_mode"])
        conn_radio_frame = tk.Frame(conn_row, bg=CARD_BG)
        conn_radio_frame.pack(fill="x")
        ttk.Radiobutton(conn_radio_frame, text="USB", value="usb", variable=self.conn_var,
                         command=self.on_conn_mode_change, style="Light.TRadiobutton").pack(
            side="left", padx=(0, 24))
        ttk.Radiobutton(conn_radio_frame, text="WiFi", value="wifi", variable=self.conn_var,
                         command=self.on_conn_mode_change, style="Light.TRadiobutton").pack(side="left")

        self.wifi_host_var = tk.StringVar(value=self.cfg["wifi_host"])
        self.wifi_host_entry = tk.Entry(conn_row, textvariable=self.wifi_host_var,
                                         font=self.font_small, fg=TEXT_PRIMARY,
                                         relief="flat", bd=6,
                                         highlightbackground=BORDER, highlightcolor=ACCENT,
                                         highlightthickness=1)
        self.wifi_host_entry.pack(fill="x", pady=(10, 0), ipady=2)
        self.wifi_host_entry.bind("<FocusOut>", self.on_wifi_host_change)
        self.wifi_host_entry.bind("<Return>", self.on_wifi_host_change)
        self._update_conn_widgets()

        self._divider(card)

        # ---- Source ----
        source_row = tk.Frame(card, bg=CARD_BG, padx=22, pady=16)
        source_row.pack(fill="x")
        tk.Label(source_row, text="WATCHING", font=self.font_label,
                 fg=TEXT_SECONDARY, bg=CARD_BG).pack(anchor="w", pady=(0, 8))

        self.source_var = tk.StringVar(value=self.cfg["source"])
        radio_frame = tk.Frame(source_row, bg=CARD_BG)
        radio_frame.pack(fill="x")
        ttk.Radiobutton(radio_frame, text="Codex", value="codex", variable=self.source_var,
                         command=self.on_source_change, style="Light.TRadiobutton").pack(
            side="left", padx=(0, 24))
        ttk.Radiobutton(radio_frame, text="Claude Code", value="claude", variable=self.source_var,
                         command=self.on_source_change, style="Light.TRadiobutton").pack(side="left")

        self._divider(card)

        # ---- Hooks setup, one section per integration ----
        self._build_hook_setup_section(card, "codex")
        self._divider(card)
        self._build_hook_setup_section(card, "claude")
        self._divider(card)

        # ---- Mute ----
        mute_row = tk.Frame(card, bg=CARD_BG, padx=22, pady=16)
        mute_row.pack(fill="x")
        self.mute_var = tk.BooleanVar(value=self.cfg["muted"])
        ttk.Checkbutton(mute_row, text="Mute buzzer", variable=self.mute_var,
                         command=self.on_mute_change, style="Light.TCheckbutton").pack(anchor="w")

        tk.Label(outer, text="Closing this window stops monitoring and quits.",
                 font=self.font_small, fg=TEXT_SECONDARY, bg=BG).pack(pady=(14, 0))

        self._poll()

    # ---- UI helpers ----

    def _setup_fonts(self):
        family = "SF Pro Text" if "SF Pro Text" in tkfont.families() else "Helvetica"
        self.font_app_title = tkfont.Font(family=family, size=13, weight="bold")
        self.font_title = tkfont.Font(family=family, size=15, weight="bold")
        self.font_button = tkfont.Font(family=family, size=13, weight="bold")
        self.font_label = tkfont.Font(family=family, size=10, weight="bold")
        self.font_small = tkfont.Font(family=family, size=11)

    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("aqua")
        except tk.TclError:
            pass
        style.configure("Light.TRadiobutton", background=CARD_BG, foreground=TEXT_PRIMARY,
                         font=self.font_small)
        style.configure("Light.TCheckbutton", background=CARD_BG, foreground=TEXT_PRIMARY,
                         font=self.font_small)

    def _divider(self, parent):
        tk.Frame(parent, bg=BORDER, height=1).pack(fill="x")

    def _build_hook_setup_section(self, card, key):
        integration = HOOK_INTEGRATIONS[key]
        hook_path = get_hook_script_path(integration["script_name"])
        self.hook_script_paths[key] = hook_path

        row = tk.Frame(card, bg=CARD_BG, padx=22, pady=16)
        row.pack(fill="x")
        tk.Label(row, text=f"{integration['label'].upper()} SETUP", font=self.font_label,
                 fg=TEXT_SECONDARY, bg=CARD_BG).pack(anchor="w", pady=(0, 8))
        status_label = tk.Label(row, text="Checking...", font=self.font_small,
                                 fg=TEXT_SECONDARY, bg=CARD_BG, anchor="w", justify="left",
                                 wraplength=232)
        status_label.pack(anchor="w", pady=(0, 8))
        self.hooks_status_labels[key] = status_label

        btn = RoundedButton(
            row, text="Configure Hooks", command=lambda k=key: self.on_configure_hooks_click(k),
            bg_color=TEXT_PRIMARY, hover_color="#3a3a3c", font=self.font_small,
            width=276, height=34, radius=8,
        )
        btn.pack(fill="x")
        self.configure_hooks_btns[key] = btn
        self._refresh_hooks_status(key)

    # ---- Event callbacks ----

    def on_source_change(self):
        self.cfg["source"] = self.source_var.get()
        save_config(self.cfg)
        self.last_code = None  # force re-sync on next poll

    def on_conn_mode_change(self):
        self.cfg["conn_mode"] = self.conn_var.get()
        save_config(self.cfg)
        self._update_conn_widgets()

    def on_wifi_host_change(self, _event=None):
        self.cfg["wifi_host"] = self.wifi_host_var.get().strip()
        save_config(self.cfg)

    def _update_conn_widgets(self):
        state = "normal" if self.conn_var.get() == "wifi" else "disabled"
        self.wifi_host_entry.config(state=state)

    def on_mute_change(self):
        self.cfg["muted"] = self.mute_var.get()
        save_config(self.cfg)
        code = "M" if self.cfg["muted"] else "U"
        threading.Thread(target=self._send_raw, args=(code,), daemon=True).start()

    def _refresh_hooks_status(self, key):
        integration = HOOK_INTEGRATIONS[key]
        hook_path = self.hook_script_paths.get(key)
        status_label = self.hooks_status_labels[key]
        btn = self.configure_hooks_btns[key]

        if not hook_path:
            status_label.config(
                text=f"Couldn't find {integration['script_name']} bundled with this app.",
                fg=DANGER,
            )
            btn.pack_forget()
            return
        if hooks_configured(integration, hook_path):
            status_label.config(text="Hooks configured ✓", fg="#2f9e56")
            btn.set_text("Reconfigure Hooks")
        else:
            installed = shutil.which(integration["cli_name"]) is not None
            note = "" if installed else f" (install {integration['label']} CLI first: {integration['cli_hint']})"
            status_label.config(
                text=f"One-time setup so {integration['label']} can drive this light.{note}",
                fg=TEXT_SECONDARY,
            )
            btn.set_text("Configure Hooks")

    def on_configure_hooks_click(self, key):
        integration = HOOK_INTEGRATIONS[key]
        hook_path = self.hook_script_paths.get(key)
        if not hook_path:
            messagebox.showerror(
                "Hook script missing",
                f"Couldn't find {integration['script_name']} bundled with this app.",
            )
            return
        try:
            added = configure_hooks(integration, hook_path)
        except RuntimeError as e:
            messagebox.showerror(f"Couldn't update {integration['settings_path']}", str(e))
            return
        self._refresh_hooks_status(key)
        if added:
            messagebox.showinfo(
                f"{integration['label']} configured",
                "Added hooks for: " + ", ".join(added) + ".\n\n"
                f"Restart {integration['label']} (or run /hooks to check) for it to take effect.",
            )
        else:
            messagebox.showinfo("Already configured", "Every hook was already set up -- nothing to change.")

    def toggle_running(self):
        if self.running:
            self._send_raw("O")  # tell the device to go dark before we stop watching it
            self.stop_monitor()
            self.toggle_btn.set_text("Start Monitoring")
            self.toggle_btn.set_colors(ACCENT, ACCENT_HOVER)
            self.status_label.config(text="Not connected")
            self.sub_label.config(text="Click Start Monitoring to connect")
            self.canvas.itemconfig(self.dot, fill=STATE_COLOR["idle"])
        else:
            if self.start_monitor():
                self.toggle_btn.set_text("Stop Monitoring")
                self.toggle_btn.set_colors(DANGER, DANGER_HOVER)
                self.sub_label.config(text=f"Watching {self.cfg['source']}")
            elif self.cfg["conn_mode"] == "wifi":
                messagebox.showerror(
                    "Device not reachable",
                    f"Couldn't reach the traffic light at "
                    f"\"{self.wifi_host_var.get().strip()}\". Make sure the ESP32 is "
                    "powered on, connected to the same WiFi network, and that the "
                    "address is correct (check the Serial Monitor after flashing "
                    "for the exact address).",
                )
            else:
                messagebox.showerror(
                    "Device not found",
                    "Couldn't find the traffic light device. Make sure the ESP32 "
                    "is plugged in via USB, and that nothing else (like the "
                    "Arduino IDE Serial Monitor) is already using the port.",
                )

    def on_close(self):
        if self.running:
            self._send_raw("O")
        self.stop_monitor()
        self.root.destroy()

    # ---- Serial + WiFi + monitoring ----

    def start_monitor(self):
        if self.cfg["conn_mode"] == "wifi":
            return self._start_monitor_wifi()
        return self._start_monitor_usb()

    def _start_monitor_usb(self):
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
        return True

    def _start_monitor_wifi(self):
        host = self.wifi_host_var.get().strip()
        if not host:
            return False
        self.cfg["wifi_host"] = host
        save_config(self.cfg)

        if not self._wifi_reachable(host):
            return False

        self._send_raw("M" if self.cfg["muted"] else "U")

        self.running = True
        self.last_code = None
        threading.Thread(target=self._monitor_loop, daemon=True).start()
        return True

    def _wifi_reachable(self, host, timeout=3):
        try:
            urllib.request.urlopen(f"http://{host}/", timeout=timeout)
            return True
        except urllib.error.HTTPError:
            return True  # got a real HTTP response (even an error page) -> device is there
        except Exception:
            return False

    def stop_monitor(self):
        self.running = False
        with self._lock:
            if self.ser:
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser = None

    def _send_raw(self, code):
        if self.cfg["conn_mode"] == "wifi":
            host = self.cfg.get("wifi_host", "").strip()
            if not host:
                return
            try:
                url = f"http://{host}/cmd?c={urllib.parse.quote(code)}"
                with urllib.request.urlopen(url, timeout=3):
                    pass
            except Exception:
                pass
        else:
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
            time.sleep(0.4)

    # ---- UI refresh ----

    def _poll(self):
        if self.running:
            path = SOURCE_PATHS[self.cfg["source"]]
            state = read_aggregate_state(path) or "idle"
            self.canvas.itemconfig(self.dot, fill=STATE_COLOR.get(state, STATE_COLOR["idle"]))
            self.status_label.config(text=STATE_LABEL.get(state, "Idle"))
            self.sub_label.config(text=f"Watching {self.cfg['source']}")
        self.root.after(400, self._poll)


def _force_repaint(root):
    """Hide and re-show the window. On macOS, packaged Tk apps sometimes come
    up with an unpainted (blank) window until it loses and regains front-window
    status -- exactly what switching to another app and back does manually.
    This reproduces that cycle automatically on startup."""
    try:
        root.withdraw()
        root.after(60, root.deiconify)
        root.after(60, root.lift)
        root.after(60, root.focus_force)
    except tk.TclError:
        pass


def main():
    root = tk.Tk()
    TrafficLightWindow(root)

    root.update_idletasks()
    root.lift()
    root.attributes("-topmost", True)
    root.after(30, lambda: root.attributes("-topmost", False))
    root.after(120, lambda: _force_repaint(root))

    root.mainloop()


if __name__ == "__main__":
    main()
