# Changelog

All notable changes to Duo are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- `duo start` defaults to defer mode — Copilot launches but waits for `duo send` before consuming a Premium Request
- `duo start --immediate` flag to skip defer mode and send bootstrap immediately
- `bypass_permissions` config (default: true) — controls `--yolo` for Copilot CLI and `--dangerously-skip-permissions` for Claude Code
- `duo logs --filter TYPE` — filter journal events by event type substring
- `duo diff --stat` — show diffstat summary for task changes
- `duo diff --name-only` — list changed file names only
- `duo list --status STATUS` — filter tasks by status with validation
- `duo queue --json-output` — machine-readable queue status
- `duo recover --json-output` — structured recovery results with change details
- `duo retry` now supports ESCALATED tasks (transitions to PROMPT_SENT)
- `duo stop --json-output` — structured stop results with pane status
- `duo retry --json-output` — structured retry results with FSM transition info
- `duo merge --json-output` — structured merge results (dry-run and actual)
- `duo kill --json-output` — structured cleanup results
- `duo send --json-output` — structured prompt delivery results
- `duo start --json-output` — task creation results for scripting
- `duo resume --json-output` — session recovery results with per-task details
- `duo cleanup --json-output` — structured cleanup results (normal and corrupted)
- `duo events list --json-output` — machine-readable event listing
- `duo events clear --json-output` — structured clear results
- `duo batch --json-output` — structured batch creation results
- `duo diff --json-output` — structured diff info (files, stat, base commit)
- `duo version --json-output` — machine-readable version info
- `duo config get --json-output` — machine-readable config value
- `duo config list --json-output` — all config values as JSON
- `duo init --json-output` — structured init results for CI/CD
- `duo doctor` stale-locks check — warns about orphaned `.lock` files in TASKS_DIR
- `duo doctor` orphan-worktrees check — detects stale `duo-*` git worktrees
- `duo doctor --fix` — auto-resolve stale locks, quarantined tasks, and orphan worktrees
- `__all__` exports on all modules (cli.py was the last)

### Fixed
- Duplicate task ID detection in `create_task()` — raises ValueError instead of silently overwriting
- Extracted `kill_pane()` into transport layer — all 7 direct tmux subprocess calls now routed through validated transport function
- Semantic fix in `resume`: `cleanup_pane_state` only runs on successful pane teardown
- Debug logging for pane teardown failures (OSError, TimeoutExpired, non-zero returncode)
- Resolved fnmatch case sensitivity known-issue (implementation uses PurePosixPath.match, always case-sensitive)
- `_THREAD_LOCKS` auto-eviction: max 256 cached locks with idle eviction to prevent unbounded growth
- `events list` no longer prints each event twice in non-JSON mode
- `config reset` now rejects unknown keys with helpful error message
- **All FSM `transition()` call sites now check return values** — critical paths abort/rollback on failure, CORRECTING rolls back attempt counter, scheduler slot accounting protected
- `wait_for_dialog()` accepts optional `stop_event` parameter — watch threads now wake within one interval of stop being set instead of blocking for full 300s timeout
- `send_text_dialog_message()` now fail-closed — aborts Enter when typed text not confirmed visible after 3 retries
- `send_shell_command`, `send_bootstrap`, `send_message` now wrapped with `pane_lock` for atomic multi-step operations
- Cross-process monitor race: `task_lock(task_id)` context manager using `fcntl.flock(LOCK_EX | LOCK_NB)` — second monitor skips locked tasks; fresh task reload under lock prevents stale state

### Security
- Regex validators (`_SAFE_LABEL`, `_SAFE_SESSION_ID`, `_validate_task_name`) now use `\Z` instead of `$` — prevents trailing newline bypass
- `_get_copilot_model()` validates env var characters — rejects shell metacharacters
- `copilot --model` argument now uses `shlex.quote()` to prevent command injection
- `verify_and_advance` rolls back step/attempt on prompt-send failure (prevents zombie state)
- `poll_task` adopts fresh task state after disk reload (prevents stale incarnation race)
- `restart_session` terminates old crashed pane before creating new one (prevents orphan accumulation)
- `start_session` now transitions to FAILED on startup timeout instead of blindly sending commands
- `start_session` extended try/except covers /allow-all and bootstrap phases (prevents orphaned panes)
- Dead `resend_last_prompt` path now reachable via fallback to `session_started_at`/`created_at`
- Bootstrap now sets `last_prompt_sent_at` for correct monitor/retry state tracking
- `subprocess.TimeoutExpired` caught in all session lifecycle paths (prevents monitor crash)
- `_count_corrections()` bounded journal read with tail=200 (performance)
- Consecutive poll error counter: tasks auto-fail after 10 consecutive poll errors (prevents indefinite stuck state)
- `name_pane()` failure after split-window now kills orphaned pane and transitions to FAILED
- Root-anchored path matching in verifier (fixes `PurePosixPath.match()` not anchoring from root)
- Unicode NFC normalization for path matching in verifier
- `read_json` uses explicit UTF-8 encoding + catches `UnicodeDecodeError`
- `read_jsonl` resilient to `OSError` (permission denied) and invalid byte sequences
- `transition()` saves task.json before journal append (crash consistency)
- 10 new modern secret patterns in `SecurityPolicy` defaults (encrypted PK, PGP, ghu_, xoxc/a, ya29, etc.)
- `SecurityPolicy` patterns now merged with defaults on `load_task()` (older tasks get new patterns)
- `verify_and_advance()` accepts pre-read result, eliminating double I/O in poll→verify path
- `DUO_COPILOT_MODEL` env var length bounded to 64 chars

