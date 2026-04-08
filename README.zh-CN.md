# Duo

> ⚠️ **注意：** 中文版文档可能不是最新版本。最新内容请参阅 [README.md](README.md)。

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-845%20passed-brightgreen.svg)]()
[![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen.svg)]()

**Agent Orchestration Runtime — Commander 指挥，Executor 执行**

## 概述

Duo 是一个轻量级 Agent 编排运行时。Commander（Python CLI）通过文件协议指挥 Executor（Copilot CLI / Claude Code）完成复杂的编码任务。

核心能力：

- **多任务并行** — 每个任务独立 git worktree + tmux pane，互不干扰
- **自适应轮询** — 指数退避（5s → 120s），状态变更时立即重置
- **自动纠错** — 质量门禁失败自动重试，≥3 次升级给人类
- **安全边界** — 可写路径白名单、secret 泄漏检测、禁止命令
- **Crash 恢复** — Journal 回放重建状态，incarnation 机制隔离旧会话

## 架构

```
┌─────────────┐     file protocol      ┌──────────────┐
│  Commander   │◄──────────────────────►│   Executor   │
│  (duo CLI)   │   ack/heartbeat/result │ (Copilot CLI)│
│              │                        │              │
│  ┌─────────┐ │    tmux-bridge         │  ┌────────┐  │
│  │ Poller  │ │◄──────────────────────►│  │ Pane   │  │
│  │ Verifier│ │    send keys/read      │  │        │  │
│  └─────────┘ │                        │  └────────┘  │
└──────┬───────┘                        └──────────────┘
       │                                       │
       │  ┌──────────────────┐                 ▼
       │  │ Claude Commander │          /tmp/duo-worktrees/{id}/
       │  │ (可选, 独立面板)  │          └── (git worktree)
       │  │ 规划与代码审查    │              └── CLAUDE.md (自动生成)
       │  └──────────────────┘
       ▼
  ~/.duo/tasks/{id}/
  ├── task.json
  ├── journal.jsonl
  ├── heartbeat.json
  └── steps/step-NNNN/
      ├── ack-attempt-NN.json
      ├── result-attempt-NN.json
      └── prompt-attempt-NN.txt
```

Commander 通过文件协议与 Executor 通信：Executor 写入 ack/heartbeat/result 文件，Commander 轮询读取并驱动 FSM 状态机推进任务。tmux-bridge 负责底层的终端交互（发送 prompt、读取输出）。

**双执行器模式：** 当 `auto_claude_commander` 启用时（默认开启），`duo start` 会同时打开一个 Claude Code CLI 面板，包含自动生成的 `CLAUDE.md`（项目上下文、文件协议规范、工作流示例）。Claude Code 作为高层规划者，Copilot 负责执行具体任务。

## 安装

```bash
git clone https://github.com/user/duo.git && cd duo
bash install.sh
```

`install.sh` 会自动检测环境、安装 uv（如缺失）、同步依赖并安装 `duo` CLI。

