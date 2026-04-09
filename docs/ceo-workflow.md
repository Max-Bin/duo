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

### Monitoring

| Command | Purpose | Example |
|---------|---------|---------|
| `ceo-status <task>` | Pane state as JSON | `duo ceo-status e2e` |
| `ceo-now` | One-screen CEO dashboard | `duo ceo-now` |

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
| `ceo-metrics` | Aggregate analytics across sessions | `duo ceo-metrics --format json` |
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
duo ceo-metrics --format json    # Machine-readable JSON
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