### Changed
- `session_started_at` field added to `duo status --json-output` and `duo list --json-output`
- Removed last `@pytest.mark.xfail` — all tests now pass without expected failures
- All modules now export `__all__` (added to `errors.py`)
- Test suite 67% faster (82s → 24s) via sleep mocking in 6 transport test classes
- 22 Hypothesis property-based tests for validators, hash, age, path matching
- All subprocess.run calls now have explicit timeout parameters
- `git init` in `duo go` has 30s timeout
- All 10 dataclasses use `slots=True` for lower memory footprint (Python 3.10+)
- 6 AI platform secret detection patterns added (Anthropic, OpenAI, HF, Replicate)
- `duo stats` text output now shows all 13 FSM states (was missing `session_starting` and `result_reported`)
- CI workflow uses full ruff ruleset from pyproject.toml (not hardcoded subset)
- Test fixtures properly type-annotated — 10 `type: ignore` comments removed
- `SECURITY.md` expanded with 9 specific security safeguards, version updated to 1.0.x
- `ROADMAP.md` v1.x items all marked as complete with ✅
- `__main__.py` added — Duo now runnable as `python -m duo`
- `pyproject.toml` Development Status promoted from Beta to Production/Stable
- Removed redundant `[project.optional-dependencies]` yaml section (already in main deps)
- `make build` target added for sdist + wheel packaging via `uv build`
- Smoke test fixed: uses `poll_base_interval` (not `poll_interval`) — 74/74 pass
- Pre-release check validates SECURITY.md version alignment
- Public API stability tests for `duo.__all__` exports
- 4 Hypothesis property-based tests for config coercion (int, float, bool round-trips)
- Enabled SIM/PIE/PERF ruff rule categories; fixed all violations (startswith/endswith tuple, redundant pass, simplified returns)
- 6 new Hypothesis property tests: atomic_write roundtrip, JSONL append+tail, prompt_hash stability
- `.editorconfig` for consistent contributor formatting across editors
- GitHub templates improved: streamlined PR checklist, `duo doctor` in bug reports
- 5 new cross-module integration tests: config round-trip, journal audit trail, event ordering

## [1.0.0] — 2026-04-09

### Added
- `duo cost` command — Premium Request usage tracking and reporting
  - `--task`, `--since`, `--budget`, `--json-output` flags for flexible consumption analysis
  - Exit non-zero when `--budget` threshold exceeded (CI-friendly)
- `duo bench` command — performance benchmarks for dialog-detection, file-protocol, journal-append
  - `--baseline`, `--save`, `--json-output` flags for regression tracking
- Multi-project isolation awareness — improved error messages showing worktree path
- `DuoUserError` hierarchy — actionable error messages with `fix:` suggestions
  - Extends `click.ClickException` for clean CLI output without tracebacks
- `duo think` command for pre-start brainstorming with Claude Code in a tmux pane
  - `--ask` sends questions, `--finalize` generates plan.md, `--close`/`--delete` manage lifecycle
  - `duo think list` shows all thinking sessions with status
- `duo start --from-thinking` reads plan.md from a thinking session as the task description
- `duo ceo-loop <task>` — automated dialog handling with YAML policy files
  - Permission auto-approve, option pattern matching, text dialog auto-respond
  - Pause/resume workflow with `duo ceo-resume <task> "instruction"`
  - State persistence in `~/.duo/ceo-loops/{task}.json`
