# Relay Contract

本文档记录 `codex-butler-relay` 的稳定接力契约。它面向维护者、Codex Skill 作者和想审计这个工具是否可靠的人。

Relay 的项目身份不是“调度更多模型”，而是用很少的状态和检查成本，把同一个 Goal 从 Codex 交给 Claude/Butler，再把终态交回 Codex 验收。

## 最小拓扑

```text
User Goal
  -> Codex boundary clarification
  -> Relay delivery contract
  -> Claude/Butler execution
  -> terminal signal
  -> Codex evidence-based acceptance
```

Relay 只固定中间契约，不固定 Claude 如何拆任务、是否调用临时工、使用哪些项目内工具。

## Stable Inputs

Relay 接受两类输入：

| 输入 | 来源 | 契约 |
|---|---|---|
| project | 当前工作目录或用户明确路径 | 必须是存在的目录；状态只写入该目录 |
| text | 用户目标或下一阶段指令 | 新Goal包装为`goal-capsule-v1`，只包含目标、明确边界、验收标准、项目锚点和终态信号 |

新 Goal 必须使用 `--goal`，它会启动新 session 并只在首次加载 `/butler`。后续续接不重复加载 Butler。

## State File

目标项目根目录只保存一个状态文件：

```text
.butler-relay.json
```

当前稳定字段如下：

| 字段 | 含义 | 谁写入 |
|---|---|---|
| goal | 原始用户目标 | `--goal` 或 detached worker |
| goal_id | 当前Goal唯一身份 | `--goal` |
| goal_status | 接力状态 | Relay |
| last_signal | 最近一次识别到的信号 | Relay |
| session_id | Claude session id | Relay |
| screen_name | Relay 管理的 screen 名称 | Relay |
| mode | `interactive` 或 `headless` | Relay |
| watchdog_minutes | 丢失终态事件时的远期检查间隔 | `--detach` |
| result_path | detached worker 结果文件 | `--detach` |
| event_path | `butler-event-v1`终态事件文件 | `--detach` |
| log_path | detached worker 日志文件 | `--detach` |
| finished_at | worker 结束时间 | detached worker |

维护者可以新增字段，但不应改变现有字段的含义。未知字段应保持可忽略。

## Goal Status

| 状态 | 含义 | 下一步 |
|---|---|---|
| starting | detached worker 已创建，还未进入执行 | 本地等待终态事件 |
| running | Claude 正在执行或续接执行 | 不向Codex传递中间态 |
| switching_window | Claude 要求换窗，Relay 正在交接 | 本地继续等待终态事件 |
| awaiting_acceptance | Claude 返回 `GOAL_DONE` | Codex 检查文件、测试和用户可见结果 |
| needs_decision | Claude 返回 `NEED_DECISION` | Codex 决策，必要时询问用户 |
| protocol_error | Claude 已结束但未给出终态信号 | Codex 检查结果并发送纠正指令 |
| failed | Relay 或 Claude 调用异常退出 | 查看 `log_path` 和 `result_path` |
| accepted | Codex 验收通过 | Goal Loop 结束 |

`GOAL_DONE` 不是完成证据，只是验收入口。只有 Codex 验证真实结果并运行比例适当的检查后，才可以执行 `--accept`。

## Terminal Signals

Claude/Butler 只能用以下终态信号结束接力：

| 信号 | 触发条件 | Relay 行为 |
|---|---|---|
| GOAL_DONE | 已满足目标和验收标准 | 状态转为 `awaiting_acceptance` |
| NEED_DECISION | 需要关键取舍且目标授权不足 | 状态转为 `needs_decision` |
| NEW_WINDOW | context 质量下降，需要新窗口继续 | Relay 携带交接内容新开 session |

Relay 会识别前 5 个非空行内的独立信号，也接受最后一行以信号开头并跟随摘要。正文里提到 `GOAL_DONE` 不会被当成终态。

## Event and Watchdog Contract

正常路径由detached worker在终态原子写入`butler-event-v1`。事件只包含`goal_id`、signal、项目路径、结果路径、SHA-256与完成时间；完整结果不重复搬运。

当前Codex App没有供独立进程调用的稳定thread callback，因此Relay同时发送macOS通知。`--collect`只在用户返回、收到通知或远期watchdog时读取终态事件并验证`goal_id`、路径和摘要。

首次watchdog为6小时。若仍运行，返回：

```text
GOAL_RUNNING
NEXT_WATCHDOG_MINUTES=1440
```

运行态返回幂等，不因重复调用继续放大间隔。运行中不应读取项目、不检查screen、不向用户复述普通进度。

终态时返回signal和最小事件。Codex按结果指针读取真实成果，随后做验收、决策或故障处理。

## Recovery Rules

1. 如果`--collect`返回`GOAL_RUNNING`，只把同一watchdog移到24小时后。
2. 如果返回 `GOAL_DONE`，先验证真实文件和测试，再 `--accept`。
3. 如果返回 `NEED_DECISION`，优先由 Codex 在原目标边界内决策；只有影响用户意图时再询问用户。
4. 如果返回`RELAY_FAILED`或事件摘要校验失败，读取一次`log_path`与`result_path`，定位具体失败。
5. 如果 screen 丢失但没有终态，不伪造完成；发送明确缺陷或重启新的 Goal。
6. 只允许关闭 `butler-native-*` 命名的 screen，避免误杀用户会话。

## Non-goals

Relay 不做这些事：

- 不拆解用户任务；
- 不规定 Claude 的工具选择；
- 不要求必须调用临时工；
- 不维护数据库、Dashboard 或成本报表；
- 不把运行时间本身视为失败；
- 不替代 Codex 的最终验收。

这些边界保护了项目的轻量性：Relay 是接力窄腰，不是第二个项目管理系统。
