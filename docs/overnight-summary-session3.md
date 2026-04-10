# Overnight Session 3 Summary

## Stats
- **Commits this session:** ~22 (Rounds EN through FA)
- **Total commits:** 340
- **Tests:** 1946 (up from ~1911)
- **Coverage:** 100%
- **Statements covered:** 5256

## Rounds Completed

### Round EN: _count_corrections perf optimization
- Bounded journal reads with `tail=200` in `_count_corrections()`
- Prevents O(n) journal scanning for large tasks

### Round EO: Fix 4 blocking commander.py audit findings
- Startup timeout → FAILED transition (not blindly continuing)
- Extended try/except covers /allow-all and bootstrap phases
- Dead resend_last_prompt path made reachable via fallback
- subprocess.TimeoutExpired caught in all lifecycle paths
- 7 new tests

### Round EP-EQ: Documentation updates
- CHANGELOG + badges updated
- 8 deferred audit findings recorded in known-issues.md

### Round ER: Poll failure counter
- Tasks auto-fail after 10 consecutive poll errors
- Counter resets on success, stale entries cleaned up
- 3 new tests

### Round ES: name_pane pane leak fix
- Orphaned pane killed on name_pane failure
- Task transitions to FAILED instead of leaving orphan

### Round ET: Protocol + verifier hardening (SECURITY)
- Root-anchored path matching (fnmatch replaces PurePosixPath.match)
- Unicode NFC normalization for path matching
- read_json: UTF-8 encoding + UnicodeDecodeError handling
- read_jsonl: OSError resilience + errors="replace"
- transition(): save task.json before journal (crash consistency)
- 10 new modern secret patterns (encrypted PK, PGP, ghu_, xoxc/a, ya29, etc.)
- 21 new tests

### Round EU: Verifier architectural limitations documented
- CRITICAL: symlink escape (architectural, can't fix without redesign)
- HIGH: ignored-file writes invisible
- HIGH: diff size DoS
- Protocol deferred findings documented

### Round EV: SecurityPolicy default pattern merge
- load_task() merges saved patterns with current defaults
- Older tasks benefit from new secret patterns on reload
- 3 new tests

### Round EW: Double result-file read elimination (S1)
- verify_and_advance() accepts pre-read result parameter
- Eliminates redundant I/O in poll→verify path

### Round EX: DUO_COPILOT_MODEL length bound (S5)
- 64-char limit prevents unbounded shell command args
- 2 new tests

### Round EY: Architecture documentation refresh
- Module line counts updated (~11k total)
- Command counts updated (52)
- Test counts updated in CLAUDE.md

### Round EZ: Event/transition ordering standardization (S4)
- 4 inconsistent paths fixed: transition() now always before append_event()
- Consistent journal ordering for replay analysis

### Round FA: load_task type validation
- current_attempt (int) and status (str) validated on load
- 2 new tests

## Known Issues Resolved This Session
| Issue | Resolution |
|-------|-----------|
| S1 — Double result read | verify_and_advance accepts pre-read result |
| S4 — Event/transition ordering | Standardized: transition before event |
| S5 — Model length unbounded | 64-char limit enforced |
| HIGH — SecurityPolicy defaults lost on load | Patterns merged with defaults |
| MED — PurePosixPath root-anchoring | Replaced with fnmatch.fnmatch |
| MED — Pane leak on name_pane failure | Orphan killed + FAILED transition |
| HIGH — Poll error counter | 10 consecutive errors → FAILED |

## Key Architectural Decisions
1. **fnmatch over PurePosixPath**: fnmatch.fnmatch is root-anchored by default,
   which is the correct security behavior for writable_paths matching
2. **Save-before-journal**: transition() saves task.json first, then appends journal.
   If save fails, journal stays consistent (no phantom transitions)
3. **Pattern merge on load**: DEFAULT_SECRET_PATTERNS is the authoritative baseline;
   loaded patterns are merged using dict.fromkeys for dedup
