# CLAUDE.md — Duo Project Conventions

## Project

Duo is an Agent Orchestration Runtime. Commander (Python CLI) orchestrates Executor (Copilot CLI / Claude Code) via a file-based protocol. Python 3.12+, Click CLI, uv build system.

## Commands

```bash
uv sync                              # install dependencies
make check                           # run ALL checks (lint + format + type-check + coverage)
make coverage                        # run tests with coverage (fail_under=100)
make test                            # run tests
make guard                           # run guard/meta tests only (~5s)
python -m pytest tests/ -v           # run all tests verbosely
python -m pytest tests/test_protocol.py -v   # run specific module tests
duo --help                           # see CLI commands
duo config list                      # list all config values
duo config set copilot_model <model> # change config value
```

## Architecture

```
src/duo/
├── protocol.py    — FSM, data models (dataclass), file I/O, journal (the source of truth)
├── commander.py   — orchestration logic, prompt templates, session lifecycle, verify & advance loop
├── transport.py   — tmux-bridge wrapper (never call tmux directly)
├── poller.py      — adaptive polling with exponential backoff (5s → 120s)
├── verifier.py    — quality gate checks (security scope, secret leak, acceptance test)
├── config.py      — persistent config management (~/.duo/config.json), type coercion, defaults
└── cli/               — Click CLI package (16 submodules, 33 commands)
    ├── __init__.py    — main group, version/completion, command registration (340 lines)
    ├── _helpers.py    — shared utilities: validation, formatting, git ops, completions
    ├── batch_cmd.py   — batch/queue commands
    ├── ceo_cmd.py     — CEO workflow commands (wait/select/approve/status)
    ├── cleanup_cmd.py — task cleanup and pruning
    ├── config_cmd.py  — config get/set/list/reset/validate
    ├── doctor.py      — environment diagnostics and auto-fix
    ├── events_cmd.py  — watch-event list/show/tail/clear
    ├── inspect_cmd.py — task inspection
    ├── lifecycle_cmd.py — start/init/go (task creation and setup)
    ├── logs_cmd.py    — task log viewing
    ├── monitoring_cmd.py — status/list/monitor/watch/dashboard
    ├── reporting_cmd.py — audit/cost/diff
    ├── task_mgmt_cmd.py — stop/kill/merge
    ├── task_ops_cmd.py — send/resume/retry/recover
    └── think_cmd.py   — think session management
```

### transport.py — Protocol Classes

Three `typing.Protocol` classes define the transport abstraction:
- `TransportBridge` — execute tmux-bridge commands
- `PaneReader` — read tmux pane content
- `DialogDetector` — detect dialog state in a pane

These allow easy mocking in tests and future transport backends.

### Security Guards

- **Path traversal protection** — verifier rejects changes outside `writable_paths` (fnmatch)
- **Label sanitization** — `_validate_label()` in transport.py enforces `^[a-zA-Z0-9_.-]+$`; `_validate_task_name()` in cli/_helpers.py enforces `^[a-zA-Z0-9_-]+$`
- **Secret detection** — verifier scans diffs for sensitive patterns
- **`shell=False`** — all subprocess calls use list-form arguments

### Key data flow

```
Commander sends prompt (via transport)
  → Executor writes ack file
  → Executor writes heartbeat files (ongoing)
  → Executor writes result file
  → Commander runs verifier
  → Pass: advance to next step / Complete
  → Correction: retry with feedback (max 3 attempts, then escalate)
```

### FSM States (13)

CREATED → QUEUED → SESSION_STARTING → PROMPT_SENT → ACKED → RUNNING → RESULT_REPORTED → VERIFYING → COMPLETED

Side paths: CORRECTING (retry), BLOCKED (executor stuck), ESCALATED (human needed), FAILED (crash → auto restart)

Legal transitions are defined in `protocol.TRANSITIONS` dict. All transitions are validated and logged to journal.

## Conventions

- All state is in `~/.duo/tasks/{id}/` — files are the source of truth, not memory
- Transport abstraction: always use `duo.transport` functions, never raw subprocess to tmux
- FSM transitions are validated — check `TRANSITIONS` dict before adding new states
- Journal is append-only JSONL — never modify existing entries
- Atomic writes: `write_json` uses tmp+rename pattern for crash safety
- Incarnation ID (16-char hex) isolates sessions — ack/heartbeat/result must match current incarnation
- Tests use shared `_isolate_tasks_dir` fixture in `conftest.py` to isolate `TASKS_DIR` to `tmp_path`
- Prompts sent to executor are in Chinese (the executor agents understand Chinese)

## Data Models

All defined in `protocol.py` as dataclasses:

- **Task** — id, description, worktree, branch, status, current_step, current_attempt, incarnation_id, subtasks, security_policy
- **Subtask** — step_id, description, target_files (soft), writable_paths (hard, fnmatch), acceptance (shell command)
- **SecurityPolicy** — writable_paths, secret_patterns, forbidden_commands, allow_network, require_human_approval
- **Heartbeat** — ts, incarnation, step, status, current_file
- **AckResult** — step, attempt, incarnation, prompt_hash, acked_at
- **StepResult** — step, attempt, incarnation, status ("done"/"blocked"/"error"), files_changed, summary, reason

## Style

- Type hints on all function signatures
- Dataclasses for data models (not dicts)
- `from __future__ import annotations` in all modules
- Minimal comments — code should be self-documenting
- Union types use `X | Y` syntax (Python 3.12+)
- Frozen dataclasses for immutable result types (e.g., `Pass`, `Correction` in verifier)

## Testing

- 2285+ tests, **100% test coverage required** (enforced via `make coverage`)
- Tests organized by module in `tests/test_*.py` (35 test files)
- CLI tests split to match cli/ package: `test_cli_doctor.py`, `test_cli_lifecycle.py`, etc.
- Mock `subprocess.run` for git/tmux-bridge calls
- Use `click.testing.CliRunner` for CLI tests
- Shared fixtures in `conftest.py`: `_isolate_tasks_dir` (autouse), `isolated_tasks`, `runner`, `make_task`
- Every new feature needs tests — coverage must not drop
- Tests are organized by class per function/component under test

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `DUO_COPILOT_MODEL` | `claude-opus-4.6` | Model used by Copilot CLI |

## File Layout

```
~/.duo/tasks/{id}/
├── task.json              # task metadata (atomic writes)
├── journal.jsonl          # append-only event log
├── heartbeat.json         # executor's latest pulse
└── steps/step-{NNNN}/
    ├── prompt-attempt-{AA}.txt
    ├── ack-attempt-{AA}.json
    └── result-attempt-{AA}.json
```

## Iron Rules

### CI Prohibition
**NEVER** create `.github/workflows/` directory or any CI/CD workflow files. CI is permanently disabled (`.github/workflows.disabled/`). Past CI creation flooded the user with thousands of failure notification emails. Violating this rule is a catastrophic incident.

### Value Self-Assessment
Every `ask_user` progress report **must** include:
```
VALUE: HIGH / MEDIUM / LOW
REASON: one sentence
```
- **HIGH**: Fixed a real bug, added user-requested feature, prevented security risk
- **MEDIUM**: Improved code quality/docs/error messages
- **LOW**: Added tests without finding bugs, badge updates, duplicated linter checks

If 3 consecutive rounds produce only LOW value, proactively suggest pausing.
