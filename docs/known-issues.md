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

## Diff size limit enforced late in verifier

`git_diff()` calls `subprocess.run()` which buffers the entire diff in memory
before the byte-length check. A very large diff (e.g. binary file committed)
could cause high memory usage before being rejected.

**Severity**: Low — the `_MAX_DIFF_BYTES` check (5 MB) still rejects the diff,
and git's own limits typically prevent extreme sizes.

## Secret detection does not cover JSON/YAML key forms or base64

The secret detection regex patterns check for `key=value` style patterns but
miss JSON forms like `"api_key": "sk-..."` and base64-encoded secrets. This
reduces detection coverage for config files.

**Severity**: Medium — defense-in-depth measure; primary secret protection
is via writable_paths scope restriction.

## FSM bypass via direct status assignment

Code that does `task.status = X; save_task(task)` bypasses the `TRANSITIONS`
dict validation and journal logging. All production code uses `transition()`,
but there is no runtime guard preventing direct assignment on the dataclass.

**Severity**: Low — would require intentional misuse by a developer.
Consider frozen status field or `__setattr__` guard in future.

## Journal rotation can corrupt replayed state

If journal rotation (archiving old entries) happens during a crash recovery,
the replayed state may not match the actual task state. Currently journal
rotation is not implemented, but the append-only design should be preserved.

**Severity**: Low — theoretical; journal rotation is not yet implemented.
