# Getting Started with Duo

> **Quick verify:** Run `bash scripts/quickstart-test.sh --local` to validate your environment in one command.

Duo is an **Agent Orchestration Runtime** — it coordinates Copilot CLI (or Claude Code) executors via tmux and a file-based protocol. The Python CLI (`duo`) acts as the Commander, managing tasks through a 13-state finite state machine.

---

## 1. Prerequisites

| Dependency | Version | Purpose |
|---|---|---|
| **Python** | ≥ 3.12 | Runtime for the `duo` CLI |
| **tmux** | any | Terminal multiplexer — Duo runs executors in tmux panes |
| **tmux-bridge (smux)** | any | Bridge layer for programmatic tmux control |
| **Copilot CLI** or **Claude Code** | any | The AI executor that does the actual coding |
| **uv** | any | Python package manager (auto-installed by `install.sh`) |

### Installing tmux-bridge (smux)

```bash
git clone https://github.com/anthropic-ai/tmux-bridge
cd tmux-bridge
# Follow the install instructions in the repo README
```

### Installing Copilot CLI

```bash
npm install -g @githubnext/github-copilot-cli
```

Or install [Claude Code](https://docs.anthropic.com/en/docs/claude-code) if you prefer that executor.

---

## 2. Installation

```bash
git clone https://github.com/maxbin/duo.git && cd duo
bash install.sh
```

### What `install.sh` does

1. Checks prerequisites (Python 3.12+, git, tmux)
2. Installs `uv` package manager if not found
3. Runs `uv sync` to install dependencies
4. Installs the `duo` CLI in editable mode
5. Verifies installation with `uv run duo version`
6. Checks for tmux-bridge (smux) and offers install guidance
7. Prints a summary of all component versions

The installer supports these flags:

| Flag | Description |
|---|---|
| `--check` | Dry-run: check prerequisites without installing anything |
| `--force` | Force reinstall even if duo is already installed |

### Verify installation

```bash
duo version
```

Expected output:

```
duo 1.0.0
```

---

## 3. Initialize Your Project

```bash
cd your-project
duo init --repo .
```

### What `duo init` creates

- **`~/.duo/`** — Global config directory
- **`~/.duo/config.json`** — Global configuration (created if missing)
- **`~/.duo/tasks/`** — Task state storage
- **`.duo/`** — Project-level directory in your repo root
- **`.duo/instructions.md`** — Template for project-specific instructions (coding conventions, testing requirements, etc.)
- Updates **`.gitignore`** to exclude `.duo/`

Edit `.duo/instructions.md` to describe your project's conventions — Duo includes this context when prompting the executor.

### Run the health check

```bash
duo doctor
```

`duo doctor` verifies 13 checks:

1. Python version (≥ 3.12)
2. tmux is installed (version ≥ 3.0)
3. tmux-bridge (smux) is installed
4. Claude Code CLI is installed
5. Copilot CLI is installed
6. `~/.duo` directory exists and is writable
7. Config file is valid JSON
8. An active tmux session exists
9. Task timeout configuration is valid
10. Corrupted task detection
11. Stale lock file detection
12. Orphan worktree detection
13. Git is installed

Flags:

```bash
duo doctor --strict     # exit non-zero on warnings too
duo doctor --fix        # auto-fix stale locks, quarantined tasks, orphan worktrees
duo doctor --json-output  # machine-readable JSON diagnostics
```

Fix any issues it reports before continuing.

---

## 4. Your First Task — Complete Walkthrough

### Start inside a tmux session

Duo manages executor panes inside tmux, so you must be in a tmux session:

```bash
tmux new -s work
```

### Create a task

```bash
duo start fix-auth --repo . --desc "Fix authentication bug in login handler"
```

This command:

1. Creates a git worktree at `/tmp/duo-worktrees/fix-auth` (configurable via `worktree_base_path`)
2. Creates a new branch `duo/fix-auth` from your current HEAD
3. Opens a tmux pane with Copilot CLI
4. **Waits for your first `duo send`** before consuming a Premium Request (defer mode)
5. Auto-sends `/allow-all` if `auto_allow_all` is enabled (default: true)

Additional options:

| Flag | Description |
|---|---|
| `--model MODEL` | Override the copilot model for this task |
| `--queue` | Create in queued state instead of starting immediately |
| `--immediate` | Send bootstrap prompt immediately (skip defer mode) |

### Send the first prompt

```bash
duo send fix-auth "Fix the authentication bug in the login handler"
```

In defer mode (default), this sends the initial bootstrap prompt to Copilot, consuming a Premium Request. You can send follow-up prompts the same way.

### Check status

```bash
duo status fix-auth    # Show status of a single task
duo list               # List all tasks
duo list --status running    # Filter by status
duo list --json-output       # Machine-readable JSON
```

### Watch for dialogs (CEO workflow)

When the executor hits a dialog (permission prompt, clarification, etc.), you can handle it interactively:

```bash
# Wait for a dialog to appear (blocks until one shows up or timeout)
duo ceo-wait fix-auth

# Check the current pane state as JSON
duo ceo-status fix-auth
# Output: {"task":"fix-auth","state":"dialog","options":5}

# Approve a permission dialog (picks the most positive option)
duo ceo-approve fix-auth

# Or select a specific dialog option (1-9)
duo ceo-select fix-auth 2
```

### Auto-approve mode

To automatically approve permission dialogs across all tasks:

```bash
duo watch --auto-approve
```

`duo watch` supports these options:

| Flag | Default | Description |
|---|---|---|
| `--timeout FLOAT` | 300 | Seconds to wait for each dialog |
| `--interval FLOAT` | 5.0 | Poll interval in seconds |
| `--once` | — | Exit after detecting one dialog |
| `--auto-approve` | — | Auto-approve permission dialogs |

### Monitor progress

```bash
duo monitor fix-auth       # Adaptive polling monitor for specific task
duo monitor                # Monitor all active tasks
duo dashboard              # Live Rich terminal dashboard
```

### View logs

```bash
duo logs fix-auth          # Show last 20 journal events
duo logs fix-auth --all    # Show all events
duo logs fix-auth -n 50    # Show last 50 events
duo logs fix-auth --filter error    # Only show error events
duo logs fix-auth --json-output     # Machine-readable JSON
```

### Inspect a task in detail

```bash
duo inspect fix-auth                # Detailed task info
duo inspect fix-auth --include-files  # Include changed files and diff preview
duo diff fix-auth                   # Show git diff for worktree changes
duo diff fix-auth --stat            # Show diffstat summary
duo diff fix-auth --name-only       # List changed file names only
```

### When the task completes

```bash
# Merge the task branch back to main (fast-forward)
duo merge fix-auth

# Preview the merge without executing
duo merge fix-auth --dry-run

# Machine-readable merge results
duo merge fix-auth --json-output

# Clean up completed and failed tasks
duo cleanup --all --force
```

### Send follow-up instructions

If the task needs more guidance while running:

```bash
duo send fix-auth "Also add unit tests for the new auth middleware"
duo send fix-auth "update the docs" --json-output  # Structured result
```

---

## 5. Batch Operations

Create a batch file (`tasks.json`):

```json
{
  "tasks": [
    {
      "name": "feat-auth",
      "description": "Add JWT authentication module",
      "target_files": ["src/auth.py", "src/models/user.py"],
      "writable_paths": ["src/auth.py", "src/models/*", "tests/test_auth.py"]
    },
    {
      "name": "feat-api",
      "description": "Add REST API routes with CRUD endpoints",
      "target_files": ["src/routes.py"],
      "writable_paths": ["src/routes.py", "src/schemas/*", "tests/test_routes.py"]
    }
  ]
}
```

Run the batch:

```bash
duo batch tasks.json --repo .      # Start tasks (respects max_parallel)
duo batch tasks.json --repo . --queue  # Queue all tasks
duo batch tasks.json --dry-run     # Preview without creating
```

Monitor the queue:

```bash
duo queue                          # See queued tasks and active slots
duo queue --json-output            # Machine-readable JSON
duo dashboard                      # Live monitoring of all tasks
duo stats                          # Task statistics summary
```

YAML batch files are also supported (requires `pyyaml`: `uv pip install pyyaml`).

---

## 6. Configuration

```bash
duo config list                    # See all settings with modification markers
duo config get max_parallel        # Get a single value
duo config set max_parallel 5      # Run more tasks in parallel
duo config set pr_budget 100       # Cap premium requests per task
duo config reset max_parallel      # Reset a single key to default
duo config reset                   # Reset all settings to defaults
```

Configuration is stored at `~/.duo/config.json`.

### Key settings

| Setting | Default | Range | Description |
|---|---|---|---|
| `copilot_model` | `claude-opus-4.6` | — | AI model used for task execution |
| `max_parallel` | 3 | 1–100 | Maximum concurrent tasks running |
| `max_corrections` | 3 | 1–100 | Auto-retries before escalation |
| `pr_budget` | 0 | 0–100000 | Max premium requests per task (0 = unlimited) |
| `auto_allow_all` | true | — | Auto-allow all operations |
| `bypass_permissions` | true | — | Add --yolo (Copilot) / --dangerously-skip-permissions (Claude) |
| `heartbeat_timeout` | 90 | 1–3600 | Seconds to wait for task heartbeat |
| `task_timeout` | 0 | 0–604800 | Max seconds per task (0 = disabled) |
| `poll_base_interval` | 5.0 | >0–300 | Initial polling interval in seconds |
| `poll_max_interval` | 120.0 | >0–3600 | Maximum polling interval in seconds |
| `worktree_base_path` | `/tmp/duo-worktrees` | — | Base directory for git worktrees |

---

## 7. Troubleshooting

### Pane is stuck / not responding

```bash
duo ceo-status my-task    # Check pane state (idle, processing, dialog, dead)
duo retry my-task          # Retry from current step (FAILED, BLOCKED, or ESCALATED)
duo stop my-task           # Graceful stop (preserves worktree for resume)
duo kill my-task           # Force kill — removes pane, worktree, and branch
duo stop my-task --json-output   # Structured JSON result
duo retry my-task --json-output  # JSON with FSM transition details
duo kill my-task --json-output   # JSON cleanup report
```

### Task got corrupted

```bash
duo cleanup --corrupted           # List quarantined corrupted tasks
duo cleanup --corrupted --force   # Purge them
```

### Task exhausted the correction budget

After `max_corrections` failures (default: 3), the task moves to **ESCALATED** status. Options:

```bash
duo send my-task "Here's what went wrong: ..."   # Give more specific instructions
duo retry my-task                                  # Retry with a fresh attempt
```

### Premium requests over budget

```bash
duo audit my-task                  # See premium request usage for this task
duo audit                          # See global premium request log
duo config set pr_budget 200       # Increase the budget
```

### Recovering from crashes

```bash
duo recover                  # Replay journals, restore FSM state
duo recover --json-output    # Machine-readable recovery report
duo resume                   # Resume all interrupted sessions
duo resume my-task           # Resume a specific task
duo resume --json-output     # Structured resume results
```

### Cleaning up old tasks

```bash
duo cleanup --all --force                # Clean all finished tasks
duo cleanup --all --force --keep-journal  # Clean but preserve journal files
duo cleanup --all --age 7d               # Only clean tasks older than 7 days
```

---

## 8. Tracking PR Usage

The `duo cost` command shows Premium Request consumption across tasks:

```bash
duo cost                    # Show all PR consumption
duo cost --task fix-auth    # Filter to one task
duo cost --since 7          # Last 7 days only
duo cost --budget 50        # Fail if over 50 PRs
duo cost --json-output      # JSON format
```

Use `--budget` in CI to fail the pipeline when Premium Request spending exceeds a threshold.

---

## 9. Thinking Workflow

Use `duo think` to brainstorm with Claude Code before writing any code:

```bash
duo think my-feature --ask "What's the best approach for adding OAuth support?"
duo think my-feature                    # Continue the conversation
duo think my-feature --finalize         # Generate plan.md
duo start my-feature --from-thinking    # Start task using the plan
duo think list                          # List all thinking sessions
duo think my-feature --close            # Close pane, keep files
```

---

## 10. Next Steps

- **[`docs/ceo-workflow.md`](ceo-workflow.md)** — CEO command reference with bash loop examples and safety model documentation
- **[`docs/architecture.md`](architecture.md)** — Deep dive into the FSM, file-based protocol, and security model
- **`duo --help`** — Full CLI reference with all 51 commands
- **`examples/tasks.json`** — Example batch file with multiple task definitions
