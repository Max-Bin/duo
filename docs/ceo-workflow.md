# CEO Workflow Commands

> **Audience:** The "CEO Claude" — the Commander agent that orchestrates
> Copilot CLI executors via tmux + file protocol.

## Overview

The `duo ceo-*` commands replace ad-hoc inline Python with ergonomic CLI
calls. Each command is an atomic operation with safety guards and JSON output
for easy scripting.

## Command Reference

### Core Dialog Operations

| Command | Purpose | Example |
|---------|---------|---------|
| `ceo-wait <task>` | Block until dialog appears | `duo ceo-wait e2e --timeout 300` |
| `ceo-select <task> <opt>` | Pick a numbered option | `duo ceo-select e2e 2` |
| `ceo-approve <task>` | Auto-approve permission dialog | `duo ceo-approve e2e` |
| `ceo-smart <task>` | Auto-decide trivial, defer complex | `duo ceo-smart e2e` |
| `ceo-dispatch <task>` | Single-shot policy-driven handler | `duo ceo-dispatch e2e` |

### Automation

| Command | Purpose | Example |
|---------|---------|---------|
| `ceo-loop <task>` | Automated dialog handling loop | `duo ceo-loop e2e --policy smart` |
| `ceo-resume <task>` | Resume a paused ceo-loop | `duo ceo-resume e2e` |

### Monitoring & Health

| Command | Purpose | Example |
|---------|---------|---------|
| `ceo-status <task>` | Pane state as JSON | `duo ceo-status e2e` |
| `ceo-now` | One-screen CEO dashboard | `duo ceo-now` |
| `ceo-cleanup <task>` | Reclaim leaked fds from idle children | `duo ceo-cleanup e2e` |

### Focus Management

| Command | Purpose | Example |
|---------|---------|---------|
| `ceo-focus <task>` | Set current focus task | `duo ceo-focus e2e` |
| `ceo-focus-show` | Show current focus task | `duo ceo-focus-show` |
| `ceo-focus-clear` | Clear focus | `duo ceo-focus-clear` |

### Session Tracking

| Command | Purpose | Example |
|---------|---------|---------|
| `ceo-session-start` | Start new session | `duo ceo-session-start` |
| `ceo-session-list` | List all sessions | `duo ceo-session-list` |
| `ceo-session-replay <id>` | Replay session events | `duo ceo-session-replay abc123` |
| `ceo-session-stats <id>` | Show session stats | `duo ceo-session-stats abc123` |

### Analytics & Configuration

| Command | Purpose | Example |
|---------|---------|---------|
| `ceo-metrics` | Aggregate analytics across sessions | `duo ceo-metrics --json-output` |
| `ceo-smart-config` | Show effective smart patterns | `duo ceo-smart-config` |

---

## Command Details

### `duo ceo-wait`

Blocks until the pane shows a stable dialog (double-checked with a 1 s gap).
On detection: prints the dialog content to stdout, writes a signal file to
`~/.duo/watch-events/`, and exits 0. On timeout: exits 1.

```bash
duo ceo-wait my-task --timeout 300 --interval 5
```

**Options:**
- `--timeout` (default 300): Max seconds to wait.
- `--interval` (default 5): Poll interval in seconds.

### `duo ceo-select`

Selects a dialog option by number, or navigates to "Other" and types custom
text.

```bash
# Pick option 2
duo ceo-select my-task 2

# Type custom text in the "Other" field
duo ceo-select my-task _ --other "my custom response"
```

**Safety:** Refuses if the pane is not in a stable dialog.

### `duo ceo-approve`

Reads the dialog options and picks the "most positive" yes response:
- 3 options → picks "Yes + approve for session" (option 2)
- 2 options → picks "Yes" (option 1)

```bash
duo ceo-approve my-task
```

### `duo ceo-smart`

Combines pattern matching with intelligent decision-making:
- **Trivial dialogs** (permission grants, "Yes/No") → auto-approve
- **Complex dialogs** (multi-option, custom text needed) → defer to Commander

Uses configurable patterns from `~/.duo/smart-patterns.yaml` merged
with built-in defaults. See `duo ceo-smart-config` for active patterns.

```bash
duo ceo-smart my-task
```

### `duo ceo-dispatch`

