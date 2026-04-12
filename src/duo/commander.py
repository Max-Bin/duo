"""Commander — orchestration brain.

Handles task lifecycle: create sessions, send prompts, poll for results,
verify output, correct or advance, and manage session recovery.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path

import click

__all__ = [
    "monitor",
    "restart_session",
    "send_task_prompt",
    "start_claude_commander",
    "start_session",
    "watch_tasks",
    "write_commander_claude_md",
    "write_project_claude_md",
]

logger = logging.getLogger(__name__)

from duo.config import get_config
from duo.errors import TaskLockedError
from duo.poller import AdaptivePoller, PollResult, age
from duo.protocol import (
    DUO_DIR,
    StepResult,
    Task,
    TaskStatus,
    append_event,
    atomic_write_text,
    list_tasks,
    load_task,
    new_incarnation,
    now_iso,
    prompt_hash,
    read_ack_for_step,
    read_jsonl,
    read_result_for_step,
    save_task,
    task_lock,
    transition,
    write_json,
)
from duo.transport import (
    approve_permission,
    clear_bootstrap_done,
    detect_copilot_api_error,
    diagnose_pane,
    get_tmux_session_target,
    is_at_main_prompt,
    is_capi_context_error,
    is_process_alive,
    kill_pane,
    name_pane,
    read_pane,
    select_dialog_option,
    send_bootstrap,
    send_shell_command,
    send_slash_command,
    wait_for_dialog,
    wait_for_idle,
)
from duo.verifier import Correction, Pass, verify_step

# Named constants for sleep durations (seconds)
_SESSION_SPLIT_WAIT = 0.5
_SESSION_CD_WAIT = 0.3

_TERMINAL_SLICE = 500  # chars of terminal output to include in events
_IDLE_GRACE_SECONDS = 30  # seconds before considering a prompted task idle
_LOG_LOCK = threading.Lock()  # guards _log_monitor output across watch threads
_WATCH_EVENTS_DIR = DUO_DIR / "watch-events"

# Dialog wait timeouts (seconds)
_DIALOG_TIMEOUT_SEND = 60.0
_DIALOG_TIMEOUT_RESEND = 30.0
_DIALOG_TIMEOUT_MONITOR = 15.0
_IDLE_TIMEOUT_START = 30.0
_IDLE_TIMEOUT_ALLOW_ALL = 10  # seconds to wait for /allow-all
_MAX_CONSECUTIVE_POLL_ERRORS = 10

# Common exception tuple for transport/subprocess errors
_TRANSPORT_ERRORS = (
    RuntimeError,
    subprocess.CalledProcessError,
    subprocess.TimeoutExpired,
    OSError,
)


def _get_copilot_model() -> str:
    """Get copilot model from config, env var override, or default."""
    env_model = os.environ.get("DUO_COPILOT_MODEL")
    if env_model:
        if not re.match(r"^[a-zA-Z0-9._-]+$", env_model) or len(env_model) > 64:
            logger.warning(
                "DUO_COPILOT_MODEL contains invalid chars or exceeds 64 chars, using config default"
            )
            return get_config("copilot_model") or "claude-opus-4.6"
        return env_model
    return get_config("copilot_model") or "claude-opus-4.6"


# === Session Bootstrap ===

SESSION_BOOTSTRAP_TEMPLATE = """\
你现在进入 Duo 协作模式。你是执行者（Executor），有一个 Commander 在监控你的工作。

## 文件协议 v1

任务目录: {task_dir}
步骤目录: {task_dir}/steps/step-{{NNNN}}/

### 必须遵守：

1. 收到指令后立即写 ack:
   文件: steps/step-{{N}}/ack-attempt-{{A}}.json
   内容: {{"step":N, "attempt":A, "incarnation":"{incarnation}", "prompt_hash":"从 #hash 获取", "acked_at":"ISO时间"}}

2. 每修改一个文件后更新 heartbeat:
   文件: heartbeat.json
   内容: {{"ts":"ISO时间", "incarnation":"{incarnation}", "step":N, "status":"working", "current_file":"路径"}}

3. 完成后写 result:
   文件: steps/step-{{N}}/result-attempt-{{A}}.json
   内容: {{"step":N, "attempt":A, "incarnation":"{incarnation}", "status":"done", "files_changed":["路径"], "summary":"一句话"}}

4. 代码直接写入 worktree 文件，不在终端打印完整代码

5. 遇到阻塞写 result 并设 status 为 "blocked" 或 "error"

6. 只修改任务指定的 writable_paths 中的文件

当前 incarnation: {incarnation}
"""

# Template for CLAUDE.md placed in the worktree so Claude Code CLI
# automatically learns its Commander role when opened in that directory.
CLAUDE_COMMANDER_TEMPLATE = """\
# Duo CEO Operating Manual

## Identity

You are the **CEO** (Commander) of task **{task_id}**.
Left pane = you (Claude Code). Right pane = Copilot CLI (Executor).
The user talks to you. You operate all `duo` commands on their behalf.
**The user never runs `duo` commands directly — you are the sole operator.**

## Defer Workflow (CRITICAL)

The Copilot pane is already open but **has NOT received any prompt yet**
(defer mode). This means:

1. **Talk to the user first** — clarify requirements, design the approach
2. Use `duo ceo-status {task_id}` to confirm Copilot is idle at ❯ prompt
3. When ready, send the first instruction:
   ```bash
   duo send {task_id} "Your detailed instruction here"
   ```
   This consumes **1 Premium Request** — make it count.

## Premium Request Budget (IRON RULES)

- Every prompt at Copilot's main ❯ prompt = **1 Premium Request (PR)**
- Dialog options (`duo ceo-select`, `duo ceo-approve`) and Other text
  input are **FREE** — they don't consume PRs
- **NEVER type directly at the Copilot ❯ prompt** (unless recovering
  from a system crash)
- Use `duo watch` + `duo ceo-select/approve` to operate within dialogs
- Quality over speed — one well-crafted prompt beats five sloppy ones

## Command Reference

### Core Workflow
| Command | Purpose | Example |
|---------|---------|---------|
| `duo send {task_id} "..."` | Send instruction to Copilot (costs 1 PR) | `duo send {task_id} "Add JWT auth to src/auth.py"` |
| `duo status {task_id}` | Check task status and current step | |
| `duo watch` | Watch for Copilot dialog events | `duo watch --once` |
| `duo ceo-select {task_id} N` | Select option N in a dialog (FREE) | `duo ceo-select {task_id} 1` |
| `duo ceo-approve {task_id}` | Approve/accept current dialog (FREE) | |

### Monitoring & Debugging
| Command | Purpose |
|---------|---------|
| `duo ceo-status {task_id}` | Show Copilot pane state (JSON: idle/processing/dialog/dead) |
| `duo diff {task_id}` | Show code changes in worktree |
| `duo inspect {task_id}` | Detailed task info (steps, attempts, events) |
| `duo logs {task_id}` | Show task journal events |
| `duo doctor` | Health check: tmux, Copilot, fd leaks, session health |

### Session Management
| Command | Purpose |
|---------|---------|
| `duo stop {task_id}` | Stop task and kill pane |
| `duo start --resume` | Resume stopped task in fresh session |
| `duo monitor` | Start automated polling monitor |
| `duo list` | List all tasks |

## Rubber-duck Quality Protocol

Before non-trivial decisions, use independent validation:

- **Mode A (Before)**: Critique plan BEFORE sending to Copilot
- **Mode B (After)**: Review Copilot's output BEFORE accepting (primary mode)
- **Mode C (Sandwich)**: Both before and after for critical changes

**Trigger conditions**: architectural decisions, security-sensitive code,
multi-file changes, unfamiliar codebases, anything the user would regret
if done wrong.

