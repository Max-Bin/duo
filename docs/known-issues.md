# Known Issues

## Concurrent dialog operations have no pane-level locking

When two CEO commands target the same pane simultaneously, they may
interfere with each other. Workaround: run one CEO command at a time per task.

**Severity**: Low — single-operator workflow makes this rare.

## Copilot CAPIError 400 Bad Request on long sessions

After extended use (4+ hours, many tool calls), the Copilot CLI backend
may return a CAPIError 400. This is an upstream issue in the Copilot CLI.

**Mitigation**: `duo doctor` detects CAPIError signals and recommends
session restart. Keep sessions under 4 hours of heavy use.

## duo watch intermittently fails to wake CEO on dialog detection

Occasionally `duo watch` detects a dialog but the notification does not
reach the CEO process. Root cause is likely a race between pane content
capture and notification dispatch.

**Workaround**: Run `duo doctor` periodically to catch stuck dialogs.