Single-shot policy-driven dialog handler. Applies the configured policy
(approve, smart, or custom) to whatever dialog is currently showing.

```bash
duo ceo-dispatch my-task --policy smart
```

**Error recovery:** If the dialog is dismissed between detection and
action, exits gracefully with a warning.

### `duo ceo-loop`

Automated CEO workflow loop — combines `ceo-wait` + `ceo-dispatch` in a
continuous loop with configurable policy and timing.

```bash
duo ceo-loop my-task --policy smart --timeout 3600 --interval 5
```

**Options:**
- `--policy` (default `smart`): Dialog handling policy.
- `--timeout` (default 3600): Max loop duration in seconds.
- `--interval` (default 5): Poll interval.

**Exit conditions:** Timeout reached, pane dead, or keyboard interrupt.

### `duo ceo-resume`

Resumes a paused `ceo-loop` for a task. Useful when the loop was
interrupted (e.g., SSH disconnect) and needs to continue.

```bash
duo ceo-resume my-task
```

### `duo ceo-status`

Prints one JSON line describing the pane state:

```bash
duo ceo-status my-task
# {"task":"my-task","state":"dialog","options":5}
```

**Possible states:**
| State | Meaning |
|-------|---------|
| `idle` | At the `❯` prompt, no spinner |
| `processing` | Spinner visible (Copilot is thinking) |
| `dialog` | Inside a `╭╰` dialog box with numbered options |
| `dead` | Pane process is gone |

**Flags:**
- `--assert-in-dialog`: Exit 0 if in dialog, exit 1 otherwise.

### `duo ceo-now`

One-screen CEO dashboard showing all active tasks, their states,
recent events, and resource usage.

```bash
duo ceo-now
```

### `duo ceo-focus` / `ceo-focus-show` / `ceo-focus-clear`

Focus management for multi-task sessions. Tells the Commander which
task is currently active.

```bash
duo ceo-focus my-task        # Set focus
duo ceo-focus-show           # Show current → "my-task"
duo ceo-focus-clear           # Clear focus
```

### `duo ceo-session-*`

Session lifecycle management for audit and replay:

```bash
# Start a new session (prints export command for DUO_CEO_SESSION)
eval $(duo ceo-session-start)

# List all sessions
duo ceo-session-list

# Replay events from a session
duo ceo-session-replay $DUO_CEO_SESSION

# Show aggregate stats
duo ceo-session-stats $DUO_CEO_SESSION
```

### `duo ceo-metrics`

Aggregate analytics across all CEO sessions — dialog counts, approval
rates, timing distributions, error frequencies.

```bash
duo ceo-metrics                  # Human-readable table
duo ceo-metrics --json-output    # Machine-readable JSON
```

### `duo ceo-smart-config`

Shows the effective smart patterns (built-in defaults merged with
user overrides from `~/.duo/smart-patterns.yaml`).

```bash
duo ceo-smart-config
```

---

## Typical CEO Loop

```bash
#!/usr/bin/env bash
set -euo pipefail

TASK="e2e-test"

while true; do
    # Wait for dialog (exit 1 = timeout / pane dead → break)
    duo ceo-wait "$TASK" --timeout 600 || break

    # Read the state
    STATE=$(duo ceo-status "$TASK" | jq -r .state)

    case "$STATE" in
        dialog)
            # Auto-approve permission dialogs
            duo ceo-approve "$TASK"
            ;;
        dead)
            echo "Task pane is dead, exiting"
            break
            ;;
        *)
            echo "Unexpected state: $STATE"
            ;;
    esac
done
```

## Safety Model

All commands enforce the tmux-bridge **read guard**: every interaction is
preceded by a `read_pane()` call. The stability check reads the dialog
twice with a 1 s gap — both reads must confirm a dialog is present.

Commands that modify pane state (`ceo-select`, `ceo-approve`) refuse to
act if the pane is not in a stable dialog.

**Pane locking:** All dialog operations acquire an advisory pane lock
(`pane_lock()`) preventing concurrent dialog interleaving from separate
CLI invocations. See `docs/send-keys-resilience-audit.md` for full
defense analysis.

## PR Budget Protection

> **Iron Rule:** Never send input to Copilot's main `❯` prompt from a CEO
> command. While Copilot is inside a dialog, all responses are free
> continuations of the current Premium Request. The moment Copilot returns
> to the `❯` prompt, **any** new input creates a new PR and burns budget.