**Speed does NOT matter. Quality does.**

## Known Pitfalls & Lessons

1. **send_keys uses raw hex** — `tmux send-keys -H 0d` not key names
2. **Select pane first** — `tmux select-pane -t <target>` before sending
3. **Long sessions leak** — kqueue/fd leak is upstream Copilot bug.
   Run `duo doctor` periodically. Use `duo stop` + `duo start --resume` to recover.
4. **No parallel same-file edits** — never let Copilot sub-agents edit
   the same file concurrently
5. **CAPIError = context exhaustion** — if you see this in the pane,
   the session needs restart (`duo stop` + `duo start --resume`)

## Current Task Context

- **Task ID:** {task_id}
- **Worktree:** {worktree}
- **Branch:** {branch}
- **Incarnation:** {incarnation}
- **Task directory:** {task_dir}
- **Copilot pane:** `{pane_label}`

## File Protocol v1

The executor writes JSON files in `{task_dir}`:

| File | When | Content |
|------|------|---------|
| `steps/step-NNNN/ack-attempt-AA.json` | After receiving instruction | `{{"step":N, "attempt":A, "incarnation":"..."}}` |
| `heartbeat.json` | After each file edit | `{{"ts":"ISO", "step":N, "status":"working"}}` |
| `steps/step-NNNN/result-attempt-AA.json` | After completing work | `{{"step":N, "attempt":A, "status":"done", "files_changed":[...]}}` |

## Quick Start Sequence

```bash
# 1. Confirm Copilot is ready (should show "idle at prompt")
duo ceo-status {task_id}

# 2. Discuss requirements with user, then send first instruction
duo send {task_id} "Implement X with Y approach as discussed"

# 3. Monitor progress
duo watch --once

# 4. Handle dialogs (FREE — no PR cost)
duo ceo-select {task_id} 1
duo ceo-approve {task_id}

# 5. Review output
duo diff {task_id}

# 6. If correction needed (costs 1 PR)
duo send {task_id} "Fix: the login endpoint needs rate limiting"

# 7. Periodic health check
duo doctor
```

## Rules

- **You** plan, review, and coordinate. **Copilot** writes code.
- Do NOT edit code files directly — that's the executor's job.
- Keep instructions specific, verifiable, and self-contained.
- One instruction at a time for best results.
- Review changes with `duo diff` before approving.
- Protect the PR budget — every `duo send` costs 1 PR.
"""

_DUO_MANAGED_START = "<!-- duo-managed-start -->"
_DUO_MANAGED_END = "<!-- duo-managed-end -->"

PROJECT_CEO_TEMPLATE = """\
{marker_start}
# Duo CEO Mode

You are the **CEO** (Commander). The user talks to you naturally about what
they want to build. A Copilot CLI executor is running in an adjacent tmux
pane, ready to write code on your command.

## ABSOLUTE PROHIBITION — Read This First

**YOU DO NOT WRITE CODE. EVER.**

- You do NOT create files, edit files, run scripts, or write any code yourself.
- You do NOT use Bash/Edit/Write tools to modify project source code.
- ALL code changes go through Copilot via `duo send`.
- Your only tools are: `duo` commands, chatting with the user, and thinking.
- If you catch yourself about to write code: STOP. Use `duo send` instead.
- Violating this rule wastes the user's money (your PR are 10x more expensive than Copilot's).

**YOU DO NOT TOUCH TMUX. EVER.**

- You do NOT run `tmux send-keys`, `tmux load-buffer`, `tmux paste-buffer`, `tmux capture-pane`, or ANY raw tmux command.
- ALL tmux interaction is handled by `duo` commands internally.
- If `duo send` fails, report the error — do NOT try to work around it with raw tmux.
- If Copilot needs input, use `duo ceo-select` or `duo ceo-approve` — NEVER raw tmux.

**YOU DO NOT ASK THE USER TO DO MECHANICAL WORK.**

- NEVER ask the user to press keys, click buttons, or perform actions you should handle.
- If a `duo` command fails, try a different `duo` command or report the blocker.
- The user is your boss, not your assistant.

**YOU MUST KEEP COPILOT ALIVE AND WORKING.**

- After your FIRST `duo send`, immediately start a **background** watch:
  `duo watch` (run with `run_in_background: true` so it does NOT block your conversation with the user)
- The background watch will notify you when dialogs appear. Handle them immediately with `duo ceo-select` or `duo ceo-approve`.
- If Copilot exits or crashes, restart with `duo stop <task> && duo start <task>`.
- NEVER leave Copilot idle while you work on something yourself.
- **NEVER run `duo watch` in foreground** — it blocks your ability to chat with the user. ALWAYS use background mode.

## Your Workflow

### Phase 1: Understand (FREE — no Premium Requests burned)
Chat with the user. Clarify requirements. Design the approach. Take your time.

### Phase 2: Start a Task
```bash
duo start <task-name> --reuse-pane duo-copilot-standby
```
This creates a task + worktree and reuses the existing Copilot pane. No PR burned yet.

### Phase 3: Execute (costs 1 PR per instruction)
```bash
duo send <task> "Your detailed instruction to Copilot"
```
Each `duo send` = 1 Premium Request. Make each instruction count.

### Phase 4: Monitor & Guide (FREE)
```bash
duo watch                           # MUST run in BACKGROUND (run_in_background: true)
duo ceo-select <task> N             # Pick option N in a dialog (FREE)
duo ceo-approve <task>              # Accept current dialog (FREE)
duo ceo-status <task>               # Check Copilot state
```
**IMPORTANT**: `duo watch` MUST run as a background task. Never run it in foreground — it blocks conversation.

### Phase 5: Review & Iterate
```bash
duo diff <task>                     # See code changes
duo send <task> "Fix: add rate limiting"  # Correction (1 PR)
```

### Phase 6: Finish
```bash
duo merge <task>                    # Merge changes to main branch
```

## Premium Request Budget (IRON RULES)

| Action | PR Cost |
|--------|---------|
| `duo send` (instruction at ❯ prompt) | **1 PR** |
| `duo ceo-select` (pick dialog option) | FREE |
| `duo ceo-approve` (accept dialog) | FREE |
| Dialog "Other" text input | FREE |
| `duo watch/status/diff/inspect/logs` | FREE |
| Chatting with user (Phase 1) | FREE |

**NEVER type directly at Copilot's ❯ prompt.** Use `duo send` only.

## Command Reference

| Command | Purpose |
|---------|---------|
| `duo start <name> --reuse-pane duo-copilot-standby` | Create task, reuse standby pane |
| `duo send <task> "..."` | Send instruction to Copilot (1 PR) |
| `duo watch` | Watch for Copilot dialogs |
| `duo ceo-select <task> N` | Select dialog option N (FREE) |
| `duo ceo-approve <task>` | Approve dialog (FREE) |
| `duo ceo-status <task>` | Copilot pane state (JSON) |
| `duo diff <task>` | Show code changes |
| `duo inspect <task>` | Detailed task info |
| `duo logs <task>` | Task journal events |
| `duo doctor` | Health check |
| `duo stop <task>` | Stop task and kill pane |
| `duo merge <task>` | Merge changes to main |
| `duo list` | List all tasks |

## Rubber-duck Quality Protocol

- **Mode A (Before)**: Critique your plan BEFORE sending to Copilot
- **Mode B (After)**: Review Copilot's output BEFORE accepting
- **Mode C (Sandwich)**: Both for critical changes

Use for: architectural decisions, security code, multi-file changes.

## Known Pitfalls

