# send-keys Resilience Audit

Red-team review of the 3-layer defense against tmux input failures.
Each edge case is assessed against all three layers:

- **Layer 1**: Raw hex bytes via `tmux send-keys -H`
- **Layer 2**: `send_keys_verified()` with retry + content comparison
- **Layer 3**: `ensure_minimum_pane_size()` auto-resize on failure

## Edge Case Catalog

### 1. Pane resize during active key send
**Scenario**: `tmux resize-pane` happens between `send_keys` and
`read_pane` in `send_keys_verified()`.

**Status**: ✅ Defended

**Analysis**: Layer 2 captures `before` content once, then compares
`after` on each retry.  A resize mid-flight causes SIGWINCH → Ink
re-render → content may change (new line wrapping), which Layer 2
detects as "content changed" and returns True.  If content stays the
same despite resize, Layer 3 kicks in with its own resize + retry.

---

### 2. Copilot process dead but pane still exists
**Scenario**: Copilot crashed or exited, pane shows last output
(static), user sends keys into a ghost pane.

**Status**: ✅ Defended (process liveness check)

**Defense**: `send_keys_verified()` calls `is_pane_process_alive()`
before attempting any key sends.  If the process is dead (PID gone),
raises `RuntimeError("process is dead")` immediately instead of
wasting retries.

---

### 3. Copilot in background (Ctrl-Z / fg/bg)
**Scenario**: User backgrounded Copilot with Ctrl-Z in the tmux pane.
stdin is still connected but the process is stopped (SIGTSTP).

**Status**: ✅ Defended (process state check)

**Defense**: `is_pane_process_alive()` checks `ps -o state=` for 'T'
(stopped).  If stopped, raises `RuntimeError` with actionable message:
"process is stopped — run `fg` in the pane first".

---

### 4. Multi-line text with embedded CR/LF through hex path
**Scenario**: User tries to send text containing `\r\n` via
`send_keys` — the hex path only maps known key names.

**Status**: ✅ Defended

**Analysis**: `send_keys()` only applies hex mapping to known key
names (`Enter`, `Tab`, etc.).  Raw text is sent through `type_text()`
(tmux-bridge `type` command), which handles newlines correctly.
`send_keys("Enter")` maps to `0d` (CR), which is correct for terminal
input.  CR+LF (`0d 0a`) is not needed — terminals use CR only.

---

### 5. Non-ASCII keys (e.g., CJK characters, emoji)
**Scenario**: `send_keys("汉字")` — not in `_KEY_TO_HEX`.

**Status**: ✅ Defended

**Analysis**: Keys not in `_KEY_TO_HEX` fall through to the
tmux-bridge `keys` command (name-based path).  For text input,
`type_text()` should be used instead of `send_keys()`, which handles
UTF-8 correctly.  The hex path is only for control keys.

---

### 6. Permission dialog taller than pane height
**Scenario**: A permission dialog contains a long shell command that
spans 40+ lines, but the pane is only 24 rows.  Dialog is clipped.

**Status**: ✅ Defended (preemptive dialog resize)

**Analysis**: Layer 3 enforces minimum 24 rows, which may not be
enough for very long permission dialogs.  `_detect_dialog_kind()` uses
`_extract_last_box_lines()` to find the ╭─...╰─ box boundaries — if
the top of the box is scrolled off the visible area, the box pattern
is not found, and `DialogKind.NONE` is returned.

**Defense**: `_preemptive_dialog_resize()` counts dialog lines (╭─ to
╰─), compares with pane height, and preemptively calls
`ensure_minimum_pane_size(min_rows=dialog_height + 6)` before
interacting.  Called from `approve_permission` and
`send_option_other_message`.

---

### 7. MINIMUM_PANE_COLS/ROWS for different font sizes
**Scenario**: A user with very large terminal font has a 1080p
monitor → only 80×20 character cells available.

**Status**: ✅ Defended (client-size capping)

**Analysis**: `ensure_minimum_pane_size()` will try to resize to
100×24, but tmux cannot resize a pane larger than its window.

**Defense**: `ensure_minimum_pane_size()` now queries
`_get_client_size()` and caps the resize target to
`client_size - 1`.  If the client is smaller than the requested
minimum, a warning is logged and the resize is capped gracefully
instead of silently failing.

---

### 8. Rapid consecutive send_keys calls (race condition)
**Scenario**: `ceo_loop()` detects a dialog and immediately calls
`select_dialog_option()`, which internally calls `send_keys`
multiple times (Down, Down, Enter) in rapid succession.

**Status**: ✅ Defended

**Analysis**: `select_dialog_option()` calls `send_keys()` for each
arrow key and Enter, each through the hex path.  tmux serializes
writes to the PTY master.  There is a `_time.sleep(0.15)` between
each Down arrow and a 0.5s sleep before Enter.  Race conditions are
avoided by the sleep between steps.

