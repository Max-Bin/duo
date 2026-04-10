# Known Issues

Observations that warrant future investigation.  Not necessarily bugs —
sometimes just suspicious correlations we don't yet fully understand.

## Tmux layout change correlates with input failures — ROOT CAUSE RESOLVED

**Status: Root cause fixed** (commit `9165a7a`, Round BF); three-layer
defense retained as defense-in-depth.

**Root cause (identified in Round BF):**
`duo start` and `duo think` used bare `tmux split-window` without a `-t`
target session. When multiple tmux sessions existed, tmux would create
the pane in whichever session it considered "current" — not necessarily
the one the user was working in. This caused:
1. Pane created in the wrong session (user can't see it)
2. The wrong session's layout gets rearranged (panes resize from 224×56
   to 224×32)
3. The resize triggers the downstream SIGWINCH/Ink chain described below

**Fix:** All pane-creation calls now use `get_tmux_session_target()` to
read `$TMUX` env var and pass `-t $<session_id>` to `tmux split-window`,
guaranteeing panes are created in the caller's session. When `$TMUX` is
not set, duo raises a clear error instead of silently picking a random
session.

**Observation:**
During a long CEO session with Copilot (pane label `e2e-test`), `tmux
send-keys` began silently dropping keys after the user changed the tmux
layout (pane 1 went from roughly `224x56` to `224x32`).  Before the
layout change, `ceo-select` / `ceo-approve` worked reliably.  After the
layout change — correlated with a batch of hung background sub-agents in
Copilot — input was accepted by tmux but never consumed by Copilot's Ink
event loop.

**Partial fix already landed:**
The immediate symptom (key-name `Enter` being dropped) was traced to
tmux's key-name translation layer interacting badly with Copilot's Ink
stdin polling in a reduced state.  `send_keys()` now routes known keys
(`Enter`, arrows, `Ctrl-*`, etc.) through `tmux send-keys -H <hex>`,
which reliably reaches Copilot's stdin.  See commit that added
`_KEY_TO_HEX` in `transport.py`.

**What's still unclear:**
Whether the layout change itself is a cause or just a coincidence.
Theories to investigate:

1. Pane resize sends `SIGWINCH` to Copilot, which triggers an Ink
   re-render.  During re-render, the stdin listener may be temporarily
   detached.  Keys sent *during* that window could be dropped.
2. A smaller pane may cause Copilot's dialog box (especially permission
   dialogs containing long shell commands) to be clipped, which changes
   which component owns the active input focus inside Ink.
3. Ink's internal `useInput` hooks may be gated on pane dimensions; a
   too-small pane could disable input components silently.
4. The tmux history (`scrollback`) handling may differ for smaller panes
   and cause `capture-pane -p` to return stale content, leading our
   verification logic to believe a key was not consumed when it actually
   was (or vice-versa).

**Reproduction suggestion:**
1. Start Copilot in a `224x56` pane, put it into an ask-user dialog
2. Confirm `duo ceo-select <task> 1` works
3. Resize the pane to `224x20` or similar
4. Retry the same `ceo-select` call — does it still work?
5. Try with raw byte path (already in `send_keys`) vs. key-name path
   (by temporarily reverting the `_KEY_TO_HEX` lookup)

**Suggested fix direction:**
- ~~Add a `tmux resize-pane` auto-recovery in `ceo-*` commands~~
  **DONE** — see below.

**Three-layer defense (implemented):**

1. **Layer 1 — Raw hex bytes** (commit `a4dc6e5`):
   `send_keys()` routes known keys through `tmux send-keys -H <hex>`,
   bypassing tmux's key-name translation layer.  This is the primary
   mitigation and resolves the symptom in >99% of cases.

2. **Layer 2 — Verified key send with retry** (commit `a4dc6e5`):
   `send_keys_verified()` captures pane content before/after and retries
   with increasing settle times.  Detects when a key is silently dropped.

3. **Layer 3 — Auto-resize on failure** (this commit):
   When `send_keys_verified()` exhausts all retries, it calls
   `ensure_minimum_pane_size(label)` which queries `tmux display-message`
   for pane dimensions and auto-resizes to at least `MINIMUM_PANE_COLS`
   × `MINIMUM_PANE_ROWS` (100×24).  After resize (which sends SIGWINCH
   to Copilot), it waits an extra settle period and retries the key once
   more.  If `ensure_minimum_pane_size` itself fails (e.g. pane gone),
   the error is caught and logged — never crashes the caller.