- `duo ceo-select <task> N` — select dialog option by number, with `--other TEXT` for free-text
- `send_text_dialog_message()` in transport — reliable text dialog submission with retry logic
- ANSI escape code stripping in `read_pane()` — dialog detection works with colored tmux output
- New module `src/duo/thinking.py` with thinking session management, pane lifecycle, response extraction
- Concurrent duplicate start protection via file-based locking (fcntl)
- Journal rotation when journal exceeds 10MB (keeps last half)
- Monitor crash protection — `start_session` failures no longer crash the monitor loop
- `_retry` decorator now catches `OSError` alongside `RuntimeError` for transient OS errors
- Bounds checking in `build_continue_prompt()` for out-of-range step numbers
- Config upper bound validation (max_parallel≤100, heartbeat≤3600, etc.)
- Symlink protection in `write_json()` — refuses to write through symlinks
- JSON corruption logging in `read_json()` with warning on decode errors
- Dashboard `_build_events_panel` catches OSError for missing journal files
- Batch file duplicate task name detection
- `approve_permission()` with operator precedence fix for option selection
- `_PR_LOG` capped at 10,000 entries to prevent memory leak
- Monitor pollers dict cleanup for completed tasks
- All `subprocess.run()` calls have explicit timeout (30s git, 10s tmux, 300s acceptance)
- `TimeoutExpired` handling in `_run_git` and `run_in_worktree` (exit code 124)
### Changed
- Scheduler `promote_queued()` optimized: single `list_tasks()` call instead of O(3N)
- Config validation: replaced `assert isinstance()` with explicit type checks (safe under `-O`)
- Narrowed bare `except Exception` to specific types in commander and CLI
- Verifier `run_in_worktree()` returns 127 for malformed commands instead of crashing
- Dashboard timestamp split uses `maxsplit=1` for robustness
- Git worktree line parsing uses bounds-checked split
- `send_task_prompt` checks PR budget before writing prompt file
- Cleanup `--all` now includes ESCALATED and BLOCKED states
- `--refresh` validates >0, `--lines` validates ≥1, `--age` rejects 0
- Thread-safe `_pr_callback` access via local variable under lock
- UTF-8 encoding on all subprocess calls
- Commander tests use isolated config (no leakage from user config)

### Fixed
- Monitor loop survives `start_session()` failure for promoted tasks
- OSError from tmux kill-pane in orphan cleanup is properly suppressed
- Float config validation error messages now show `repr()` of the value
- Operator precedence bug in `approve_permission` option matching
- `verify_and_advance` catches exceptions from `verify_step` → FAILED state
- `write_json` fsyncs tmp file before rename (was after)
- Dialog option counting now bounded to `╭─`…`╰─` dialog box (ignores scrollback above)
- Replaced `assert` statements in CLI with proper `ClickException` (safe under `python -O`)
- Config float validation uses `_FLOAT_MINIMUMS` dict values instead of hardcoded 0

## [0.6.0] — 2026-04-08

### Added
- `duo retry` command for retrying failed/blocked tasks
- `duo stop` command for graceful task stopping (preserves worktree for resume)
- `duo stats` command with `--json-output` support
- `duo diff` command for viewing worktree changes
- `duo init` command for project initialization
- `duo doctor` command for environment diagnostics
- `duo resume` command for resuming interrupted sessions
- Shell completion support (bash/zsh/fish) via `duo completion`
- `--json-output` flag on list, status, logs, inspect, audit, stats
- `--dry-run` flag on `duo batch` and `duo merge` commands
- `--model` and `--queue` flags on `duo start`
- `--queue` flag on `duo batch` for deferred task creation
- `duo --version` flag (standard CLI behavior, in addition to `duo version`)
- Categorized command help: 22 commands organized into 7 sections
- Task timeout enforcement (`task_timeout` config + `--max-time` on monitor)
- `inspect --include-files` flag for viewing changed files and diff preview
- `export --format jsonl` for structured line-delimited JSON export
- `cleanup --age` flag for time-based task cleanup
- 41 edge case tests (protocol, scheduler, poller, config, CLI)
- Expanded secret detection patterns (11 patterns)

### Changed
- Categorized CLI help output with 7 command sections
- Deduplicated batch task creation into single `_create_single_task()` helper
- Centralized git operations via `_run_git()` helper
- Dashboard descriptions now show ellipsis when truncated
- Cleanup operations (merge/kill/cleanup) now show warnings on git failures
- `resume` command logs warnings instead of silently swallowing exceptions
- `load_task()` handles corrupted task.json gracefully (returns None)
- Heartbeat cleared on `restart_session` to prevent stale reads

### Fixed
- Thread safety: lock protection for transport globals
- Durability: fsync on journal writes and atomic file saves
- Crash on corrupted batch files (JSON/YAML parse errors)
- Crash when git is not installed
- Crash when TASKS_DIR cannot be created
- Orphaned tmux pane cleanup on session start failure
- Bridge subprocess timeout (30s) prevents hangs
- Monitor polling interval floor (≥1.0s)
- Empty prompt rejection on `duo send`
- Task name validation on kill/merge/diff/batch commands

