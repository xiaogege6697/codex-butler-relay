#!/usr/bin/env python3
"""Codex ↔ Claude 轻量 Goal Loop 接力器。"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path


STATE_FILE = ".butler-relay.json"
CLAUDE = os.environ.get("BUTLER_RELAY_CLAUDE") or shutil.which("claude") or "claude"
SCREEN = os.environ.get("BUTLER_RELAY_SCREEN") or shutil.which("screen") or "screen"
TRANSCRIPT_ROOT = Path(
    os.environ.get("BUTLER_RELAY_TRANSCRIPTS", Path.home() / ".claude" / "projects")
)
CACHE_DIR = Path.home() / ".cache" / "codex-butler-relay"
SIGNALS = {"GOAL_DONE", "NEED_DECISION", "NEW_WINDOW"}


def load_state(project: Path) -> dict[str, str]:
    path = project / STATE_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items() if isinstance(value, str)}


def load_session(project: Path) -> str | None:
    return load_state(project).get("session_id") or None


def update_state(project: Path, **values: str) -> None:
    data = load_state(project)
    data.update(values)
    temporary = project / f"{STATE_FILE}.tmp"
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(project / STATE_FILE)


def save_session(project: Path, session_id: str, **extra: str) -> None:
    update_state(project, session_id=session_id, **extra)


def first_nonblank_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def goal_signal(text: str) -> str:
    first_line = first_nonblank_line(text)
    return first_line if first_line in SIGNALS else "RUNNING"


def goal_prompt(text: str) -> str:
    return (
        "进入 Goal Loop，持续执行直至出现终态。普通进度只在当前 Claude TUI 展示，"
        "不要因此结束本轮。仅在以下情况结束回复：完成时首行 GOAL_DONE；需要关键决策时"
        "首行 NEED_DECISION；context 质量下降需换窗时首行 NEW_WINDOW，并附完整交接。\n"
        "context 约 60% 时开始阶段收尾与整理交接；超过 70% 时直接返回 NEW_WINDOW，"
        "不要等到窗口堆满。\n"
        "本 Goal 已授权沿用当前临时工配置并自动检测；配置可用时不要询问，只有失效或额度"
        "问题才返回 NEED_DECISION。非核心任务仅在“临时工执行 + Claude 审核 + 预期返工”"
        "成本低于 Claude 直接完成，且结果可客观验证时外包；关键判断与最终审核由你负责。\n\n"
        f"{text}"
    )


def update_goal_from_result(project: Path, result: str) -> None:
    if not load_state(project).get("goal"):
        return
    signal = goal_signal(result)
    statuses = {
        "GOAL_DONE": "awaiting_acceptance",
        "NEED_DECISION": "needs_decision",
        "NEW_WINDOW": "switching_window",
        "RUNNING": "running",
    }
    update_state(project, goal_status=statuses[signal], last_signal=signal)


def _screen_command(*parts: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run([SCREEN, *parts], text=True, capture_output=True, check=check)


def _start_screen(project: Path, *parts: str) -> None:
    subprocess.Popen(
        [SCREEN, "-DmS", *parts],
        cwd=project,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(0.2)


def screen_status(name: str) -> str | None:
    completed = _screen_command("-ls", check=False)
    for line in completed.stdout.splitlines():
        if name not in line or "Dead" in line:
            continue
        if "(Attached)" in line:
            return "attached"
        if "(Detached)" in line:
            return "detached"
        return "alive"
    return None


def screen_is_alive(name: str) -> bool:
    return screen_status(name) is not None


def close_screen(name: str | None) -> bool:
    """只关闭本工具创建的 screen，绝不触碰用户的其他会话。"""
    if not name or not name.startswith("butler-native-") or not screen_is_alive(name):
        return False
    _screen_command("-S", name, "-X", "quit", check=False)
    return True


def open_native_terminal(screen_name: str) -> None:
    command = f"{shlex.quote(SCREEN)} -x {shlex.quote(screen_name)}"
    escaped = command.replace("\\", "\\\\").replace('"', '\\"')
    apple_script = f'tell application "Terminal" to do script "{escaped}"'
    subprocess.run(["osascript", "-e", apple_script], check=True, capture_output=True)


def native_screen_snapshot(screen_name: str) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{screen_name}.txt"
    _screen_command("-S", screen_name, "-p", "0", "-X", "hardcopy", str(path))
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def wait_for_native_ready(screen_name: str) -> None:
    ready_markers = ("Welcome back!", "Tips for getting started", "auto mode on")
    trust_markers = (
        "Quick safety check:",
        "Yes, I trust this folder",
        "Enter to confirm",
    )
    trust_confirmed = False
    while True:
        if not screen_is_alive(screen_name):
            raise RuntimeError("交互式 Claude 在就绪前退出")
        snapshot = native_screen_snapshot(screen_name)
        if any(marker in snapshot for marker in ready_markers):
            return
        if not trust_confirmed and all(marker in snapshot for marker in trust_markers):
            _screen_command("-S", screen_name, "-p", "0", "-X", "stuff", "\r")
            trust_confirmed = True
        time.sleep(0.25)


def ensure_screen_attached(screen_name: str) -> None:
    for _ in range(3):
        open_native_terminal(screen_name)
        time.sleep(1)
        if screen_status(screen_name) == "attached":
            return
    raise RuntimeError("Terminal 未能附着到 screen")


def find_transcript(session_id: str) -> Path | None:
    matches = list(TRANSCRIPT_ROOT.glob(f"*/{session_id}.jsonl"))
    return max(matches, key=lambda path: path.stat().st_mtime) if matches else None


def transcript_reply(path: Path, marker: str) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "assistant":
            continue
        message = event.get("message") or {}
        texts = [
            block.get("text", "")
            for block in message.get("content") or []
            if block.get("type") == "text"
        ]
        reply = "\n".join(texts).strip()
        if marker in reply:
            return reply.replace(marker, "").rstrip()
    return None


def transcript_turn_ended_without_marker(path: Path, marker: str) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    request_seen = False
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "user" and marker in line:
            request_seen = True
            continue
        if not request_seen or event.get("type") != "assistant":
            continue
        message = event.get("message") or {}
        texts = [
            block.get("text", "")
            for block in message.get("content") or []
            if block.get("type") == "text" and block.get("text")
        ]
        if texts and message.get("stop_reason") == "end_turn":
            return marker not in "\n".join(texts)
    return False


def wait_for_native_reply(session_id: str, marker: str, screen_name: str) -> str:
    while True:
        transcript = find_transcript(session_id)
        if transcript:
            reply = transcript_reply(transcript, marker)
            if reply is not None:
                return reply
            if transcript_turn_ended_without_marker(transcript, marker):
                raise RuntimeError("Claude 本轮已结束，但回复缺少 relay marker，无法安全匹配结果")
        if not screen_is_alive(screen_name):
            raise RuntimeError("Claude/screen 已退出，任务未返回可识别结果")
        time.sleep(1)


def turn_prompt(text: str, marker: str, *, load_butler: bool) -> str:
    prefix = "/butler " if load_butler else ""
    return f"{prefix}{text}\n\n回复末尾原样保留 {marker}"


def send_native_turn(
    screen_name: str,
    session_id: str,
    text: str,
    *,
    load_butler: bool,
) -> str:
    marker = f"[relay-marker:{uuid.uuid4()}]"
    prompt = turn_prompt(text, marker, load_butler=load_butler)
    _screen_command("-S", screen_name, "-p", "0", "-X", "stuff", prompt)
    # Claude TUI 处理长文本需要一个极短间隔，否则紧随其后的 Enter 偶尔会被吞掉。
    time.sleep(0.1)
    _screen_command("-S", screen_name, "-p", "0", "-X", "stuff", "\r")
    return wait_for_native_reply(session_id, marker, screen_name)


def start_native(project: Path, text: str) -> tuple[str, str, str]:
    session_id = str(uuid.uuid4())
    screen_name = f"butler-native-{session_id[:8]}"
    _start_screen(project, screen_name, os.environ.get("SHELL", "/bin/zsh"), "-l")
    save_session(project, session_id, screen_name=screen_name, mode="interactive")
    ensure_screen_attached(screen_name)
    launch = f"exec {shlex.quote(CLAUDE)} --session-id {shlex.quote(session_id)}"
    _screen_command("-S", screen_name, "-p", "0", "-X", "stuff", launch)
    _screen_command("-S", screen_name, "-p", "0", "-X", "stuff", "\r")
    wait_for_native_ready(screen_name)
    result = send_native_turn(
        screen_name, session_id, text, load_butler=True
    )
    return result, session_id, screen_name


def continue_native(screen_name: str, session_id: str, text: str) -> str:
    if not screen_is_alive(screen_name):
        raise RuntimeError("原生 Claude 窗口已退出；请使用 --new 新开窗口")
    return send_native_turn(screen_name, session_id, text, load_butler=False)


def requests_new_window(text: str) -> bool:
    return goal_signal(text) == "NEW_WINDOW"


def relay_native(project: Path, text: str, *, force_new: bool = False) -> str:
    previous = load_state(project)
    old_screen = previous.get("screen_name")
    session_id = None if force_new else previous.get("session_id")
    screen_name = None if force_new else old_screen
    if session_id and screen_name and screen_is_alive(screen_name):
        if screen_status(screen_name) != "attached":
            open_native_terminal(screen_name)
        result = continue_native(screen_name, session_id, text)
    else:
        result, session_id, screen_name = start_native(project, text)
        if force_new and old_screen != screen_name:
            close_screen(old_screen)
    save_session(project, session_id, screen_name=screen_name, mode="interactive")

    if requests_new_window(result):
        update_goal_from_result(project, result)
        handoff = f"根据以下旧窗口交接继续执行：\n\n{result}"
        if load_state(project).get("goal"):
            handoff = goal_prompt(handoff)
        replaced_screen = screen_name
        result, session_id, screen_name = start_native(project, handoff)
        save_session(project, session_id, screen_name=screen_name, mode="interactive")
        close_screen(replaced_screen)
    update_goal_from_result(project, result)
    return result


def command_version(command: str, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            [command, *args], text=True, capture_output=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = completed.stdout.strip() or completed.stderr.strip()
    return output.splitlines()[0] if output else None


def environment_check(project: Path) -> dict[str, object]:
    claude_path = shutil.which(CLAUDE)
    screen_path = shutil.which(SCREEN)
    osascript_path = shutil.which("osascript")
    terminal_app = next(
        (
            str(path)
            for path in (
                Path("/System/Applications/Utilities/Terminal.app"),
                Path("/Applications/Utilities/Terminal.app"),
            )
            if path.exists()
        ),
        None,
    )
    butler_candidates = (
        project / ".claude" / "skills" / "butler" / "SKILL.md",
        Path.home() / ".claude" / "skills" / "butler" / "SKILL.md",
        Path.home() / ".claude" / "commands" / "butler.md",
    )
    butler_skill = next((str(path) for path in butler_candidates if path.is_file()), None)
    checks = {
        "macos": {"ok": platform.system() == "Darwin", "value": platform.platform()},
        "python": {"ok": sys.version_info >= (3, 10), "version": platform.python_version()},
        "claude": {
            "ok": claude_path is not None,
            "path": claude_path,
            "version": command_version(claude_path, "--version") if claude_path else None,
        },
        "screen": {
            "ok": screen_path is not None,
            "path": screen_path,
            "version": command_version(screen_path, "--version") if screen_path else None,
        },
        "terminal": {
            "ok": osascript_path is not None and terminal_app is not None,
            "osascript": osascript_path,
            "app": terminal_app,
        },
        "transcripts": {
            "ok": TRANSCRIPT_ROOT.is_dir() and os.access(TRANSCRIPT_ROOT, os.R_OK),
            "path": str(TRANSCRIPT_ROOT),
        },
        "butler_skill": {"ok": butler_skill is not None, "path": butler_skill},
    }
    return {"ok": all(value["ok"] for value in checks.values()), "checks": checks}


def relay_status(project: Path) -> dict[str, object]:
    state = load_state(project)
    screen_name = state.get("screen_name")
    return {
        "project": str(project),
        "session_id": state.get("session_id"),
        "mode": state.get("mode") if state else None,
        "screen_name": screen_name,
        "screen_status": screen_status(screen_name) if screen_name else None,
        "goal": state.get("goal"),
        "goal_status": state.get("goal_status"),
        "last_signal": state.get("last_signal"),
    }


def call_claude_headless(
    project: Path,
    text: str,
    session_id: str,
    *,
    resume: bool,
) -> tuple[str, str]:
    command = [
        CLAUDE,
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        "auto",
        "--resume" if resume else "--session-id",
        session_id,
        f"{'/butler ' if not resume else ''}{text}",
    ]
    completed = subprocess.run(command, cwd=project, text=True, capture_output=True)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"Claude 调用失败（exit={completed.returncode}）：{detail}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Claude 未返回有效 JSON") from exc
    result = payload.get("result")
    if not isinstance(result, str):
        raise RuntimeError("Claude 返回中缺少文本 result")
    return result, str(payload.get("session_id") or session_id)


def relay_headless(project: Path, text: str, *, force_new: bool = False) -> str:
    session_id = None if force_new else load_session(project)
    if session_id:
        result, session_id = call_claude_headless(
            project, text, session_id, resume=True
        )
    else:
        session_id = str(uuid.uuid4())
        result, session_id = call_claude_headless(
            project, text, session_id, resume=False
        )
    save_session(project, session_id, mode="headless", screen_name="")
    if requests_new_window(result):
        update_goal_from_result(project, result)
        handoff = f"根据以下旧窗口交接继续执行：\n\n{result}"
        if load_state(project).get("goal"):
            handoff = goal_prompt(handoff)
        session_id = str(uuid.uuid4())
        result, session_id = call_claude_headless(
            project, handoff, session_id, resume=False
        )
        save_session(project, session_id, mode="headless", screen_name="")
    update_goal_from_result(project, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex ↔ Claude 轻量 Goal Loop 接力器")
    parser.add_argument("text", nargs="*", help="发送给 Claude 的文本；省略则从 stdin 读取")
    parser.add_argument("--project", default=".", help="Claude 工作目录，默认当前目录")
    parser.add_argument("--new", action="store_true", help="主动新开 Claude 窗口")
    parser.add_argument("--headless", action="store_true", help="显式使用非交互 -p 模式")
    parser.add_argument("--check", action="store_true", help="检测本机运行条件")
    parser.add_argument("--status", action="store_true", help="查看当前项目接力状态")
    parser.add_argument("--goal", action="store_true", help="启动新的 Goal Loop")
    parser.add_argument("--accept", action="store_true", help="标记当前 Goal 已通过 Codex 验收")
    args = parser.parse_args()

    if sum((args.check, args.status, args.accept)) > 1:
        parser.error("--check、--status 与 --accept 不能同时使用")
    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        parser.error(f"项目目录不存在：{project}")
    if args.check:
        result = environment_check(project)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    if args.status:
        print(json.dumps(relay_status(project), ensure_ascii=False, indent=2))
        return 0
    if args.accept:
        if not load_state(project).get("goal"):
            parser.error("当前项目没有 Goal 可验收")
        update_state(project, goal_status="accepted", last_signal="ACCEPTED")
        print("GOAL_ACCEPTED")
        return 0

    text = " ".join(args.text).strip() if args.text else sys.stdin.read().strip()
    if not text:
        parser.error("转发文本不能为空")
    if args.goal:
        update_state(project, goal=text, goal_status="running", last_signal="STARTED")
        text = goal_prompt(text)
    elif load_state(project).get("goal"):
        update_state(project, goal_status="running", last_signal="CONTINUED")

    try:
        relay = relay_headless if args.headless else relay_native
        print(relay(project, text, force_new=args.new or args.goal))
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