需要：
- Python ≥ 3.12
- [uv](https://docs.astral.sh/uv/) 包管理器（install.sh 会自动安装）
- tmux + [smux](https://github.com/user/smux)（提供 tmux-bridge）
- [Copilot CLI](https://docs.github.com/en/copilot/github-copilot-in-the-cli) 或 Claude Code

## 快速开始

> **⚠️ 前提条件：** Duo 必须在 `tmux` 会话中运行。每个任务会分配独立的 tmux pane。

```bash
# 在 tmux session 中运行

# 1. 创建任务（自动创建 worktree + 启动 Copilot 会话）
duo start my-task --repo . --desc "实现用户认证模块"

# 2. 发送具体指令
duo send my-task "在 src/auth.py 中实现 JWT 认证，包含 login/logout/refresh"

# 3. 监控任务进度（自适应轮询）
duo monitor

# 4. 查看任务状态
duo status my-task

# 5. 任务完成后合并到主分支
duo merge my-task

# 其他常用命令
duo dashboard            # 实时仪表盘
duo version              # 查看版本
```

### 全部命令

| 命令 | 说明 |
|------|------|
| `duo init [--repo PATH]` | 初始化项目以使用 Duo |
| `duo doctor` | 检查环境依赖是否满足 |
| `duo start <name> --repo <path> --desc <text>` | 创建任务，初始化 worktree 和 Copilot 会话 |
| `duo send <name> <prompt>` | 向任务发送工作指令 |
| `duo stop <name>` | 优雅停止任务（保留 worktree 以便恢复） |
| `duo status [name]` | 查看单个任务或所有任务状态 |
| `duo list` | 表格形式列出所有任务（ID / STATUS / STEP / INCARNATION） |
| `duo stats [--json-output]` | 显示任务统计和摘要信息 |
| `duo monitor [names...]` | 启动自适应轮询监控（可指定任务，默认全部） |
| `duo watch [names...]` | 事件驱动的对话框处理器（自动审批权限提示） |
| `duo resume [NAME]` | 恢复中断的任务会话 |
| `duo recover` | 通过回放 journal 恢复中断的任务 |
| `duo merge <name>` | 将已完成任务的 worktree 合并到主分支（fetch + rebase + ff-only） |
| `duo diff <name>` | 显示任务 worktree 变更的 git diff |
| `duo kill <name>` | 终止任务，清理 worktree 和分支 |
| `duo retry <name>` | 重试失败或被阻塞的任务 |
| `duo batch <file> --repo <path>` | 从 JSON/YAML 文件批量创建任务 |
| `duo queue` | 查看并行队列状态（活跃/排队任务数） |
| `duo dashboard [names...] --refresh <sec>` | Rich 实时终端仪表盘（默认刷新间隔 2s） |
| `duo logs <name> [-n N] [--all]` | 查看任务事件流（默认最近 20 条） |
| `duo inspect <name>` | 查看任务详细信息；`--include-files` 显示变更文件和 diff 预览 |
| `duo export <name> --format json\|text\|jsonl [-o file]` | 导出任务报告；`jsonl` 为逐行 JSON 格式 |
| `duo cleanup [--all] [--force] [--keep-journal]` | 清理已完成/失败的任务（worktree + 状态目录） |
| `duo config list\|get\|set\|reset` | 配置管理（查看/修改/重置配置项） |
| `duo version` | 显示 Duo 版本号 |
| `duo completion SHELL` | 生成 Shell 补全脚本（bash/zsh/fish） |
| `duo audit [name]` | 查看 Premium Request 消耗审计（每任务或全局） |

### 全局选项

| 选项 | 说明 |
|------|------|
| `--verbose` | 启用详细输出，显示调试信息 |
| `--help` | 显示帮助信息 |

## 完整使用示例

一个端到端的工作流：

```bash
# 1. 安装
bash install.sh

# 2. 配置（可选）
duo config set copilot_model claude-sonnet-4-20250514
duo config list

# 3. 在 tmux 中创建任务
duo start auth-module --repo . --desc "实现用户认证模块"

# 4. 发送具体指令
duo send auth-module "在 src/auth.py 中实现 JWT 认证，包括 login/logout/refresh 端点"

# 5. 实时仪表盘监控
duo dashboard auth-module

# 6. 或者自适应轮询监控
duo monitor auth-module

# 7. 查看状态
duo status auth-module
duo list

# 8. 查看详情和事件流
duo inspect auth-module
duo logs auth-module -n 50

# 9. 导出任务报告
duo export auth-module --format json -o report.json
duo export auth-module --format text

# 10. 任务完成后合并
duo merge auth-module

# 11. 清理已完成的任务
duo cleanup --all --force

# 12. 清理单个失败的任务
duo kill failed-task

# 13. 查看版本
duo version
```

## 配置管理

配置文件位于 `~/.duo/config.json`，通过 `duo config` 子命令管理：

```bash
duo config list              # 查看所有配置
duo config get copilot_model # 查看单个配置
duo config set copilot_model claude-sonnet-4-20250514  # 修改配置
duo config reset             # 重置所有配置
duo config reset copilot_model  # 重置单个配置
```

### 可用配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `copilot_model` | `claude-opus-4.6` | Copilot 模型 |
| `max_corrections` | `3` | 最大纠错次数 |
| `heartbeat_timeout` | `90` | 心跳超时秒数 |
| `poll_base_interval` | `5.0` | 轮询基础间隔 |
| `poll_max_interval` | `120.0` | 轮询最大间隔 |
| `auto_allow_all` | `true` | 自动发送 /allow-all |
| `max_parallel` | `3` | 最大并行任务数 |
| `pr_budget` | `0` | 每任务最大 PR 消耗（0=无限制） |
| `auto_claude_commander` | `true` | `duo start` 时自动启动 Claude Code 面板 |
| `worktree_base_path` | `/tmp/duo-worktrees` | Git worktree 创建的基础路径 |

### Shell 补全

```bash
# Bash (~/.bashrc)
eval "$(duo completion bash)"

# Zsh (~/.zshrc)
eval "$(duo completion zsh)"

# Fish (~/.config/fish/config.fish)
duo completion fish | source
```

## 并行调度

Duo 支持最多 N 个任务并行执行（默认 3，可配置）。超出限制的任务自动进入 FIFO 队列。

```bash
# 配置并发数
duo config set max_parallel 5

# 批量创建任务
duo batch examples/tasks.json --repo .

# 查看队列状态
duo queue

# monitor 会自动在有空位时启动排队任务
duo monitor
```

## 调试

```bash
# 查看任务详情
duo inspect my-task

# 查看事件流
duo logs my-task
duo logs my-task -n 50     # 最近 50 条
duo logs my-task --all     # 全部

# 查看队列
duo queue

# 恢复中断的任务
duo recover
```

## 核心概念

### Task（任务）

一个独立的工作单元。每个 Task 拥有自己的：
- **Git worktree** — `/tmp/duo-worktrees/{name}/`，分支 `duo/{name}`
- **Tmux pane** — 运行 Copilot CLI 的独立终端
- **状态目录** — `~/.duo/tasks/{id}/`，包含 task.json、journal、heartbeat 等

### Step / Attempt（步骤 / 尝试）

任务分解为多个步骤（step），每个步骤可以有多次尝试（attempt）。质量门禁失败时自动递增 attempt 并重试，≥3 次失败升级为 ESCALATED 状态。

### Incarnation（会话标识）

8 字符十六进制 UUID，每次启动或重启会话时生成新的。用于隔离旧会话的过期数据 —— ack/heartbeat/result 文件必须携带匹配的 incarnation 才会被接受。

### File Protocol（文件通信协议）

Commander 和 Executor 之间通过文件系统通信，流程：

```
Commander 发送 prompt → Executor 写入 ack → Executor 写入 heartbeat（持续） → Executor 写入 result
```

- **ack** — Executor 确认收到 prompt（含 prompt_hash 校验）
- **heartbeat** — Executor 定期报告进度（当前文件、状态）
- **result** — Executor 报告完成（状态、变更文件、摘要）

所有写入使用原子操作（tmp + rename），避免读到半写文件。

### FSM（有限状态机）

Task 有 13 个状态，所有转换经过校验并记录到 journal：

```
CREATED → QUEUED（排队等待） / SESSION_STARTING（直接启动）
QUEUED → SESSION_STARTING（有空位时调度） / FAILED
SESSION_STARTING → PROMPT_SENT → ACKED → RUNNING → RESULT_REPORTED → VERIFYING
                                                                          │
                                      ┌───────────────────────────────────┘
                                      ▼
                                ┌─ COMPLETED（终态）
                                ├─ CORRECTING → 重试
                                ├─ BLOCKED → ESCALATED / 重试
                                └─ ESCALATED → 人工介入

FAILED → SESSION_STARTING（自动重启）
```

## 模块说明

| 模块 | 行数 | 职责 |
|------|------|------|
| `cli.py` | ~1310 | Click CLI 入口，17 个命令 + config 子命令，git worktree/branch 管理 |
| `protocol.py` | ~550 | FSM 状态机（13 状态） + 数据模型（dataclass） + 文件 I/O + journal |
| `commander.py` | ~640 | 编排大脑：prompt 构建、会话管理、轮询调度、纠错循环 |
| `config.py` | ~77 | 配置管理：持久化配置读写，类型自动转换，默认值 |
| `scheduler.py` | ~129 | 并行调度器：FIFO 队列、max_parallel 限流、自动出队 |
| `dashboard.py` | ~158 | Rich 实时仪表盘：任务状态表格、心跳进度、自动刷新 |
| `transport.py` | ~400 | tmux-bridge 封装，所有 tmux 交互的唯一入口 |
| `poller.py` | ~120 | 自适应轮询器，指数退避 + 心跳超时检测 |
| `verifier.py` | ~239 | 质量门禁：安全边界、secret 检测、未跟踪文件、验收测试 |

### protocol.py — 数据模型

```python
@dataclass
class Task:
    id: str
    description: str
    worktree: str
    branch: str
    base_commit: str
    pane_label: str
    incarnation_id: str        # 8-char hex, 每次重启更新
    status: TaskStatus
    current_step: int
    current_attempt: int
    subtasks: list[Subtask]
    security_policy: SecurityPolicy

@dataclass
class Subtask:
    step_id: int
    description: str
    target_files: list[str]    # 预期变更文件（软约束）
    writable_paths: list[str]  # 可写路径（硬约束，fnmatch）
    acceptance: str            # 验收命令

@dataclass
class SecurityPolicy:
    writable_paths: list[str]
    secret_patterns: list[str]  # ["API_KEY=", "password=", "token="]
    forbidden_commands: list[str]
    allow_network: bool
    require_human_approval: list[str]
```

### verifier.py — 质量门禁

验证按顺序执行，首个硬失败立即短路返回 Correction：

1. **安全边界检查**（硬） — 变更文件必须匹配 `writable_paths`（fnmatch）
2. **任务范围检查**（软） — 偏离 `target_files` 仅记录警告
3. **Secret 泄漏检测**（硬） — 扫描 diff 中新增行的敏感模式
4. **未跟踪文件检查**（硬） — `git ls-files --others` 必须为空
5. **验收测试**（硬） — 执行 `acceptance` 命令，exit code 必须为 0

### poller.py — 自适应轮询

```
初始间隔: 5s   →   指数退避 ×1.5   →   最大间隔: 120s
心跳超时: 90s（触发诊断 + 可能重启会话）
Grace period: prompt 发送后 90s 内不判超时
```

轮询结果：
- `RESULT_READY` — 结果文件就绪，推进到验证
- `WORKING` — 心跳正常，继续等待
- `HEARTBEAT_TIMEOUT` — 超时，检查进程存活状态
- `UNKNOWN` — 无心跳无 prompt，尝试重发

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DUO_COPILOT_MODEL` | `claude-opus-4.6` | 覆盖 config 中的 `copilot_model` |

## 安全

Duo 实施多层安全机制：

- **路径遍历防护** — 变更文件通过 `fnmatch` 与 `writable_paths` 白名单校验，超出声明范围的文件会被硬拒绝
- **标签净化** — Pane 标签和任务名通过严格正则校验（`^[a-zA-Z0-9_.-]+$`），防止 Shell 注入
- **Secret 检测** — 在接受结果前扫描 diff 中的敏感模式（`API_KEY=`、`password=`、`token=`）
- **PR 安全** — Premium Request 预算（`pr_budget`）限制每任务资源消耗
- **全程 `shell=False`** — 所有 `subprocess.run` 调用使用列表参数，命令通过 `shlex.split()` 拆分，防止 Shell 注入

## 开发

```bash
bash install.sh              # 安装
make check                   # 运行所有检查（lint + format + 类型检查 + 覆盖率）
make coverage                # 运行测试并检查覆盖率（fail_under=95）
make format                  # 使用 ruff 自动格式化
make lint                    # 使用 ruff 检查代码
make type-check              # 使用 mypy 进行严格类型检查
make test                    # 运行测试（pytest）
duo --help                   # 查看命令
```

### Pre-commit 设置

```bash
pip install pre-commit
pre-commit install
```

已配置的钩子：**ruff**（lint + fix）、**ruff-format**、**mypy**（严格类型检查）。

### 运行单个模块测试

```bash
python -m pytest tests/test_protocol.py -v
python -m pytest tests/test_verifier.py -v
python -m pytest tests/test_poller.py -v
python -m pytest tests/test_commander.py -v
python -m pytest tests/test_cli.py -v
python -m pytest tests/test_config.py -v
python -m pytest tests/test_scheduler.py -v
python -m pytest tests/test_dashboard.py -v
python -m pytest tests/test_transport.py -v
python -m pytest tests/test_integration.py -v
```

## 项目结构

```
duo/
├── pyproject.toml
├── README.md
├── README.zh-CN.md
├── CLAUDE.md
├── src/duo/
│   ├── __init__.py
│   ├── cli.py          # CLI 入口（17 个命令）
│   ├── config.py       # 配置管理
│   ├── protocol.py     # FSM + 数据模型 + 文件 I/O
│   ├── commander.py    # 编排逻辑
│   ├── scheduler.py    # 并行调度器
│   ├── dashboard.py    # Rich 实时仪表盘
│   ├── transport.py    # tmux-bridge 封装
│   ├── poller.py       # 自适应轮询
│   └── verifier.py     # 质量门禁
└── tests/
    ├── test_cli.py
    ├── test_protocol.py
    ├── test_commander.py
    ├── test_config.py
    ├── test_scheduler.py
    ├── test_dashboard.py
    ├── test_transport.py
    ├── test_poller.py
    └── test_verifier.py
```

## 贡献

请参阅 [CONTRIBUTING.md](CONTRIBUTING.md) 了解开发指南和 PR 流程。

## License

MIT