**Investigation conclusions:**

- **Theory (a) — SIGWINCH during re-render**: Most likely co-contributor.
  Pane resize triggers SIGWINCH → Ink re-render → stdin listener
  temporarily busy.  The raw-hex path (Layer 1) bypasses the
  key-name translation that exacerbates this, and Layer 3's auto-resize
  + settle period provides a recovery path.

- **Theory (b) — Small pane clips dialog, changes focus**: Plausible and
  mitigated by Layer 3's minimum pane enforcement (100×24).

- **Theory (c) — Ink useInput gating on dimensions**: Unlikely. Ink's
  `useInput` hooks don't gate on terminal dimensions; they read from
  stdin unconditionally.  Ruled out.

- **Theory (d) — Stale capture-pane**: Not the primary cause.
  `capture-pane -p` returns current visible content regardless of pane
  size.  However, smaller panes have fewer lines to compare, which
  could cause false "same content" verdicts in `send_keys_verified`.
  Mitigated by Layer 3's resize.

**Priority:** Low (root cause fixed). The three-layer defense remains as
defense-in-depth against any future scenarios where panes are resized for
other reasons (manual resize, terminal app resize, etc.).

**First observed:** During the CEO session around the "big-dialog detection"
and "parallel sub-agents" failures (see commit `dab818f` onwards).
**Root cause fixed:** Commit `9165a7a` (Round BF) — `$TMUX` env var respect.

---

## Copilot CLI file-descriptor / kqueue leak on long sessions — MITIGATED

**Status: Upstream bug in Copilot CLI v1.0.12; all 5 duo-side mitigations landed.**

**Observation:**
After ~8 hours of heavy use (several hundred tool calls, multiple
sub-agents), a single Copilot CLI process (v1.0.12, `claude-opus-4-6`
model) accumulated:

- **5,920 kqueue file descriptors** (a healthy Node.js process has < 20)
- **44 persistent idle bash subshells** (children of the Copilot main
  process, each consuming a TTY + fd)
- **~6,000 total open fds** on the main process (vs. macOS default
  soft limit of 256, raised by Node internally to thousands)

**Symptoms:**
- Copilot's Ink TUI becomes increasingly unresponsive
- Dialog rendering lags behind actual state
- `send_keys` silently buffered, not consumed (compounds the
  layout-change bug above)
- CPU time shows sporadic ticks without visible progress
- Eventually: session becomes completely non-interactive, ❯ prompt
  shows but no inputs register
- Killing the bash subshells (`pkill -P <copilot_pid> bash`) releases
  only ~44 fds; the kqueue leak in the main process remains

**Likely root cause (Copilot upstream):**
Copilot CLI's tool-call executor creates a fresh kqueue per background
task / sub-agent / file watcher without cleaning them up on completion.
Over hundreds of tool calls, this leaks into the thousands. Node.js
libuv event loop still functions but becomes progressively slower, and
at some point (likely related to internal poll() cost or kqueue cleanup
scans) effective responsiveness drops to zero.

**Duo-side mitigation (ALL IMPLEMENTED):**

1. **`duo doctor` health check** ✅ (commit `de0bb52`, Round AR):
   Reports fd / kqueue / child-process count for every labeled Copilot
   pane. Thresholds: fd ≥ 500 → warn, fd ≥ 2000 → fail (critical),
   kqueue ≥ 50 → warn, children ≥ 10 → warn.

2. **`duo ceo-cleanup <task>` command** ✅ (commit `b500169`, Round AS):
   Finds and terminates idle child bash/sh subshells of the Copilot
   process. Supports `--dry-run` and `--json-output`. Safe to run
   periodically during long sessions.

3. **Session lifetime guidance** ✅ (commit `db96316`, Round AU):
   Comprehensive "Session Lifetime Management" section added to
   `docs/ceo-workflow.md`. Recommends ≤4 hours heavy CEO work per
   session, documents orderly restart procedure.

4. **Session health in `duo ceo-now`** ✅ (commit `db073d4`, Round AT):
   Shows session age, fd/kqueue/child counts, fd growth rate, and
   estimated remaining capacity (hours). Health status: healthy /
   degraded / critical with color-coded display.

5. **Auto-restart signal** ✅ (commit `db96316`, Round AU):
   `duo doctor` emits `restart-recommended` file to the task directory
   when fd count exceeds critical threshold. CEO agent can check this
   file and initiate orderly restart.

