---
name: butler-relay
description: Plan work in Codex and relay execution to a visible Claude Code session that automatically loads /butler. Use when the user says 打开接力器、启动管家接力器、交给 Claude、继续接力任务、新开 Claude 窗口、查看接力器状态, or wants Codex to plan and review while Claude/Butler delegates non-core execution to cheap temporary-worker models.
---

# 管家接力器

Use Codex for planning, key decisions, and acceptance. Use Claude Code with `/butler` for execution. Let Butler delegate non-core work to any available tested temporary-worker model; do not hard-code MiMo or DeepSeek.

## Workflow

1. Resolve the target project directory. Ask only when it cannot be inferred safely.
2. Run `butler-relay --check` before the first relay in the current environment. Report missing dependencies instead of silently changing models or modes.
3. Turn the user's goal into one concise task message containing the objective, relevant context, constraints, and acceptance criteria. Include context-health or handoff requirements in the message when relevant; let the models decide implementation details.
4. Start or resume the visible Claude session:

   ```bash
   butler-relay --native --project "/absolute/project/path" "任务文本"
   ```

   Add `--new` only when the user requests a fresh Claude window or the prior response requests `NEW_WINDOW`.
5. Treat Claude's returned text as an execution report. Verify important claims against project files or tests before approving the next stage.
6. Send the next decision or correction through the same command so the recorded session resumes automatically.

## Commands

```bash
butler-relay --check
butler-relay --status --project "/absolute/project/path"
butler-relay --native --new --project "/absolute/project/path" "任务文本"
butler-relay --native --project "/absolute/project/path" "下一阶段任务"
```

Prefer `--native` because it exposes the real Claude TUI to the user. Use background mode only when the user requests it.

## Handoff Contract

Tell Claude to begin its response with `NEW_WINDOW` when context quality is declining or a clean session is needed. Require the remaining response to preserve the project objective, completed work, current state, key files, tools/skills, and next action. The relay starts a new session and forwards that handoff automatically.

Do not impose fixed task time limits. Monitor liveness through `--status`; long execution alone is not failure.
