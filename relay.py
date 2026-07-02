#!/usr/bin/env python3
"""Codex ↔ Claude 最小文本转发器。"""

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
LOG_FILE = ".butler-relay.log"
CLAUDE = os.environ.get("BUTLER_RELAY_CLAUDE") or shutil.which("claude") or "claude"
SCREEN = os.environ.get("BUTLER_RELAY_SCREEN") or shutil.which("screen") or "screen"
TRANSCRIPT_ROOT = Path(
    os.environ.get("BUTLER_RELAY_TRANSCRIPTS", Path.home() / ".claude" / "projects")
)


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


def save_session(project: Path, session_id: str, **extra: str) -> None:
    path = project / STATE_FILE
    temporary = project / f"{STATE_FILE}.tmp"
    temporary.write_text(
        json.dumps({"session_id": session_id, **extra}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _screen_command(*parts: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [SCREEN, *parts],
        text=True,
        capture_output=True,
        check=check,
    )


def _start_screen(project: Path, *parts: str) -> None:
    subprocess.run(
        [SCREEN, "-DmS", *parts],
        cwd=project,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


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


def open_native_terminal(screen_name: str) -> None:
    command = f"{shlex.quote(SCREEN)} -r {shlex.quote(screen_name)}"
    escaped = command.replace("\\", "\\\\").replace('"', '\\"')
    apple_script = f'tell application "Terminal" to do script "{escaped}"'
    subprocess.run(["osascript", "-e", apple_script], check=True, capture_output=True)


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


def native_prompt(text: str, marker: str) -> str:
    return f"/butler {text}\n\n回复末尾原样保留 {marker}"


def start_native(project: Path, text: str) -> tuple[str, str, str]:
    session_id = str(uuid.uuid4())
    screen_name = f"butler-native-{session_id[:8]}"
    marker = f"[relay-marker:{uuid.uuid4()}]"
    _start_screen(
        project,
        screen_name,
        CLAUDE,
        "--session-id",
        session_id,
        "--permission-mode",
        "auto",
        native_prompt(text, marker),
    )
    for _ in range(20):
        if screen_is_alive(screen_name):
            break
        time.sleep(0.1)
    else:
        raise RuntimeError("原生 Claude screen 启动失败")
    save_session(project, session_id, screen_name=screen_name, mode="native")
    open_native_terminal(screen_name)
    return wait_for_native_reply(session_id, marker, screen_name), session_id, screen_name


def continue_native(screen_name: str, session_id: str, text: str) -> str:
    if not screen_is_alive(screen_name):
        raise RuntimeError("原生 Claude 窗口已退出；请使用 --native --new 新开窗口")
    marker = f"[relay-marker:{uuid.uuid4()}]"
    _screen_command("-S", screen_name, "-p", "0", "-X", "stuff", native_prompt(text, marker))
    _screen_command("-S", screen_name, "-p", "0", "-X", "stuff", "\r")
    return wait_for_native_reply(session_id, marker, screen_name)


def relay_native(project: Path, text: str, *, force_new: bool = False) -> str:
    state = {} if force_new else load_state(project)
    session_id = state.get("session_id")
    screen_name = state.get("screen_name")
    if session_id and screen_name and screen_is_alive(screen_name):
        if screen_status(screen_name) != "attached":
            open_native_terminal(screen_name)
        result = continue_native(screen_name, session_id, text)
    else:
        result, session_id, screen_name = start_native(project, text)
    save_session(project, session_id, screen_name=screen_name, mode="native")
    if requests_new_window(result):
        result, session_id, screen_name = start_native(
            project,
            f"根据以下旧窗口交接继续执行：\n\n{result}",
        )
        save_session(project, session_id, screen_name=screen_name, mode="native")
    return result


def command_version(command: str, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            [command, *args],
            text=True,
            capture_output=True,
            timeout=5,
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
        "python": {
            "ok": sys.version_info >= (3, 10),
            "version": platform.python_version(),
        },
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
        "mode": state.get("mode", "background") if state else None,
        "screen_name": screen_name,
        "screen_status": screen_status(screen_name) if screen_name else None,
    }


def append_log(project: Path, message: str) -> None:
    with (project / LOG_FILE).open("a", encoding="utf-8") as stream:
        stream.write(message.rstrip() + "\n")
        stream.flush()


def open_visible_monitor(project: Path) -> None:
    log = project / LOG_FILE
    log.write_text("=== Butler Relay 可视窗口 ===\n", encoding="utf-8")
    command = f"tail -n +1 -f {shlex.quote(str(log))} | sed '/=== RELAY_DONE ===/q'"
    escaped = command.replace("\\", "\\\\").replace('"', '\\"')
    apple_script = f'tell application "Terminal" to do script "{escaped}"'
    subprocess.run(["osascript", "-e", apple_script], check=True, capture_output=True)


def open_foreground_monitor(project: Path) -> None:
    log = project / LOG_FILE
    log.write_text("=== Butler Relay 前台窗口 ===\n", encoding="utf-8")
    busy_check = subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "Terminal" to return busy of selected tab of front window',
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    if busy_check.stdout.strip().lower() == "true":
        raise RuntimeError("当前 Terminal 前台 tab 正在运行程序，请切换到一个空闲 tab 后重试")

    command = f"tail -n +1 -f {shlex.quote(str(log))} | sed '/=== RELAY_DONE ===/q'"
    escaped = command.replace("\\", "\\\\").replace('"', '\\"')
    apple_script = (
        'tell application "Terminal" to do script '
        f'"{escaped}" in selected tab of front window'
    )
    subprocess.run(["osascript", "-e", apple_script], check=True, capture_output=True)


def _base_command(
    session_id: str,
    *,
    resume: bool,
    stream: bool,
    continue_latest: bool = False,
) -> list[str]:
    command = [
        CLAUDE,
        "-p",
        "--output-format",
        "stream-json" if stream else "json",
        "--permission-mode",
        "auto",
    ]
    if stream:
        command.append("--verbose")
    if continue_latest:
        command.append("--continue")
    else:
        command.extend(["--resume" if resume else "--session-id", session_id])
    return command


def call_claude(
    project: Path,
    text: str,
    session_id: str,
    *,
    resume: bool,
    visible: bool = False,
    continue_latest: bool = False,
) -> tuple[str, str]:
    command = _base_command(
        session_id,
        resume=resume,
        stream=visible,
        continue_latest=continue_latest,
    )
    command.append(f"/butler {text}")

    if visible:
        return call_claude_visible(project, command, session_id)

    completed = subprocess.run(
        command,
        cwd=project,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"Claude 调用失败（exit={completed.returncode}）：{detail}")

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Claude 未返回有效 JSON") from exc

    result = payload.get("result")
    returned_session = payload.get("session_id") or session_id
    if not isinstance(result, str):
        raise RuntimeError("Claude 返回中缺少文本 result")
    return result, str(returned_session)


def call_claude_visible(
    project: Path, command: list[str], session_id: str
) -> tuple[str, str]:
    append_log(project, f"\n▶ Claude session: {session_id}")
    append_log(project, "Claude 正在运行…")
    process = subprocess.Popen(
        command,
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    result: str | None = None
    returned_session = session_id
    assert process.stdout is not None
    for line in process.stdout:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            if line.strip():
                append_log(project, f"[Claude] {line.strip()}")
            continue

        if event.get("session_id"):
            returned_session = str(event["session_id"])
        if event.get("type") == "system" and event.get("subtype") == "init":
            append_log(project, f"✓ 已启动，模型：{event.get('model', 'unknown')}")
        elif event.get("type") == "assistant":
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if block.get("type") == "tool_use":
                    append_log(project, f"🔧 tool: {block.get('name', 'unknown')}")
                elif block.get("type") == "text" and block.get("text"):
                    append_log(project, block["text"])
        elif event.get("type") == "result":
            value = event.get("result")
            if isinstance(value, str):
                result = value

    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(f"Claude 调用失败（exit={return_code}），详见 {project / LOG_FILE}")
    if result is None:
        raise RuntimeError("Claude事件流中缺少最终 result")
    append_log(project, "✓ Claude 本轮完成")
    return result, returned_session


def requests_new_window(text: str) -> bool:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first_line == "NEW_WINDOW"


def relay(
    project: Path,
    text: str,
    *,
    force_new: bool = False,
    visible: bool = False,
    adopt: bool = False,
) -> str:
    session_id = None if force_new or adopt else load_session(project)
    if adopt:
        provisional = str(uuid.uuid4())
        result, session_id = call_claude(
            project,
            text,
            provisional,
            resume=False,
            visible=visible,
            continue_latest=True,
        )
    elif session_id:
        result, session_id = call_claude(
            project, text, session_id, resume=True, visible=visible
        )
    else:
        session_id = str(uuid.uuid4())
        result, session_id = call_claude(
            project, text, session_id, resume=False, visible=visible
        )
    save_session(project, session_id)

    if requests_new_window(result):
        new_session = str(uuid.uuid4())
        result, new_session = call_claude(
            project,
            f"根据以下旧窗口交接继续执行：\n\n{result}",
            new_session,
            resume=False,
            visible=visible,
        )
        save_session(project, new_session)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex ↔ Claude 最小文本转发器")
    parser.add_argument("text", nargs="*", help="发送给 Claude 的文本；省略则从 stdin 读取")
    parser.add_argument("--project", default=".", help="Claude 工作目录，默认当前目录")
    parser.add_argument("--new", action="store_true", help="主动新开 Claude 窗口")
    parser.add_argument("--adopt", action="store_true", help="自动接管该项目最近一次 Claude 会话")
    parser.add_argument("--visible", action="store_true", help="打开 Terminal 实时显示 Claude 运行过程")
    parser.add_argument("--foreground", action="store_true", help="在当前 Terminal 前台空闲 tab 显示运行过程")
    parser.add_argument("--native", action="store_true", help="在 Terminal 显示并续接真实交互式 Claude")
    parser.add_argument("--check", action="store_true", help="检测本机运行条件")
    parser.add_argument("--status", action="store_true", help="查看当前项目接力状态")
    args = parser.parse_args()

    display_modes = sum((args.visible, args.foreground, args.native))
    if display_modes > 1:
        parser.error("--visible、--foreground 与 --native 不能同时使用")
    if args.new and args.adopt:
        parser.error("--new 与 --adopt 不能同时使用")
    if args.native and args.adopt:
        parser.error("--native 暂不支持接管非 screen 启动的现有 Claude；可直接续接本项目已记录的 native 会话")
    if args.check and args.status:
        parser.error("--check 与 --status 不能同时使用")

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
    text = " ".join(args.text).strip() if args.text else sys.stdin.read().strip()
    if not text:
        parser.error("转发文本不能为空")

    try:
        if args.native:
            print(relay_native(project, text, force_new=args.new))
        elif args.foreground:
            open_foreground_monitor(project)
        elif args.visible:
            open_visible_monitor(project)
        show_stream = args.visible or args.foreground
        if not args.native:
            print(
                relay(
                    project,
                    text,
                    force_new=args.new,
                    visible=show_stream,
                    adopt=args.adopt,
                )
            )
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if args.visible or args.foreground:
            append_log(project, "=== RELAY_DONE ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