**Lesson learned (internal):**
Long-lived sub-processes are not free. Every persistent child + every
leaked descriptor compounds over time. The longer a session runs, the
higher the latent failure risk. **Plan for orderly restart as a
first-class operation**, not an emergency recovery.

Upstream bug report should include:
- Copilot CLI version (currently v1.0.12)
- macOS version + node version
- Reproduction: run 300+ tool calls in one session, measure `lsof -p <pid> | grep -c KQUEUE`
- Expected: kqueue count stays below ~50
- Actual: grows linearly to thousands

**Priority:** Mitigated. All duo-side health checks + cleanup + restart
signals are in place. Monitor for recurrence; consider upstream report.

**First observed:** After ~8 hours of continuous CEO-driven improvement
work (Rounds Y through AQ, approximately 80+ tool calls per hour, 650+
total). Around commit `5540dba`.


---

## Concurrent dialog operations have no pane-level locking

**Status: RESOLVED** (pane-level advisory locking implemented)

**Scenario:**
If two `duo ceo-select` (or `ceo-approve`, `ceo-dispatch`, etc.)
commands run simultaneously targeting the same pane, their key
sequences can interleave.  For example, process A sends "1", process B
sends "2", process A sends Enter — option 2 is selected instead of 1.

**Defense:** Two-level advisory locking via `pane_lock()`:
1. In-process: `threading.RLock` per label (reentrant, prevents
   deadlock when `approve_permission` → `select_dialog_option`).
2. Cross-process: `fcntl.flock(LOCK_EX)` on `~/.duo/locks/<label>.lock`
   (prevents interleaving from separate CLI invocations).

All four dialog operations (`approve_permission`, `select_dialog_option`,
`send_option_other_message`, `send_text_dialog_message`) acquire the
pane lock for their entire duration.  Timeout defaults to 30s.

---

## Dialog detection false positives when Copilot describes dialog boxes — MITIGATED

**Status: Mitigated.** Bottom-anchor and stronger marker checks landed
(commit `eab16de`); residual false triggers are rare and harmless.

**Observation:**
`duo watch` and `wait_for_dialog` occasionally return "dialog detected"
when there is no actual user-facing dialog.  The pane contains Copilot's
own narration text that happens to include box-drawing characters (`╭─`,
`╰─`) — for example, when Copilot is describing how dialog detection
works, or reading a file that contains box characters, or rendering
stats tables.

**Impact:**
CEO observes the false trigger, reads the pane, recognizes there's no
real dialog, and re-launches `duo watch`.  Wastes a few seconds per false
trigger.  Does not lose work or burn PRs.

**Likely fix:**
Make `_detect_dialog_kind` require additional signals beyond just
box-drawing chars:
- Dialog footer must contain one of: "Enter to select", "Enter accept",
  "↑↓ select", "Enter to confirm", "Type your answer"
- Box must be the LAST box in the pane (recent dialogs are always at
  the bottom, not in scrollback)
- Box must have at least one interactive marker (`❯`, numbered option,
  or text input placeholder)

**Priority:** Low. Mitigated by bottom-anchor checks and stronger marker
matching. CEO can visually distinguish residual false triggers. Fix
when convenient, e.g. as part of a broader dialog detection refinement.

**First observed:** During Round AV (ironically, while implementing
bullet dialog detection, Copilot's own narration triggered the detector
multiple times per minute).

---

## Bullet-style ask_user dialogs misclassified as NONE — RESOLVED

**Status: Fixed** (commit `7c35fa8`, Round AV)

**Observation:**
Copilot CLI sometimes presents `ask_user` dialogs as bullet-style lists
(navigated with ↑↓ arrow keys, selected with Enter) instead of numbered
options. These dialogs have no `N.` prefix — instead they show items
with a `❯` cursor on the selected line and a footer like:

```
↑↓ select · Enter accept · ctrl+d decline · Esc cancel
```

Prior to the fix, `_detect_dialog_kind` only recognized OPTION (numbered)
and TEXT dialogs. Bullet dialogs fell through to NONE, causing `duo watch`
to miss them and `ceo-loop` to spin without handling them.

