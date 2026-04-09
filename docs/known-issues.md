# Known Issues

Observations that warrant future investigation.  Not necessarily bugs —
sometimes just suspicious correlations we don't yet fully understand.

## Tmux layout change correlates with input failures — MITIGATED

**Status: Mitigated** (three-layer defense in place; root cause narrowed
to theories (a)/(b) — see below)

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

**Priority:** Low (mitigated).  Monitor for recurrence.  If the problem
reappears despite all three layers, the remaining investigation path is
to instrument Ink's stdin event loop directly.

**First observed:** During the CEO session around the "big-dialog detection"
and "parallel sub-agents" failures (see commit `dab818f` onwards).

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

## Dialog detection false positives when Copilot describes dialog boxes — LOW PRIORITY

**Status: Open, low priority.**

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

**Priority:** Low.  CEO can visually distinguish false triggers.  Fix
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
