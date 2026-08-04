#!/usr/bin/env python3
"""
Codex CLI Traffic Light Hook

Called by Codex CLI's hooks mechanism (see ~/.codex/hooks.json); translates
hook events into the state stored in state.json.

Writes to the same state file Codex has always used, overridable via
CODEX_TRAFFIC_LIGHT_STATE_PATH. bridge.py / the desktop app pick this up
automatically with --source codex.

Unlike Claude Code, Codex only ever sends the event as JSON on stdin (no
argv), so hook_event_name always comes from the payload.

Event mapping:
    UserPromptSubmit / PreToolUse / PostToolUse -> working
    PermissionRequest                           -> waiting (a real approval prompt)
    Stop / SubagentStop                         -> done
    SessionEnd                                  -> drop this session's entry (light off)
    anything else                               -> ignored

PostToolUse is mapped to working because Codex doesn't fire a separate
"approved" event after PermissionRequest -- without it the light would stay
stuck on waiting/red until the whole turn ends.

Codex requires Stop/SubagentStop hooks to print valid JSON on stdout when
they exit 0, so this always prints "{}" regardless of event.

Concurrent sessions are tracked in the tasks dict; aggregate_state is derived
by priority: waiting > working > done (within 10 min) > idle.

Any exception here is swallowed and the script exits 0 so it can never affect
Codex itself.
"""

import json
import os
import sys
import time

STATE_PATH = os.path.expanduser(
    os.environ.get(
        "CODEX_TRAFFIC_LIGHT_STATE_PATH",
        "~/Library/Application Support/CodexTrafficLight/state.json",
    )
)

DONE_EXPIRY_SECONDS = 10 * 60
# A working/waiting entry this old is assumed to be from a session that ended
# abnormally (terminal killed, machine slept) rather than via a proper event,
# so it's excluded from the aggregate instead of leaving the light stuck.
STALE_EXPIRY_SECONDS = 20 * 60
# Entries older than this are dropped entirely to keep the state file tidy.
PRUNE_AFTER_SECONDS = 24 * 60 * 60


def read_stdin_json():
    try:
        if sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        return json.loads(raw)
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def load_state(path):
    if not os.path.exists(path):
        return {"tasks": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("tasks", {})
    return data


def save_state(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def compute_aggregate(tasks):
    now = time.time()
    has_waiting = False
    has_working = False
    has_recent_done = False
    for info in tasks.values():
        if not isinstance(info, dict):
            continue
        state = info.get("state")
        updated_at = info.get("updated_at", 0)
        age = now - updated_at
        if state == "waiting" and age <= STALE_EXPIRY_SECONDS:
            has_waiting = True
        elif state == "working" and age <= STALE_EXPIRY_SECONDS:
            has_working = True
        elif state == "done" and age <= DONE_EXPIRY_SECONDS:
            has_recent_done = True
    if has_waiting:
        return "waiting"
    if has_working:
        return "working"
    if has_recent_done:
        return "done"
    return "idle"


def prune_stale_tasks(tasks):
    now = time.time()
    return {
        key: info
        for key, info in tasks.items()
        if isinstance(info, dict) and (now - info.get("updated_at", 0)) <= PRUNE_AFTER_SECONDS
    }


def determine_state(event, payload):
    if event in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
        return "working"
    if event == "PermissionRequest":
        return "waiting"
    if event in ("Stop", "SubagentStop"):
        return "done"
    return None


def main():
    payload = read_stdin_json()
    event = payload.get("hook_event_name", "")

    session_id = payload.get("session_id", "unknown")
    cwd = payload.get("cwd", "")
    task_key = session_id

    data = load_state(STATE_PATH)

    if event == "SessionEnd":
        data["tasks"].pop(task_key, None)
    else:
        new_state = determine_state(event, payload)
        if new_state is None:
            print("{}")
            return
        data["tasks"][task_key] = {
            "state": new_state,
            "updated_at": time.time(),
            "cwd": cwd,
            "source": "codex",
        }

    data["tasks"] = prune_stale_tasks(data["tasks"])
    data["aggregate_state"] = compute_aggregate(data["tasks"])
    data["updated_at"] = time.time()
    save_state(STATE_PATH, data)
    print("{}")  # Stop/SubagentStop require valid JSON on stdout when exiting 0


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("{}")
    sys.exit(0)
