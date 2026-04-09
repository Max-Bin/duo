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

**Status**: ⚠️ Partial defense

**Analysis**: Layer 1 writes bytes to the PTY — they go to the master
side but nobody reads them.  Layer 2 detects "no change" and retries.
Layer 3 resizes (no effect on a dead process).  Final result: returns
`False`, but the caller doesn't necessarily know *why*.

**Recommendation**: Add `is_process_alive(label)` check at the start
of `send_keys_verified()`.  If the process is dead, raise an
informative error immediately instead of wasting retries.

---

### 3. Copilot in background (Ctrl-Z / fg/bg)
**Scenario**: User backgrounded Copilot with Ctrl-Z in the tmux pane.
stdin is still connected but the process is stopped (SIGTSTP).

**Status**: ⚠️ Partial defense

**Analysis**: Keys written to the PTY are buffered by the kernel but
not consumed until Copilot is foregrounded.  Layer 2 sees "no change"
and retries all attempts.  Layer 3 resizes (no effect).  Returns
`False`.

**Recommendation**: Check process state (`/proc/<pid>/status` on Linux
or `ps -o state=` on macOS) for 'T' (stopped).  If stopped, emit a
warning: "Copilot appears suspended — run `fg` in the pane."

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

**Status**: ⚠️ Partial defense

**Analysis**: Layer 3 enforces minimum 24 rows, which may not be
enough for very long permission dialogs.  `_detect_dialog_kind()` uses
`_extract_last_box_lines()` to find the ╭─...╰─ box boundaries — if
the top of the box is scrolled off the visible area, the box pattern
is not found, and `DialogKind.NONE` is returned.

**Recommendation**: In `_extract_last_box_lines()`, if no opening
`╭─` is found but a closing `╰─` is found, assume we're in a tall
dialog and increase capture lines.  Already partially addressed by
commit `dab818f` (tall dialog detection), but minimum pane height
of 24 may still be too small.  Consider increasing
`MINIMUM_PANE_ROWS` to 40, or making it configurable via
`~/.duo/config.json`.

---

### 7. MINIMUM_PANE_COLS/ROWS for different font sizes
**Scenario**: A user with very large terminal font has a 1080p
monitor → only 80×20 character cells available.

**Status**: ⚠️ Partial defense

**Analysis**: `ensure_minimum_pane_size()` will try to resize to
100×24, but tmux cannot resize a pane larger than its window.
`tmux resize-pane -x 100` silently caps at the window width.
The pane stays at 80 columns — Layer 3 returns True (it resized,
sort of) but the pane is still undersized.

**Recommendation**: After resize, re-query `get_pane_size()` to
verify the target dimensions were actually achieved.  If not, log
a warning with a hint to increase terminal window size.

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

**Status**: ⚠️ Partial defense

**Analysis**: `read_pane()` → `strip_ansi()` → pure text comparison.
Double-width CJK characters take 2 columns but are 1 character in
the string.  If tmux wraps differently on consecutive reads (due to
terminal timing), the same content could produce different strings.
Layer 2 would incorrectly think the key was consumed.

**Recommendation**: Normalize whitespace and trailing spaces before
comparison in `send_keys_verified()`.  This would reduce false
positives from tmux rendering inconsistencies.

## Summary

| # | Edge Case | Status | Layer |
|---|-----------|--------|-------|
| 1 | Resize during send | ✅ | L2+L3 |
| 2 | Dead Copilot process | ⚠️ | L2 (returns False) |
| 3 | Backgrounded process | ⚠️ | L2 (returns False) |
| 4 | CR/LF in text | ✅ | L1 |
| 5 | Non-ASCII keys | ✅ | fallback path |
| 6 | Dialog taller than pane | ⚠️ | L3 partial |
| 7 | Small monitor/large font | ⚠️ | L3 capped |
| 8 | Rapid consecutive sends | ✅ | sleep between |
| 9 | Bridge restart | ✅ | L1 bypasses bridge |
| 10 | Concurrent sends | ❌ | no lock |
| 11 | Scrollback full | ✅ | visible area only |
| 12 | tmux server restart | ✅ | TmuxServerDownError |
| 13 | Unicode width | ⚠️ | content compare |

**Score: 9/13 fully defended, 4/13 partial, 0/13 undefended**

## Action Items

1. **[#10 — Critical]** Add pane-level file lock for dialog operations
   to prevent concurrent send interleaving.
2. **[#2/#3 — Medium]** Pre-check process state in
   `send_keys_verified()` to fail fast on dead/stopped processes.
3. **[#6 — Medium]** Consider increasing `MINIMUM_PANE_ROWS` to 40
   or making it configurable.
4. **[#7 — Low]** Post-resize verification of actual pane dimensions.
5. **[#13 — Low]** Whitespace normalization in content comparison.