1. **Long sessions leak fds** — run `duo doctor` periodically, `duo stop` + `duo start --resume` to recover
2. **CAPIError = context full** — restart the Copilot session
3. **No parallel same-file edits** — don't let sub-agents edit one file concurrently
4. **One task at a time** recommended for best results
5. **Ctrl+Q submits at Copilot prompt** — `tmux send-keys -l` triggers multi-line mode where Enter inserts newline. Use `tmux send-keys -t <pane> C-q` to submit. Ctrl+S opens slash menu (WRONG).
6. **Copilot input freezes after long run** — `tmux send-keys` silently fails. Fix: Ctrl+C twice → `copilot --resume=<id>`. Ctrl+Q only works on freshly resumed sessions.

## Iron Rules

### Dialog Selection — NEVER Let Copilot Stop

When Copilot shows an `ask_user` dialog with options, you MUST select an option that continues real work.

**FORBIDDEN options** — NEVER select these, no matter what:
- "Stand by", "Standing by", "Wait", "Idle", "Pause"
- "Done", "Finish", "Complete", "Stop", "Exit", "收工"
- "Other" (unless you have a specific concrete task to type)
- "OK" / "Acknowledged" (when it means ending work)
- Any option that results in Copilot going idle at the ❯ prompt

**REQUIRED behavior**:
- Always select options that produce code changes, audits, tests, or fixes
- If ALL options are stop/idle variants, use "Other" and type a concrete new task
- Good "Other" inputs: "grep TODO/FIXME/HACK and fix them", "audit accessibility on all screens", "run stricter linter rules and fix warnings", "check memory leaks", "improve test edge cases"
- Copilot must NEVER return to the idle ❯ prompt. Every dialog must lead to more work.
- If Copilot claims "everything is done": it is WRONG. There is always more to improve.

### CI Prohibition
**NEVER** create `.github/workflows/` directory or any CI/CD workflow files. CI is permanently disabled. Past CI creation flooded the user with thousands of failure emails.

### Value Self-Assessment
Every progress report **must** include:
```
VALUE: HIGH / MEDIUM / LOW
REASON: one sentence
```
- **HIGH**: Fixed a real bug, added user-requested feature, prevented security risk
- **MEDIUM**: Improved code quality/docs/error messages
- **LOW**: Added tests without finding bugs, badge updates, duplicated linter checks

If 3 consecutive rounds produce only LOW value, proactively suggest pausing.

{marker_end}
"""


def write_project_claude_md(repo_root: str) -> None:
    """Write or update project-level CLAUDE.md with CEO operating manual.

    Merge strategy:
    - No existing CLAUDE.md → create with full template
    - Existing with duo markers → replace duo section, preserve rest
    - Existing without markers → prepend duo section with markers
    """
    from pathlib import Path

    claude_md = Path(repo_root) / "CLAUDE.md"
    duo_section = PROJECT_CEO_TEMPLATE.format(
        marker_start=_DUO_MANAGED_START,
        marker_end=_DUO_MANAGED_END,
    )

    # Insert auto-detected project context before the end marker
    project_ctx = _detect_project_context(repo_root)
    if project_ctx.strip():
        ctx_block = "\n## Project Context\n\n" + project_ctx + "\n\n"
        duo_section = duo_section.replace(
            _DUO_MANAGED_END, ctx_block + _DUO_MANAGED_END
        )

    try:
        if claude_md.exists():
            existing = claude_md.read_text()
            if _DUO_MANAGED_START in existing and _DUO_MANAGED_END in existing:
                # Replace existing duo section
                before = existing[: existing.index(_DUO_MANAGED_START)]
                after = existing[
                    existing.index(_DUO_MANAGED_END) + len(_DUO_MANAGED_END) :
                ]
                before_stripped = before.rstrip("\n")
                prefix = before_stripped + "\n" if before_stripped else ""
                content = prefix + duo_section + after.lstrip("\n")
            else:
                # Prepend duo section, preserve existing content
                content = duo_section + "\n" + existing
        else:
            content = duo_section

        atomic_write_text(claude_md, content)
        logger.info("Wrote project CLAUDE.md to %s", claude_md)
    except OSError as exc:
        logger.warning("Failed to write project CLAUDE.md: %s", exc)


def build_bootstrap_prompt(task: Task, *, override_prompt: str = "") -> str:
    """Build the initial session bootstrap prompt with file protocol instructions.

    When *override_prompt* is provided (deferred start), the protocol header
    is followed by the user's actual instruction instead of the default
    task description.
    """
    base = SESSION_BOOTSTRAP_TEMPLATE.format(
        task_dir=str(task.dir),
        incarnation=task.incarnation_id,
    )
    if override_prompt:
        return base + f"\n## 第一个任务\n\n{override_prompt}\n"
    return base


def _detect_project_context(worktree: str) -> str:
    """Auto-detect project type and context from the worktree."""
    from pathlib import Path

    root = Path(worktree)
    sections: list[str] = []

    # Detect project type
    project_type = "Unknown"
    build_cmd = ""
    test_cmd = ""
    if (root / "pyproject.toml").exists():
        project_type = "Python"
        build_cmd = "uv sync"
        test_cmd = "python -m pytest"
    elif (root / "package.json").exists():
        project_type = "Node.js"
        build_cmd = "npm install"
        test_cmd = "npm test"
    elif (root / "Cargo.toml").exists():
        project_type = "Rust"
        build_cmd = "cargo build"
        test_cmd = "cargo test"
    elif (root / "go.mod").exists():
        project_type = "Go"
        build_cmd = "go build ./..."
        test_cmd = "go test ./..."
    elif (root / "pom.xml").exists():
        project_type = "Java (Maven)"
        build_cmd = "mvn package"
        test_cmd = "mvn test"

    sections.append(f"- **Type:** {project_type}")
    if build_cmd:
        sections.append(f"- **Build:** `{build_cmd}`")
    if test_cmd:
        sections.append(f"- **Test:** `{test_cmd}`")

    # Read .duo/instructions.md if it exists
    instructions = root / ".duo" / "instructions.md"
    if instructions.exists():
        try:
            text = instructions.read_text().strip()
            if text and "<!-- " not in text[:200]:
                sections.append(f"\n### Project Instructions\n\n{text[:2000]}")
        except OSError:
            pass

    # Read first part of README
    for readme_name in ("README.md", "readme.md", "README.rst", "README"):
        readme = root / readme_name
        if readme.exists():
            try:
                text = readme.read_text()[:1000].strip()
                if text:
                    sections.append(f"\n### README (excerpt)\n\n{text}")
            except OSError:
                pass
            break

    # List top-level directory structure (exclude CLAUDE.md to keep idempotent)
    try:
        entries = sorted(
            p.name
            for p in root.iterdir()
            if not p.name.startswith(".") and p.name != "CLAUDE.md"
        )
        if entries:
            tree = "  ".join(entries[:30])
            sections.append(f"\n### Directory\n\n`{tree}`")
    except OSError:
        pass

    return "\n".join(sections)


def write_commander_claude_md(task: Task) -> None:
    """Write CLAUDE.md into the task worktree so Claude Code CLI picks it up.

    Includes auto-detected project context (type, instructions, README).
    """
    from pathlib import Path

    base = CLAUDE_COMMANDER_TEMPLATE.format(
        task_dir=str(task.dir),
        task_id=task.id,
        worktree=task.worktree,
        branch=task.branch,
        incarnation=task.incarnation_id,
        pane_label=task.pane_label,
    )

    project_ctx = _detect_project_context(task.worktree)
    content = base + "\n## Project Context\n\n" + project_ctx + "\n"

    # Include thinking session plan if it exists
    plan_path = DUO_DIR / "thinking" / task.id / "plan.md"
    try:
        if plan_path.exists():
            plan_text = plan_path.read_text().strip()[:3000]
            if plan_text:
                content += "\n## Thinking Session Plan\n\n" + plan_text + "\n"
    except OSError:
        pass

    claude_md = Path(task.worktree) / "CLAUDE.md"
    try:
        atomic_write_text(claude_md, content)
        logger.info("Wrote CLAUDE.md to %s", claude_md)
    except OSError as exc:
        logger.warning("Failed to write CLAUDE.md to %s: %s", claude_md, exc)


def start_claude_commander(task: Task) -> str | None:
    """Open a tmux pane running Claude Code CLI as the Commander.

    Returns the pane ID on success, None on failure.
    """
    # Write CLAUDE.md so Claude Code auto-discovers its role
    write_commander_claude_md(task)

    # Target the caller's session to prevent cross-session pollution
    try:
        session_target = get_tmux_session_target()
    except RuntimeError:
        logger.warning("Cannot determine tmux session for commander pane")
        return None

    try:
        result = subprocess.run(
            [
                "tmux",
                "split-window",
                "-v",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                session_target,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        logger.warning("tmux split-window timed out for Claude commander")
        return None
    if result.returncode != 0:
        logger.warning("Failed to create Claude commander pane: %s", result.stderr)
        return None

    pane_id = result.stdout.strip()
    commander_label = f"duo-commander-{task.id}"
    try:
        name_pane(pane_id, commander_label)
    except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
        logger.warning("Failed to name commander pane: %s", exc)
        kill_pane(pane_id)
        return None

    # Tile layout — target the new pane to resolve correct window
    try:
        subprocess.run(
            ["tmux", "select-layout", "-t", pane_id, "tiled"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        logger.warning("select-layout timed out for commander pane")

    time.sleep(_SESSION_SPLIT_WAIT)
    try:
        send_shell_command(commander_label, f"cd {shlex.quote(str(task.worktree))}")
        time.sleep(_SESSION_CD_WAIT)
        claude_cmd = "claude"
        if get_config("bypass_permissions"):
            claude_cmd += " --dangerously-skip-permissions"
        send_shell_command(commander_label, claude_cmd)
    except _TRANSPORT_ERRORS as exc:
        logger.warning("Failed to start Claude commander: %s", exc)
        kill_pane(pane_id)
        return None

    append_event(
        task,
        "claude_commander_started",
        {"pane": commander_label, "pane_id": pane_id},
    )
    return pane_id


# === Task Prompt Templates ===


def build_task_prompt(task: Task) -> str:
    """Build prompt for current step+attempt."""
    step = task.current_step
    attempt = task.current_attempt
    if step < 1 or step > len(task.subtasks):
        raise ValueError(f"Step {step} out of range (1..{len(task.subtasks)})")
    subtask = task.subtasks[step - 1]

    target_list = "\n".join(f"- {f}" for f in subtask.target_files)
    writable_list = "\n".join(f"- {f}" for f in subtask.writable_paths)

    prompt = f"""\
