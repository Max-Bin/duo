# Duo — Architecture Specification

> **Duo** is a lightweight agent-orchestration runtime (≈ 12 000 lines of Python)
> that coordinates coding tasks across isolated git worktrees.  A **Commander**
> (Python CLI) directs an **Executor** (Copilot CLI / Claude Code) through a
> durable, file-based protocol.

---

## 1. Design Goals

### Zero-spam principle

Premium Requests (PRs) are scarce, metered resources.  Every CLI interaction
that could consume a PR is gated, audited, and budget-checked.  Operations
like `safe_enter()` refuse to press Enter when the pane is at the main `❯`
prompt because doing so would silently burn a request.

### Commander / Executor separation

The Commander (this Python CLI) **never** touches the codebase directly.  All
file modifications flow through the Executor (Copilot CLI or Claude Code)
running in a dedicated tmux pane.  The Commander communicates intent via the
file protocol and reads results back from disk.

### Crash-safe

All persistent state lives in append-only journals and atomically-written JSON
files.  On restart, `replay_state()` walks the journal to recover the exact FSM
state.  Incarnation IDs (16-char hex) prevent stale heartbeats or results
from a previous session from corrupting current state.

### Security by default

- `writable_paths` allowlists restrict which files the Executor may change.
- Secret-pattern scanning rejects diffs containing leaked credentials.
- Forbidden-command blocklists are enforced per-subtask.
- All subprocess calls use `shell=False` (list-form arguments).
- `write_json()` rejects paths containing `..` and refuses to follow symlinks.

---

## 2. Component Overview

```
┌─────────────────────────────────────────────────────┐
│                      CLI (cli.py)                   │
│           Click entry point · 52 commands           │
│              + config / events subgroups             │
└──────┬───────────────┬──────────────┬───────────────┘
       │               │              │
       ▼               ▼              ▼
┌────────────┐  ┌────────────┐  ┌───────────┐
│ Commander  │  │ Scheduler  │  │ Dashboard │
│commander.py│  │scheduler.py│  │dashboard.py│
│ Orchestrate│  │ FIFO queue │  │ Rich live  │
│ + prompts  │  │ max_parallel│ │ terminal   │
└──────┬─────┘  └─────┬──────┘  └───────────┘
       │               │
       ▼               │
┌────────────┐         │
│  Protocol  │◄────────┘
│protocol.py │
│ FSM · I/O  │
│ Journal    │
└──┬─────┬───┘
   │     │
   ▼     ▼
┌──────┐ ┌──────────┐
│Poller│ │ Verifier  │
│poll  │ │verifier.py│
│adapt │ │ security  │
│back- │ │ secrets   │
│off   │ │ tests     │
└──────┘ └──────────┘
       │
       ▼
┌────────────┐        ┌──────────────────────┐
│ Transport  │───────▶│  tmux-bridge binary  │
│transport.py│        │  (external process)  │
│ read/write │        └──────────────────────┘
│ dialog det │
│ PR tracking│
└────────────┘
```

### Modules

| Module | Stmts | Role |
|--------|------:|------|
| `cli.py` | ~4 900 | Click CLI entry point — 35 commands + `config` / `events` subgroups, CEO commands |
| `protocol.py` | ~990 | FSM (13 states), dataclasses (`Task`, `Subtask`, `SecurityPolicy`), atomic file I/O, journal |
| `commander.py` | ~1 800 | Orchestration brain — prompt construction, session lifecycle, verification loop, watch/dialog handling, Claude Commander pane |
| `transport.py` | ~1 700 | tmux-bridge wrapper — `read_pane`, `send_keys`, dialog detection, read-guard enforcement, PR tracking |
| `thinking.py` | ~430 | Pre-start brainstorming with Claude Code — problem decomposition and analysis |
| `verifier.py` | ~440 | Quality gates — security scope, secret-leak detection, symlink defense, hardlink detection, acceptance tests |
| `poller.py` | ~147 | Adaptive polling — exponential back-off 5 s → 120 s, heartbeat timeout |
| `scheduler.py` | ~146 | FIFO queue, `max_parallel` throttling, auto-dequeue on slot availability |
| `config.py` | ~235 | Persistent JSON config with type coercion and validated ranges |
| `dashboard.py` | ~186 | Rich live terminal dashboard — task table, queue panel, event stream |
| `ceo_log.py` | ~150 | CEO session event logging — structured JSONL with categories |
| `errors.py` | ~48 | Domain-specific exception hierarchy |

