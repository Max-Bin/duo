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

## CLI send/resume/start lack task_lock

`duo send`, `duo resume`, and `duo start` do not hold `task_lock` when
mutating task state (prompt files, journal, FSM transitions). Concurrent
operations from multiple CLI processes or `duo monitor` can race on task
state. `duo monitor` does hold `task_lock` for `_monitor_one_task`, but
the promoted-queue path is also outside the lock.

**Severity**: Medium — single-operator workflow makes concurrent mutations
rare, but `duo monitor` running alongside manual `duo send` can race.

## Cross-process bootstrap deduplication is incomplete

`send_bootstrap()` uses an in-process `_BOOTSTRAP_DONE` set to prevent
duplicate bootstrap sends. This does not protect across separate CLI
processes. Two `duo send` invocations on a deferred session can each
send a bootstrap prompt.

**Severity**: Medium — requires concurrent CLI processes targeting same task.

## lifecycle_cmd.start does not check post-start task status

`duo start` always prints "started" or "deferred" after `start_session()`,
even if startup silently failed (task set to FAILED). Scripting consumers
may receive false success signals.

**Severity**: Low — `start_session()` now bails early on FAILED status,
so Claude commander and other post-start side effects are skipped. Only
the caller's output message is affected.
