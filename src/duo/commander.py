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
]

logger = logging.getLogger(__name__)

from duo.config import get_config
from duo.poller import AdaptivePoller, PollResult, age
from duo.protocol import (
    DUO_DIR,
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
    transition,
    write_json,
)
from duo.transport import (
    approve_permission,
    clear_bootstrap_done,
    detect_copilot_api_error,
    diagnose_pane,
    get_tmux_session_target,
    is_capi_context_error,
    is_process_alive,
    kill_pane,
    name_pane,
    read_pane,
    select_dialog_option,
    send_bootstrap,
    send_shell_command,
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


def _get_copilot_model() -> str:
    """Get copilot model from config, env var override, or default."""
    env_model = os.environ.get("DUO_COPILOT_MODEL")
    if env_model:
        if not re.match(r"^[a-zA-Z0-9._-]+$", env_model):
            logger.warning(
                "DUO_COPILOT_MODEL contains invalid chars, using config default"
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
# Duo Commander Mode

You are the **Commander** (规划者) in the Duo agent orchestration framework.
A Copilot CLI executor is running in an adjacent tmux pane, following the file
protocol below.  Your job is to **plan**, **decompose**, **review**, and
**coordinate** — NOT to edit code files directly.

## Your Responsibilities

1. **Plan** — Break the user's request into small, verifiable subtasks
2. **Send** — Use `duo send <task> "<instruction>"` to give the executor work
3. **Monitor** — Use `duo status <task>` or `duo watch` to track progress
4. **Review** — Read the executor's result files and verify correctness
5. **Correct** — If the result is wrong, send a correction via `duo send`

## Key Commands

```bash
duo status <task>          # Check task status and current step
duo send <task> "prompt"   # Send instruction to executor
duo list                   # List all tasks
duo watch                  # Watch for dialog events
duo monitor                # Start automated polling monitor
duo inspect <task>         # Show detailed task info
duo logs <task>            # Show task journal events
duo diff <task>            # Show code changes in worktree
```

## File Protocol v1

The executor writes JSON files in the task directory:

**Task directory:** `{task_dir}`

| File | When | Content |
|------|------|---------|
| `steps/step-NNNN/ack-attempt-AA.json` | After receiving instruction | `{{"step":N, "attempt":A, "incarnation":"...", "acked_at":"ISO"}}` |
| `heartbeat.json` | After each file edit | `{{"ts":"ISO", "incarnation":"...", "step":N, "status":"working"}}` |
| `steps/step-NNNN/result-attempt-AA.json` | After completing work | `{{"step":N, "attempt":A, "status":"done", "files_changed":[...], "summary":"..."}}` |

## Current Task

- **Task ID:** {task_id}
- **Worktree:** {worktree}
- **Branch:** {branch}
- **Incarnation:** {incarnation}

## Workflow Example

```bash
# 1. Check what's happening
duo status {task_id}

# 2. Give the executor a specific instruction
duo send {task_id} "Implement JWT authentication in src/auth.py with login/logout endpoints"

# 3. Monitor progress
duo watch --once

# 4. Review the result
duo diff {task_id}
duo inspect {task_id}

# 5. If it needs correction, send feedback
duo send {task_id} "The login endpoint is missing rate limiting. Add it."
```

## Rules

- Do NOT edit code files directly — that's the executor's job
- Keep instructions specific and verifiable
- One instruction at a time for best results
- Review changes with `duo diff` before approving
"""


def build_bootstrap_prompt(task: Task) -> str:
    """Build the initial session bootstrap prompt with file protocol instructions."""
    return SESSION_BOOTSTRAP_TEMPLATE.format(
        task_dir=str(task.dir),
        incarnation=task.incarnation_id,
    )


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

    # List top-level directory structure
    try:
        entries = sorted(p.name for p in root.iterdir() if not p.name.startswith("."))
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
    )

    project_ctx = _detect_project_context(task.worktree)
    content = base + "\n## Project Context\n\n" + project_ctx + "\n"

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
    name_pane(pane_id, commander_label)

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
        send_shell_command(commander_label, "claude")
    except (
        RuntimeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        OSError,
    ) as exc:
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

    return prompt  # noqa: RET504


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


def start_session(task: Task) -> None:
    """Start a Copilot session in tmux for this task."""
    logger.debug("Starting session for task %r", task.id)
    if task.status != TaskStatus.SESSION_STARTING:
        transition(task, TaskStatus.SESSION_STARTING)

    # Target the caller's session to prevent cross-session pollution
    session_target = get_tmux_session_target()

    # Create tmux pane and start copilot
    # Note: tmux must already be running (user starts duo inside tmux)
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
        append_event(
            task, "session_start_failed", {"error": "tmux split-window timeout"}
        )
        transition(task, TaskStatus.FAILED)
        return
    if result.returncode != 0:
        append_event(task, "session_start_failed", {"error": result.stderr})
        transition(task, TaskStatus.FAILED)
        return

    pane_id = result.stdout.strip()

    # Label the pane
    name_pane(pane_id, task.pane_label)

    # Tile layout — target the new pane to resolve correct window
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

    # cd to worktree, start copilot, wait for readiness, and send bootstrap.
    # All post-pane-creation steps share a single try/except so any failure
    # (including subprocess.TimeoutExpired from tmux send-keys) cleans up
    # the orphaned pane and transitions the task to FAILED.
    time.sleep(_SESSION_SPLIT_WAIT)
    copilot_cmd = f"copilot --model {shlex.quote(_get_copilot_model())} --yolo"
    try:
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

        # Record when the session actually started
        task.session_started_at = now_iso()
        save_task(task)

        # Wait for copilot to start (adaptive instead of hardcoded sleep)
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
            # Do NOT continue to /allow-all or bootstrap — Copilot may not
            # be ready and we could be typing into a raw shell.
            transition(task, TaskStatus.FAILED)
            return

        # Auto-approve all operations to avoid interactive prompts (configurable)
        if get_config("auto_allow_all"):
            click.echo("Sending /allow-all...")
            send_shell_command(task.pane_label, "/allow-all")
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

        # Send bootstrap prompt (this is the first and only ❯ prompt message)
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

        transition(task, TaskStatus.PROMPT_SENT)
    except (
        RuntimeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        OSError,
    ) as exc:
        kill_pane(pane_id)
        logger.warning("start_session transport error for '%s': %s", task.id, exc)
        transition(task, TaskStatus.FAILED)
        append_event(task, "session_start_failed", {"error": str(exc)})
        raise

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
    """
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
    except (
        RuntimeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        OSError,
    ) as exc:
        logger.warning("restart_session transport error for '%s': %s", task.id, exc)
        transition(task, TaskStatus.FAILED)
        append_event(task, "session_restart_failed", {"error": str(exc)})
        raise


def _escalate_pr_budget(task: Task, step: int, attempt: int) -> None:
    """Escalate task when PR budget is exceeded."""
    append_event(task, "pr_budget_exceeded", {"step": step, "attempt": attempt})
    transition(task, TaskStatus.ESCALATED)
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

    transition(task, TaskStatus.PROMPT_SENT)


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


def verify_and_advance(task: Task) -> None:
    """Verify the current step result and advance or correct."""
    step = task.current_step
    attempt = task.current_attempt
    result = read_result_for_step(task, step, attempt)

    if result is None:
        return

    logger.info("Verification result: %s", type(result).__name__)
    transition(task, TaskStatus.VERIFYING)

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
        # Advance to next step
        if step >= len(task.subtasks):
            # All done!
            transition(task, TaskStatus.COMPLETED)
            append_event(task, "task_completed", {"id": task.id})
        else:
            # Next step — create dir before save so it exists when task.json references it
            next_step_dir = task.step_dir(step + 1)
            next_step_dir.mkdir(parents=True, exist_ok=True)
            task.current_step = step + 1
            task.current_attempt = 1
            save_task(task)

            # Send continuation prompt — rollback on failure
            try:
                prompt = build_continue_prompt(task)
                send_task_prompt(task, prompt)
            except (
                RuntimeError,
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
                OSError,
            ) as exc:
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

    elif isinstance(verdict, Correction):
        # Check correction count
        correction_count = _count_corrections(task, step)
        max_corrections = int(get_config("max_corrections") or 3)
        if correction_count >= max_corrections:
            transition(task, TaskStatus.ESCALATED)
            append_event(
                task,
                "escalated_to_human",
                {
                    "step": step,
                    "reason": f"{max_corrections} corrections exhausted: {verdict.reason}",
                },
            )
            return

        # Send correction — rollback on failure
        task.current_attempt = attempt + 1
        save_task(task)
        task.step_dir(step).mkdir(parents=True, exist_ok=True)

        transition(task, TaskStatus.CORRECTING)
        try:
            prompt = build_correction_prompt(task, verdict.reason)
            send_task_prompt(task, prompt)
        except (
            RuntimeError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
        ) as exc:
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
                "reason": verdict.reason,
            },
        )


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
            verify_and_advance(task)
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


