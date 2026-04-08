"""Commander — orchestration brain.

Handles task lifecycle: create sessions, send prompts, poll for results,
verify output, correct or advance, and manage session recovery.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time

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
    Task,
    TaskStatus,
    append_event,
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
)
from duo.transport import (
    approve_permission,
    clear_bootstrap_done,
    diagnose_pane,
    is_process_alive,
    name_pane,
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


def write_commander_claude_md(task: Task) -> None:
    """Write CLAUDE.md into the task worktree so Claude Code CLI picks it up."""
    from pathlib import Path

    content = CLAUDE_COMMANDER_TEMPLATE.format(
        task_dir=str(task.dir),
        task_id=task.id,
        worktree=task.worktree,
        branch=task.branch,
        incarnation=task.incarnation_id,
    )
    claude_md = Path(task.worktree) / "CLAUDE.md"
    try:
        claude_md.write_text(content)
        logger.info("Wrote CLAUDE.md to %s", claude_md)
    except OSError as exc:
        logger.warning("Failed to write CLAUDE.md to %s: %s", claude_md, exc)


def start_claude_commander(task: Task) -> str | None:
    """Open a tmux pane running Claude Code CLI as the Commander.

    Returns the pane ID on success, None on failure.
    """
    # Write CLAUDE.md so Claude Code auto-discovers its role
    write_commander_claude_md(task)

    result = subprocess.run(
        ["tmux", "split-window", "-v", "-P", "-F", "#{pane_id}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        logger.warning("Failed to create Claude commander pane: %s", result.stderr)
        return None

    pane_id = result.stdout.strip()
    commander_label = f"duo-commander-{task.id}"
    name_pane(pane_id, commander_label)

    # Tile layout
    subprocess.run(
        ["tmux", "select-layout", "tiled"], capture_output=True, text=True, timeout=10
    )

    time.sleep(_SESSION_SPLIT_WAIT)
    try:
        send_shell_command(commander_label, f"cd {task.worktree}")
        time.sleep(_SESSION_CD_WAIT)
        send_shell_command(commander_label, "claude")
    except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
        logger.warning("Failed to start Claude commander: %s", exc)
        try:
            subprocess.run(
                ["tmux", "kill-pane", "-t", pane_id],
                capture_output=True, check=False, timeout=10,
            )
        except (subprocess.CalledProcessError, OSError):
            pass
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


def start_session(task: Task) -> None:
    """Start a Copilot session in tmux for this task."""
    logger.debug(f"Starting session for task '{task.id}'")
    if task.status != TaskStatus.SESSION_STARTING:
        transition(task, TaskStatus.SESSION_STARTING)

    # Create tmux pane and start copilot
    # Note: tmux must already be running (user starts duo inside tmux)
    # Create a new window in the current tmux session
    result = subprocess.run(
        ["tmux", "split-window", "-h", "-P", "-F", "#{pane_id}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        append_event(task, "session_start_failed", {"error": result.stderr})
        transition(task, TaskStatus.FAILED)
        return

    pane_id = result.stdout.strip()

    # Label the pane
    name_pane(pane_id, task.pane_label)

    # Tile layout for balance
    _layout = subprocess.run(
        ["tmux", "select-layout", "tiled"], capture_output=True, text=True, timeout=10
    )
    if _layout.returncode != 0:
        logger.warning("select-layout failed: %s", _layout.stderr.strip())

    # cd to worktree, then start copilot (no -C flag available)
    time.sleep(_SESSION_SPLIT_WAIT)
    copilot_cmd = f"copilot --model {_get_copilot_model()} --yolo"
    try:
        send_shell_command(task.pane_label, f"cd {task.worktree}")
        time.sleep(_SESSION_CD_WAIT)
        send_shell_command(task.pane_label, copilot_cmd)
    except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
        # Kill orphaned pane if it was created
        try:
            subprocess.run(
                ["tmux", "kill-pane", "-t", pane_id],
                capture_output=True, check=False, timeout=10,
            )
        except (subprocess.CalledProcessError, OSError):
            pass
        logger.warning("start_session transport error for '%s': %s", task.id, exc)
        transition(task, TaskStatus.FAILED)
        append_event(task, "session_start_failed", {"error": str(exc)})
        raise

    append_event(
        task,
        "session_started",
        {
            "incarnation": task.incarnation_id,
            "pane": task.pane_label,
            "pane_id": pane_id,
        },
    )

    # Wait for copilot to start (adaptive instead of hardcoded sleep)
    click.echo("Waiting for Copilot to start...")
    wait_for_idle(task.pane_label, timeout=_IDLE_TIMEOUT_START, poll_interval=2.0)

    # Auto-approve all operations to avoid interactive prompts
    click.echo("Sending /allow-all...")
    send_shell_command(task.pane_label, "/allow-all")
    wait_for_idle(task.pane_label, timeout=_IDLE_TIMEOUT_ALLOW_ALL, poll_interval=1.0)

    # Send bootstrap prompt (this is the first and only ❯ prompt message)
    bootstrap = build_bootstrap_prompt(task)
    send_bootstrap(task.pane_label, bootstrap)
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

    # Also start Claude Code CLI as commander (non-blocking, best-effort)
    if get_config("auto_claude_commander"):
        click.echo("Starting Claude Code commander pane...")
        pane = start_claude_commander(task)
        if pane:
            click.echo(f"Claude commander started. Pane: duo-commander-{task.id}")
        else:
            click.echo("Warning: Failed to start Claude commander (non-fatal).")


def restart_session(task: Task) -> None:
    """Restart a crashed session with new incarnation."""
    old_inc = task.incarnation_id
    task.incarnation_id = new_incarnation()
    task.current_attempt = 1  # reset attempt for current step
    save_task(task)

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
    except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
        logger.warning("restart_session transport error for '%s': %s", task.id, exc)
        transition(task, TaskStatus.FAILED)
        append_event(task, "session_restart_failed", {"error": str(exc)})
        raise


def send_task_prompt(task: Task, prompt: str) -> None:
    """Send a prompt and record it."""
    logger.debug(f"Sending prompt for step {task.current_step}")
    phash = prompt_hash(prompt)

    # Check PR budget before consuming a Premium Request
    if not _check_pr_budget(task):
        append_event(
            task,
            "pr_budget_exceeded",
            {
                "step": task.current_step,
                "attempt": task.current_attempt,
            },
        )
        transition(task, TaskStatus.ESCALATED)
        click.echo(f"⚠ PR budget exceeded for task '{task.id}' — escalating to human.")
        return

    # Save prompt to file for debug (after budget check)
    prompt_path = task.prompt_path(task.current_step, task.current_attempt)
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt)

    # Wait for dialog then send (all post-bootstrap interaction goes through dialog)
    if not wait_for_dialog(task.pane_label, timeout=_DIALOG_TIMEOUT_SEND):
        logger.warning(f"Dialog timeout for '{task.pane_label}'")
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
    if prompt_path.exists():
        prompt = prompt_path.read_text()
        # Check PR budget before consuming a Premium Request
        if not _check_pr_budget(task):
            append_event(
                task,
                "pr_budget_exceeded",
                {
                    "step": task.current_step,
                    "attempt": task.current_attempt,
                },
            )
            transition(task, TaskStatus.ESCALATED)
            click.echo(
                f"⚠ PR budget exceeded for task '{task.id}' — escalating to human."
            )
            return
        if not wait_for_dialog(task.pane_label, timeout=_DIALOG_TIMEOUT_RESEND):
            logger.warning(f"Dialog timeout for '{task.pane_label}'")
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

    logger.info(f"Verification result: {type(result).__name__}")
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

            # Send continuation prompt (doesn't consume Premium Request!)
            prompt = build_continue_prompt(task)
            send_task_prompt(task, prompt)

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

        # Send correction
        task.current_attempt = attempt + 1
        save_task(task)
        task.step_dir(step).mkdir(parents=True, exist_ok=True)

        transition(task, TaskStatus.CORRECTING)
        prompt = build_correction_prompt(task, verdict.reason)
        send_task_prompt(task, prompt)

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

    Reads events in reverse to avoid scanning the entire journal for
    long-running tasks.
    """
    events = read_jsonl(task.journal_path)
    count = 0
    for ev in reversed(events):
        event_type = ev.get("event", "")
        data = ev.get("data", {})
        if event_type == "correction_sent" and data.get("step") == step:
            count += 1
        elif event_type == "task_created":
            break  # no need to scan before task creation
    return count


