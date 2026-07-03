# Codex 自动管家接力器

简称“管家接力器”或“接力器”。它实现一个轻量、跨模型的 Goal Loop：Codex 负责规划、关键决策与真实验收；前台 Claude Code 负责项目管理、执行与审核；Butler 只在经济上划算且结果可验证时，把非核心工作派给可用的低成本临时工模型。

## 核心功能

1. 转发续接：Codex 把任务文本发送给 Claude，同一项目自动续接原 session。
2. 状态监测：查看 session、screen 及前台附着状态，不给长任务设置时间上限。
3. 保存换窗：Claude 回复首行 `NEW_WINDOW` 时，接力器把交接内容发送到新 session，并关闭被替换的接力器 screen。
4. Goal 闭环：`GOAL_DONE` 后由 Codex 验收，`NEED_DECISION` 交回 Codex，验收通过后标记 `accepted`。

## 安装

要求：macOS、Python 3.10+、Claude Code、GNU screen，以及 Claude 中可用的 `/butler` Skill。

```bash
python3 -m pip install --user .
butler-relay --check
```

`--check` 会检查 macOS、Python、Claude Code 与 GNU screen 版本、Terminal、Claude transcript 目录及 `/butler` Skill。长任务没有时间上限；只有 Claude/screen 已退出，或本轮明确结束但无法匹配回复时，接力器才会报错停止等待。

Claude 首次进入某个项目目录时，接力器会识别目录信任提示，并在默认选中“信任此目录”时自动确认，然后继续加载真实 TUI。

将 `skills/butler-relay` 安装到 Codex Skills 后，可以直接说“打开接力器，把这个任务交给 Claude”。

## 使用

```bash
# 启动新 Goal：自动新开并显示真实 Claude TUI
butler-relay --goal --project /path/to/project "目标与验收标准"

# 继续同一项目的 Claude（不会再次加载 /butler）
butler-relay --project /path/to/project "下一阶段任务"

# 查看状态
butler-relay --status --project /path/to/project

# Codex 验收通过
butler-relay --accept --project /path/to/project

# 显式后台模式
butler-relay --headless --project /path/to/project "任务文本"
```

项目状态只写入目标项目根目录的 `.butler-relay.json`。Claude transcript 仍由 Claude Code 自己管理。

## 工作流原则

- Codex 负责目标、关键决策和最终验收；Claude 负责拆分、项目执行与初审。
- 临时工不限模型。只有“临时工执行 + Claude 审核 + 预期返工”比 Claude 直接完成更便宜，且结果可验证时才外包。
- 接力器只负责流程连接，不复制模型配置，不替模型设计复杂状态机。
- 默认前台可见；普通进度留在 Claude TUI，只有 `GOAL_DONE`、`NEED_DECISION`、`NEW_WINDOW` 回到 Codex。
- 新 session 按手动方式启动 Claude TUI 并加载一次 `/butler`；同 session 续接只转发文本，权限沿用 Claude 本地配置。

## 设计边界

接力器不做模型路由、任务拆分、数据库、Dashboard 或复杂状态机。跨模型能力来自 Codex、Claude 与可替换临时工的协作协议，而不是在 Relay 内堆 provider adapter。

## 测试

```bash
python3 -m unittest -v tests/test_relay.py
```

## License

MIT
