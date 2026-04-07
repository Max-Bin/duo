# Changelog

All notable changes to Duo are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/).

## [0.5.0] — 2025-07-18

### Added
- `duo dashboard` — Rich live terminal dashboard with real-time task monitoring
- `duo export` — task report export in JSON and text formats
- `duo cleanup` — clean completed/failed tasks with journal preservation option
- `duo version` — show installed version
- Premium Request zero-cost safety: `send_bootstrap` one-time lock, `send_prompt` BANNED, `safe_enter` prompt guard, `is_in_dialog_stable` double-check
- Deep code review fixes: retry init, bounds check, tmux error handling, age() type safety
- Complete docstrings for all modules
- Updated README covering all 17 commands
- Updated `install.sh` with `--check`, `--force`, verification, summary table

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