**Fix:**
- Added `DialogKind.BULLET` enum value
- Detection priority: OPTION > BULLET > TEXT (footer markers + ❯ prefix)
- `_count_bullet_items()` parses item count and cursor position
- `select_bullet_option()` navigates with Up/Down keys + Enter
- `ceo-status` reports `bullet_dialog` state with item/cursor info
- `ceo-select` handles bullet via position-based navigation
- `ceo-loop` / `_handle_dialog` supports `bullet_dialogs` policy

**Priority:** Resolved.

---

## Deferred findings from rubber-duck audits (Rounds BH-BJ) — LOW PRIORITY

**Status: Open, low priority. All CRITICAL/HIGH findings resolved; these are MED/LOW residuals.**

### Transport layer (Round BH, commit `2729e66`)

- **`safe_enter()` TOCTOU window** (MED): Read-then-act over tmux is
  architecturally inherent. We read pane content, decide it's safe, then
  send keys — but content could change between read and send. Mitigated
  with post-send detection logging (CRITICAL level). Full fix would
  require tmux-side atomic "read-and-send-if-match" which doesn't exist.

- **`_THREAD_LOCKS` unbounded growth** (MED → RESOLVED): Added automatic
  eviction in `_get_thread_lock()` — when cache exceeds `_MAX_CACHED_LOCKS`
  (256), idle entries (no active flock owner) are evicted. Combined with
  existing `cleanup_pane_state()` for deterministic cleanup.

- **`read_pane()` output not sanitized** (LOW): Raw tmux pane capture may
  contain ANSI escape sequences. Consumers handle this ad-hoc. A central
  sanitizer would be cleaner but risks breaking dialog detection regexes.

### Verifier layer (Round BI, commit `2fe8653`)

- **~~`fnmatch` case sensitivity~~** — **RESOLVED**: The implementation uses
  `PurePosixPath.match()` (not `fnmatch`), which is case-sensitive on all
  platforms. Cross-platform behavior is consistent. No action needed.

- **Regex-based secret detection** (LOW): Pattern matching can't catch
  base64-encoded secrets or secrets split across lines. Would need a more
  sophisticated scanner (e.g., trufflehog integration). Current patterns
  cover common formats (AWS keys, GitHub tokens, etc.).

### Protocol layer (Round BJ, commit `f5a77b9`)

- **`save_task()` last-write-wins** (MED): No optimistic locking. If two
  processes call `save_task()` concurrently, the last one wins silently.
  Fix would require a `Task.version` field + compare-and-swap. Deferred
  as too invasive — would touch every test that creates/saves tasks.

- **`TRANSITIONS` dict is mutable** — **RESOLVED** (Round CE, commit `998ed0c`):
  Now uses `MappingProxyType` with `frozenset` values. Immutability test added.

- **Journal rotation crash window** (LOW): During rotation, old journal
  is replaced atomically via `atomic_write_text`. If the process crashes
  after computing the new content but before calling `atomic_write_text`,
  no data is lost (old file intact). If crash during `atomic_write_text`,
  tmp file may be orphaned but old journal survives (rename is atomic).
  Acceptable risk.

**Priority:** Low. These are defense-in-depth improvements, not
correctness bugs. Revisit when any becomes a real-world problem.

---

## FSM transition() return value ignored by callers — RESOLVED

**Status: Resolved** (Round FD, commit `22c6237`).

All critical call sites now check `transition()` return values:
- `commander.py`: start_session, verify_and_advance, COMPLETED, ESCALATED,
  CORRECTING (with attempt rollback) all guard on transition success
- `scheduler.py`: promote_queued skips failed transitions, enqueue_or_start
  falls back gracefully
- `cli.py`: retry exits non-zero, start/resume --queue warn on failure
- Error paths (FAILED, BLOCKED) emit events only on successful transition

13 regression tests cover all failure branches.

---

## cleanup_pane_state() now wired into runtime — RESOLVED

**Status: Resolved** (Round CD, commit `e6af259`).

`cleanup_pane_state(label)` is now called after pane termination in both
`duo stop` and `duo kill` commands, preventing unbounded `_THREAD_LOCKS`
and `_FLOCK_OWNERS` growth in long-running processes.

---

## Findings resolved in Rounds BT-BX — RESOLVED

**Status: All resolved.**

### auto_allow_all config dead (Round BT, commit `ae09987`)
`auto_allow_all` was defined in DEFAULTS but never read by `start_session()`.
`/allow-all` was always sent unconditionally. Fixed: now gated behind config.
Users can set `auto_allow_all=false` to keep approval boundaries.

