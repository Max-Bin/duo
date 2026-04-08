# CEO Workflow Commands

> **Audience:** The "CEO Claude" — the Commander agent that orchestrates
> Copilot CLI executors via tmux + file protocol.

## Overview

The `duo ceo-*` commands replace ad-hoc inline Python with ergonomic CLI
calls. Each command is an atomic operation with safety guards and JSON output
for easy scripting.

| Command | Purpose |
|---------|---------|
| `duo ceo-wait <task>` | Block until a dialog appears in the task's pane |
| `duo ceo-select <task> <option>` | Pick a numbered dialog option |
| `duo ceo-approve <task>` | Auto-approve a permission dialog |
| `duo ceo-status <task>` | Print the pane state as a single JSON line |

## Commands

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