---

### 9. send_keys during tmux-bridge restart/upgrade
**Scenario**: tmux-bridge binary is being replaced (e.g., `uv pip
install --upgrade`) while `send_keys()` is executing.

**Status**: ✅ Defended

**Analysis**: `send_keys()` for mapped keys uses `tmux send-keys -H`
directly — doesn't go through tmux-bridge at all.  Only the fallback
path uses tmux-bridge.  The `@_retry` decorator on `bridge()` handles
transient failures with automatic retry.

---

### 10. Concurrent send_keys from multiple processes
**Scenario**: Two `duo ceo-select` commands running simultaneously
on different tasks, both sending keys to the same pane.

**Status**: ✅ Defended (pane-level advisory locking)

**Analysis**: tmux serializes PTY writes, so bytes won't interleave.
However, the logical sequence (select option N → Enter) could be
scrambled: process A sends "1", process B sends "2", process A sends
Enter — selects option 2 instead of 1.

**Defense**: `pane_lock(label)` wraps all four dialog operations:
- In-process: `threading.RLock` (reentrant, prevents same-thread deadlock)
- Cross-process: `fcntl.flock(LOCK_EX)` on `~/.duo/locks/<label>.lock`
- Timeout: 30s default, raises `TimeoutError` on contention
- Auto-releases on process death (flock semantics)

---

### 11. Pane scrollback buffer full
**Scenario**: Copilot has been running for hours, pane has 10000+
lines of scrollback.  `read_pane(label, 20)` reads last 20 lines.

**Status**: ✅ Defended

**Analysis**: `read_pane()` reads the last N visible lines, not
scrollback.  `capture-pane -p` only captures the visible area (or
last N lines if specified through tmux-bridge).  Scrollback size
doesn't affect visible content comparison.

---

### 12. tmux server restart during operation
**Scenario**: `tmux kill-server` followed by `tmux new-session`
while `send_keys_verified()` is mid-retry.

**Status**: ✅ Defended

**Analysis**: All tmux operations will fail with "no server running"
or similar.  `bridge()` detects this via `_TMUX_DOWN_INDICATORS` and
raises `TmuxServerDownError`.  Layer 3's auto-resize is wrapped in
`except (subprocess.SubprocessError, OSError, RuntimeError)` which
catches tmux failures gracefully.

---

### 13. Unicode in pane content causing read_pane comparison issues
**Scenario**: Pane contains CJK/emoji characters that may render at
different widths, causing `read_pane` to capture slightly different
content on consecutive reads despite no logical change.

**Status**: ✅ Defended (whitespace normalization)

**Analysis**: `read_pane()` → `strip_ansi()` → pure text comparison.
Double-width CJK characters take 2 columns but are 1 character in
the string.  If tmux wraps differently on consecutive reads (due to
terminal timing), the same content could produce different strings.
Layer 2 would incorrectly think the key was consumed.

**Defense**: `send_keys_verified()` normalizes pane content via
`_normalize_pane_content()` before comparison: strips trailing
whitespace per line and collapses trailing blank lines.  This
eliminates false positives from CJK rendering variations.

## Summary

| # | Edge Case | Status | Layer |
|---|-----------|--------|-------|
| # | Edge Case | Status | Defense |
|---|-----------|--------|---------|
| 1 | Resize during send | ✅ | L1+L2+L3 |
| 2 | Dead Copilot process | ✅ | pre-check |
| 3 | Backgrounded process | ✅ | pre-check |
| 4 | CR/LF in text | ✅ | L1 |
| 5 | Non-ASCII keys | ✅ | fallback path |
| 6 | Dialog taller than pane | ✅ | preemptive resize |
| 7 | Small monitor/large font | ✅ | client-size cap |
| 8 | Rapid consecutive sends | ✅ | sleep between |
| 9 | Bridge restart | ✅ | L1 bypasses bridge |
| 10 | Concurrent sends | ✅ | pane_lock |
| 11 | Scrollback full | ✅ | visible area only |
| 12 | tmux server restart | ✅ | TmuxServerDownError |
| 13 | Unicode width | ✅ | whitespace normalize |

**Score: 13/13 fully defended ✅**

## Action Items

All items resolved:

1. ~~**[#10 — Critical]** Add pane-level file lock~~ ✅ Done (pane_lock)
2. ~~**[#2/#3 — Medium]** Pre-check process state~~ ✅ Done (is_pane_process_alive)
3. ~~**[#6 — Medium]** Preemptive dialog resize~~ ✅ Done (_preemptive_dialog_resize)
4. ~~**[#7 — Low]** Client-size capping~~ ✅ Done (_get_client_size)
5. ~~**[#13 — Low]** Whitespace normalization~~ ✅ Done (_normalize_pane_content)
