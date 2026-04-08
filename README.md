# Duo

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-774%20passed-brightgreen.svg)]()
[![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen.svg)]()

**Agent Orchestration Runtime — Commander directs, Executor delivers**

> [中文文档](README.zh-CN.md)

## Overview

Duo is a lightweight agent orchestration runtime. The Commander (Python CLI) coordinates the Executor (Copilot CLI / Claude Code) via a file-based protocol to accomplish complex coding tasks.

Key capabilities:

- **Parallel task execution** — Each task gets its own git worktree + tmux pane, fully isolated
- **Adaptive polling** — Exponential backoff (5s → 120s), resets immediately on state changes
- **Auto-correction** — Automatic retries on quality gate failures; escalates to a human after ≥3 attempts
- **Security boundaries** — Writable path allowlists, secret leak detection, forbidden commands
- **Crash recovery** — Journal replay rebuilds state; incarnation IDs isolate stale sessions

## Architecture

```
┌─────────────┐     file protocol      ┌──────────────┐
│  Commander   │◄──────────────────────►│   Executor   │
│  (duo CLI)   │   ack/heartbeat/result │ (Copilot CLI)│
│              │                        │              │
│  ┌─────────┐ │    tmux-bridge         │  ┌────────┐  │
│  │ Poller  │ │◄──────────────────────►│  │ Pane   │  │
│  │ Verifier│ │    send keys/read      │  │        │  │
│  └─────────┘ │                        │  └────────┘  │
└─────────────┘                        └──────────────┘
       │                                       │
       ▼                                       ▼
  ~/.duo/tasks/{id}/                    /tmp/duo-worktrees/{id}/
  ├── task.json                        └── (git worktree)
  ├── journal.jsonl
  ├── heartbeat.json
  └── steps/step-NNNN/
      ├── ack-attempt-NN.json
      ├── result-attempt-NN.json
      └── prompt-attempt-NN.txt
```

Commander communicates with the Executor through a file-based protocol: the Executor writes ack/heartbeat/result files, and the Commander polls these files to drive the FSM forward. The tmux-bridge handles low-level terminal interaction (sending prompts, reading output).

## Installation

```bash
git clone https://github.com/user/duo.git && cd duo
bash install.sh
```

`install.sh` automatically detects your environment, installs uv (if missing), syncs dependencies, and sets up the `duo` CLI.

