# Duo

[![CI](https://github.com/Max-Bin/duo/actions/workflows/ci.yml/badge.svg)](https://github.com/Max-Bin/duo/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)](https://github.com/Max-Bin/duo)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-2454%20passed-brightgreen.svg)]()
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
# The simplest way — one command does everything:
tmux new -s work         # start tmux (if not already inside)
cd your-project
duo go                   # sets up CEO (Claude Code) + Executor (Copilot)
# Now just chat with Claude Code about what you want to build!
```

<details>
<summary>Manual step-by-step (advanced)</summary>

```bash
# Inside a tmux session:
cd your-project
duo init --repo .
duo start fix-auth --repo . --desc "Fix the login handler bug"
duo send fix-auth "Fix the login handler bug"  # sends first prompt (defer mode)
duo watch                # blocks until dialog → prints it → exits
duo ceo-approve fix-auth # approve the permission dialog
duo merge fix-auth       # fast-forward merge when done
```
</details>

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
| `duo go` | **One-command setup** — CEO (Claude Code) + Executor (Copilot) side by side |
| `duo start <task> --repo . --desc "..."` | Create worktree + executor session |
| `duo send <task> "instruction"` | Send a follow-up prompt to a running task |
| `duo send <task> -f prompt.txt` | Send prompt from a file |
| `duo stop <task>` | Gracefully stop a task and its pane |
| `duo stop --all` | Stop all active tasks at once |
| `duo status [task]` | Show FSM state for one or all tasks |
| `duo merge <task>` | Fast-forward merge the worktree into the target branch |
| `duo watch` | Block until a dialog appears, print it, then exit |
| `duo ceo-approve <task>` | Approve a permission dialog |
| `duo ceo-loop <task>` | Automated dialog handling with policy files |
| `duo think <name> --ask "question"` | Brainstorm with Claude Code before spending PRs |
| `duo cost` | Show Premium Request consumption across tasks |
| `duo doctor` | Check that all dependencies are installed |
| `duo config list` | Show all configuration values |
| `duo config set <key> <value>` | Set a configuration value |
| `duo config edit` | Open config in $EDITOR |
| `duo config validate` | Check config file for errors |
| `duo list` | List all tasks and their statuses |
| `duo list --sort age` | Sort tasks by age, status, or name |
| `duo list -q --status running` | Print only running task IDs (for scripting) |
| `duo list --recent 5` | Show only the 5 most recently created tasks |
| `duo diff <task>` | Show git diff for a task's worktree |
| `duo bench` | Run performance benchmarks |

## Full Command Reference

```bash
duo --help               # 52 commands in 10 groups
```

### Scripting Helpers

```bash
duo start my-task --repo . -q          # print only the task name
duo send my-task "instruction" -q      # print 'sent' or 'queued'
duo list -q --status running          # one task ID per line — pipe-friendly
duo list -c --status completed        # print count (for conditionals)
duo list --wide                       # include description column
duo status my-task -q                 # print just the status value (e.g. "running")
duo inspect my-task -q                # print just the status value
duo cost -q                           # print total PR count only
duo stats -q                          # print total task count only
duo audit -q                          # print total PR consumed only
duo audit my-task -q                  # print task-specific PR count
duo doctor -q                         # print only failures (empty = all pass)
duo recover -q                        # print recovered count only
duo resume my-task -q                 # print resumed count only
duo logs my-task -q                   # one event type per line (pipe to sort | uniq -c)
duo cleanup --force -q                # print cleaned count only
duo diff my-task -q                   # print changed file count only
duo retry my-task -q                  # print new status value only
duo stop my-task -q                   # print 'stopped' or current status
duo kill my-task -q                   # print only task name
duo merge my-task -q                  # print merged branch name only
duo merge my-task --dry-run -q        # print changed file count only
duo queue -q                          # print queue length only
duo config get copilot_model -q       # print raw value (for shell vars)
duo export my-task -q                 # print event count only
duo list --finished                   # only completed/failed/escalated
duo list --active -c                  # count active tasks
duo status my-task --wait completed   # block until status reached
duo status my-task --wait running --timeout 60  # with timeout
duo logs my-task -c                   # print event count (works with --filter)
duo logs my-task -c --filter error    # count error events
duo cleanup --dry-run --age 7d        # preview stale tasks without deleting
duo send my-task -f prompt.txt        # read prompt from file
duo merge my-task --dry-run           # preview merge (commits, files changed)

# Aliases for common commands
duo ls                                # → duo list
duo st my-task                        # → duo status my-task
duo log my-task                       # → duo logs my-task
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
