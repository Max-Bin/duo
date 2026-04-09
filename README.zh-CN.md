# Duo

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-1195%20passed-brightgreen.svg)]()
[![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen.svg)]()

**把 Premium Request 当稀缺资源管理的 AI agent 编排器。**

> [English](README.md)

## 痛点

AI 编程 agent（Copilot CLI、Claude Code）能力强，但贵。每次交互消耗一个 Premium Request。放任不管会浪费 PR：弹窗等审批、幻觉重试、失控的纠错循环。手动盯多个 agent 更不现实。

## Duo 怎么解决

Duo 将工作拆成 **Commander**（Python CLI，负责决策）和 **Executor**（Copilot CLI，负责写代码），通过 tmux 上的文件协议连接。每个任务有独立的 git worktree，多个 agent 并行互不干扰。13 状态 FSM 跟踪每个任务从创建到合并的全生命周期，内置自动验证、纠错预算和基于日志回放的崩溃恢复。

## 快速开始

```bash
git clone https://github.com/user/duo.git && cd duo
bash install.sh          # 安装 uv、同步依赖、配置 CLI

# 在 tmux 会话中：
cd your-project
duo init --repo .
duo start fix-auth --repo . --desc "修复登录处理器的 bug"
duo watch                # 阻塞等待弹窗 → 打印内容 → 退出
duo ceo-approve fix-auth # 批准权限弹窗
duo merge fix-auth       # 完成后 fast-forward 合并
```

## Thinking 工作流

在消耗 Premium Request 之前先头脑风暴和规划：

```bash
# 启动 thinking 会话（在 tmux pane 中生成 Claude Code）
duo think my-feature --ask "认证系统应该怎么架构？"

# 追问
duo think my-feature --ask "限流怎么处理？"

# 从对话生成计划
duo think my-feature --finalize

# 用计划启动编码任务（规划阶段零 PR 消耗）
duo start my-feature --from-thinking --repo .
```

Thinking 会话使用 Claude Code（你已有的订阅），在 `duo start` 之前不消耗 Copilot PR。

## 架构

```
  Commander (duo CLI)              Executor (Copilot CLI)
  ┌──────────────────┐             ┌──────────────────┐
  │ Scheduler        │  prompt.txt │                  │
  │ Poller ──────────│────────────►│  tmux pane       │
  │ Verifier         │◄────────────│  (git worktree)  │
  │ Watch            │  ack/result │                  │
  └────────┬─────────┘             └──────────────────┘
           │
    ~/.duo/tasks/{id}/
    ├── task.json        ← FSM 状态
    ├── journal.jsonl    ← 只追加事件日志
    └── steps/step-NNNN/
        ├── ack-attempt-NN.json
        ├── result-attempt-NN.json
        └── prompt-attempt-NN.txt
```

Commander 不直接碰代码。它写 prompt、轮询结果、跑验证门禁、推进 FSM。Executor 写 ack/heartbeat/result 文件。所有文件写入都是原子操作（临时文件 + fsync + rename）。

## 核心概念

**Task** — 一个隔离的工作单元，有独立的 git worktree（`/tmp/duo-worktrees/{id}/`）、tmux pane 和状态目录（`~/.duo/tasks/{id}/`）。

**FSM** — 从 CREATED 到 COMPLETED 共 13 个状态。正常路径：`CREATED → SESSION_STARTING → PROMPT_SENT → ACKED → RUNNING → RESULT_REPORTED → VERIFYING → COMPLETED`。验证失败走 CORRECTING 循环（最多 3 次，然后 ESCALATED）。每次状态转移都写入日志。

**CEO 工作流** — `ceo-*` 命令让编排 agent（"CEO"）程序化地与 executor pane 交互：等待弹窗、读取状态 JSON、选择选项、批准权限——全部带安全保护。

## 常用命令

| 命令 | 作用 |
|------|------|
| `duo start <task> --repo . --desc "..."` | 创建 worktree + Copilot 会话 |
| `duo think <name> --ask "问题"` | 用 Claude Code 在编码前头脑风暴 |
| `duo start <task> --from-thinking` | 从 thinking 计划启动任务 |
| `duo send <task> "指令"` | 给运行中的任务发送 prompt |
| `duo status <task>` | 查看任务 FSM 状态 |
| `duo watch` | 检测弹窗 → 打印 → 写信号文件 → 退出 |
| `duo ceo-approve <task>` | 智能批准权限弹窗 |
| `duo ceo-select <task> N` | 在弹窗中选择第 N 个选项 |
| `duo ceo-status <task>` | JSON 状态：`{"state":"dialog","options":5}` |
| `duo ceo-loop <task>` | 按策略文件自动处理弹窗 |
| `duo ceo-resume <task>` | 恢复暂停的 ceo-loop 并附加指令 |
| `duo monitor` | 自适应轮询 + 自动纠错 |
| `duo merge <task>` | Fast-forward 合并到主分支 |
| `duo dashboard` | Rich 实时终端面板 |
| `duo cleanup --force` | 清理已完成/失败的任务 |

运行 `duo --help` 查看完整命令列表（9 组 35 个命令）。

## 更多资源

- **[快速上手指南](docs/getting-started.md)** — 从安装到合并的完整走读
- **[架构规格](docs/architecture.md)** — FSM 状态、文件协议 schema、安全模型
- **[CEO 工作流](docs/ceo-workflow.md)** — 面向编排 agent 的弹窗处理命令参考
- **[Thinking 设计文档](docs/design-duo-think.md)** — `duo think` 的架构和设计

## 开发

```bash
make check       # lint + format + 类型检查 + 覆盖率（fail_under=100）
make test        # 仅 pytest
make coverage    # 含覆盖率报告
```

## 贡献

参见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

MIT
