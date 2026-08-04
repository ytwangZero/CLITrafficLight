#!/usr/bin/env python3
"""
Claude Code Traffic Light Hook

给Claude Code的hooks机制调用,把hook事件翻译成state.json里的状态。

默认写到自己独立的一份状态文件(跟Codex的state.json分开,不共用),通过
CLAUDE_TRAFFIC_LIGHT_STATE_PATH 环境变量可以覆盖路径。同一时间只跑Codex或者
只跑Claude Code的话,bridge.py用 --source codex / --source claude 选择监控哪一份
状态文件就行,互不干扰。

用法(在Claude Code的settings.json hooks配置里调用,事件名作为第一个参数传入):
    python3 claude_light_hook.py <EventName>

事件名优先取命令行参数,取不到时退回读取stdin JSON里的 hook_event_name 字段。

事件映射:
    UserPromptSubmit / PreToolUse / PostToolUse -> working (黄灯)
    PermissionRequest                           -> waiting (红灯,Claude Code真实弹出权限确认框时触发)
    Stop / SubagentStop                         -> done (绿灯)
    SessionEnd                                  -> 直接删掉这个session的记录(退出Claude Code就熄灯)
    其它事件                                     -> 忽略,不改变状态

注:PermissionRequest触发红灯后,Claude Code本身不会再单独发一个"已批准"事件,
所以加了PostToolUse(工具执行完毕)映射回working,避免用户点批准后灯一直卡在红色
直到整轮对话结束才变绿。

多个并发session分别记录在tasks字典里,按下面优先级聚合出aggregate_state:
    waiting > working > 10分钟内的done > idle

这个脚本任何异常都会静默吞掉、正常退出(exit 0),不应该影响Claude Code本身的行为。
"""

import json
import os
import sys
import time

STATE_PATH = os.path.expanduser(
    os.environ.get(
        "CLAUDE_TRAFFIC_LIGHT_STATE_PATH",
        "~/Library/Application Support/CodexTrafficLight/claude-state.json",
    )
)

DONE_EXPIRY_SECONDS = 10 * 60
# working/waiting 状态如果超过这么久没被新事件刷新,视为那次session被异常中断了
# (强制关终端、电脑休眠等),不再计入聚合状态,避免灯永远卡住。正常使用时每次
# 工具调用都会刷新,不会接近这个时长。
STALE_EXPIRY_SECONDS = 20 * 60
# 太久没更新的session记录,直接从状态文件里清掉,避免越堆越多
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
        # working/waiting 如果太久没更新,大概率是那次session没正常结束就被强制关掉了
        # (直接关终端、电脑休眠等),不应该让灯永远卡在黄/红。正常使用时这两个状态会
        # 频繁被hook事件刷新,不会接近这个超时时间。
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
    """清掉太久没更新的session记录,避免状态文件里堆积僵尸session。"""
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
    return None  # 不认识/不关心的事件


def main():
    payload = read_stdin_json()
    event = sys.argv[1] if len(sys.argv) > 1 else payload.get("hook_event_name", "")

    session_id = payload.get("session_id", "unknown")
    cwd = payload.get("cwd", "")
    task_key = session_id

    data = load_state(STATE_PATH)

    if event == "SessionEnd":
        # 用户退出Claude Code(正常退出,比如/exit或者Ctrl+D触发的),
        # 直接把这个session的记录删掉,不再计入聚合状态,灯马上灭。
        data["tasks"].pop(task_key, None)
    else:
        new_state = determine_state(event, payload)
        if new_state is None:
            return
        data["tasks"][task_key] = {
            "state": new_state,
            "updated_at": time.time(),
            "cwd": cwd,
            "source": "claude-code",
        }

    data["tasks"] = prune_stale_tasks(data["tasks"])
    data["aggregate_state"] = compute_aggregate(data["tasks"])
    data["updated_at"] = time.time()
    save_state(STATE_PATH, data)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # hook脚本出任何问题都不应该影响Claude Code本身,静默失败退出0
        pass
    sys.exit(0)
