# Changelog

All notable changes to Duo are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/).

## [0.5.0] — 2025-07-18

### Added
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