def monitor(task_ids: list[str] | None = None) -> None:
    """Run the adaptive polling monitor loop.

    Note: Callers should handle ``KeyboardInterrupt`` to allow graceful
    shutdown when the user presses Ctrl-C (see ``cli.py``).
    """
    from duo.scheduler import promote_queued, queue_status

    pollers: dict[str, AdaptivePoller] = {}
    poll_errors: dict[str, int] = {}
    _MAX_CONSECUTIVE_POLL_ERRORS = 10
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
            except (
                RuntimeError,
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
                OSError,
            ) as exc:
                _log_monitor("✗", task.id, f"failed to start: {exc}")
                logger.warning("Failed to start promoted task '%s': %s", task.id, exc)

        if not active and not promoted:
            # Check if there are queued tasks waiting
            queued = [t for t in tasks if t.status == TaskStatus.QUEUED]
            if not queued:
                click.echo("[duo] No active or queued tasks. Nothing to monitor.")
                break

        for task in active:
            # Enforce task_timeout before polling
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
                    _log_monitor(
                        "⏱", task.id, f"timeout ({int(elapsed)}s > {task_timeout}s)"
                    )
                    append_event(
                        task,
                        "timeout_exceeded",
                        {"elapsed": int(elapsed), "limit": task_timeout},
                    )
                    transition(task, TaskStatus.FAILED)
                    continue

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
                continue

            # Reset consecutive error counter on success
            poll_errors.pop(task.id, None)

            if result == PollResult.RESULT_READY:
                _log_monitor("✓", task.id, f"result_ready (step={task.current_step})")
            elif result == PollResult.HEARTBEAT_TIMEOUT:
                _log_monitor("⚠", task.id, "heartbeat_timeout")
            elif result == PollResult.UNKNOWN:
                _log_monitor("?", task.id, "unknown state")
            # WORKING is silent (normal operation)

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
            found = wait_for_dialog(label, timeout=timeout, interval=interval)
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