### How it works

Every sending command (`ceo-select`, `ceo-approve`, and any future
`ceo-send`) calls `assert_not_at_main_prompt(label)` **before** doing
anything. This helper:

1. Reads the last 20 lines of the pane.
2. Runs `_is_at_main_prompt()` — checks for the `❯` prompt with no
   spinner and no dialog box.
3. If the pane **is** at the main prompt → raises an error and **refuses**
   the command outright.

```
$ duo ceo-select my-task 2
Error: REFUSED: 'duo:my-task' is at Copilot main ❯ prompt.
Sending any input here would create a NEW Premium Request and burn budget.
Either wait for a new dialog or explicitly use --force-new-session.
```

### `--force-new-session`

For the rare case where you intentionally want to start a new PR (e.g.
the task legitimately returned to idle and needs a new nudge):

```bash
duo ceo-select my-task 1 --force-new-session
duo ceo-approve my-task --force-new-session
```

Using `--force-new-session` bypasses the safety check but **logs a
warning** to `~/.duo/pr-budget.log` so budget consumption is auditable.

### `--assert-in-dialog` (scripting)

`ceo-status` supports `--assert-in-dialog` for bash scripts that need
to branch on dialog presence:

```bash
# Exit 0 if in dialog, exit 1 otherwise
duo ceo-status my-task --assert-in-dialog || echo "Not in dialog!"
```

This is useful in CEO loops to detect when Copilot has left the dialog
without consuming a new PR.

---

## Session Lifetime Management

### The Problem

Copilot CLI (v1.0.12) has an upstream bug that leaks kqueue file
descriptors and idle bash child processes during long sessions. After ~4
hours of heavy use (~100+ tool calls/hour), the process accumulates
thousands of leaked fds, causing increasing unresponsiveness and
eventually a full hang.

See `docs/known-issues.md` for detailed observations.

### Recommended Limits

| Session Type | Max Duration | Max Tool Calls |
|-------------|-------------|----------------|
| Light (occasional dialogs) | ~8 hours | ~400 |
| Heavy (continuous CEO loop) | ~4 hours | ~300 |
| Intensive (parallel sub-agents) | ~2 hours | ~200 |

### Health Monitoring

Use `duo doctor` and `duo ceo-now` to monitor session health:

```bash
# Check all pane health metrics
duo doctor

# Dashboard shows session age, fd count, capacity estimate
duo ceo-now
```

**Threshold reference:**

| Metric | Healthy | Warning | Critical |
|--------|---------|---------|----------|
| Open fds | < 500 | 500–2000 | > 2000 |
| kqueue fds | < 50 | ≥ 50 | — |
| Child processes | < 10 | ≥ 10 | — |

### Cleanup During Sessions

Run `ceo-cleanup` periodically to reclaim leaked idle child processes:

```bash
# See what would be cleaned up
duo ceo-cleanup my-task --dry-run

# Actually clean up
duo ceo-cleanup my-task
```

This typically recovers ~1 fd per idle child. It does **not** fix the
kqueue leak (which is in the main Node.js process), so it only buys
partial relief.

### Auto-Restart Signal

When `duo doctor` detects critical health thresholds, it writes a signal
file that CEO automation can check:

```
~/.duo/ceo-sessions/{session-id}/restart-recommended
```

The CEO loop or automation scripts should check for this file and
initiate an orderly session restart when present:

```bash
if [ -f ~/.duo/ceo-sessions/$SESSION_ID/restart-recommended ]; then
    echo "Session degraded — initiating restart"
    duo ceo-restart my-task
fi
```

### Orderly Restart with `duo ceo-restart`

Use `duo ceo-restart <task>` for an in-place restart that preserves
the tmux pane and avoids a full `stop` / `start` cycle:

```bash
# Restart a degraded session
duo ceo-restart my-task

# The command will:
# 1. Record pre-restart health (PID, fds, kqueue)
# 2. Clean up idle child processes
# 3. Exit the current Copilot session
# 4. Wait for the shell prompt to return
# 5. Launch a fresh Copilot with the same model + /allow-all
# 6. Report post-restart health improvement
# 7. Remove the restart-recommended signal file
```

**When to restart your Copilot session:**