---

## 3. File Protocol Specification

The file protocol is the **sole** communication channel between Commander and
Executor.  Every piece of state is a file on disk — no sockets, no IPC.

### Directory Layout

```
~/.duo/
├── config.json                          # Global settings
├── tasks/
│   ├── {task_id}/
│   │   ├── task.json                    # Task state (FSM status, step, attempt, incarnation)
│   │   ├── journal.jsonl                # Append-only event log (auto-rotates at 10 MB)
│   │   ├── heartbeat.json              # Latest executor heartbeat
│   │   └── steps/
│   │       ├── step-0001/
│   │       │   ├── prompt-attempt-01.txt    # Commander → Executor: the prompt text
│   │       │   ├── ack-attempt-01.json      # Executor → Commander: "I received the prompt"
│   │       │   └── result-attempt-01.json   # Executor → Commander: "I'm done"
│   │       └── step-0002/
│   │           └── ...
│   └── _corrupted/                      # Auto-quarantined corrupted tasks
│       └── {task_id}-{timestamp}/
└── watch-events/                        # Dialog-detection signal files
    └── {task_id}-{timestamp}.json
```

Worktrees live outside `~/.duo/`:

```
~/.duo/worktrees/
└── {task_id}/                           # Isolated git worktree per task
    ├── .git
    ├── CLAUDE.md                        # Auto-generated project context
    └── ...
```

### JSON Schemas

**task.json** — master record for a task:

```json
{
  "id": "my-task",
  "description": "Fix auth bug",
  "worktree": "~/.duo/worktrees/my-task",
  "branch": "duo/my-task",
  "base_commit": "abc123",
  "pane_label": "my-task",
  "incarnation_id": "a1b2c3d4",
  "status": "running",
  "current_step": 1,
  "current_attempt": 1,
  "subtasks": [
    {
      "step_id": 1,
      "description": "...",
      "target_files": ["src/auth.py"],
      "writable_paths": ["src/**"],
      "acceptance": "pytest tests/test_auth.py",
      "forbidden_commands": []
    }
  ],
  "created_at": "2026-04-08T00:00:00Z",
  "security_policy": {
    "writable_paths": ["src/**"],
    "secret_patterns": ["API_KEY=", "PASSWORD=", "TOKEN=", "SECRET=", "PRIVATE_KEY", "Authorization: Bearer"],
    "forbidden_commands": [],
    "allow_network": false,
    "require_human_approval": ["delete_file", "modify_config", "change_dependency"]
  },
  "last_prompt_sent_at": "2026-04-08T00:01:00Z"
}
```

**ack-attempt-NN.json** — Executor acknowledges receipt:

```json
{
  "step": 1,
  "attempt": 1,
  "incarnation": "a1b2c3d4",
  "prompt_hash": "sha256:...",
  "acked_at": "2026-04-08T00:01:05Z"
}
```

**heartbeat.json** — Executor alive signal:

```json
{
  "ts": "2026-04-08T00:02:30Z",
  "incarnation": "a1b2c3d4",
  "step": 1,
  "status": "working",
  "current_file": "src/auth.py"
}
```

**result-attempt-NN.json** — Executor reports completion:

```json
{
  "step": 1,
  "attempt": 1,
  "incarnation": "a1b2c3d4",
  "status": "done",
  "files_changed": ["src/auth.py", "tests/test_auth.py"],
  "summary": "Fixed token refresh logic"
}
```

### Atomic File Writes

All file writes use a crash-safe sequence to prevent partial reads:

1. Validate payload size (10 MB limit)
2. Serialize to JSON
3. Write to temporary file `.{name}.{pid}.{uuid}.tmp`
4. `fsync(file)` — flush to storage
5. Atomic `rename()` into final path
6. `fsync(parent_directory)` — flush directory entry
7. Clean up temp file on error

`write_json()` additionally rejects:
- Paths containing `..` (path traversal protection)
- Symlink destinations
- Payloads exceeding 10 MB

### Journal

`journal.jsonl` is an append-only event log.  Every state transition, prompt
send, ack, result, error, and dialog event is recorded as a single JSON line:

```jsonl
{"ts":"...","event":"status_changed","data":{"from":"created","to":"session_starting"}}
{"ts":"...","event":"prompt_sent","data":{"step":1,"attempt":1}}
{"ts":"...","event":"ack_received","data":{"step":1,"attempt":1}}
```

The journal auto-rotates when it exceeds 10 MB (keeps the last half).
`replay_state()` walks the journal to reconstruct FSM state after a crash.

---

## 4. Finite State Machine

### 13 States (TaskStatus enum)

| State | Meaning |
|-------|---------|
| `CREATED` | Task record written, not yet started |
| `QUEUED` | Waiting for a parallel slot to open |
| `SESSION_STARTING` | tmux pane being created / bootstrap prompt sent |
| `PROMPT_SENT` | Task prompt delivered to Executor |
| `ACKED` | Executor acknowledged receipt of prompt |
| `RUNNING` | Executor actively working (heartbeats arriving) |
| `RESULT_REPORTED` | Executor wrote a result file |
| `VERIFYING` | Commander running quality-gate checks |
| `CORRECTING` | Sending a correction prompt after verification failure |
| `BLOCKED` | Dialog or permission prompt detected — needs intervention |
| `FAILED` | Unrecoverable error (can restart) |
| `COMPLETED` | All steps passed verification — terminal state |
| `ESCALATED` | Exceeded max corrections or PR budget — needs human |

### Transition Table

```
CREATED          → { SESSION_STARTING, QUEUED, BLOCKED }
QUEUED           → { SESSION_STARTING, FAILED, BLOCKED }
SESSION_STARTING → { PROMPT_SENT, FAILED, BLOCKED }
PROMPT_SENT      → { ACKED, PROMPT_SENT, FAILED, VERIFYING, RUNNING, BLOCKED }
ACKED            → { RUNNING, RESULT_REPORTED, FAILED, BLOCKED }
RUNNING          → { RESULT_REPORTED, BLOCKED, FAILED }
RESULT_REPORTED  → { VERIFYING, FAILED, BLOCKED }
VERIFYING        → { PROMPT_SENT, CORRECTING, COMPLETED, ESCALATED, BLOCKED, FAILED }
CORRECTING       → { ACKED, ESCALATED, PROMPT_SENT, FAILED, BLOCKED }
BLOCKED          → { SESSION_STARTING, PROMPT_SENT, ESCALATED, FAILED }
ESCALATED        → { PROMPT_SENT, FAILED, BLOCKED }
FAILED           → { SESSION_STARTING }
COMPLETED        → { }   ← terminal, no outgoing transitions
```

> **Note:** BLOCKED is reachable from nearly every non-terminal state because
> dialog/permission prompts can appear at any time during Executor operation.

### State Diagram

```
                        ┌──────────────────────────────────────────────┐
                        │                                              │
                        ▼                                              │
                   ┌─────────┐                                         │
              ┌───▶│ CREATED  │                                        │
              │    └────┬─────┘                                        │
              │         │                                              │
              │    ┌────┴─────┐     slot available                     │
              │    │          ├──────────────────┐                     │
              │    ▼          │                  ▼                     │
              │  QUEUED ──────┘          SESSION_STARTING              │
              │                                 │                     │
              │                                 ▼                     │
              │                           PROMPT_SENT ◄───────┐      │
              │                                 │             │      │
              │                                 ▼             │      │
              │                              ACKED            │      │
              │                                 │             │      │
              │                                 ▼             │      │
              │                             RUNNING           │      │
              │                                 │             │      │
              │                                 ▼             │      │
              │                         RESULT_REPORTED       │      │
              │                                 │             │      │
              │                                 ▼             │      │
              │                            VERIFYING          │      │
              │                           /    |    \         │      │
              │                          /     |     \        │      │
              │                         ▼      ▼      ▼      │      │
              │               COMPLETED  CORRECTING  ESCALATED│      │
              │                  (end)        │               │      │
              │                               └───────────────┘      │
              │                                                      │
              │    ┌────────┐                                        │
              └────│ FAILED │◄───── (any non-terminal state) ────────┘
                   └────────┘
                        ▲
                        │
                   ┌────┴────┐
                   │ BLOCKED │ ◄── dialog / permission detected
                   └─────────┘
```

### Key Flows

