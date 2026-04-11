# Changelog

All notable changes to Duo are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- `config edit` command — opens config file in `$EDITOR` / `$VISUAL` / `vi`
- `config validate` command — check config file for errors (types, ranges, unknown keys)
- `send --file` option — read prompt from a file instead of inline text
- `list --count` option — print just the number of matching tasks
- `list --no-header` option — omit table header for cleaner scripting output
- `list --active` flag — shorthand for showing only running/active tasks
- `list --recent N` option — show only the N most recently created tasks
- `cleanup --dry-run` option — preview what would be cleaned without acting
- `stop --all` flag — stop all active tasks in one command
- `kill --all` flag — terminate all tasks and clean up resources
- `inspect --events N` option — control how many journal events to show (0 for all)
- `config validate` integration with `duo doctor` for automatic config health checks
- Heartbeat info in `duo status` output (current file + last pulse)
- Shell completion for thinking session names (`duo think <TAB>`)
- Shell completion for `duo diff` task names
- Shell completion for `--status` filter values
- `status` now shows Branch and Description fields
- `status -q/--quiet` — print just the status value (single task) or ID+status (all tasks) for scripting
- `logs --step N` — filter journal events by step number
- `merge --dry-run` now shows commit count and changed files
- `export` JSON/text now includes `pr_consumed` count and heartbeat data
- Similar task name suggestions on not-found errors
- `cost -q/--quiet` — print only the total PR count for scripting
- `stats -q/--quiet` — print only the total task count for scripting
- `list --wide/-w` — include DESCRIPTION column in table output
- `audit -q/--quiet` — print only the PR consumed count for scripting
- `doctor -q/--quiet` — print only failures (empty output = all pass)
- `recover -q/--quiet` — print only the recovered count
- `inspect -q/--quiet` — print only the task status value
- `cleanup -q/--quiet` — print only the cleaned count, suppresses prompts
- `logs --count/-c` — print only the event count (works with --filter and --step)
- Task name validation suggests corrected name on error
- AGE column in `duo list` output showing elapsed time since task creation
- `--sort` flag for `duo list` (sort by name, status, or age)
- `--reverse` flag for `duo list` to reverse sort order
- Shell completion for `config get/set/reset` KEY argument
- `CONFIG_DESCRIPTIONS` dict with descriptions for all 12 config keys
- `config list` shows inline descriptions in text and rich JSON objects
- `version --json-output` includes Python version and platform info
- All 63 `DuoUserError` instances now have actionable `fix=` suggestions
- Enriched JSON output for `list` and `status` commands (age, total_steps, incarnation_id)

### Changed
- Standardized `--json-output` parameter naming to `as_json` across all 36 commands
- Standardized help text to "Output as JSON" (no trailing period) everywhere
- Fixed doctor output column width (`:<16` → `:<18`) for proper spacing

