# Codex 自动管家接力器

简称“管家接力器”或“接力器”。Codex 负责规划、关键决策与验收，前台 Claude Code 自动加载 `/butler` 执行，并由 Butler 将非核心任务尽可能派给可用的低成本临时工模型。

## 核心功能

1. 转发续接：Codex 把任务文本发送给 Claude，同一项目自动续接原 session。
2. 状态监测：查看 session、screen 及前台附着状态，不给长任务设置时间上限。
3. 保存换窗：Claude 回复首行 `NEW_WINDOW` 时，接力器把交接内容发送到新 session。

## 安装

要求：macOS、Python 3.10+、Claude Code、GNU screen，以及 Claude 中可用的 `/butler` Skill。

```bash
python3 -m pip install --user .
butler-relay --check
```

`--check` 会检查 macOS、Python、Claude Code 与 GNU screen 版本、Terminal、Claude transcript 目录及 `/butler` Skill。长任务没有时间上限；只有 Claude/screen 已退出，或本轮明确结束但无法匹配回复时，接力器才会报错停止等待。

将 `skills/butler-relay` 安装到 Codex Skills 后，可以直接说“打开接力器，把这个任务交给 Claude”。

## 使用

```bash
# 新开并显示真实 Claude TUI
butler-relay --native --new --project /path/to/project "任务文本"

# 继续同一项目的 Claude
butler-relay --native --project /path/to/project "下一阶段任务"

# 查看状态
butler-relay --status --project /path/to/project

# 后台模式
butler-relay --project /path/to/project "任务文本"
```

项目状态只写入目标项目根目录的 `.butler-relay.json`。Claude transcript 仍由 Claude Code 自己管理。

## 工作流原则

- 强模型负责规划、拆分、判断和验收。
- 低成本模型负责非核心执行，具体模型由 Butler 按本地可用配置选择。
- 接力器只负责流程连接，不复制模型配置，不替模型设计复杂状态机。
- 默认前台可见；已有 session 优先续接，需要时才新开窗口。

## 测试

```bash
python3 -m unittest -v tests/test_relay.py
```

## License

MIT
