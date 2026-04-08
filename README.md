# Duo

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-947%20passed-brightgreen.svg)]()
[![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen.svg)]()

**AI agent orchestrator that treats Premium Requests as a scarce resource.**

> [中文文档](README.zh-CN.md)

## The Problem

AI coding agents (Copilot CLI, Claude Code) are powerful but expensive. Every interaction costs a Premium Request. Running one unsupervised wastes PRs on approval dialogs, hallucinated retries, and runaway correction loops. Running multiple agents manually doesn't scale.

## How Duo Solves It

Duo splits the work into **Commander** (a Python CLI that makes decisions) and **Executor** (Copilot CLI that writes code), connected by a file-based protocol over tmux. Each task gets an isolated git worktree so agents work in parallel without conflicts. A 13-state FSM tracks every task from creation to merge, with automatic verification, correction budgets, and crash recovery via journal replay.

## Quickstart

```bash
git clone https://github.com/user/duo.git && cd duo
bash install.sh          # installs uv, syncs deps, sets up CLI

# Inside a tmux session:
cd your-project
duo init --repo .
duo start fix-auth --repo . --desc "Fix the login handler bug"
duo watch                # blocks until dialog → prints it → exits
duo ceo-approve fix-auth # approve the permission dialog
duo merge fix-auth       # fast-forward merge when done
```

## Architecture

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
    ├── task.json        ← FSM state
    ├── journal.jsonl    ← append-only event log
    └── steps/step-NNNN/
        ├── ack-attempt-NN.json
        ├── result-attempt-NN.json
        └── prompt-attempt-NN.txt
```

The Commander never touches code directly. It writes prompts, polls for results, runs verification gates, and advances the FSM. The Executor writes ack/heartbeat/result files. All file writes are atomic (tmp + fsync + rename).

## Core Concepts

**Task** — An isolated unit of work with its own git worktree (`/tmp/duo-worktrees/{id}/`), tmux pane, and state directory (`~/.duo/tasks/{id}/`).

**FSM** — 13 states from CREATED to COMPLETED. Happy path: `CREATED → SESSION_STARTING → PROMPT_SENT → ACKED → RUNNING → RESULT_REPORTED → VERIFYING → COMPLETED`. Failed verification loops through CORRECTING (up to 3 retries, then ESCALATED). Every transition is journal-logged.

**CEO Workflow** — The `ceo-*` commands let an orchestrating agent (the "CEO") interact with executor panes programmatically: wait for dialogs, read state as JSON, select options, approve permissions — all with safety guards.

## Common Commands

| Command | What it does |
|---------|-------------|
| `duo start <task> --repo . --desc "..."` | Create worktree + Copilot session |
| `duo send <task> "instruction"` | Send a prompt to a running task |
| `duo status <task>` | Show task FSM state |
| `duo watch` | Detect dialog → print → write signal file → exit |
| `duo ceo-approve <task>` | Smart-approve a permission dialog |
| `duo ceo-status <task>` | JSON state: `{"state":"dialog","options":5}` |
| `duo monitor` | Adaptive polling with auto-correction |
| `duo merge <task>` | Fast-forward merge to main branch |
| `duo dashboard` | Live Rich terminal dashboard |
| `duo cleanup --force` | Remove completed/failed tasks |

Run `duo --help` for the full command list (31 commands in 8 groups).

## More Resources

- **[Getting Started Guide](docs/getting-started.md)** — Complete walkthrough from install to merge
- **[Architecture Spec](docs/architecture.md)** — FSM states, file protocol schema, security model
- **[CEO Workflow](docs/ceo-workflow.md)** — Programmatic dialog handling for orchestrating agents

## Development

```bash
make check       # lint + format + type-check + coverage (fail_under=100)
make test        # pytest only
make coverage    # with coverage report
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT
