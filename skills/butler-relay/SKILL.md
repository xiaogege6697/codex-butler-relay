---
name: butler-relay
description: Run an event-first cross-model Goal Loop in which Codex sends a narrow Goal Capsule, Claude executes in a visible TUI, Relay writes a terminal event, and a distant watchdog is only a lost-event fallback before final Codex acceptance. Use when the user says 打开接力器、启动管家接力器、交给 Claude、做好后直接给我成果、继续接力任务、新开 Claude 窗口、查看接力器状态, or wants one-sentence delegation without intermediate polling or progress token use.
---

# 管家接力器

Use Codex for planning, key decisions, and acceptance. Use Claude Code with `/butler` for execution. Let Butler delegate non-core work to any available tested temporary-worker model; do not hard-code MiMo or DeepSeek.

## Goal Loop

1. Use the current workspace or an explicit user path as the project directory. Never search memory or scan the home directory merely to guess a project; ask once if neither is safe.
2. In each new Codex window, run `butler-relay --check` once when this Skill is first loaded. It checks local dependencies, Claude CLI, Butler Skill format, and directly probes the unified temporary-worker router until the first provider succeeds. Do not start Claude, a Goal, screen, or Terminal for this check. `READY` proceeds normally; `DEGRADED` means Claude can work without a temporary worker; `BLOCKED` reports only the missing core dependency and shortest fix. Do not repeat the check in the same window unless a real failure occurs.
3. Forward a concise goal containing only intent/outcome, user-stated boundaries, acceptance criteria, and authoritative project anchors. Relay wraps it as `goal-capsule-v1`. Do not pre-plan Claude's subtasks, tools, worker routing, or reporting format.
4. Start a fresh visible Goal Loop asynchronously. A new Goal gets a new Claude session and loads `/butler` once:

   ```bash
   butler-relay --detach --goal --project "/absolute/project/path" "目标与验收标准"
   ```

   The visible Terminal/screen process must run with normal local GUI/process permission. If a managed sandbox kills GNU screen immediately, rerun this same launch with the narrow approval required for Terminal and background screen; do not change models or fall back to UI scripting.

5. After `GOAL_STARTED`, create one Codex App watchdog attached to the current thread for 6 hours later, then tell the user once that Claude is running and end the current Codex turn. Relay waits locally and writes `butler-event-v1` immediately on a terminal state; macOS notification is the immediate signal available to the standalone process. Do not keep a blocking tool call alive and do not create intermediate progress heartbeats.
6. A watchdog calls exactly once:

   ```bash
   butler-relay --collect --project "/absolute/project/path"
   ```

   - `GOAL_RUNNING` with `NEXT_WATCHDOG_MINUTES=1440`: the terminal event has not arrived. If the process remains healthy, move the same watchdog to 24 hours later and end silently. Do not read files, inspect screen/transcripts, or narrate progress.
   - `GOAL_DONE` plus `butler-event-v1`: read the referenced result, verify its digest, inspect real files, and run proportional tests. Never accept Claude's claim without verification.
   - `NEED_DECISION` plus `butler-event-v1`: read the referenced result and decide in Codex when the existing goal authorizes it. Ask the user only for a genuinely material choice.
   - `RELAY_FAILED` or a protocol error: inspect the saved error once and report the concrete failure.
7. On any terminal result, pause or delete that Goal's watchdog before continuing. If acceptance fails, send exact defects with `--detach`, schedule a fresh 6-hour watchdog, and end the turn again. When acceptance passes, run `butler-relay --accept --project ...`.

The standalone Relay process cannot directly inject a message into a Codex App thread. Until the App exposes a supported local callback, the terminal event plus macOS notification is the immediate signal and the distant watchdog is the lost-event fallback. Do not emulate a callback with AppleScript UI automation or an unsupported CLI.

## Commands

```bash
butler-relay --check
butler-relay --status --project "/absolute/project/path"
butler-relay --detach --goal --project "/absolute/project/path" "目标与验收标准"
butler-relay --collect --project "/absolute/project/path"
butler-relay --detach --project "/absolute/project/path" "下一阶段任务"
butler-relay --accept --project "/absolute/project/path"
```

The real interactive Claude TUI with Claude's `auto` permission mode is the default, so ordinary project-local tool approvals do not return to Codex. Use `--headless` only when the user explicitly requests background operation.

## Handoff Contract

Claude must not end a turn merely to report normal progress. It begins a terminal response with `GOAL_DONE` only when the acceptance criteria are met, `NEED_DECISION` only for a key decision, or `NEW_WINDOW` when context quality is declining. At roughly 60% context, prepare a clean phase boundary and handoff; above 70%, return `NEW_WINDOW` instead of waiting for saturation. A `NEW_WINDOW` response must preserve the project objective, completed work, current state, key files, tools/skills, and next action. The relay forwards it automatically.

Authorize Butler to reuse the current temporary-worker configuration. Let Claude decide whether delegation is economical; do not require a worker call or delegation report merely to prove compliance.

Do not impose fixed task time limits. `--status` is for explicit diagnostics; `--collect` reads the terminal event or performs the distant watchdog check. Long execution alone is not failure.