**Happy path:**
`CREATED → SESSION_STARTING → PROMPT_SENT → ACKED → RUNNING → RESULT_REPORTED → VERIFYING → COMPLETED`

**Correction loop** (max 3 by default, then escalate):
`VERIFYING → CORRECTING → PROMPT_SENT → ACKED → RUNNING → RESULT_REPORTED → VERIFYING → …`

**Recovery after failure:**
`FAILED → SESSION_STARTING → PROMPT_SENT → …`

**Queue flow:**
`CREATED → QUEUED → SESSION_STARTING → …` (when no parallel slot available)

### Incarnation IDs

Each session generates a fresh 16-character hex ID (64-bit).  All heartbeats, acks, and
results carry this incarnation ID.  Readers reject data whose incarnation does
not match `task.incarnation_id`, preventing stale files from a crashed session
from corrupting the current run.

---

## 5. Security Model

Security is enforced in layers, from the broadest scope down to individual
characters in a diff.

### Layer 1 — Writable Path Enforcement

Changed files are checked against `writable_paths` glob patterns using
`PurePosixPath.match()`.  Any file outside the allowlist causes a **hard
rejection** and triggers the correction loop.

### Layer 2 — Secret Leak Detection

The verifier scans **added diff lines** (lines starting with `+`, excluding
`+++` headers) for 63 literal patterns covering:

- Generic credentials: `API_KEY=`, `PASSWORD=`, `TOKEN=`, `SECRET=`, `PRIVATE_KEY`, `Authorization: Bearer`
- GitHub tokens: `github_pat_`, `ghp_`, `gho_`, `ghs_`, `ghr_`, `ghu_`, `GITHUB_TOKEN=`, `GH_TOKEN=`
- AI platform keys: `sk-proj-`, `sk-ant-`, `ANTHROPIC_API_KEY=`, `OPENAI_API_KEY=`, `HF_TOKEN=`, `REPLICATE_API_TOKEN=`
- Cloud credentials: `AKIA`, `ASIA`, `AWS_SECRET_ACCESS_KEY=`, `AZURE_CLIENT_SECRET=`, `ya29.`
- Private keys: `-----BEGIN RSA PRIVATE KEY`, `-----BEGIN OPENSSH PRIVATE KEY`, `-----BEGIN PGP PRIVATE KEY BLOCK`, etc.
- Service tokens: `xoxb-`, `xoxp-`, `glpat-`, `npm_`, `SG.`, `sq0csp-`, `sq0atp-`
- Database URIs: `DATABASE_URL=`, `REDIS_URL=`, `MONGODB_URI=`
- JWT tokens: `eyJhbGci` (Base64 JWT header)

Matching uses `re.escape()` + `re.IGNORECASE`, so patterns are treated as
literal strings.  A match is a **hard rejection**.

### Layer 3 — Forbidden Commands

Each subtask can define a `forbidden_commands` blocklist.  The Executor is
instructed not to run these commands.

### Layer 4 — PR Budget

The `pr_budget` config (default 0 = unlimited) caps the number of Premium
Requests consumed per task.  Exceeding the budget triggers escalation to a
human.  All PR consumption is logged in the task journal:

| Action | PR Cost |
|--------|---------|
| `send_bootstrap()` | 1 (locked — once per pane) |
| `send_task_prompt()` | 1 |
| `select_dialog_option()` | 1 |
| `approve_permission()` | 1 |

### Layer 5 — Read-Before-Interact Guard

Every `type_text()` or `send_keys()` call in the transport layer **must** be
preceded by a `read_pane()` call.  This ensures the Commander always knows the
current pane state before sending input, preventing blind writes to unknown
terminal states.

### Layer 6 — Label Sanitization

Pane labels are validated against `^[a-zA-Z0-9_.-]+$` before any tmux
operation.  This prevents shell injection through crafted label strings.

### Layer 7 — Path Traversal Protection

`write_json()` rejects any path containing `..` components and refuses to follow
symlinks, preventing writes outside the intended directory tree.

### Layer 8 — shell=False Everywhere

All `subprocess` calls use list-form arguments (`shell=False`), eliminating
shell-injection attack surface.

---

## 6. Transport Layer

All Executor interaction flows through `transport.py`, which wraps the external
`tmux-bridge` binary.

### Bridge Function

