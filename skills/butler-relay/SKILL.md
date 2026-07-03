---
name: butler-relay
description: Run a lightweight cross-model Goal Loop in which Codex plans and verifies while a visible Claude Code session loads /butler and delegates non-core execution to cheap temporary-worker models. Use when the user says 打开接力器、启动管家接力器、交给 Claude、盯着 Claude 干活、做好后直接给我成果、继续接力任务、新开 Claude 窗口、查看接力器状态, or wants one-sentence delegation with automatic execution, continuation, decisions, and acceptance.
---

# 管家接力器

Use Codex for planning, key decisions, and acceptance. Use Claude Code with `/butler` for execution. Let Butler delegate non-core work to any available tested temporary-worker model; do not hard-code MiMo or DeepSeek.

## Goal Loop

1. Resolve the target project directory. Ask only when it cannot be inferred safely.
2. Run `butler-relay --check` before the first relay in the current environment. Report missing dependencies instead of silently changing models or modes.
3. Turn the user's sentence into one concise goal containing the objective, relevant context, constraints, and verifiable acceptance criteria. Make reasonable implementation decisions without asking the user.
4. Start a fresh visible Goal Loop. A new Goal always gets a new Claude session and loads `/butler` once. The relay automatically confirms Claude's first-use directory trust prompt only when the expected trust option is detected:

   ```bash
   butler-relay --goal --project "/absolute/project/path" "目标与验收标准"
   ```

5. Claude should keep ordinary progress inside the visible TUI and continue working. Interpret the first nonblank line only when its turn returns:
   - `GOAL_DONE`: inspect real files and run proportional tests. Never accept Claude's claim without verification.
   - `NEED_DECISION`: decide in Codex when the existing goal authorizes it. Ask the user only for a genuinely material choice, then relay the answer.
   - `NEW_WINDOW`: let the relay transfer the handoff automatically.
   - Anything else: send one concise protocol correction through the same session. If it happens again, report the protocol failure instead of creating an infinite relay loop.
6. If acceptance fails, send the exact defects back through the same session. Continuations must not reload `/butler`. Allow at most three consecutive repair rounds; do not impose a task runtime limit.
7. When acceptance passes, run `butler-relay --accept --project ...` and return the verified outcome to the user.

## Commands

```bash
butler-relay --check
butler-relay --status --project "/absolute/project/path"
butler-relay --goal --project "/absolute/project/path" "目标与验收标准"
butler-relay --project "/absolute/project/path" "下一阶段任务"
butler-relay --accept --project "/absolute/project/path"
```

The real interactive Claude TUI is the default. Use `--headless` only when the user explicitly requests background operation.

## Handoff Contract

Claude must not end a turn merely to report normal progress. It begins a terminal response with `GOAL_DONE` only when the acceptance criteria are met, `NEED_DECISION` only for a key decision, or `NEW_WINDOW` when context quality is declining. At roughly 60% context, prepare a clean phase boundary and handoff; above 70%, return `NEW_WINDOW` instead of waiting for saturation. A `NEW_WINDOW` response must preserve the project objective, completed work, current state, key files, tools/skills, and next action. The relay forwards it automatically.

Authorize Butler to reuse and test the current temporary-worker configuration without asking when it works. Delegate only when worker execution plus Claude review plus expected rework costs less than Claude doing the task directly, and the output is objectively verifiable.

Do not impose fixed task time limits. Monitor liveness through `--status`; long execution alone is not failure.
