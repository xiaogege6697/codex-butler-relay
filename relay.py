#!/usr/bin/env python3
"""Codex ↔ Claude 轻量 Goal Loop 接力器。"""

from __future__ import annotations

import argparse
import hashlib
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
TERMINAL_SIGNALS = SIGNALS | {"RELAY_FAILED", "PROTOCOL_ERROR"}
GOAL_PROTOCOL = "goal-capsule-v1"
EVENT_PROTOCOL = "butler-event-v1"
FIRST_WATCHDOG_MINUTES = 360
NEXT_WATCHDOG_MINUTES = 1440
DELIVERY_ATTEMPTS = 3
DELIVERY_CHECKS = 20
SCREEN_STUFF_MAX_BYTES = 160


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


def project_cache_dir(project: Path) -> Path:
    digest = hashlib.sha256(str(project).encode("utf-8")).hexdigest()[:16]
    return CACHE_DIR / "projects" / digest


def write_detached_result(path: Path, result: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(result, encoding="utf-8")
    temporary.replace(path)


def write_terminal_event(
    path: Path,
    *,
    goal_id: str,
    signal: str,
    project: Path,
    result_path: Path,
) -> dict[str, str]:
    """原子写入最小终态事件；详细内容留在 result_path，避免重复传递。"""
    if signal not in TERMINAL_SIGNALS:
        raise ValueError(f"非终态信号不能写入事件：{signal}")
    digest = hashlib.sha256(result_path.read_bytes()).hexdigest()
    event = {
        "protocol": EVENT_PROTOCOL,
        "goal_id": goal_id,
        "signal": signal,
        "project": str(project),
        "result_path": str(result_path),
        "sha256": digest,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(event, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)
    return event


def format_terminal_event(event: dict[str, str]) -> str:
    return f"{event['signal']}\n{EVENT_PROTOCOL}\n{json.dumps(event, ensure_ascii=False)}"


def validate_terminal_event(
    event: object, *, state: dict[str, str], project: Path
) -> tuple[dict[str, str] | None, str | None]:
    if not isinstance(event, dict) or event.get("protocol") != EVENT_PROTOCOL:
        return None, "终态事件协议无效"
    required = {"goal_id", "signal", "project", "result_path", "sha256"}
    if not required.issubset(event) or not all(
        isinstance(event[key], str) and event[key] for key in required
    ):
        return None, "终态事件字段不完整"
    if event["signal"] not in TERMINAL_SIGNALS:
        return None, "终态事件 signal 无效"
    if state.get("goal_id") and event["goal_id"] != state["goal_id"]:
        return None, "终态事件 goal_id 与当前任务不一致"
    if Path(event["project"]).resolve() != project.resolve():
        return None, "终态事件项目路径不一致"
    if state.get("result_path") and event["result_path"] != state["result_path"]:
        return None, "终态事件结果路径与当前任务不一致"
    result_path = Path(event["result_path"])
    if not result_path.is_file():
        return None, "终态结果文件不存在"
    actual_digest = hashlib.sha256(result_path.read_bytes()).hexdigest()
    if actual_digest != event["sha256"]:
        return None, "终态结果摘要校验失败"
    return {key: str(value) for key, value in event.items()}, None


def notify_terminal(signal: str, project: Path) -> None:
    messages = {
        "GOAL_DONE": "Claude 已完成；终态事件已写入，等待 Codex 验收",
        "NEED_DECISION": "Claude 需要关键决策；终态事件已写入",
        "PROTOCOL_ERROR": "Claude 已返回但缺少终态；错误事件已写入",
        "RELAY_FAILED": "接力任务异常退出；错误事件已写入",
    }
    body = messages.get(signal)
    if not body or not shutil.which("osascript"):
        return
    script = (
        f"display notification {json.dumps(body, ensure_ascii=False)} "
        f"with title {json.dumps(f'管家接力器 · {project.name}', ensure_ascii=False)}"
    )
    subprocess.run(
        ["osascript", "-e", script], check=False, capture_output=True, text=True
    )


def first_nonblank_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def goal_signal(text: str) -> str:
    nonblank = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(nonblank):
        if line in SIGNALS:
            return line
        if index >= 4:
            break
    if nonblank:
        final_line = nonblank[-1]
        for signal in SIGNALS:
            if final_line.startswith((f"{signal} ", f"{signal}：", f"{signal}:")):
                return signal
    return "RUNNING"


def goal_prompt(
    text: str, *, goal_id: str = "", project: Path | None = None
) -> str:
    goal_id = goal_id or "unspecified"
    project_anchor = str(project) if project else "当前项目目录"
    return (
        f"[{GOAL_PROTOCOL}]\n"
        f"goal_id: {goal_id}\n"
        f"intent_and_outcome: {text}\n"
        "boundaries: 仅在用户明确授权的项目范围和边界内执行；未授权的外部写入、删除、"
        "发布和敏感操作必须暂停。\n"
        "acceptance: 以用户给出的验收标准和真实可复核证据为准；不得用自述代替验证。\n"
        f"anchors: project={project_anchor}; 优先读取项目内权威文件，不复制无关上下文。\n"
        "execution: 加载 Butler 后自主规划、选工具并决定是否调用低成本临时工；普通过程"
        "不回传 Codex。\n"
        "terminal: 完成首行 GOAL_DONE；需决策首行 NEED_DECISION；context 质量下降首行"
        " NEW_WINDOW 并附完整交接。"
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
    completed = subprocess.run(
        [SCREEN, "-dmS", *parts],
        cwd=project,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"screen 启动失败：{detail or completed.returncode}")
    screen_name = parts[0] if parts else ""
    for _ in range(20):
        if screen_name and screen_is_alive(screen_name):
            return
        time.sleep(0.05)
    raise RuntimeError(f"screen 启动后未保持存活：{screen_name}")


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
    completed = subprocess.run(
        ["osascript", "-e", apple_script],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            f"Terminal 无法附着 screen {screen_name}：{detail or completed.returncode}"
        )


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


def transcript_request_seen(path: Path, marker: str) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "user" and marker in line:
            return True
    return False


def native_request_seen(session_id: str, marker: str) -> bool:
    transcript = find_transcript(session_id)
    return bool(transcript and transcript_request_seen(transcript, marker))


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


def screen_text_chunks(text: str, max_bytes: int = SCREEN_STUFF_MAX_BYTES) -> list[str]:
    chunks: list[str] = []
    current = ""
    current_bytes = 0
    for character in text:
        size = len(character.encode("utf-8"))
        if current and current_bytes + size > max_bytes:
            chunks.append(current)
            current = ""
            current_bytes = 0
        current += character
        current_bytes += size
    if current:
        chunks.append(current)
    return chunks


def stuff_native_text(screen_name: str, text: str) -> None:
    for chunk in screen_text_chunks(text):
        _screen_command("-S", screen_name, "-p", "0", "-X", "stuff", chunk)
        time.sleep(0.01)


def deliver_native_prompt(
    screen_name: str,
    session_id: str,
    prompt: str,
    marker: str,
) -> None:
    for attempt in range(DELIVERY_ATTEMPTS):
        snapshot = native_screen_snapshot(screen_name) if attempt else ""
        # 首次正常输入；若前两轮完全未进入输入框，最后一轮重发全文。
        if attempt == 0 or (attempt == DELIVERY_ATTEMPTS - 1 and marker not in snapshot):
            stuff_native_text(screen_name, prompt)
            time.sleep(0.25)
        # 第二轮只补 Enter，可处理“文字已在输入框但提交键被吞”的情况。
        _screen_command("-S", screen_name, "-p", "0", "-X", "stuff", "\r")
        for _ in range(DELIVERY_CHECKS):
            if native_request_seen(session_id, marker):
                return
            if not screen_is_alive(screen_name):
                raise RuntimeError("Claude/screen 在消息投递确认前退出")
            time.sleep(0.25)
    raise RuntimeError("Claude 首条消息投递失败：3 次后 transcript 仍未出现 relay marker")


def send_native_turn(
    screen_name: str,
    session_id: str,
    text: str,
    *,
    load_butler: bool,
) -> str:
    marker = f"[relay-marker:{uuid.uuid4()}]"
    prompt = turn_prompt(text, marker, load_butler=load_butler)
    deliver_native_prompt(screen_name, session_id, prompt, marker)
    return wait_for_native_reply(session_id, marker, screen_name)


def start_native(project: Path, text: str) -> tuple[str, str, str]:
    session_id = str(uuid.uuid4())
    screen_name = f"butler-native-{session_id[:8]}"
    _start_screen(project, screen_name, os.environ.get("SHELL", "/bin/zsh"), "-l")
    save_session(project, session_id, screen_name=screen_name, mode="interactive")
    ensure_screen_attached(screen_name)
    launch = (
        f"exec {shlex.quote(CLAUDE)} --session-id {shlex.quote(session_id)} "
        "--permission-mode auto"
    )
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


def start_detached(
    project: Path,
    text: str,
    *,
    goal: bool,
    force_new: bool,
    headless: bool,
) -> dict[str, str]:
    job_id = str(uuid.uuid4())
    goal_id = job_id if goal else load_state(project).get("goal_id") or job_id
    cache = project_cache_dir(project)
    cache.mkdir(parents=True, exist_ok=True)
    job_path = cache / f"job-{job_id}.json"
    result_path = cache / f"result-{job_id}.md"
    event_path = cache / f"event-{job_id}.json"
    log_path = cache / f"worker-{job_id}.log"
    job_path.write_text(
        json.dumps(
            {
                "project": str(project),
                "text": text,
                "goal": goal,
                "force_new": force_new,
                "headless": headless,
                "goal_id": goal_id,
                "result_path": str(result_path),
                "event_path": str(event_path),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if goal:
        update_state(
            project,
            goal=text,
            goal_id=goal_id,
            goal_status="starting",
            last_signal="GOAL_STARTED",
            watchdog_minutes=str(FIRST_WATCHDOG_MINUTES),
            result_path=str(result_path),
            event_path=str(event_path),
            log_path=str(log_path),
        )
    elif load_state(project).get("goal"):
        update_state(
            project,
            goal_status="starting",
            last_signal="CONTINUING",
            watchdog_minutes=str(FIRST_WATCHDOG_MINUTES),
            result_path=str(result_path),
            event_path=str(event_path),
            log_path=str(log_path),
        )
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", str(job_path)]
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    return {
        "signal": "GOAL_STARTED",
        "goal_id": goal_id,
        "project": str(project),
        "event_path": str(event_path),
        "watchdog_minutes": str(FIRST_WATCHDOG_MINUTES),
        "worker_pid": str(process.pid),
    }


def run_detached_job(job_path: Path) -> int:
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
        project = Path(job["project"]).resolve()
        result_path = Path(job["result_path"])
        event_path = Path(
            job.get("event_path") or result_path.with_suffix(".event.json")
        )
        goal_id = str(job.get("goal_id") or uuid.uuid4())
        raw_text = str(job["text"])
        is_goal = bool(job["goal"])
        text = (
            goal_prompt(raw_text, goal_id=goal_id, project=project)
            if is_goal
            else raw_text
        )
        if is_goal:
            update_state(project, goal=raw_text, goal_status="running", last_signal="STARTED")
        elif load_state(project).get("goal"):
            update_state(project, goal_status="running", last_signal="CONTINUED")
        relay = relay_headless if bool(job["headless"]) else relay_native
        result = relay(project, text, force_new=bool(job["force_new"]) or is_goal)
        write_detached_result(result_path, result)
        signal = goal_signal(result)
        if signal == "RUNNING":
            signal = "PROTOCOL_ERROR"
        write_terminal_event(
            event_path,
            goal_id=goal_id,
            signal=signal,
            project=project,
            result_path=result_path,
        )
        if signal == "PROTOCOL_ERROR":
            update_state(
                project, goal_status="protocol_error", last_signal=signal
            )
        else:
            update_goal_from_result(project, result)
        update_state(
            project,
            result_path=str(result_path),
            event_path=str(event_path),
            finished_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        )
        notify_terminal(signal, project)
        return 0
    except (OSError, KeyError, TypeError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        project_value = locals().get("project")
        result_value = locals().get("result_path")
        if isinstance(project_value, Path):
            if isinstance(result_value, Path):
                write_detached_result(result_value, f"RELAY_FAILED\n{exc}")
                event_value = locals().get("event_path")
                goal_value = str(locals().get("goal_id") or "unknown")
                if isinstance(event_value, Path):
                    write_terminal_event(
                        event_value,
                        goal_id=goal_value,
                        signal="RELAY_FAILED",
                        project=project_value,
                        result_path=result_value,
                    )
            update_state(
                project_value,
                goal_status="failed",
                last_signal="RELAY_FAILED",
                finished_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            )
            notify_terminal("RELAY_FAILED", project_value)
        print(str(exc), file=sys.stderr)
        return 1


def collect_result(project: Path) -> tuple[str, int]:
    state = load_state(project)
    event_path = state.get("event_path")
    if event_path and Path(event_path).is_file():
        try:
            event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "RELAY_FAILED\n终态事件无法解析", 1
        validated, error = validate_terminal_event(
            event, state=state, project=project
        )
        if error:
            return f"RELAY_FAILED\n{error}", 1
        return format_terminal_event(validated or {}), 0
    result_path = state.get("result_path")
    if not event_path and result_path and Path(result_path).is_file():
        return Path(result_path).read_text(encoding="utf-8"), 0
    status = state.get("goal_status")
    if status in {"starting", "running", "switching_window"}:
        return f"GOAL_RUNNING\nNEXT_WATCHDOG_MINUTES={NEXT_WATCHDOG_MINUTES}", 0
    if status == "accepted":
        return "GOAL_ACCEPTED", 0
    return "NO_RESULT", 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex ↔ Claude 轻量 Goal Loop 接力器")
    parser.add_argument("text", nargs="*", help="发送给 Claude 的文本；省略则从 stdin 读取")
    parser.add_argument("--project", default=".", help="Claude 工作目录，默认当前目录")
    parser.add_argument("--new", action="store_true", help="主动新开 Claude 窗口")
    parser.add_argument("--headless", action="store_true", help="显式使用非交互 -p 模式")
    parser.add_argument("--detach", action="store_true", help="后台等待终态并立即返回")
    parser.add_argument("--collect", action="store_true", help="读取后台任务终态结果")
    parser.add_argument("--check", action="store_true", help="检测本机运行条件")
    parser.add_argument("--status", action="store_true", help="查看当前项目接力状态")
    parser.add_argument("--goal", action="store_true", help="启动新的 Goal Loop")
    parser.add_argument("--accept", action="store_true", help="标记当前 Goal 已通过 Codex 验收")
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.worker:
        return run_detached_job(Path(args.worker))
    if sum((args.check, args.status, args.collect, args.accept)) > 1:
        parser.error("--check、--status、--collect 与 --accept 不能同时使用")
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
    if args.collect:
        result, code = collect_result(project)
        print(result)
        return code
    if args.accept:
        if not load_state(project).get("goal"):
            parser.error("当前项目没有 Goal 可验收")
        update_state(project, goal_status="accepted", last_signal="ACCEPTED")
        print("GOAL_ACCEPTED")
        return 0

    text = " ".join(args.text).strip() if args.text else sys.stdin.read().strip()
    if not text:
        parser.error("转发文本不能为空")
    if args.detach:
        started = start_detached(
            project,
            text,
            goal=args.goal,
            force_new=args.new,
            headless=args.headless,
        )
        print(json.dumps(started, ensure_ascii=False))
        return 0
    if args.goal:
        goal_id = str(uuid.uuid4())
        update_state(
            project,
            goal=text,
            goal_id=goal_id,
            goal_status="running",
            last_signal="STARTED",
        )
        text = goal_prompt(text, goal_id=goal_id, project=project)
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
