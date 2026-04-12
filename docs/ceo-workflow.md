# CEO Workflow Commands

> **Audience:** The "CEO Claude" — the Commander agent that orchestrates
> Copilot CLI executors via tmux + file protocol.

## Overview

The `duo ceo-*` commands provide ergonomic CLI calls for interacting with
Copilot CLI dialogs. Each command is an atomic operation with safety guards
and JSON output for easy scripting.

## Command Reference

| Command | Purpose | Example |
|---------|---------|---------|
| `ceo-wait <task>` | Block until dialog appears | `duo ceo-wait e2e --timeout 300` |
| `ceo-select <task> <opt>` | Pick a numbered option | `duo ceo-select e2e 2` |
| `ceo-approve <task>` | Auto-approve permission dialog | `duo ceo-approve e2e` |
| `ceo-status <task>` | Show current pane state (JSON) | `duo ceo-status e2e` |

## Command Details

### `duo ceo-wait`

Blocks until a Copilot dialog appears in the task pane, or times out.

```bash
duo ceo-wait my-task                     # default 120s timeout
duo ceo-wait my-task --timeout 300       # 5 min timeout
duo ceo-wait my-task --interval 2.0      # poll every 2s
```

Returns JSON with dialog kind, option count, and raw content.

### `duo ceo-select`

Sends a numbered option selection to a Copilot dialog.

```bash
duo ceo-select my-task 2                 # pick option 2
duo ceo-select my-task 1 --force-new-session  # bypass main-prompt guard
```

### `duo ceo-approve`

Sends "Y" or "Yes" to approve a Copilot permission dialog.

```bash
duo ceo-approve my-task
```

### `duo ceo-status`

Returns JSON describing the current state of the Copilot pane.

```bash
duo ceo-status my-task
duo ceo-status my-task --assert-in-dialog  # exit 1 if not in dialog
```

## Safety Model

**Pane locking:** All dialog operations acquire an advisory pane lock
(`pane_lock()`) preventing concurrent dialog interleaving from separate
CLI invocations.

## PR Budget Protection

> **Iron Rule:** Never send input to Copilot's main `❯` prompt from a CEO
> command. While Copilot is inside a dialog, all responses are free
> continuations of the current Premium Request. The moment Copilot returns
> to the `❯` prompt, **any** new input creates a new PR and burns budget.

### How it works

Every sending command (`ceo-select`, `ceo-approve`) calls
`assert_not_at_main_prompt(label)` **before** doing anything. This helper:

1. Reads the last 20 lines of the pane.
2. Runs `_is_at_main_prompt()` — checks for the `❯` prompt with no
   spinner and no dialog box.
3. If the pane **is** at the main prompt → raises an error and **refuses**
   the command outright.

### `--force-new-session`

For the rare case where you intentionally want to start a new PR:

```bash
duo ceo-select my-task 1 --force-new-session
duo ceo-approve my-task --force-new-session
```

Using `--force-new-session` bypasses the safety check but **logs a
warning** to `~/.duo/pr-budget.log` so budget consumption is auditable.

### `--assert-in-dialog` (scripting)

`ceo-status` supports `--assert-in-dialog` for bash scripts that need
to branch on whether a dialog is active:

```bash
if duo ceo-status my-task --assert-in-dialog 2>/dev/null; then
    duo ceo-approve my-task
fi
```
