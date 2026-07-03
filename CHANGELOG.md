# Changelog

## 0.2.1 - 2026-07-03

### 修复

- 自动识别 Claude Code 首次进入项目时的目录信任提示，并仅在默认信任选项存在时确认一次。
- 在注入长文本后短暂等待再提交，避免 Claude TUI 偶发吞掉 Enter。

## 0.2.0 - 2026-07-03

- 新 Goal 自动使用新 session，仅首次加载 `/butler`；同 session 续接只转发文本。
- 普通进度留在前台 TUI，仅以 `GOAL_DONE`、`NEED_DECISION`、`NEW_WINDOW` 触发关键节点接力。
- 明确临时工的经济性门槛：执行、审核与预期返工总成本更低，且结果可验证。
- 换窗后安全关闭被替换的 `butler-native-*` screen。
- 删除 `--native`、`--visible`、`--foreground`、`--adopt`；默认即真实交互式前台，后台仅保留显式 `--headless`。

## 0.1.0 - 2026-07-02

- 首个公开版本：文本转发、会话续接、状态监测、自动换窗与 Goal 验收闭环。
