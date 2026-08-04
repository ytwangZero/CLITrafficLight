#!/usr/bin/env python3
"""
CLI Traffic Light bridge script

Reads the state.json maintained by Codex or Claude Code CLI, maps
aggregate_state to a single-character command, and sends it to the ESP32.
Pick one of --port or --host:
    1. USB serial: --port /dev/tty.usbserial-XXXX
    2. WiFi (firmware must already be connected to your router):
       --host cli-light.local   or   --host 192.168.1.23

No hardware yet? Use --dry-run to print commands instead of sending them:
    python3 bridge.py --dry-run --state-path /tmp/fake_state.json

With hardware:
    python3 bridge.py --list-ports                     # find the ESP32's serial port
    python3 bridge.py --port /dev/tty.usbserial-XXXX    # run over serial
    python3 bridge.py --host cli-light.local            # run over WiFi
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_STATE_PATH = os.path.expanduser(
    os.environ.get(
        "CODEX_TRAFFIC_LIGHT_STATE_PATH",
        "~/Library/Application Support/CodexTrafficLight/state.json",
    )
)

# Only one source is watched at a time, selected via --source.
SOURCE_PATHS = {
    "codex": DEFAULT_STATE_PATH,
    "claude": os.path.expanduser(
        os.environ.get(
            "CLAUDE_TRAFFIC_LIGHT_STATE_PATH",
            "~/Library/Application Support/CodexTrafficLight/claude-state.json",
        )
    ),
}

STATE_TO_CODE = {
    "waiting": "R",
    "working": "Y",
    "done": "G",
    "idle": "O",
}


def read_aggregate_state(state_path):
    """Return aggregate_state; "idle" if the file doesn't exist; None (keep
    previous state) if it can't be parsed."""
    if not os.path.exists(state_path):
        return "idle"
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("aggregate_state", "idle")
    except (json.JSONDecodeError, OSError) as e:
        print(f"[warn] failed to read state file: {e}", file=sys.stderr)
        return None


def list_ports():
    import serial.tools.list_ports
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("No serial devices found. Make sure the ESP32 is connected via a data-capable USB cable.")
        return
    for p in ports:
        print(f"{p.device}  -  {p.description}")


def open_serial(port, baud):
    import serial
    ser = serial.Serial(port, baud, timeout=1)
    time.sleep(2)  # opening the port resets the ESP32; give it time to boot
    return ser


def send_http(host, code, timeout=3):
    """Send one command over WiFi. Returns success; failures are only logged,
    not fatal, since the next poll will retry."""
    url = f"http://{host}/cmd?c={urllib.parse.quote(code)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            pass
        return True
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        print(f"[warn] WiFi send failed ({host}): {e}", file=sys.stderr)
        return False


def run(args):
    ser = None
    send = None  # unified sender, signature send(code: str) -> None

    if not args.dry_run:
        if args.port and args.host:
            print("--port and --host are mutually exclusive.", file=sys.stderr)
            sys.exit(1)
        if args.port:
            ser = open_serial(args.port, args.baud)
            print(f"[info] connected to {args.port} @ {args.baud}")
            send = lambda code: ser.write((code + "\n").encode("utf-8"))
        elif args.host:
            print(f"[info] sending commands over WiFi to {args.host}")
            send = lambda code: send_http(args.host, code)
        else:
            print(
                "Pass --port for the ESP32's serial device (see --list-ports), "
                "--host for its WiFi address (e.g. cli-light.local), "
                "or --dry-run to try the logic without hardware.",
                file=sys.stderr,
            )
            sys.exit(1)

        if args.buzzer is not None:
            code = "M" if args.buzzer == "off" else "U"
            send(code)
            print(f"[info] sent buzzer {'mute' if code == 'M' else 'unmute'} command, the ESP32 will remember it")

    mode = "dry-run (no hardware)" if args.dry_run else ("serial" if args.port else "WiFi")
    print(f"[info] watching state file: {args.state_path}")
    print(f"[info] poll interval: {args.interval}s  mode: {mode}")

    last_code = None
    try:
        while True:
            state = read_aggregate_state(args.state_path)
            if state is not None:
                code = STATE_TO_CODE.get(state, "O")
                if code != last_code:
                    ts = time.strftime("%H:%M:%S")
                    print(f"[{ts}] state changed: {state} -> sending '{code}'")
                    if send:
                        send(code)
                    last_code = code
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[info] stopped.")
    finally:
        if ser:
            ser.close()


def main():
    parser = argparse.ArgumentParser(description="CLI Traffic Light hardware bridge")
    parser.add_argument(
        "--source",
        choices=sorted(SOURCE_PATHS.keys()),
        default=None,
        help="which tool's state to watch (codex/claude); defaults to Codex's path. Overridden by --state-path.",
    )
    parser.add_argument("--state-path", default=None, help="explicit state.json path, takes priority over --source")
    parser.add_argument("--port", default=None, help="ESP32 serial device, e.g. /dev/tty.usbserial-XXXX")
    parser.add_argument(
        "--host", default=None,
        help="ESP32 WiFi address (mDNS or IP, e.g. cli-light.local or 192.168.1.23), "
             "mutually exclusive with --port; the firmware must already be connected to your router",
    )
    parser.add_argument(
        "--buzzer",
        choices=["on", "off"],
        default=None,
        help="also set the buzzer mute state on connect (on=unmuted, off=muted); leave unset to keep the ESP32's last setting",
    )
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--interval", type=float, default=2.0, help="poll interval in seconds")
    parser.add_argument("--dry-run", action="store_true", help="don't connect to hardware, just print commands")
    parser.add_argument("--list-ports", action="store_true", help="list available serial ports and exit")
    parser.add_argument("--once", action="store_true", help="check once and exit (for testing)")
    args = parser.parse_args()

    if args.list_ports:
        list_ports()
        return

    if args.state_path:
        args.state_path = os.path.expanduser(args.state_path)
    elif args.source:
        args.state_path = SOURCE_PATHS[args.source]
    else:
        args.state_path = DEFAULT_STATE_PATH

    run(args)


if __name__ == "__main__":
    main()
