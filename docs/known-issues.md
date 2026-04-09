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