## 任务: {subtask.description}
任务目录: {task.dir}
步骤目录: steps/step-{step:04d}/
工作目录: {task.worktree}
incarnation: {task.incarnation_id}  step: {step}  attempt: {attempt}

### 目标文件（task scope，期望修改）
{target_list}

### 安全边界（writable_paths，硬上限）
{writable_list}

### 要求
{subtask.description}

### 协议
- 写 ack → steps/step-{step:04d}/ack-attempt-{attempt:02d}.json
- 每改一个文件更新 heartbeat.json
- 完成后写 result → steps/step-{step:04d}/result-attempt-{attempt:02d}.json
- 代码写入文件，不打印

#hash:{prompt_hash(subtask.description)}"""

    return prompt


def build_continue_prompt(task: Task) -> str:
    """Build continuation prompt (doesn't consume Premium Request)."""
    step = task.current_step
    attempt = task.current_attempt
    if step < 1 or step > len(task.subtasks):
        raise ValueError(f"current_step {step} out of range [1..{len(task.subtasks)}]")
    subtask = task.subtasks[step - 1]
    target_list = ", ".join(subtask.target_files)

    return (
        f"好的。step {step} attempt {attempt}: {subtask.description}。"
        f"目标文件: {target_list}。\n"
        f"incarnation: {task.incarnation_id}\n"
        f"#hash:{prompt_hash(subtask.description)}"
    )


def build_correction_prompt(task: Task, reason: str) -> str:
    """Build correction prompt."""
    step = task.current_step
    attempt = task.current_attempt

    return (
        f"step {step} attempt {attempt}: 上次结果需修正。\n"
        f"问题: {reason}\n"
        f"写 result 到 steps/step-{step:04d}/result-attempt-{attempt:02d}.json\n"
        f"incarnation: {task.incarnation_id}\n"
        f"#hash:{prompt_hash(reason)}"
    )


# === PR Budget ===


def _check_pr_budget(task: Task) -> bool:
    """Check if task has exceeded its PR budget. Returns True if OK to proceed."""
    budget = int(get_config("pr_budget") or 0)
    if budget <= 0:
        return True  # unlimited
    events = read_jsonl(task.journal_path)
    pr_count = sum(1 for ev in events if ev.get("event") == "pr_consumed")
    return pr_count < budget


# === Session Management ===

# States that can directly transition to SESSION_STARTING for restart.
_DIRECTLY_RESTARTABLE = frozenset(
    {
        TaskStatus.CREATED,
        TaskStatus.QUEUED,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
        TaskStatus.SESSION_STARTING,
    }
)


def normalize_for_restart(task: Task) -> bool:
    """Transition a crashed/stuck task to FAILED so it can be restarted.

    Active states (PROMPT_SENT, ACKED, RUNNING, etc.) cannot directly
    transition to SESSION_STARTING.  This helper first moves them to
    FAILED — representing a crash — making the restart path legal.

    Returns True if the task is now in a state that allows SESSION_STARTING.
    """
    if task.status in _DIRECTLY_RESTARTABLE:
        return True
    if not transition(task, TaskStatus.FAILED):
        logger.error(
            "Cannot normalize task '%s' from %s to FAILED for restart",
            task.id,
            task.status.value,
        )
        return False
    return True