### Security
- Replaced `fnmatch` with `PurePosixPath.match()` for safer path matching
- Expanded secret detection patterns (11 patterns)
- Input validation on all user-facing commands
- `load_task()` schema validation for corrupted JSON
- Path traversal protection in `write_json()` — rejects paths with `..` components
- Pane label sanitisation in `resolve_label()` — rejects shell metacharacters

## [0.5.0] — 2026-04-08

### Added
- `duo init` — Initialize a project for Duo (creates .duo config and instructions)
- `duo doctor` — Check environment dependencies with actionable fix suggestions
- `duo resume` — Resume interrupted task sessions (auto-detects live panes)
- `duo completion` — Shell completion for bash, zsh, and fish
- `duo dashboard` — Rich live terminal dashboard with real-time task monitoring
- `duo export` — task report export in JSON and text formats
- `duo cleanup` — clean completed/failed tasks with journal preservation option
- `duo version` — show installed version
- `duo audit` — Premium Request consumption audit per task and global
- PR budget system: `pr_budget` config key, auto-escalate when exceeded
- Premium Request zero-cost safety: `send_bootstrap` one-time lock, `send_prompt` BANNED, `safe_enter` prompt guard, `is_in_dialog_stable` double-check
- `wait_for_dialog()` before all dialog interactions (eliminates race conditions)
- `_BOOTSTRAP_DONE` cleared on session restart (fixes deadlock)
- Deep code review fixes: retry init, bounds check, tmux error handling, age() type safety
- Complete docstrings for all modules
- Updated README covering all 18 commands
- Updated `install.sh` with `--check`, `--force`, verification, summary table
- Ruff linting integrated (unused imports, f-strings cleaned)
- Integration tests simulating full task lifecycle

### Improved
- Refactored `export()` into `_export_as_json()` / `_export_as_text()` helpers
- Extracted `_create_worktree()` helper from `start()` command
- Configurable worktree base path via `worktree_base_path` config key
- Public API exported from `duo.__init__` with `__all__`
- Task name validation (alphanumeric + dash/underscore only)
- Error messages now include actionable suggestions
- Magic sleep values extracted to named constants in commander.py
- Warning on unknown config keys in `set_config()`
- `create_task()` validates non-empty subtasks
- Enhanced docstrings in transport.py (`bridge`, `resolve_label`, `get_pane_id`, etc.)
- SECURITY.md, CODE_OF_CONDUCT.md, GitHub issue/PR templates
- CI uses `ruff check` + `ruff format --check` (replaces py_compile)
- 13 new edge case tests

### Security
- Fix command injection: `run_in_worktree` now uses `shlex.split()` + `shell=False`
- Fix export off-by-one: completed steps now scan all result files (not just attempt 1)
- Git failures in verifier now raise `RuntimeError` instead of silently passing
- Config `set_config` validates type coercion (catches invalid int/float input)
- Timestamp parsing uses explicit `split("T", 1)` maxsplit

## [0.4.0] — 2026-04-08

### Added
- Multi-task parallel scheduler with FIFO queue (`duo batch`, `duo queue`)
- `duo config` subcommand for persistent configuration management
- `duo logs` command for viewing task journal events
- `duo inspect` command for detailed task information
- `duo batch` command for bulk task creation from JSON/YAML files
- `duo queue` command for queue status monitoring
- `--verbose` global option for detailed output
- Auto-send `/allow-all` when starting Copilot sessions
- `install.sh` one-click installer with `--help` support
- Configurable copilot model via `DUO_COPILOT_MODEL` env var or `duo config`
- Configurable max parallel executors (default: 3)
- Quality gate: security scope, secret leak, untracked files, acceptance tests
- Adaptive polling with exponential backoff
- Session crash recovery with incarnation tracking
- Automatic correction with 3-attempt limit before escalation
- FSM with 13 states and validated transitions
- Atomic JSON writes with unique temp files
- Append-only JSONL event journal
- Unit tests covering all modules
- Full type annotations with py.typed marker
- GitHub Actions CI (pytest + mypy)
- MIT License, CONTRIBUTING.md

### Security
- Secret leak detection in git diffs (added lines only)
- Writable path enforcement via fnmatch patterns
- Forbidden command support in subtask definitions

## [0.1.0] — 2026-04-08

### Added
- Project scaffolding with Click CLI
- Basic task lifecycle (create, send, monitor, merge, kill)
- tmux-bridge transport layer
- File-based communication protocol (ack/heartbeat/result)