```python
bridge(cmd: list[str], check: bool = True) → str
```

- **Retry policy:** 3 attempts, 0.5 s base delay, 2.0× back-off, ±20% jitter
- **Timeout:** 30 s per call
- Runs `tmux-bridge` as a subprocess (`shell=False`)

### Core Operations

| Function | Purpose |
|----------|---------|
| `read_pane(label, lines=50)` | Read terminal output; satisfies read guard |
| `type_text(label, text)` | Type text into pane (requires prior `read_pane`) |
| `send_keys(label, *keys)` | Send special keys (Enter, C-c, C-d) |
| `send_bootstrap(label, prompt)` | Bootstrap prompt — 1 PR, permanently locked per pane |
| `send_shell_command(label, cmd)` | Shell command — no PR, only when not at `❯` prompt |
| `send_prompt()` | **Permanently banned** — always raises to prevent accidental PR burn |

### Dialog Detection

| Function | Mechanism |
|----------|-----------|
| `is_in_dialog(label)` | Checks for `╭─` / `╰─` box-drawing characters + numbered options |
| `is_in_dialog_stable(label)` | Double-reads with 1 s gap — both must agree |
| `is_permission_dialog(label)` | Checks for "Do you want to…", "Allow directory" |
| `wait_for_dialog(label, timeout, interval)` | Blocks until a stable dialog appears |

### Dialog Selection (all triple-safety checked)

| Function | Behaviour |
|----------|-----------|
| `safe_enter(label)` | Press Enter **only** if not at `❯` prompt — prevents accidental PR burn |
| `select_dialog_option(label, option)` | Select a numbered option in a dialog |
| `select_other_option(label, text)` | Navigate to last option ("Other"), type custom text, submit |
| `approve_permission(label)` | Auto-approve: picks "approve for session" > "Yes" > fallback |

### Spinner Detection

Active processing is detected by scanning for spinner markers in the pane:

```
◉   ◎   ○
```

If any of these markers are present (with a trailing space), the Executor is
still working and the pane is **not** idle.

### Main Prompt Detection

`_is_at_main_prompt(content)` determines whether the pane is idle at the
Copilot `❯` prompt.  The check is conservative — it returns `True` only when
**all** of the following hold:

1. No spinner markers (`◉ `, `◎ `, `○ `) present anywhere in the pane
2. No dialog-box borders (`╭─`, `╰─`) present anywhere in the pane
3. The last significant line starts with `❯` and contains `Type @`, `mention
   files`, or is exactly `❯`

This three-part check prevents false positives: the `❯` prompt is always
rendered by Copilot CLI (even during processing or inside dialogs), so its
presence alone is insufficient.

---

## 7. Orchestration (Commander)

`commander.py` is the orchestration brain.  It manages the full lifecycle:

### Session Lifecycle

1. **`start_session(task, defer=True)`** — Create tmux pane. In defer mode
   (default), Copilot launches but waits for `duo send` before consuming a PR.
   With `defer=False` (or `--immediate`), sends bootstrap prompt immediately (1 PR).
   Optionally opens Claude Commander pane.
2. **`restart_session(task)`** — Kill pane, clear bootstrap lock, re-create,
   re-bootstrap, optionally reopen Claude Commander.

### Prompt Construction

| Builder | Purpose |
|---------|---------|
| `build_bootstrap_prompt(task)` | File-protocol instructions for the Executor |
| `build_task_prompt(task)` | Current step + attempt + writable paths |
| `build_continue_prompt(task)` | Resume without consuming a PR |
| `build_correction_prompt(task, reason)` | Explain what failed + retry |
| `write_commander_claude_md(task)` | Auto-generate `CLAUDE.md` in worktree |

### Monitoring Loop

`monitor(task_ids)` runs a continuous loop per task:

1. `poll_task(task, poller)` — Adaptive polling for result files
2. On `RESULT_READY`: `verify_and_advance(task)`
3. On verification pass: advance to next step or `COMPLETED`
4. On verification failure: send correction prompt (up to `max_corrections`)
5. After exhausting corrections: transition to `ESCALATED`
6. All events journaled

### Verification Flow

```
result file found
       │
       ▼
  ┌──────────┐    Pass    ┌───────────┐
  │ VERIFYING ├──────────▶│ next step │──▶ … ──▶ COMPLETED
  └────┬─────┘            └───────────┘
       │ Correction
       ▼
  ┌────────────┐   attempt < max    ┌─────────────┐
  │ CORRECTING ├──────────────────▶ │ PROMPT_SENT │──▶ (retry)
  └────┬───────┘                    └─────────────┘
       │ attempt >= max_corrections
       ▼
   ESCALATED (human intervention)
```

### Dialog / Permission Handling

`watch_tasks()` monitors panes for permission dialogs.  When detected:
- Writes a signal file to `~/.duo/watch-events/`
- If `auto_allow_all` is `True`: auto-approves immediately
- If `False`: waits for CEO intervention (`ceo-approve` / `ceo-select`)

---

## 8. Adaptive Polling

`AdaptivePoller` adjusts its polling interval based on Executor activity:

| Parameter | Value | Purpose |
|-----------|-------|---------|
| `BASE_INTERVAL` | 5 s | Interval right after prompt send |
| `MAX_INTERVAL` | 120 s | Cap during stable cruising |
| `RAMP_FACTOR` | 1.5× | Multiplier per cycle when heartbeat is active |
| `HEARTBEAT_TIMEOUT` | 90 s | No heartbeat for this long → timeout |

**Poll results:**

| Result | Meaning |
|--------|---------|
| `RESULT_READY` | Result file found — highest priority |
| `WORKING` | Heartbeat active — ramp interval up |
| `HEARTBEAT_TIMEOUT` | No heartbeat for 90 s — diagnose pane |
| `UNKNOWN` | No state information available |

---

## 9. Scheduler

FIFO queue with parallel-slot management:

- `enqueue_or_start(task)` — Start immediately if a slot is available; otherwise
  queue the task as `QUEUED`.
- `promote_queued()` — When a slot frees up, dequeue the oldest `QUEUED` task
  and transition it to `SESSION_STARTING`.
- `max_parallel` (default 3, configurable 1–100) — Maximum concurrent tasks.

**Slot-consuming states:**
`SESSION_STARTING`, `PROMPT_SENT`, `ACKED`, `RUNNING`, `RESULT_REPORTED`,
`VERIFYING`, `CORRECTING`, `BLOCKED`, `ESCALATED`

---

## 10. Configuration

Persistent settings in `~/.duo/config.json` with type coercion and range
validation.

| Key | Default | Range | Purpose |
|-----|---------|-------|---------|
| `copilot_model` | `"claude-opus-4.6"` | — | Model for Copilot CLI |
| `max_corrections` | `3` | 1–100 | Correction attempts before escalation |
| `heartbeat_timeout` | `90` | 1–3 600 s | Seconds before heartbeat timeout |
| `poll_base_interval` | `5.0` | 0–300 s | Initial polling interval |
| `poll_max_interval` | `120.0` | 0–3 600 s | Maximum polling interval |
| `auto_allow_all` | `true` | bool | Auto-approve permission dialogs |
| `auto_claude_commander` | `true` | bool | Open Claude Code pane alongside |
| `bypass_permissions` | `true` | bool | Add `--yolo` (Copilot) / `--dangerously-skip-permissions` (Claude) |
| `max_parallel` | `3` | 1–100 | Maximum concurrent tasks |
| `pr_budget` | `0` | int | PR cap per task (0 = unlimited) |
| `task_timeout` | `0` | int | Max seconds per task (0 = disabled) |
| `worktree_base_path` | `"~/.duo/worktrees"` | path | Where git worktrees are created |

---

## 11. CLI Commands

### Task Lifecycle

`start` · `send` · `stop` · `status` · `merge` · `diff` · `kill`

### Monitoring

`list` · `monitor` · `watch` · `dashboard` · `logs` · `inspect` · `stats`

### Batch & Queue

`batch` · `queue`

### CEO Workflow

`ceo-wait` · `ceo-select` · `ceo-approve` · `ceo-status`

### Recovery

`recover` · `resume` · `retry`

### Data & Audit

`export` · `audit` · `cost` · `cleanup` · `events` (subcommands: `list` · `show` · `tail` · `clear`)

### Planning & Setup

`think` · `go` · `init` · `doctor` · `config` (subcommands: `get` · `set` · `list` · `reset`)

### Miscellaneous

`version` · `completion`