| Symptom | Check command | Action |
|---------|---------------|--------|
| Slow responses | `duo ceo-now` — check fd_count | Restart if >500 fds |
| kqueue leak | `duo doctor` — check kqueue_count | Restart if >2000 |
| Idle children accumulating | `duo ceo-cleanup <task> --dry-run` | Cleanup first, restart if not enough |
| Session age >4 hours | `duo ceo-now` — check session_age | Preventive restart recommended |
| `restart-recommended` signal | `duo doctor` emits this | Restart immediately |

**Smoke testing restarts:**

```bash
# Run the automated restart smoke test
make test-ceo-restart TASK=my-task
# or directly:
bash scripts/test-ceo-restart.sh my-task
```

Plan for orderly restart as a first-class operation, not an emergency
recovery.

## Self-Care Walkthrough

This section shows a typical health monitoring and maintenance workflow
using the CEO commands. All output below is from a real session.

### Step 1: Check overall health with `duo doctor`

```bash
$ duo doctor --json-output
```

```json
{
  "checks": [
    {"name": "python", "status": "ok", "detail": "Python 3.12.8"},
    {"name": "uv", "status": "ok", "detail": "uv 0.7.12"},
    {"name": "tmux", "status": "ok", "detail": "tmux 3.5a"},
    {"name": "tmux-bridge", "status": "ok", "detail": "tmux-bridge found"},
    {"name": "copilot", "status": "ok", "detail": "GitHub Copilot CLI"}
  ],
  "copilot_health": {
    "pid": 42195,
    "fd_count": 127,
    "kqueue_count": 3,
    "child_count": 2,
    "status": "healthy"
  }
}
```

**What to look for:**
- `fd_count` < 500 = healthy; 500–2000 = degraded; >2000 = critical
- `kqueue_count` < 50 = normal; >50 = Copilot CLI upstream bug accumulating
- `child_count` < 10 = normal; >10 = idle bash processes need cleanup

### Step 2: Dashboard overview with `duo ceo-now`

```bash
$ duo ceo-now
```

```
════════════════════════════════════════════════════
 Duo CEO Dashboard
════════════════════════════════════════════════════
Focus:     my-task (running)
Pane:      duo:my-task (alive)
  Recent output:
    Working on implementing auth module...
    Created src/auth/handler.py
Budget:    3 PRs used (limit: 10, 3/hr)
Health:    healthy (age 1.2h, fds=127, kqueue=3, children=2, ~14.8h remaining)
Git:       fd95932 (clean @ 2025-01-15T10:30:00)
```

**Key metrics:**
- **Budget burn rate** — 3/hr means you're consuming PRs fast; slow down or restart
- **Remaining capacity** — estimated hours before fd exhaustion; restart when < 2h
- **Session age** — restart preventively after 4 hours of heavy use

### Step 3: Clean up idle children with `duo ceo-cleanup`

```bash
# Dry run first to see what would be cleaned
$ duo ceo-cleanup my-task --dry-run
Found 5 idle child processes
  PID 12345 (bash, idle 45m)
  PID 12346 (bash, idle 30m)
  PID 12347 (bash, idle 22m)
  PID 12348 (bash, idle 15m)
  PID 12349 (bash, idle 8m)
Dry run — no processes terminated.

# Actually clean up
$ duo ceo-cleanup my-task
Terminated 5 idle child processes.
Recovered ~5 file descriptors.
```

### Step 4: Restart when degraded

When health degrades below acceptable thresholds:

```bash
$ duo ceo-restart my-task
Pre-restart:  PID=42195, fds=1847, kqueue=2891
  Cleaned 12 idle child process(es)
  Copilot exited. Re-launching...
  Waiting for Copilot to start...
  Sent /allow-all
Post-restart: PID=43001, fds=19, kqueue=3
Restart complete ✓ (fds: 1847 → 19)
```

### Recommended monitoring cadence

| Interval | Action | Command |
|----------|--------|---------|
| Every 30 min | Quick health check | `duo ceo-now` |
| Every 1 hour | Full diagnostics | `duo doctor --json-output` |
| When fds > 500 | Cleanup idle children | `duo ceo-cleanup <task>` |
| When fds > 1500 | Restart session | `duo ceo-restart <task>` |
| Every 4 hours | Preventive restart | `duo ceo-restart <task>` |