# === Poll task (main scheduling loop body) ===


def poll_task(task: Task, poller: AdaptivePoller) -> PollResult:
    """Core scheduling loop body. All checks use incarnation+step+attempt."""
    step = task.current_step
    attempt = task.current_attempt
    inc = task.incarnation_id

    poll_result = poller.poll(task)
    logger.debug(f"Poll result: {poll_result}")

    if poll_result == PollResult.RESULT_READY:
        result = read_result_for_step(task, step, attempt)
        if result and result.incarnation == inc:
            verify_and_advance(task)

    elif poll_result == PollResult.HEARTBEAT_TIMEOUT:
        if not is_process_alive(task.pane_label):
            append_event(task, "session_crashed", {"incarnation": inc})
            restart_session(task)
            prompt = build_task_prompt(task)
            send_task_prompt(task, prompt)
        else:
            terminal = diagnose_pane(task.pane_label)
            if "error" in terminal.lower() or "rate limit" in terminal.lower():
                append_event(
                    task,
                    "api_error",
                    {
                        "incarnation": inc,
                        "terminal": terminal[-_TERMINAL_SLICE:],
                    },
                )
                if wait_for_dialog(task.pane_label, timeout=_DIALOG_TIMEOUT_MONITOR):
                    # Check PR budget before consuming a Premium Request
                    if not _check_pr_budget(task):
                        append_event(
                            task,
                            "pr_budget_exceeded",
                            {
                                "step": step,
                                "attempt": attempt,
                            },
                        )
                        transition(task, TaskStatus.ESCALATED)
                        click.echo(
                            f"⚠ PR budget exceeded for task '{task.id}' — escalating to human."
                        )
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
        # Check if ack is missing
        ack = read_ack_for_step(task, step, attempt)
        if ack is None and task.last_prompt_sent_at:
            if age(task.last_prompt_sent_at) > _IDLE_GRACE_SECONDS:
                resend_last_prompt(task)

    return poll_result


