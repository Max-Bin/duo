# Duo

[![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)](https://github.com/Max-Bin/duo)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-1294%20passed-brightgreen.svg)]()
[![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen.svg)]()

**An orchestration runtime that treats Premium Requests as a scarce resource.**

> [中文文档](README.zh-CN.md)

## The Problem

AI coding agents are powerful but expensive — every interaction costs a Premium Request. Running an agent unsupervised wastes PRs on approval dialogs, hallucinated retries, and runaway correction loops. Running multiple agents manually doesn't scale past two or three concurrent tasks.

## How Duo Solves It

Duo splits the work into **Commander** (a Python CLI that makes decisions) and **Executor** (Copilot CLI that writes code), connected by a JSON file protocol over tmux. Each task gets an isolated git worktree so agents work in parallel without conflicts. A 13-state FSM tracks every task from creation to merge, with automatic verification, correction budgets, and crash recovery via journal replay.

## Architecture

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

## Quickstart

```bash
git clone https://github.com/Max-Bin/duo.git && cd duo
bash install.sh          # installs uv, syncs deps, sets up CLI
duo doctor               # verify tmux, git, Copilot CLI are available
```

```bash
# Inside a tmux session:
cd your-project
duo init --repo .
duo start fix-auth --repo . --desc "Fix the login handler bug"
duo watch                # blocks until dialog → prints it → exits
duo ceo-approve fix-auth # approve the permission dialog
duo merge fix-auth       # fast-forward merge when done
```

## Core Concepts

| Concept | Description |
|---------|-------------|
| **Task** | An isolated unit of work with its own git worktree, tmux pane, and state directory. |
| **FSM** | 13-state machine tracking each task from CREATED to COMPLETED, with CORRECTING loops (up to 3 retries) and ESCALATED for failures. |
| **CEO** | The `ceo-*` commands let an orchestrating agent handle permission dialogs, select options, and drive approval flows programmatically. |
| **Pane** | Each executor runs in a dedicated tmux pane. The Commander communicates via file reads/writes, never by typing into the terminal. |
| **Premium Request** | The billable unit of AI agent interaction. Duo's entire design minimizes how many PRs are spent per task. |

## Essential Commands

| Command | Description |
|---------|-------------|
| `duo start <task> --repo . --desc "..."` | Create worktree + executor session |
| `duo send <task> "instruction"` | Send a follow-up prompt to a running task |
| `duo stop <task>` | Gracefully stop a task and its pane |
| `duo status [task]` | Show FSM state for one or all tasks |
| `duo merge <task>` | Fast-forward merge the worktree into the target branch |
| `duo watch` | Block until a dialog appears, print it, then exit |
| `duo ceo-approve <task>` | Approve a permission dialog |
| `duo ceo-loop <task>` | Automated dialog handling with policy files |
| `duo think <name> --ask "question"` | Brainstorm with Claude Code before spending PRs |
| `duo cost` | Show Premium Request consumption across tasks |
| `duo doctor` | Check that all dependencies are installed |
| `duo bench` | Run performance benchmarks |

## Full Command Reference

```bash
duo --help               # 49 commands in 10 groups
```

## Examples

See the [`examples/`](examples/) directory for step-by-step walkthroughs:

- [Hello World](examples/01-hello-world/) — Your first duo task
- [Bug Fix with Thinking](examples/02-bug-fix/) — Plan before executing
- [Multi-Task Parallel](examples/03-multi-task/) — Run multiple agents simultaneously

## Documentation

- **[Getting Started Guide](docs/getting-started.md)** — Complete walkthrough from install to merge
- **[Architecture Spec](docs/architecture.md)** — FSM states, file protocol schema, security model
- **[CEO Workflow](docs/ceo-workflow.md)** — Programmatic dialog handling for orchestrating agents
- **[Thinking Design](docs/design-duo-think.md)** — Architecture and design for `duo think`

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, coding standards, and pull request guidelines.

## License

[MIT](LICENSE)