- GitHub Actions CI workflow with 3-job pipeline: lint+typecheck+coverage, Python 3.12/3.13 compat matrix, CLI smoke tests
- GitHub Actions release workflow for automated PyPI publishing + GitHub Releases on tag push
- Dependabot for automated dependency updates (GitHub Actions + pip)
- CODEOWNERS for PR review gating
- CI badge in README and README.zh-CN
- 20+ guard tests: public API stability, dead code (vulture), import cycles, docstrings, type annotations, test naming, CHANGELOG format, exception handling, dataclass conventions, logger naming, f-string logging, future annotations
- 3 prompt builder property tests (hypothesis): bootstrap/override/correction content verification
- 2 integration tests: restart normalization, escalation journal trail
- 2 integration tests: verify_and_advance blocked result + single-step completion
- 2 property tests: read_jsonl tail parameter (count + order invariants)
- 2 property tests: extract_response tail preservation (safe lines + identity)
- 125 parametrized illegal FSM transition tests (every forbidden state pair)
- 44 parametrized transport edge tests: ANSI strip, CAPI detection, label validation, dialog boundary
- 13 parametrized scheduler active_count tests (7 active + 6 non-active statuses)
- 12 parametrized __all__ exports guard tests (one per module, 155 exports verified)
- 4 hypothesis property tests: poller age range + ramp/reset sequences
- 18 parametrized config bool coercion tests (6 truthy + 6 falsy + 6 invalid)
- 16 parametrized commander tests: normalize_for_restart + model validation
- 12 parametrized CLI task name validation tests
- 19 parametrized tests across 4 files: poller age, config fallback, slots guard, unsafe label
- 6 cross-module integration tests: scheduler+commander, protocol+verifier, config+protocol
- 29 parametrized thinking module tests: extract_response, wait_for_response, thinking_dir
- 29 parametrized dashboard tests: event coloring, status text, task rows
- 56 parametrized FSM transition tests: 44 valid paths + 12 COMPLETED rejections
- 44 parametrized legal FSM transition tests (every allowed state pair)
- 14 parametrized secret pattern detection tests (complete DEFAULT_SECRET_PATTERNS coverage)
- 16 parametrized _match_writable edge cases (extensions, directories, dotfiles, empty)
- 52 parametrized --help smoke tests (every CLI command)
- 24 parametrized config key round-trip tests (12 keys × get + reset)
- 13 parametrized TaskStatus persistence round-trip tests
- 12 parametrized _safe_join traversal + valid name tests
- `make guard` target — runs all guard/meta tests in ~5s
- 8 new Hypothesis property tests: strip_ansi, incarnation IDs, FSM transitions
- 11 new property tests: label path safety, glob matching, config defaults consistency, _fmt_ts fuzz
- 12 new property tests: secret detection false positives, known format detection, writable pattern validation, poller backoff invariants
- 2 new FSM property tests: terminal state absorption proof, BFS reachability proof (every state reaches COMPLETED/FAILED)
- 11 new CLI smoke tests: cleanup, events, queue, ceo-now, ceo-cleanup, ceo-restart, ceo-dispatch, ceo-wait, ceo-approve, ceo-select, ceo-resume (83→94 total)
- 13 new JSON output validation smoke tests: verifies all `--json-output` commands produce valid JSON (94→107 total)
- 4 new smoke tests: command completeness check (all commands in --help), go/ceo-cleanup/ceo-restart help wiring (107→111 total)
- 6 new property tests: thinking module name validation + extract_response delta extraction
- 5 new property tests: config type coercion round-trips (int/float/bool) + rejection of invalid values
- 3 new error message quality smoke tests: verify Fix: suggestions in error output (111→114)
- 2 new tests: guard that all Click commands are in _COMMAND_SECTIONS (prevents orphaned commands)
- 1 new test: command count documentation guard (verifies CLAUDE.md's 52-command claim)
- 4 new JSON schema validation smoke tests: verify key presence in JSON output (114→118)
- 5 new property tests: _parse_age multiplication correctness and unit relationships (76→81)
- 6 new property tests: _validate_task_name and _fmt_ts edge cases (81→87, 27 classes)
- 2 module export guards: verify all modules define __all__ with valid attributes
- 1 FSM doc accuracy guard: architecture.md transition table must match protocol.TRANSITIONS
- 44 parametrized transport edge tests: strip_ansi (12), CAPI detection (10), labels (12), dialog boundary (5)
- 13 parametrized scheduler active_count tests (7 active + 6 non-active statuses)
- 12 parametrized __all__ exports guard tests (155 exports across 12 modules)
- 4 hypothesis property tests: poller age range + ramp/reset sequence invariants
- 18 parametrized config bool coercion tests (6 truthy + 6 falsy + 6 invalid)
- 2 config doc guards: getting-started.md and architecture.md must list all config keys
- 1 no-duplicate-test-class guard: prevents Python class shadowing (found 20 lost tests!)
- 1 no-shadowed-methods guard: prevents Python method shadowing within test classes
- `is_pane_alive()` and `split_window_horizontal()` transport functions
- Shared `conftest.py` with `_isolate_tasks_dir` fixture (replaces 5 per-file copies)
- 3 conftest isolation guards: verify TASKS_DIR, CONFIG_PATH, _CORRUPTED_DIR point to tmp
- 4 scheduler ACTIVE_STATUSES consistency guards
- 1 DuoUserError fix= suggestion guard (all raise sites must include fix=)
- 1 command help text guard (all 52 commands must have help >= 10 chars)
- 9 new CLI smoke tests: graceful failures + data commands (74→83 total)
- Getting-started.md: 5 new sections — `duo go`, `duo bench`, `duo export`, `duo events`, `duo completion`
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
- `duo think --ask` timeout error message now includes actual duration (120s) for debuggability
- Hypothesis-discovered bug: property test `test_function_calls_safe` now filters all 63 secret patterns (not just 6 keywords) to prevent false positive matches like `gho_`
- GitHub repo URL case: `maxbin` → `Max-Bin` in install.sh and getting-started.md
- 3 commands (`go`, `ceo-cleanup`, `ceo-restart`) added to `_COMMAND_SECTIONS` — were showing under 'Other' in `--help`
- **20 shadowed tests recovered**: duplicate `TestCeoMetrics` class in test_cli.py caused Python to silently discard the first class's tests
- **4 shadowed methods recovered**: duplicate method names in `TestCeoSelect` for ceo-approve tests overwrote ceo-select tests
- Consolidated duplicate `TestFmtTs` property test classes (removed redundant weaker assertion)
- Extracted `kill_pane()` into transport layer — all 7 direct tmux subprocess calls now routed through validated transport function
- Routed `go` command's 3 direct tmux subprocess calls through transport layer (`is_pane_alive`, `split_window_horizontal`, `kill_pane`)
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
- All GitHub Actions pinned to commit SHAs (supply-chain hardening)
- Release workflow: minimal top-level permissions (`permissions: {}`), job-level scoping
- Release workflow: tag↔version check prevents mismatched releases
- Release workflow: `github-release` depends on `publish-pypi` (no partial release)
- Release workflow: `if-no-files-found: error` on artifact upload
- All workflow checkouts use `persist-credentials: false`
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
- Path traversal protection: `_validate_task_name()` + `is_relative_to()` defense-in-depth on all task name inputs
- Lock file cleanup age-gated to 1 hour — prevents deletion of live locks
- `cleanup --all` restricted to terminal states (COMPLETED/FAILED) — no longer removes BLOCKED/ESCALATED tasks
- `ceo_restart` respects `bypass_permissions` config + `shlex.quote()` model name
- `approve_permission()` uses last dialog box, not first — prevents stale scrollback from influencing permission approvals
- `send_option_other_message()` same last-box fix — consistent dialog parsing across all handlers
- Protocol readers (`read_heartbeat`, `read_ack_for_step`, `read_result_for_step`) type-guard against non-dict JSON
- `replay_state()` skips non-dict JSONL entries — prevents crash on malformed journal

### Changed
- Cleaned 4 unused `noqa` directives; remaining 3 SIM115 noqas annotated with rationale
- CONTRIBUTING.md: removed redundant `uv pip install -e .`, added `security:` commit type, fixed dogfood example
- `performance-baseline.md` regression thresholds corrected to match bench-regression-check.sh
- All remaining docs audited for accuracy: release.md, send-keys-resilience-audit.md, design-duo-think.md, plan-duo-go.md, performance-baseline.md
- Python 3.13 classifier added to pyproject.toml
- Removed redundant `uv pip install -e .` from Makefile install target
- Decomposed `start_session()` (218 lines) into `_prepare_pane()` + `_start_and_prime_copilot()` + thin orchestrator
- Decomposed `verify_and_advance()` (149 lines) into `_handle_pass_verdict()` + `_handle_correction_verdict()` + thin orchestrator
- Config auto-normalizes `poll_max_interval` when `poll_base_interval` set above max
- Orphan worktree detection uses `worktree_base_path` for accurate matching
- `__all__` exports sorted alphabetically in `__init__`, `poller`, `transport` (RUF022)
- Smoke test `duo doctor` uses `run_test_allow_fail` (expected to exit non-zero without tmux-bridge in CI)
- CI smoke job installs tmux for `duo doctor` checks
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