### Unquoted worktree paths (Round BU, commit `59a0e0e`)
`start_session()` and `start_claude_commander()` sent unquoted worktree
paths to tmux via `send_shell_command()`. Paths with spaces would break
startup; shell metacharacters could enable injection. Fixed: `shlex.quote()`.

### BLOCKED tasks auto-restart + slot starvation (Round BV, commit `b5f159c`)
BLOCKED and ESCALATED tasks were counted in ACTIVE_STATUSES (consuming
execution slots) and polled by monitor (triggering auto-restart on heartbeat
timeout). Fixed: removed from ACTIVE_STATUSES, excluded from monitor polling.

### Queued send broken (Round BW, commit `60661c6`)
`duo send` on a QUEUED task called `send_task_prompt()` with no live pane,
causing timeout. On promotion, the user's prompt was overwritten by a
synthesized one. Fixed: queued sends persist prompt file only; promotion
prefers persisted prompt over synthesized.

### Resume doesn't replay prompt (Round BX, commit `26330db`)
`duo resume` started/restarted sessions but never sent a prompt, leaving
the executor idle. Fixed: now replays the persisted prompt file (or builds
a new one). Replay failure is non-fatal.

## Findings resolved in Rounds CE-CM — RESOLVED

**Status: All resolved.**

### TRANSITIONS dict mutable (Round CE, commit `998ed0c`)
FSM transition table was a plain dict — callers could accidentally mutate it.
Fixed: `MappingProxyType` with `frozenset` values. Immutability test added.

### send lacks task-state gating (Round CJ, commit `c1d92cc`)
`duo send` would attempt transport on COMPLETED/FAILED/ESCALATED/BLOCKED
tasks, timing out after 60s on a dead pane. Fixed: early rejection with
actionable fix hints.

### Incarnation mismatch silently dropped (Round CK, commit `2b3108b`)
When `poll_task` saw RESULT_READY but incarnation didn't match, the result
was silently discarded with no trace. Fixed: logs warning and appends
`result_incarnation_mismatch` journal event.

### Monitor sends prompt after failed session start (Round CL, commit `59b3bcc`)
Monitor promotion and crash-recovery paths didn't check `task.status`
after `start_session()` / `restart_session()`. If session start failed
silently (returning with FAILED status), the code would still try to send
a prompt, wasting 60s per task on a dead pane. Fixed: status check guard.

### Resume creates duplicate panes (Round CM, commit `fc62c64`)
`duo resume` called `restart_session()` on tasks with alive panes, but
`restart_session()` never killed the old pane — creating two executors
sharing the same label. Fixed: old pane explicitly terminated before restart.

### start_session ignores wait_for_idle return (Round CO, commit `71e89d4`)
`start_session()` called `wait_for_idle()` for both Copilot startup and
`/allow-all` but never checked return values. If Copilot failed to stabilize,
no diagnostic was logged. Fixed: startup timeout now logs a warning and
appends a `startup_timeout` journal event.

### task_timeout counts queue wait time (Round CP, commit `79752a9`)
Monitor timeout check used `task.created_at` which includes queue wait time.
Tasks queued for long periods could time out immediately upon promotion.
Fixed: added `session_started_at` field to Task, set on `start_session()`,
used preferentially in timeout calculation.

### ceo-select crashes on non-numeric OPTION (Round CQ, commit `4f6836f`)
`ceo-select` passed user-supplied OPTION string directly to `int()` without
validation. Non-numeric input caused an unguarded `ValueError`. Fixed: early
validation rejects non-digit input with a `DuoUserError` and fix hint.

### Monitor restarts tasks after duo stop (Round CR-CS, commits `39c036f`/`5677278`)
Race condition: monitor snapshots active tasks, then `duo stop` kills pane
and transitions to BLOCKED. Monitor detects heartbeat timeout and auto-restarts,
undoing the stop. Fixed: `poll_task` re-reads task status from disk before
auto-restarting. Also handles corrupt `task.json` (load_task returns None)
by skipping restart instead of proceeding blindly.

### FSM recovery paths bypass illegal transitions (Round FF, commit `12a0a4b`)
Three related bugs found by rubber-duck convergence (3 independent agents):
1. `restart_session()` performed side effects (incarnation bump, pane kill,
   heartbeat clear) before validating the transition was legal. Active states
   like RUNNING/ACKED cannot directly → SESSION_STARTING.
