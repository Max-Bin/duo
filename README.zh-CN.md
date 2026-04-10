# Duo

[![CI](https://github.com/Max-Bin/duo/actions/workflows/ci.yml/badge.svg)](https://github.com/Max-Bin/duo/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)](https://github.com/Max-Bin/duo)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-2409%20passed-brightgreen.svg)]()
[![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen.svg)]()

**把 Premium Request 当稀缺资源管理的 AI Agent 编排运行时。**

> [English](README.md)

## 痛点

AI 编程 agent 能力强，但每次交互都要消耗一个 Premium Request，用不好就是在烧钱。放任 agent 跑，PR 浪费在弹窗等审批、幻觉重试、失控的纠错循环上。想同时盯几个 agent？手动管根本不现实。

## Duo 怎么解决

Duo 把工作拆成 **Commander**（Python CLI，负责调度和决策）和 **Executor**（Copilot CLI，负责写代码），用 JSON 文件协议经 tmux 通信。每个任务有独立的 git worktree，多路并行互不干扰。13 状态 FSM 跟踪任务全生命周期，内置自动验证、纠错预算和日志回放崩溃恢复。

## 架构

```
┌─────────────────────────────────────────────────────┐
│                    Commander (Python CLI)            │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │ Scheduler│  │ Verifier │  │ CEO (auto-dialog)│  │
│  └──────────┘  └──────────┘  └──────────────────┘  │
└───────────────────────┬─────────────────────────────┘
                        │ File Protocol (JSON + tmux)
┌───────────────────────┴─────────────────────────────┐
│                   Executor (Copilot CLI)             │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │ Worktree │  │ Pane     │  │ ack/result files  │  │
│  └──────────┘  └──────────┘  └──────────────────┘  │
└─────────────────────────────────────────────────────┘
```

## 快速开始

```bash
git clone https://github.com/Max-Bin/duo.git && cd duo
bash install.sh          # 安装 uv、同步依赖、配置 CLI
duo doctor               # 检查 tmux、git、Copilot CLI 是否就绪
```

```bash
# 最简方式 — 一条命令搞定一切：
tmux new -s work         # 启动 tmux（如果还没在 tmux 里）
cd your-project
duo go                   # 自动设置 CEO (Claude Code) + Executor (Copilot)
# 直接和 Claude Code 聊你想做什么！
```

<details>
<summary>手动步骤（进阶）</summary>

```bash
# 在 tmux 会话中：
cd your-project
duo init --repo .
duo start fix-auth --repo . --desc "修复登录处理器的 bug"
duo send fix-auth "修复登录处理器的 bug"  # 发送第一条 prompt（延迟模式）
duo watch                # 阻塞等待弹窗 → 打印内容 → 退出
duo ceo-approve fix-auth # 批准权限弹窗
duo merge fix-auth       # 完成后 fast-forward 合并
```
</details>

## 核心概念

| 概念 | 说明 |
|------|------|
| **Task** | 隔离的工作单元，拥有独立 git worktree、tmux pane 和状态目录。 |
| **FSM** | 13 状态机，跟踪任务从 CREATED 到 COMPLETED 的全过程。验证失败进入 CORRECTING 循环（最多 3 次），超限则 ESCALATED。 |
| **CEO** | `ceo-*` 命令让编排 agent 程序化地处理权限弹窗、选择选项、驱动审批流程。 |
| **Pane** | 每个 executor 运行在独立的 tmux pane 中，Commander 通过文件读写通信，从不直接操作终端。 |
| **Premium Request** | AI agent 交互的计费单位。Duo 的整体设计目标就是让每个任务消耗最少的 PR。 |

## 常用命令

| 命令 | 说明 |
|------|------|
| `duo go` | **一键启动** — CEO (Claude Code) + Executor (Copilot) 并排工作 |
| `duo start <task> --repo . --desc "..."` | 创建 worktree + executor 会话 |
| `duo send <task> "指令"` | 给运行中的任务发送后续 prompt |
| `duo stop <task>` | 优雅停止任务及其 pane |
| `duo status [task]` | 查看单个或全部任务的 FSM 状态 |
| `duo merge <task>` | 将 worktree fast-forward 合并到目标分支 |
| `duo watch` | 阻塞等待弹窗出现，打印后退出 |
| `duo ceo-approve <task>` | 批准权限弹窗 |
| `duo ceo-loop <task>` | 按策略文件自动处理弹窗 |
| `duo think <name> --ask "问题"` | 用 Claude Code 在花 PR 之前先头脑风暴 |
| `duo cost` | 查看各任务的 Premium Request 消耗 |
| `duo doctor` | 检查所有依赖是否已安装 |
| `duo bench` | 运行性能基准测试 |

## 完整命令参考

```bash
duo --help               # 10 组共 57 个命令
```

## 示例

参见 [`examples/`](examples/) 目录，包含分步操作指南：

- [Hello World](examples/01-hello-world/) — 你的第一个 duo 任务
- [Bug Fix with Thinking](examples/02-bug-fix/) — 先规划再执行
- [Multi-Task Parallel](examples/03-multi-task/) — 同时运行多个 agent

## 文档

- **[快速上手指南](docs/getting-started.md)** — 从安装到合并的完整走读
- **[架构规格](docs/architecture.md)** — FSM 状态、文件协议 schema、安全模型
- **[CEO 工作流](docs/ceo-workflow.md)** — 面向编排 agent 的弹窗处理命令参考
- **[Thinking 设计文档](docs/design-duo-think.md)** — `duo think` 的架构和设计

## 贡献

参见 [CONTRIBUTING.md](CONTRIBUTING.md)，了解开发环境搭建、编码规范和 PR 提交指南。

## 许可证

[MIT](LICENSE)