def _prepare_pane(task: Task, reuse_pane: str) -> tuple[str, bool] | None:
    """Create or reuse a tmux pane for the session.

    Returns ``(pane_id, created)`` on success, or ``None`` on failure
    (task is transitioned to FAILED).  *created* is True when a new pane
    was split — callers must ``kill_pane`` on error only when created.
    """
    if reuse_pane:
        try:
            name_pane(reuse_pane, task.pane_label)
        except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
            transition(task, TaskStatus.FAILED)
            append_event(
                task,
                "session_start_failed",
                {"error": f"reuse_pane name: {exc}"},
            )
            return None
        return reuse_pane, False

    try:
        session_target = get_tmux_session_target()
    except RuntimeError as exc:
        transition(task, TaskStatus.FAILED)
        append_event(
            task,
            "session_start_failed",
            {"error": f"tmux session target: {exc}"},
        )
        return None

    try:
        result = subprocess.run(
            [
                "tmux",
                "split-window",
                "-h",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                session_target,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        transition(task, TaskStatus.FAILED)
        append_event(
            task, "session_start_failed", {"error": "tmux split-window timeout"}
        )
        return None
    if result.returncode != 0:
        transition(task, TaskStatus.FAILED)
        append_event(task, "session_start_failed", {"error": result.stderr})
        return None

    pane_id = result.stdout.strip()

    try:
        name_pane(pane_id, task.pane_label)
    except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
        kill_pane(pane_id)
        transition(task, TaskStatus.FAILED)
        append_event(task, "session_start_failed", {"error": f"name_pane: {exc}"})
        return None

    try:
        _layout = subprocess.run(
            ["tmux", "select-layout", "-t", pane_id, "tiled"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if _layout.returncode != 0:
            logger.warning("select-layout failed: %s", _layout.stderr.strip())
    except subprocess.TimeoutExpired:
        logger.warning("select-layout timed out for %s", task.id)

    return pane_id, True


def _start_and_prime_copilot(task: Task, pane_id: str, *, defer: bool) -> None:
    """Start Copilot in the pane, wait for idle, and optionally bootstrap.

    Raises on transport errors (caller handles cleanup).
    Returns without raising on startup health failures (task already FAILED).
    """
    copilot_cmd = f"copilot --model {shlex.quote(_get_copilot_model())}"
    if get_config("bypass_permissions"):
        copilot_cmd += " --yolo"

    send_shell_command(task.pane_label, f"cd {shlex.quote(str(task.worktree))}")
    time.sleep(_SESSION_CD_WAIT)
    send_shell_command(task.pane_label, copilot_cmd)

    append_event(
        task,
        "session_started",
        {
            "incarnation": task.incarnation_id,
            "pane": task.pane_label,
            "pane_id": pane_id,
        },
    )
    task.session_started_at = now_iso()
    save_task(task)

    click.echo("Waiting for Copilot to start...")
    if not wait_for_idle(
        task.pane_label, timeout=_IDLE_TIMEOUT_START, poll_interval=2.0
    ):
        logger.warning(
            "Copilot did not stabilize within %ss for %s",
            _IDLE_TIMEOUT_START,
            task.id,
        )
        append_event(
            task,
            "startup_timeout",
            {"timeout": _IDLE_TIMEOUT_START, "phase": "copilot_start"},
        )
        transition(task, TaskStatus.FAILED)
        return

    pane_content = read_pane(task.pane_label)
    if not is_at_main_prompt(pane_content):
        logger.warning(
            "Copilot pane stabilized but is not at main prompt for %s",
            task.id,
        )
        append_event(
            task,
            "startup_failed",
            {"reason": "not_at_prompt", "phase": "copilot_start"},
        )
        transition(task, TaskStatus.FAILED)
        return

    if get_config("auto_allow_all"):
        click.echo("Sending /allow-all...")
        send_slash_command(task.pane_label, "/allow-all")
        if not wait_for_idle(
            task.pane_label,
            timeout=_IDLE_TIMEOUT_ALLOW_ALL,
            poll_interval=1.0,
        ):
            logger.warning(
                "/allow-all did not stabilize within %ss for %s",
                _IDLE_TIMEOUT_ALLOW_ALL,
                task.id,
            )
    else:
        click.echo("Skipping /allow-all (auto_allow_all=false)")

    if defer:
        click.echo("Session deferred — Copilot idle, awaiting 'duo send'.")
    else:
        bootstrap = build_bootstrap_prompt(task)
        send_bootstrap(task.pane_label, bootstrap)
        task.last_prompt_sent_at = now_iso()
        save_task(task)
        append_event(
            task,
            "pr_consumed",
            {
                "action": "bootstrap",
                "step": task.current_step,
                "attempt": task.current_attempt,
            },
        )

        if not transition(task, TaskStatus.PROMPT_SENT):
            logger.warning(
                "Prompt sent but transition to PROMPT_SENT failed for %s (status=%s)",
                task.id,
                task.status.value,
            )


def start_session(task: Task, *, defer: bool = False, reuse_pane: str = "") -> None:
    """Start a Copilot session in tmux for this task.

    When *defer* is True, the session is launched and made ready (idle +
    /allow-all) but **no bootstrap prompt is sent** — the first user-facing
    ``duo send`` will deliver the initial instruction without burning a PR
    up-front.

    When *reuse_pane* is a non-empty pane ID, skip split-window creation
    and reuse the existing pane (rename + cd + start copilot).
    """
    logger.debug("Starting session for task %r", task.id)
    if task.status != TaskStatus.SESSION_STARTING:
        if not transition(task, TaskStatus.SESSION_STARTING):
            logger.error(
                "Cannot start session: illegal transition %s → SESSION_STARTING for %s",
                task.status.value,
                task.id,
            )
            return

    pane_result = _prepare_pane(task, reuse_pane)
    if pane_result is None:
        return
    pane_id, created = pane_result

    if not os.path.isdir(task.worktree):
        logger.error("Worktree does not exist: %s", task.worktree)
        if created:
            kill_pane(pane_id)
        transition(task, TaskStatus.FAILED)
        append_event(
            task,
            "session_start_failed",
            {"error": f"Worktree directory does not exist: {task.worktree}"},
        )
        return

    time.sleep(_SESSION_SPLIT_WAIT)
    try:
        _start_and_prime_copilot(task, pane_id, defer=defer)
    except _TRANSPORT_ERRORS as exc:
        if created:
            kill_pane(pane_id)
        logger.warning("start_session transport error for '%s': %s", task.id, exc)
        transition(task, TaskStatus.FAILED)
        append_event(task, "session_start_failed", {"error": str(exc)})
        raise

    # _start_and_prime_copilot may set FAILED on startup health checks
    # (timeout, not-at-prompt) without raising — bail out before callers
    # print success or replay prompts.
    if task.status == TaskStatus.FAILED:
        if created:
            kill_pane(pane_id)
        return

    # Also start Claude Code CLI as commander (non-blocking, best-effort)
    if get_config("auto_claude_commander"):
        click.echo("Starting Claude Code commander pane...")
        pane = start_claude_commander(task)
        if pane:
            click.echo(f"Claude commander started. Pane: duo-commander-{task.id}")
        else:
            click.echo("Warning: Failed to start Claude commander (non-fatal).")


def restart_session(task: Task) -> None:
    """Restart a crashed session with new incarnation.

    Preserves current_attempt to maintain correction context.
    Normalizes active states to FAILED before any side effects so the
    subsequent SESSION_STARTING transition is always legal.
    """
    # Normalize BEFORE side effects — if the task is in an active state
    # (e.g. RUNNING after a crash), move to FAILED first.
    if not normalize_for_restart(task):
        return

    old_inc = task.incarnation_id
    task.incarnation_id = new_incarnation()
    save_task(task)

    # Kill the old (crashed) pane to prevent orphan accumulation
    kill_pane(task.pane_label)

    # Clear bootstrap lock so new session can send bootstrap
    clear_bootstrap_done(task.pane_label)

    # Remove stale heartbeat from previous incarnation
    hb = task.dir / "heartbeat.json"
    hb.unlink(missing_ok=True)

    append_event(
        task,
        "session_restarted",
        {
            "old_incarnation": old_inc,
            "new_incarnation": task.incarnation_id,
        },
    )

    try:
        start_session(task)
    except _TRANSPORT_ERRORS as exc:
        logger.warning("restart_session transport error for '%s': %s", task.id, exc)
        transition(task, TaskStatus.FAILED)
        append_event(task, "session_restart_failed", {"error": str(exc)})
        raise


def _escalate_pr_budget(task: Task, step: int, attempt: int) -> None:
    """Escalate task when PR budget is exceeded."""
    if transition(task, TaskStatus.ESCALATED):
        append_event(task, "pr_budget_exceeded", {"step": step, "attempt": attempt})
    click.echo(f"⚠ PR budget exceeded for task '{task.id}' — escalating to human.")


def send_task_prompt(task: Task, prompt: str) -> None:
    """Send a prompt and record it."""
    logger.debug("Sending prompt for step %d", task.current_step)
    phash = prompt_hash(prompt)

    # Check PR budget before consuming a Premium Request
    if not _check_pr_budget(task):
        _escalate_pr_budget(task, task.current_step, task.current_attempt)
        return
    prompt_path = task.prompt_path(task.current_step, task.current_attempt)
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        atomic_write_text(prompt_path, prompt)
    except OSError as exc:
        logger.warning("Failed to write prompt file %s: %s", prompt_path, exc)
        raise

    # Wait for dialog then send (all post-bootstrap interaction goes through dialog)
    if not wait_for_dialog(task.pane_label, timeout=_DIALOG_TIMEOUT_SEND):
        logger.warning("Dialog timeout for %r", task.pane_label)
        append_event(task, "dialog_timeout", {"step": task.current_step})
        raise RuntimeError(
            f"Dialog timeout for '{task.pane_label}' — Copilot may be stuck"
        )
    select_dialog_option(task.pane_label, prompt)

    append_event(
        task,
        "pr_consumed",
        {
            "action": "task_prompt",
            "step": task.current_step,
            "attempt": task.current_attempt,
        },
    )

    task.last_prompt_sent_at = now_iso()
    save_task(task)

    append_event(
        task,
        "prompt_sent",
        {
            "step": task.current_step,
            "attempt": task.current_attempt,
            "incarnation": task.incarnation_id,
            "prompt_hash": f"sha256:{phash}",
        },
    )

    if not transition(task, TaskStatus.PROMPT_SENT):
        logger.warning(
            "Prompt sent but transition to PROMPT_SENT failed for %s (status=%s)",
            task.id,
            task.status.value,
        )


def resend_last_prompt(task: Task) -> None:
    """Resend the last prompt (e.g. after ack timeout)."""
    prompt_path = task.prompt_path(task.current_step, task.current_attempt)
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Cannot read prompt file %s — skipping resend", prompt_path)
        return
    if prompt:
        # Check PR budget before consuming a Premium Request
        if not _check_pr_budget(task):
            _escalate_pr_budget(task, task.current_step, task.current_attempt)
            return
        if not wait_for_dialog(task.pane_label, timeout=_DIALOG_TIMEOUT_RESEND):
            logger.warning("Dialog timeout for %r", task.pane_label)
            append_event(task, "dialog_timeout_resend", {"step": task.current_step})
            return
        select_dialog_option(task.pane_label, prompt)
        append_event(
            task,
            "pr_consumed",
            {
                "action": "resend_prompt",
                "step": task.current_step,
                "attempt": task.current_attempt,
            },
        )
        task.last_prompt_sent_at = now_iso()
        save_task(task)
        append_event(
            task,
            "prompt_resent",
            {
                "step": task.current_step,
                "attempt": task.current_attempt,
                "incarnation": task.incarnation_id,
            },
        )


# === Verify and Advance ===


def _handle_pass_verdict(task: Task, step: int, attempt: int) -> None:
    """Advance task after a passing verification verdict.

    Last step → COMPLETED.  Otherwise advance step, send continuation,
    rollback to BLOCKED on send failure.
    """
    if step >= len(task.subtasks):
        if transition(task, TaskStatus.COMPLETED):
            append_event(task, "task_completed", {"id": task.id})
        return

    # Next step — create dir before save so it exists when task.json references it
    next_step_dir = task.step_dir(step + 1)
    next_step_dir.mkdir(parents=True, exist_ok=True)
    task.current_step = step + 1
    task.current_attempt = 1
    save_task(task)

    try:
        prompt = build_continue_prompt(task)
        send_task_prompt(task, prompt)
    except _TRANSPORT_ERRORS as exc:
        logger.warning(
            "Failed to send continuation for '%s': %s — rolling back",
            task.id,
            exc,
        )
        task.current_step = step
        task.current_attempt = attempt
        save_task(task)
        transition(task, TaskStatus.BLOCKED)
        append_event(
            task,
            "continuation_send_failed",
            {"step": step + 1, "error": str(exc)},
        )


def _handle_correction_verdict(
    task: Task, step: int, attempt: int, reason: str
) -> None:
    """Send a correction after a failing verification verdict.

    Checks correction budget, bumps attempt, transitions to CORRECTING,
    sends correction prompt.  Rollback to FAILED on send failure.
    ``correction_sent`` event is appended only after successful send.
    """
    correction_count = _count_corrections(task, step)
    max_corrections = int(get_config("max_corrections") or 3)
    if correction_count >= max_corrections:
        if transition(task, TaskStatus.ESCALATED):
            append_event(
                task,
                "escalated_to_human",
                {
                    "step": step,
                    "reason": f"{max_corrections} corrections exhausted: {reason}",
                },
            )
        return

    task.current_attempt = attempt + 1
    save_task(task)
    task.step_dir(step).mkdir(parents=True, exist_ok=True)

    if not transition(task, TaskStatus.CORRECTING):
        logger.warning(
            "Transition to CORRECTING failed for %s — rolling back attempt bump",
            task.id,
        )
        task.current_attempt = attempt
        save_task(task)
        return

    try:
        prompt = build_correction_prompt(task, reason)
        send_task_prompt(task, prompt)
    except _TRANSPORT_ERRORS as exc:
        logger.warning(
            "Failed to send correction for '%s': %s — rolling back",
            task.id,
            exc,
        )
        task.current_attempt = attempt
        save_task(task)
        transition(task, TaskStatus.FAILED)
        append_event(
            task,
            "correction_send_failed",
            {"step": step, "attempt": attempt + 1, "error": str(exc)},
        )
        return

    append_event(
        task,
        "correction_sent",
        {
            "step": step,
            "attempt": task.current_attempt,
            "incarnation": task.incarnation_id,
            "reason": reason,
        },
    )


def verify_and_advance(task: Task, result: StepResult | None = None) -> None:
    """Verify the current step result and advance or correct."""
    step = task.current_step
    attempt = task.current_attempt
    if result is None:
        result = read_result_for_step(task, step, attempt)

    if result is None:
        return

    logger.info("Verification result: %s", type(result).__name__)
    if not transition(task, TaskStatus.VERIFYING):
        logger.warning(
            "Cannot verify: transition to VERIFYING failed for %s (status=%s)",
            task.id,
            task.status.value,
        )
        return

    # Handle blocked/error results
    if result.status in ("blocked", "error"):
        transition(task, TaskStatus.BLOCKED)
        append_event(
            task,
            "agent_blocked",
            {
                "step": step,
                "reason": result.reason,
            },
        )
        return

    # Run verification
    try:
        verdict = verify_step(task, result)
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        logger.warning("verify_step raised for '%s': %s", task.id, exc)
        transition(task, TaskStatus.FAILED)
        append_event(
            task,
            "verify_error",
            {"step": step, "error": str(exc)},
        )
        return

    if isinstance(verdict, Pass):
        _handle_pass_verdict(task, step, attempt)
    elif isinstance(verdict, Correction):
        _handle_correction_verdict(task, step, attempt, verdict.reason)


def _count_corrections(task: Task, step: int) -> int:
    """Count correction events for a step from the journal.

    Uses tail-bounded read to avoid loading the entire journal for
    long-running tasks.
    """
    events = read_jsonl(task.journal_path, tail=200)
    count = 0
    for ev in reversed(events):
        event_type = ev.get("event", "")
        data = ev.get("data", {})
        if event_type == "correction_sent" and data.get("step") == step:
            count += 1
        elif event_type == "task_created":
            break
    return count


# === Poll task (main scheduling loop body) ===


def poll_task(task: Task, poller: AdaptivePoller) -> PollResult:
    """Core scheduling loop body. All checks use incarnation+step+attempt."""
    step = task.current_step
    attempt = task.current_attempt
    inc = task.incarnation_id

    poll_result = poller.poll(task)
    logger.debug("Poll result: %s", poll_result)

    if poll_result == PollResult.RESULT_READY:
        result = read_result_for_step(task, step, attempt)
        if result and result.incarnation == inc:
            verify_and_advance(task, result)
        elif result:
            logger.warning(
                "Result incarnation mismatch for %s: got %s, expected %s",
                task.id,
                result.incarnation,
                inc,
            )
            append_event(
                task,
                "result_incarnation_mismatch",
                {
                    "got": result.incarnation,
                    "expected": inc,
                    "step": step,
                    "attempt": attempt,
                },
            )

    elif poll_result == PollResult.HEARTBEAT_TIMEOUT:
        if not is_process_alive(task.pane_label):
            # Re-read task from disk — another process (e.g. duo stop) may
            # have transitioned it to BLOCKED/FAILED while we were polling.
            fresh = load_task(task.id)
            if fresh is None:
                logger.warning(
                    "Cannot reload task '%s' from disk; skipping restart",
                    task.id,
                )
                return poll_result
            if fresh.status in (
                TaskStatus.BLOCKED,
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.ESCALATED,
                TaskStatus.QUEUED,
            ):
                return poll_result
            # Adopt fresh state to avoid acting on stale data
            task.incarnation_id = fresh.incarnation_id
            task.current_step = fresh.current_step
            task.current_attempt = fresh.current_attempt
            task.status = fresh.status
            append_event(task, "session_crashed", {"incarnation": fresh.incarnation_id})
            restart_session(task)
            if task.status == TaskStatus.FAILED:
                return poll_result
            # Prefer persisted prompt (may contain correction context)
            persisted = task.prompt_path(task.current_step, task.current_attempt)
            if persisted.exists():
                prompt = persisted.read_text()
            else:
                prompt = build_task_prompt(task)
            send_task_prompt(task, prompt)
        else:
            terminal = diagnose_pane(task.pane_label)
            if detect_copilot_api_error(terminal):
                capi = is_capi_context_error(terminal)
                event_type = "capi_error" if capi else "api_error"
                append_event(
                    task,
                    event_type,
                    {
                        "incarnation": inc,
                        "terminal": terminal[-_TERMINAL_SLICE:],
                    },
                )
                if capi:
                    logger.critical(
                        "CAPIError detected for task '%s' — session context "
                        "limit likely exhausted. Restart recommended.",
                        task.id,
                    )
                    signal = task.dir / "restart-recommended"
                    with contextlib.suppress(OSError):
                        signal.write_text(
                            "CAPIError detected — backend context limit.\n"
                        )
                if wait_for_dialog(task.pane_label, timeout=_DIALOG_TIMEOUT_MONITOR):
                    # Check PR budget before consuming a Premium Request
                    if not _check_pr_budget(task):
                        _escalate_pr_budget(task, step, attempt)
                        return poll_result
                    select_dialog_option(task.pane_label, "请重试上一个操作")
                    append_event(
                        task,
                        "pr_consumed",
                        {
                            "action": "error_retry",
                            "step": step,
                            "attempt": attempt,
                        },
                    )

    elif poll_result == PollResult.UNKNOWN:
        # UNKNOWN means no prompt was sent (last_prompt_sent_at is None).
        # Use session_started_at or created_at as fallback to determine
        # whether enough time has passed to attempt a resend.
        ack = read_ack_for_step(task, step, attempt)
        ref_time = (
            task.last_prompt_sent_at or task.session_started_at or task.created_at
        )
        if ack is None and ref_time and age(ref_time) > _IDLE_GRACE_SECONDS:
            resend_last_prompt(task)

    return poll_result


# === Monitor loop ===


def _log_monitor(symbol: str, task_id: str, message: str) -> None:
    """Format a monitor log line with timestamp.  Thread-safe."""
    from datetime import UTC, datetime

    ts = datetime.now(tz=UTC).strftime("%H:%M:%S")
    line = f"[duo] {ts} {symbol} {task_id:<20} {message}"
    with _LOG_LOCK:
        click.echo(line)


def _monitor_one_task(
    task: Task,
    pollers: dict[str, AdaptivePoller],
    poll_errors: dict[str, int],
) -> None:
    """Process a single task inside the monitor loop (under task_lock).

    Reloads the task from disk to avoid acting on stale state — another
    monitor process may have advanced the task between list_tasks() and
    lock acquisition.  Updates the caller's *task* object in place so
    that the outer loop's ``active`` filter sees any status changes.
    """
    fresh = load_task(task.id)
    if fresh is None:
        _log_monitor("·", task.id, "task vanished, skipping")
        return
    if fresh.status in (
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.ESCALATED,
        TaskStatus.BLOCKED,
        TaskStatus.QUEUED,
    ):
        task.status = fresh.status
        _log_monitor("·", task.id, f"now {fresh.status.value}, skipping")
        return
    # Sync mutable fields so subsequent code uses the latest disk state
    task.status = fresh.status
    task.current_step = fresh.current_step
    task.current_attempt = fresh.current_attempt
    task.incarnation_id = fresh.incarnation_id

    task_timeout = get_config("task_timeout")
    if task_timeout and task_timeout > 0:
        ref_time = task.session_started_at or task.created_at
        elapsed = age(ref_time)
        if elapsed > task_timeout:
            logger.warning(
                "Task %s exceeded timeout (%ds > %ds)",
                task.id,
                int(elapsed),
                task_timeout,
            )
            _log_monitor("⏱", task.id, f"timeout ({int(elapsed)}s > {task_timeout}s)")
            append_event(
                task,
                "timeout_exceeded",
                {"elapsed": int(elapsed), "limit": task_timeout},
            )
            transition(task, TaskStatus.FAILED)
            return

    if task.id not in pollers:
        pollers[task.id] = AdaptivePoller(
            base_interval=float(get_config("poll_base_interval") or 5.0),
            max_interval=float(get_config("poll_max_interval") or 120.0),
            heartbeat_timeout=float(get_config("heartbeat_timeout") or 90),
        )

    poller = pollers[task.id]
    try:
        result = poll_task(task, poller)
    except (
        RuntimeError,
        ValueError,
        OSError,
        subprocess.CalledProcessError,
    ) as exc:
        poll_errors[task.id] = poll_errors.get(task.id, 0) + 1
        count = poll_errors[task.id]
        _log_monitor(
            "✗",
            task.id,
            f"poll error ({count}/{_MAX_CONSECUTIVE_POLL_ERRORS}): {exc}",
        )
        logger.exception("poll_task failed for %s", task.id)
        append_event(
            task,
            "poll_error",
            {"error": str(exc), "consecutive": count},
        )
        if count >= _MAX_CONSECUTIVE_POLL_ERRORS:
            _log_monitor(
                "✗",
                task.id,
                f"FAILED after {count} consecutive poll errors",
            )
            transition(task, TaskStatus.FAILED)
            append_event(
                task,
                "poll_errors_exhausted",
                {"consecutive": count, "last_error": str(exc)},
            )
        return

    # Reset consecutive error counter on success
    poll_errors.pop(task.id, None)

    if result == PollResult.RESULT_READY:
        _log_monitor("✓", task.id, f"result_ready (step={task.current_step})")
    elif result == PollResult.HEARTBEAT_TIMEOUT:
        _log_monitor("⚠", task.id, "heartbeat_timeout")
    elif result == PollResult.UNKNOWN:
        _log_monitor("?", task.id, "unknown state")


def monitor(task_ids: list[str] | None = None) -> None:
    """Run the adaptive polling monitor loop.

    Note: Callers should handle ``KeyboardInterrupt`` to allow graceful
    shutdown when the user presses Ctrl-C (see ``cli.py``).
    """
    from duo.scheduler import promote_queued, queue_status

    pollers: dict[str, AdaptivePoller] = {}
    poll_errors: dict[str, int] = {}
    iteration = 0

    while True:
        tasks = list_tasks()
        active = [
            t
            for t in tasks
            if t.status
            not in (
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.ESCALATED,
                TaskStatus.BLOCKED,
                TaskStatus.QUEUED,
            )
            and (task_ids is None or t.id in task_ids)
        ]

        # Header on first iteration
        if iteration == 0:
            qs = queue_status()
            click.echo(
                f"[duo] Monitor started — {qs['active_count']} active, {qs['queued_count']} queued, max {qs['max_parallel']}"
            )
        iteration += 1

        # Promote queued tasks if slots available
        promoted = promote_queued()
        for task in promoted:
            _log_monitor("◷", task.id, "promoted from queue")
            try:
                start_session(task)
                if task.status == TaskStatus.FAILED:
                    _log_monitor("✗", task.id, "session failed to start")
                    continue
                # Prefer user-persisted prompt over synthesized one
                persisted = task.prompt_path(task.current_step, task.current_attempt)
                if persisted.exists():
                    prompt = persisted.read_text()
                else:
                    prompt = build_task_prompt(task)
                send_task_prompt(task, prompt)
            except _TRANSPORT_ERRORS as exc:
                _log_monitor("✗", task.id, f"failed to start: {exc}")
                logger.warning("Failed to start promoted task '%s': %s", task.id, exc)

        if not active and not promoted:
            # Check if there are queued tasks waiting
            queued = [t for t in tasks if t.status == TaskStatus.QUEUED]
            if not queued:
                click.echo("[duo] No active or queued tasks. Nothing to monitor.")
                break

        for task in active:
            try:
                with task_lock(task.id):
                    _monitor_one_task(task, pollers, poll_errors)
            except TaskLockedError:
                _log_monitor("⊘", task.id, "locked by another process, skipping")
                continue

        # Use the minimum interval across all active tasks
        active_ids = {t.id for t in active}
        stale = [k for k in pollers if k not in active_ids]
        for k in stale:
            del pollers[k]
        for k in [k for k in poll_errors if k not in active_ids]:
            del poll_errors[k]
        min_interval = min(
            (pollers[t.id].interval for t in active if t.id in pollers),
            default=5.0,
        )
        interval = max(1.0, min_interval)
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Event-driven watch
# ---------------------------------------------------------------------------


def _write_watch_event(task: Task, pane_content: str) -> Path:
    """Write a watch-event signal file for the detected dialog."""
    _WATCH_EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = now_iso().replace(":", "-")
    path = _WATCH_EVENTS_DIR / f"{task.id}-{ts}.json"
    write_json(
        path,
        {
            "task_id": task.id,
            "pane_label": task.pane_label,
            "detected_at": now_iso(),
            "pane_content": pane_content[-2000:],
        },
    )
    return path


def _watch_loop(
    task: Task,
    stop: threading.Event,
    *,
    timeout: float,
    interval: float,
    once: bool,
    auto_approve: bool,
) -> None:
    """Per-task watch loop: block on dialog detection.

    Default mode (auto_approve=False): detect dialog → print content →
    write signal file → set stop event → return. The external CEO process
    reads the signal file and decides what to do next.

    Legacy mode (auto_approve=True): detect dialog → auto-approve the
    permission dialog → continue watching.
    """
    label = task.pane_label
    while not stop.is_set():
        # Check pane is still alive before waiting for dialog
        if not is_process_alive(label):
            _log_monitor("·", task.id, "pane gone, stopping watch")
            break
        try:
            found = wait_for_dialog(
                label,
                timeout=timeout,
                interval=interval,
                stop_event=stop,
            )
        except (RuntimeError, OSError):
            _log_monitor("✗", task.id, "pane unavailable, stopping watch")
            break
        if stop.is_set():
            break
        if not found:
            # Timeout — check if pane is still alive
            if not is_process_alive(label):
                _log_monitor("·", task.id, "pane gone, stopping watch")
                break
            # Check if Copilot went idle at main ❯ prompt (stopped working)
            try:
                idle_content = read_pane(label, 5)
                if is_at_main_prompt(idle_content):
                    _log_monitor("⚠", task.id, "Copilot IDLE at main prompt — needs new task")
                    pane_content = read_pane(label, 40)
                    click.echo(f"\n[duo:watch] Copilot IDLE in '{task.id}':")
                    click.echo(pane_content)
                    _write_watch_signal(task.id, "idle", pane_content)
                    if once:
                        stop.set()
                        break
            except (RuntimeError, OSError):
                pass
            continue
        # Dialog detected — read pane content for signal file
        _log_monitor("⚡", task.id, "dialog detected")
        pane_content = ""
        with contextlib.suppress(RuntimeError, OSError):
            pane_content = read_pane(label, 40)
        if auto_approve:
            # Legacy mode: auto-approve the permission dialog
            try:
                approve_permission(label)
                _log_monitor("✓", task.id, "dialog auto-approved")
                append_event(task, "watch_dialog_handled", {})
            except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
                _log_monitor("✗", task.id, f"dialog error: {exc}")
            if once:
                stop.set()
                break
        else:
            # Default mode: write signal file and return control to CEO
            click.echo(f"\n[duo:watch] Dialog detected in '{task.id}':")
            if pane_content:
                click.echo(pane_content)
            sig = _write_watch_event(task, pane_content)
            _log_monitor("📄", task.id, f"signal → {sig.name}")
            append_event(task, "watch_dialog_detected", {"signal_file": str(sig)})
            stop.set()
            break


def watch_tasks(
    task_ids: list[str] | None = None,
    *,
    timeout: float = 300,
    interval: float = 5.0,
    once: bool = False,
    auto_approve: bool = False,
) -> int:
    """Event-driven pane watcher — pure detector by default.

    Default mode: monitors panes for dialogs, prints content, writes a
    signal file to ``~/.duo/watch-events/``, and returns so the CEO
    process can decide what to do.

    With ``auto_approve=True``: automatically approves permission dialogs
    (legacy behavior for unattended runs).

    Returns the number of tasks watched.

    Note: Callers should handle ``KeyboardInterrupt`` to allow graceful
    shutdown when the user presses Ctrl-C (see ``cli.py``).
    """
    tasks = list_tasks()
    if task_ids is not None:
        known = {t.id for t in tasks}
        unknown = [tid for tid in task_ids if tid not in known]
        if unknown:
            click.echo(f"[duo] Unknown task(s): {', '.join(unknown)}", err=True)
        candidates = [t for t in tasks if t.id in task_ids]
    else:
        candidates = list(tasks)

    # Filter by pane alive, not FSM status — a FAILED task may still have
    # a live pane (e.g., Copilot running a self-review loop).
    active = [t for t in candidates if is_process_alive(t.pane_label)]
    if not active:
        click.echo("[duo] No active tasks to watch.")
        return 0

    stop = threading.Event()
    threads: list[threading.Thread] = []

    for task in active:
        t = threading.Thread(
            target=_watch_loop,
            args=(task, stop),
            kwargs={
                "timeout": timeout,
                "interval": interval,
                "once": once,
                "auto_approve": auto_approve,
            },
            daemon=True,
        )
        t.start()
        threads.append(t)

    labels = ", ".join(t.id for t in active)
    click.echo(f"[duo] Watching {len(active)} task(s): {labels}")

    # Block until stop event or all threads finish
    while not stop.is_set() and any(t.is_alive() for t in threads):
        stop.wait(timeout=1.0)

    stop.set()
    for t in threads:
        t.join(timeout=5.0)

    return len(active)