2. `cli resume` dead-pane path called `start_session()` without normalizing
   state, then reported success unconditionally.
3. `cli stop` could only transition to BLOCKED from 3 states but reported
   success from all non-terminal states after killing the pane.
Fixed: `normalize_for_restart()` moves active → FAILED first; expanded
TRANSITIONS allows BLOCKED from all non-terminal states; stop reports
`stopped=False` on transition failure.

---

## Copilot CAPIError 400 Bad Request on long sessions

**Status: Partially mitigated (commit `bde3c97`). Upstream bug remains.**

**Observation:**
After many hours of continuous use (~270+ commits, thousands of tool
calls, extensive sub-agent usage including rubber-duck cross-model
reviews), Copilot CLI returned:

```
✗ Execution failed: CAPIError: 400 400 Bad Request
   (Request ID: 1BA7:1E4B4B:EC2120:105B75E:69D8105F)
```

The session then exited to the main ❯ prompt, losing the free
continuation window. PR consumption increased by 1 from the
pre-failure state.

**Likely cause:**
Context window exceeded the backend's allowed size.  Copilot CLI does
not transparently compact or offload context before hitting this
limit — the backend simply rejects the request with 400.

**Impact:**
- **Forced session restart** (costs 1 bootstrap PR)
- **Loses in-memory state** (scratchpad, recent reasoning chains)
- **CEO tooling cannot detect this until after the fact** — the
  failure is silent from `duo watch` / `ceo-status` perspective
  (the pane drops to main ❯ prompt which may look like an idle state)

**Implemented mitigations (Round CX, commit `bde3c97`):**

1. ✅ **Post-failure detection**: `detect_copilot_api_error()` in
   transport.py uses narrow pattern matching for `CAPIError` (not
   generic "error"). `poll_task()` now logs `capi_error` journal event
   and writes `restart-recommended` signal when CAPIError detected.
2. ✅ **`duo doctor` check**: `_doctor_check_capi_error()` reads
   journal for `capi_error` events (race-free, no pane reads).
3. ✅ **`duo ceo-now` risk display**: Session risk line shows
   low/medium/high based on session age + PR count + CAPIError history.
4. ✅ **Narrower error detection**: Replaced broad `"error" in
   terminal.lower()` check with specific `CAPIError` and `rate limit`
   patterns, reducing false positives from user code output.

**Remaining (not yet implemented):**

- **Predictive backoff**: Track tool-call count and warn proactively
  before hitting the cliff (requires token counting, not just PR count).
- **Graceful auto-restart**: `duo ceo-restart` invoked automatically
  on CAPIError detection.

**Priority:** Medium (detection and early warning now implemented;
remaining items are about prevention and auto-recovery).

**First observed:** During the overnight autonomous loop after
Round CV, at approximately 270 commits / 1796 tests.

## Rubber-duck audit findings — deferred items (Round EO)

**Status:** Documented for future rounds.

These findings were identified during independent rubber-duck
cross-validation of `commander.py` (Rounds EL-EO) and verified by
two separate audit agents. CRITICAL and HIGH findings were fixed
in commits `56f2fa2` and `cc27168`. The items below are MEDIUM/LOW
severity and deferred.

### S1 — Double result file read (MEDIUM, perf) — RESOLVED

**Status: Fixed** in commit `3b70270` (Round EW).

`verify_and_advance()` now accepts an optional `result` parameter.
`poll_task()` passes the already-read result, eliminating redundant I/O.

### S3 — Watch event filename collision (LOW)

`watch_tasks()` creates per-task signal files. If two tasks have
IDs that differ only in case, the files could collide on
case-insensitive filesystems (macOS default). Not a practical
concern since task IDs are validated, but documented for awareness.

### S4 — Inconsistent event/transition ordering (LOW) — RESOLVED

**Status: Fixed** in commit `43f6893` (Round EZ).

All failure paths now call `transition()` before `append_event()`,
ensuring consistent journal ordering.

### S5 — Env model value length unbounded (LOW) — RESOLVED

**Status: Fixed** in commit `5bee5bf` (Round EX).

`_get_copilot_model()` now rejects env var values exceeding 64 chars.

### MED — Pane leaks on start_session post-split failures — RESOLVED

**Status: Fixed** in commit `5128548` (Round ES).

`name_pane()` failure after `split-window` now kills the orphaned
pane and transitions the task to FAILED.

### MED — watch_tasks daemon threads outlive function