# === Monitor loop ===


def _log_monitor(symbol: str, task_id: str, message: str) -> None:
    """Format a monitor log line with timestamp.  Thread-safe."""
    from datetime import datetime

    ts = datetime.now().strftime("%H:%M:%S")
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
                prompt = build_task_prompt(task)
                send_task_prompt(task, prompt)
            except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
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
                elapsed = age(task.created_at)
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
                pollers[task.id] = AdaptivePoller()

            poller = pollers[task.id]
            try:
                result = poll_task(task, poller)
            except Exception as exc:
                _log_monitor("✗", task.id, f"poll error: {exc}")
                logger.exception("poll_task failed for %s", task.id)
                append_event(task, "poll_error", {"error": str(exc)})
                continue

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
        min_interval = min(
            (pollers[t.id].interval for t in active if t.id in pollers),
            default=5.0,
        )
        interval = max(1.0, min_interval)
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Event-driven watch
# ---------------------------------------------------------------------------

_TERMINAL_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ESCALATED}
)


def _watch_loop(
    task: Task,
    stop: threading.Event,
    *,
    timeout: float,
    interval: float,
    once: bool,
) -> None:
    """Per-task watch loop: block on dialog, handle it, repeat."""
    label = task.pane_label
    while not stop.is_set():
        try:
            found = wait_for_dialog(label, timeout=timeout, interval=interval)
        except Exception:
            _log_monitor("✗", task.id, "pane unavailable, stopping watch")
            break
        if stop.is_set():
            break
        if not found:
            # Timeout — check if task is still active
            refreshed = load_task(task.id)
            if refreshed is None or refreshed.status in _TERMINAL_STATUSES:
                _log_monitor("·", task.id, "task finished, stopping watch")
                break
            continue
        # Dialog detected
        _log_monitor("⚡", task.id, "dialog detected")
        try:
            approve_permission(label)
            _log_monitor("✓", task.id, "dialog handled")
            append_event(task, "watch_dialog_handled", {})
        except Exception as exc:
            _log_monitor("✗", task.id, f"dialog error: {exc}")
        if once:
            stop.set()
            break


def watch_tasks(
    task_ids: list[str] | None = None,
    *,
    timeout: float = 300,
    interval: float = 5.0,
    once: bool = False,
) -> int:
    """Event-driven pane watcher with auto dialog handling.

    Returns the number of dialogs handled.

    Note: Callers should handle ``KeyboardInterrupt`` to allow graceful
    shutdown when the user presses Ctrl-C (see ``cli.py``).
    """
    tasks = list_tasks()
    if task_ids is not None:
        known = {t.id for t in tasks}
        unknown = [tid for tid in task_ids if tid not in known]
        if unknown:
            click.echo(f"[duo] Unknown task(s): {', '.join(unknown)}", err=True)
    active = [
        t
        for t in tasks
        if t.status not in _TERMINAL_STATUSES
        and (task_ids is None or t.id in task_ids)
    ]
    if not active:
        click.echo("[duo] No active tasks to watch.")
        return 0

    stop = threading.Event()
    threads: list[threading.Thread] = []

    for task in active:
        t = threading.Thread(
            target=_watch_loop,
            args=(task, stop),
            kwargs={"timeout": timeout, "interval": interval, "once": once},
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