Prerequisites:
- Python ≥ 3.12
- [uv](https://docs.astral.sh/uv/) package manager (auto-installed by install.sh)
- tmux + [smux](https://github.com/user/smux) (provides tmux-bridge)
- [Copilot CLI](https://docs.github.com/en/copilot/github-copilot-in-the-cli) or Claude Code

## Quick Start

> **⚠️ Requirement:** Duo must run inside a `tmux` session. Each task gets its own tmux pane.

```bash
# Run inside a tmux session

# 1. Create a task (auto-creates worktree + starts Copilot session)
duo start my-task --repo . --desc "Implement user auth module"

# 2. Send a specific instruction
duo send my-task "Implement JWT auth in src/auth.py with login/logout/refresh"

# 3. Monitor task progress (adaptive polling)
duo monitor

# 4. Check task status
duo status my-task

# 5. Merge into the main branch when done
duo merge my-task

# Other common commands
duo dashboard            # Live dashboard
duo version              # Show version
```

### All Commands

| Command | Description |
|---------|-------------|
| `duo init [--repo PATH]` | Initialize a project for Duo |
| `duo doctor` | Check environment dependencies |
| `duo start <name> --repo <path> --desc <text>` | Create a task, initialize worktree and Copilot session |
| `duo send <name> <prompt>` | Send a work instruction to a task |
| `duo stop <name>` | Stop a task gracefully (preserves worktree for resume) |
| `duo status [name]` | Show status for a single task or all tasks |
| `duo list` | List all tasks in a table (ID / STATUS / STEP / INCARNATION) |
| `duo stats [--json-output]` | Show task statistics and summary |
| `duo monitor [names...]` | Start adaptive polling monitor (specify tasks, or default to all) |
| `duo resume [NAME]` | Resume interrupted task sessions |
| `duo recover` | Recover interrupted tasks by replaying journals |
| `duo merge <name>` | Merge a completed task's worktree into the main branch (fetch + rebase + ff-only) |
| `duo diff <name>` | Show git diff for a task's worktree changes |
| `duo kill <name>` | Terminate a task and clean up its worktree and branch |
| `duo retry <name>` | Retry a failed or blocked task |
| `duo batch <file> --repo <path>` | Batch-create tasks from a JSON/YAML file |
| `duo queue` | Show parallel queue status (active / queued task counts) |
| `duo dashboard [names...] --refresh <sec>` | Rich live terminal dashboard (default refresh: 2s) |
| `duo logs <name> [-n N] [--all]` | View the task event stream (default: last 20 entries) |
| `duo inspect <name>` | View detailed task info; `--include-files` shows changed files and diff preview |
| `duo export <name> --format json\|text\|jsonl [-o file]` | Export a task report; `jsonl` for line-delimited JSON |
| `duo cleanup [--all] [--force] [--keep-journal]` | Clean up completed/failed tasks (worktree + state directory) |
| `duo config list\|get\|set\|reset` | Manage configuration (view / modify / reset settings) |
| `duo version` | Show the Duo version |
| `duo completion SHELL` | Generate shell completion (bash/zsh/fish) |
| `duo audit [name]` | View Premium Request usage audit (per-task or global) |

### Global Options

| Option | Description |
|--------|-------------|
| `--verbose` | Enable verbose output with debug information |
| `--help` | Show help message |

## Full Walkthrough

An end-to-end workflow:

```bash
# 1. Install
bash install.sh

# 2. Configure (optional)
duo config set copilot_model claude-sonnet-4-20250514
duo config list

# 3. Create a task inside tmux
duo start auth-module --repo . --desc "Implement user auth module"

# 4. Send a specific instruction
duo send auth-module "Implement JWT auth in src/auth.py with login/logout/refresh endpoints"

# 5. Watch progress on the live dashboard
duo dashboard auth-module

# 6. Or use adaptive polling
duo monitor auth-module

# 7. Check status
duo status auth-module
duo list

# 8. View details and event stream
duo inspect auth-module
duo logs auth-module -n 50

# 9. Export a task report
duo export auth-module --format json -o report.json
duo export auth-module --format text

# 10. Merge when the task is complete
duo merge auth-module

# 11. Clean up completed tasks
duo cleanup --all --force

# 12. Kill a single failed task
duo kill failed-task

# 13. Show version
duo version
```

## Configuration

The config file is located at `~/.duo/config.json` and managed via the `duo config` subcommand:

```bash
duo config list              # List all settings
duo config get copilot_model # Get a single setting
duo config set copilot_model claude-sonnet-4-20250514  # Update a setting
duo config reset             # Reset all settings to defaults
duo config reset copilot_model  # Reset a single setting
```

### Shell Completion

```bash
# Bash (~/.bashrc)
eval "$(duo completion bash)"

# Zsh (~/.zshrc)
eval "$(duo completion zsh)"

# Fish (~/.config/fish/config.fish)
duo completion fish | source
```

### Available Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `copilot_model` | `claude-opus-4.6` | Copilot model to use |
| `max_corrections` | `3` | Max auto-correction attempts |
| `heartbeat_timeout` | `90` | Heartbeat timeout in seconds |
| `poll_base_interval` | `5.0` | Base polling interval |
| `poll_max_interval` | `120.0` | Max polling interval |
| `auto_allow_all` | `true` | Automatically send /allow-all |
| `max_parallel` | `3` | Max parallel tasks |
| `pr_budget` | `0` | Max Premium Request usage per task (0 = unlimited) |
| `worktree_base_path` | `/tmp/duo-worktrees` | Base path for git worktree creation |

## Parallel Scheduling

Duo supports up to N tasks running in parallel (default: 3, configurable). Tasks exceeding the limit are automatically queued in a FIFO order.

```bash
# Set concurrency limit
duo config set max_parallel 5

# Batch-create tasks
duo batch examples/tasks.json --repo .

# Check queue status
duo queue

# monitor auto-starts queued tasks when slots become available
duo monitor
```

## Debugging

```bash
# View task details
duo inspect my-task

# View the event stream
duo logs my-task
duo logs my-task -n 50     # Last 50 entries
duo logs my-task --all     # All entries

# Check the queue
duo queue

# Recover interrupted tasks
duo recover
```

## Core Concepts

### Task

An independent unit of work. Each Task has its own:
- **Git worktree** — `/tmp/duo-worktrees/{name}/`, on branch `duo/{name}`
- **Tmux pane** — A dedicated terminal running the Copilot CLI
- **State directory** — `~/.duo/tasks/{id}/`, containing task.json, journal, heartbeat, etc.

### Step / Attempt

Tasks are broken down into multiple steps, and each step can have multiple attempts. When a quality gate fails, the attempt counter is incremented and the step is retried. After ≥3 failures, the task is escalated to ESCALATED status for human intervention.

### Incarnation

An 8-character hex UUID generated each time a session is started or restarted. It isolates stale data from previous sessions — ack/heartbeat/result files must carry a matching incarnation ID to be accepted.

### File Protocol

Commander and Executor communicate through the filesystem:

```
Commander sends prompt → Executor writes ack → Executor writes heartbeat (ongoing) → Executor writes result
```

- **ack** — Executor confirms receipt of a prompt (includes prompt_hash for verification)
- **heartbeat** — Executor periodically reports progress (current file, status)
- **result** — Executor reports completion (status, changed files, summary)

All writes use atomic operations (write to temp file + rename) to prevent reading partial data.

### FSM (Finite State Machine)

A Task has 13 states. All transitions are validated and recorded in the journal:

```
CREATED → QUEUED (waiting for a slot) / SESSION_STARTING (launched immediately)
QUEUED → SESSION_STARTING (scheduled when a slot opens) / FAILED
SESSION_STARTING → PROMPT_SENT → ACKED → RUNNING → RESULT_REPORTED → VERIFYING
                                                                          │
                                      ┌───────────────────────────────────┘
                                      ▼
                                ┌─ COMPLETED (terminal)
                                ├─ CORRECTING → retry
                                ├─ BLOCKED → ESCALATED / retry
                                └─ ESCALATED → human intervention

FAILED → SESSION_STARTING (auto-restart)
```

## Module Reference

| Module | Lines | Responsibility |
|--------|-------|----------------|
| `cli.py` | ~1310 | Click CLI entry point — 17 commands + config subcommand, git worktree/branch management |
| `protocol.py` | ~550 | FSM (13 states) + data models (dataclass) + file I/O + journal |
| `commander.py` | ~640 | Orchestration brain: prompt construction, session management, poll scheduling, correction loop |
| `config.py` | ~77 | Configuration management: persistent read/write, automatic type coercion, defaults |
| `scheduler.py` | ~129 | Parallel scheduler: FIFO queue, max_parallel throttling, auto-dequeue |
| `dashboard.py` | ~158 | Rich live dashboard: task status table, heartbeat progress, auto-refresh |
| `transport.py` | ~400 | tmux-bridge wrapper — the sole entry point for all tmux interactions |
| `poller.py` | ~120 | Adaptive poller: exponential backoff + heartbeat timeout detection |
| `verifier.py` | ~239 | Quality gates: security boundaries, secret detection, untracked files, acceptance tests |

### protocol.py — Data Models

```python
@dataclass
class Task:
    id: str
    description: str
    worktree: str
    branch: str
    base_commit: str
    pane_label: str
    incarnation_id: str        # 8-char hex, regenerated on each restart
    status: TaskStatus
    current_step: int
    current_attempt: int
    subtasks: list[Subtask]
    security_policy: SecurityPolicy

@dataclass
class Subtask:
    step_id: int
    description: str
    target_files: list[str]    # Expected changed files (soft constraint)
    writable_paths: list[str]  # Writable paths (hard constraint, fnmatch)
    acceptance: str            # Acceptance test command

@dataclass
class SecurityPolicy:
    writable_paths: list[str]
    secret_patterns: list[str]  # ["API_KEY=", "password=", "token="]
    forbidden_commands: list[str]
    allow_network: bool
    require_human_approval: list[str]
```

### verifier.py — Quality Gates

Checks run in order. The first hard failure short-circuits and returns a Correction:

1. **Security boundary check** (hard) — Changed files must match `writable_paths` (fnmatch)
2. **Scope check** (soft) — Deviations from `target_files` produce a warning only
3. **Secret leak detection** (hard) — Scans added lines in the diff for sensitive patterns
4. **Untracked file check** (hard) — `git ls-files --others` must return empty
5. **Acceptance test** (hard) — Runs the `acceptance` command; exit code must be 0

### poller.py — Adaptive Polling

```
Initial interval: 5s   →   Exponential backoff ×1.5   →   Max interval: 120s
Heartbeat timeout: 90s (triggers diagnostics + possible session restart)
Grace period: 90s after sending a prompt before timeout is enforced
```

Poll results:
- `RESULT_READY` — Result file is ready; proceed to verification
- `WORKING` — Heartbeat is healthy; keep waiting
- `HEARTBEAT_TIMEOUT` — Timeout; check whether the process is still alive
- `UNKNOWN` — No heartbeat and no prompt; attempt to resend

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DUO_COPILOT_MODEL` | `claude-opus-4.6` | Overrides `copilot_model` from config |

## Security

Duo enforces multiple layers of security:

- **Path traversal protection** — Changed files are validated against `writable_paths` allowlists using `fnmatch`; any file outside the declared scope is a hard rejection
- **Label sanitization** — Pane labels and task names are validated against strict regex (`^[a-zA-Z0-9_.-]+$`) to prevent shell injection
- **Secret detection** — Diffs are scanned for sensitive patterns (`API_KEY=`, `password=`, `token=`) before accepting results
- **PR safety** — Premium Request budgets (`pr_budget`) cap per-task resource consumption
- **`shell=False` everywhere** — All `subprocess.run` calls use list-form arguments; commands are split with `shlex.split()` to prevent shell injection

## Development

```bash
bash install.sh              # Install
make check                   # Run all checks (lint + format + type-check + coverage)
make coverage                # Run tests with coverage (fail_under=95)
make format                  # Auto-format code with ruff
make lint                    # Lint with ruff
make type-check              # Type-check with mypy (strict)
make test                    # Run tests (pytest)
duo --help                   # View commands
```

### Pre-commit Setup

```bash
pip install pre-commit
pre-commit install
```

Configured hooks: **ruff** (lint + fix), **ruff-format**, **mypy** (strict type checking).

### Running Individual Module Tests

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

## Project Structure

```
duo/
├── pyproject.toml
├── README.md
├── README.zh-CN.md
├── CLAUDE.md
├── src/duo/
│   ├── __init__.py
│   ├── cli.py          # CLI entry point (17 commands)
│   ├── config.py       # Configuration management
│   ├── protocol.py     # FSM + data models + file I/O
│   ├── commander.py    # Orchestration logic
│   ├── scheduler.py    # Parallel scheduler
│   ├── dashboard.py    # Rich live dashboard
│   ├── transport.py    # tmux-bridge wrapper
│   ├── poller.py       # Adaptive polling
│   └── verifier.py     # Quality gates
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

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines and PR process.

## License

MIT