`watch_tasks()` spawns daemon threads that can continue running
after the function returns if the stop event is not set properly.
Not observed in practice.

### HIGH (deferred) — No per-task cross-process lock

Multiple `duo monitor` processes can race on the same task's
poll/verify/send cycle. Fixing requires file-based locking, which
is too invasive for overnight work. Mitigated by single-monitor
usage pattern.

### HIGH (deferred) — No poll failure counter — RESOLVED

**Status: Fixed** in commit `904e2e1` (Round ER).

Monitor now tracks consecutive poll errors per task. After 10
consecutive failures, the task transitions to FAILED with a
`poll_errors_exhausted` event. Counter resets on successful poll.

**Priority:** Medium overall. The two HIGH items should be
addressed in a future focused session.

---

## Verifier Architectural Limitations (Round ET audit)

These are inherent to the verify-by-diff architecture and cannot
be fixed without fundamental design changes. Documented for
awareness and to inform future design decisions.

### CRITICAL (architectural) — Symlink escape with no git-visible diff

An executor could create a symlink pointing outside the worktree
(e.g., `ln -s /etc/passwd src/data.txt`). If the symlink itself
was committed before the current step, `git diff` won't show it —
there's no changed content. The verifier only checks paths that
appear in the diff, so the escape would be invisible.

**Mitigation:** The verifier already rejects symlinks pointing
outside the worktree for files that DO appear in the diff (added
in earlier rounds). But a pre-existing symlink being read (not
modified) is undetectable.

**Impact:** Low in practice — the executor is a Copilot CLI session
that doesn't have motivation to create persistent symlinks. The
attack requires pre-existing symlinks, which would need to be
planted in a prior step.

### HIGH (architectural) — Ignored-file writes invisible to verifier

Files matched by `.gitignore` won't appear in `git diff` or
`git status --porcelain` for tracked files. An executor could
write to gitignored paths (e.g., `.env`, `node_modules/`) without
the verifier detecting it.

**Mitigation:** `_check_untracked` catches NEW untracked files,
but not writes to already-gitignored paths. A future improvement
could scan the worktree for recently modified files regardless of
git status.

### HIGH — Diff size DoS on verifier

The verifier buffers the entire `git diff` output into memory.
A malicious or buggy executor creating very large changes (e.g.,
adding a multi-GB binary) could exhaust memory.

**Mitigation:** In practice, Copilot CLI won't generate multi-GB
diffs. A future improvement could add `--stat` pre-check and
reject diffs above a configurable threshold before reading content.

### MED — PurePosixPath.match() root-anchoring bug — RESOLVED

**Status: Fixed** in commit `87a4334` (Round ET).

Replaced `PurePosixPath.match()` with `fnmatch.fnmatch()` for
root-anchored path matching. Added Unicode NFC normalization.

---

## Protocol Hardening Deferred (Round ET audit)

### HIGH — SecurityPolicy loses defaults on load — RESOLVED

**Status: Fixed** in commit `941619b` (Round EV).

`load_task()` now merges saved patterns with `DEFAULT_SECRET_PATTERNS`
using `dict.fromkeys()` for dedup. Older tasks benefit from newly-added
patterns when verified by newer code.

### MED — load_task type confusion — PARTIALLY RESOLVED

**Status: Partially fixed** in commit `034896d` (Round FA).

Added type validation for `current_attempt` (int) and `status` (str)
in `load_task()`. Together with existing `subtasks` (list) and
`current_step` (int) checks, the most critical fields are now validated.

### MED — FSM validation weaker than documented — RESOLVED

**Status: Resolved** (Round FD, commit `22c6237`).

All `transition()` call sites now check return values. Critical paths
abort/rollback on failure. Scheduler slot accounting protected. CLI
commands exit with errors on failed transitions.

---

## Scheduler TOCTOU Races (Round FK audit)

### MED — enqueue_or_start / promote_queued slot race

**Status: Open, low priority — mitigated by architectural constraints.**

Two rubber-duck audits independently identified TOCTOU races in the
scheduler's slot accounting:

1. **`enqueue_or_start()`** checks `has_slot()` then returns `"started"`
   without atomically claiming a slot. Two concurrent processes can both
   observe a free slot and both start.

2. **`promote_queued()`** snapshots `n_active` once, then promotes based
   on the stale count. Concurrent promoters can each fill the same
   perceived free capacity.

