# Changelog

All notable changes to Duo are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/).

## [0.6.0] — 2025-07-23

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
- 675 tests with 100% coverage
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

## [0.5.0] — 2025-07-18

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
- 305 tests (was 287), including 13 new edge case tests

### Security
- Fix command injection: `run_in_worktree` now uses `shlex.split()` + `shell=False`
- Fix export off-by-one: completed steps now scan all result files (not just attempt 1)
- Git failures in verifier now raise `RuntimeError` instead of silently passing
- Config `set_config` validates type coercion (catches invalid int/float input)
- Timestamp parsing uses explicit `split("T", 1)` maxsplit

## [0.4.0] — 2025-07-15

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
- 222+ unit tests covering all modules
- Full type annotations with py.typed marker
- GitHub Actions CI (pytest + mypy)
- MIT License, CONTRIBUTING.md

### Security
- Secret leak detection in git diffs (added lines only)
- Writable path enforcement via fnmatch patterns
- Forbidden command support in subtask definitions

## [0.1.0] — 2025-06-01

### Added
- Project scaffolding with Click CLI
- Basic task lifecycle (create, send, monitor, merge, kill)
- tmux-bridge transport layer
- File-based communication protocol (ack/heartbeat/result)
