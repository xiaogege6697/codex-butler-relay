---
name: butler-relay
description: Run an asynchronous cross-model Goal Loop in which Codex sets the goal, Claude executes in a visible TUI, and a same-thread heartbeat checks after 10 minutes with doubled intervals until final Codex acceptance. Use when the user says 打开接力器、启动管家接力器、交给 Claude、做好后直接给我成果、继续接力任务、新开 Claude 窗口、查看接力器状态, or wants one-sentence delegation without frequent polling or progress token use.
---

# 管家接力器

Use Codex for planning, key decisions, and acceptance. Use Claude Code with `/butler` for execution. Let Butler delegate non-core work to any available tested temporary-worker model; do not hard-code MiMo or DeepSeek.

## Goal Loop

1. Use the current workspace or an explicit user path as the project directory. Never search memory or scan the home directory merely to guess a project; ask once if neither is safe.
2. Run `butler-relay --check` only after first installation or evidence of an environment failure, not before every Goal.
3. Forward a concise goal containing only the objective and any user-stated boundary or acceptance criterion. Do not pre-plan Claude's subtasks, tools, worker routing, or reporting format.
4. Start a fresh visible Goal Loop asynchronously. A new Goal gets a new Claude session and loads `/butler` once:

   ```bash
   butler-relay --detach --goal --project "/absolute/project/path" "目标与验收标准"
   ```

5. After `GOAL_STARTED`, create a Codex App heartbeat attached to the current thread for the first check in 10 minutes, then tell the user once that Claude is running and end the current Codex turn. Do not keep a blocking tool call alive.
6. Each heartbeat calls exactly once:

   ```bash
   butler-relay --collect --project "/absolute/project/path"
   ```

   - `GOAL_RUNNING` with `NEXT_CHECK_MINUTES=N`: update the same heartbeat to run after N minutes, then end silently. Do not read files, inspect screen/transcripts, or narrate progress. Relay doubles N after every incomplete check: 10 → 20 → 40 → 80 → ...
   - `GOAL_DONE`: inspect real files and run proportional tests. Never accept Claude's claim without verification.
   - `NEED_DECISION`: decide in Codex when the existing goal authorizes it. Ask the user only for a genuinely material choice, then relay the answer.
   - `RELAY_FAILED` or a protocol error: inspect the saved error once and report the concrete failure.
7. On any terminal result, pause or delete that Goal's heartbeat before continuing. If acceptance fails, send exact defects with `--detach`, reset monitoring to 10 minutes, and end the turn again. When acceptance passes, run `butler-relay --accept --project ...`.

If Codex App heartbeat automation is unavailable, the macOS notification plus manual `--collect` is the fallback, not the normal path.

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

Do not impose fixed task time limits. `--status` is for explicit diagnostics; `--collect` is the normal terminal-result path. Long execution alone is not failure.