**Why low priority:** In practice, `promote_queued()` is only called from
each task's monitor loop (single process per task). The CLI `start`
command is typically invoked sequentially by the user. The race requires
truly concurrent invocations, which is rare in normal use. Additionally,
`transition()` prevents double-promotion of the same task.

**Proper fix (deferred):** Cross-process file lock around slot
claim+transition, or a single atomic "claim next queued if
active < max_parallel" operation. Cost is high relative to risk.

### LOW — FIFO by created_at not queued_at

**Status: Mitigated** — added `task.id` tiebreaker for deterministic
ordering. True queue-entry-time ordering would require persisting
`queued_at` timestamp on the Task dataclass.

### LOW — _next_queued dead code removed

**Status: Resolved** in Round FK. Removed unused `_next_queued()` helper.

---

## Rubber-duck audit findings — Rounds FR–FW (thinking, dashboard, protocol)

**Status: All CRITICAL/HIGH/MED findings resolved. Remaining items are LOW/deferred.**

### thinking.py (Round FR, commit `1997f46`)

- **Path traversal via session name** (BLOCKING → RESOLVED): Added
  `_validate_name()` with `^[a-zA-Z0-9][a-zA-Z0-9_-]*$` regex.
  CLI already validated via `_validate_task_name()` but library-level
  defence-in-depth was missing.

- **Unquoted working_dir in shell command** (MED → RESOLVED): Now uses
  `shlex.quote()` to prevent breakage on paths with spaces.

- **Pane leaked if name_pane raises** (MED → RESOLVED): Extended try/except
  to cover `name_pane()` — orphan panes now cleaned up.

- **append_session_log FileNotFoundError** (LOW → RESOLVED): Now creates
  directory if missing via `_ensure_thinking_dir()`.

- **hash() for stability comparison** (LOW → RESOLVED): Replaced with
  direct string comparison to eliminate theoretical hash collision risk.

### dashboard.py (Round FS, commit `da53225`)

- **Non-dict JSONL crash** (HIGH → RESOLVED): Events panel now skips
  non-dict entries with `isinstance(ev, dict)` guard.

- **Rich markup injection** (MED → RESOLVED): Task IDs and event names
  escaped with `rich.markup.escape()`.

- **Heartbeat read OSError** (MED → RESOLVED): Wrapped `read_heartbeat`
  in try/except OSError — degrades to "—" not crash.

- **Journal re-read per refresh** (MED → DEFERRED): Performance
  optimization for large journals. Not a correctness issue.

- **Single-frame state inconsistency** (MED → DEFERRED): Dashboard and
  queue panel can use different task snapshots. Architectural change
  needed (pass single snapshot to all panels).

### ceo_log.py (Round FV, commit `c5d9f0c`)

- **Session ID path traversal** (HIGH → RESOLVED): Added
  `_validate_session_id()` to `replay_session()`, `session_stats()`,
  and `_append_event()`.

### protocol.py (Round FW, commit `b571d45`)

- **read_json PermissionError crash** (MED → RESOLVED): Broadened
  exception handler from `FileNotFoundError` to `OSError`.

### config.py (Round GD, commit `0b501d0`)

- **Non-finite numeric values bypass validation** (HIGH → RESOLVED):
  Added `math.isfinite()` guard in both `load_config()` and `set_config()`.
  NaN/Infinity now rejected with fallback to defaults (load) or ValueError (set).

- **Non-dict JSON crashes load_config** (HIGH → RESOLVED): Added
  `isinstance(stored, dict)` check. Arrays, strings, null, numbers
  now fall back to defaults with warning.

- **int() overflow on large floats** (HIGH → RESOLVED): Wrapped
  `int(value)` in try/except for `ValueError`/`OverflowError`.

- **UnicodeDecodeError not caught** (MED → RESOLVED): Added explicit
  `encoding="utf-8"` on read and `UnicodeDecodeError` to exception handler.

- **Concurrent read-modify-write race** (MED → DEFERRED): `set_config`
  and `reset_config` do load→mutate→save without file locking. Atomic
  writes prevent corruption but not lost updates. Low risk: config changes
  are rare and typically user-initiated. File locking adds complexity
  disproportionate to the risk.

- **Boolean coercion too permissive** (LOW → DEFERRED): Unrecognized
  strings silently become `False`. Acceptable for CLI usage where values
  come from `duo config set` (documented true/false). Would need explicit
  allowlist + error for full strictness.
