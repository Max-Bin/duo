"""Commander — orchestration brain.

Handles task lifecycle: create sessions, send prompts, poll for results,
verify output, correct or advance, and manage session recovery.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import click

from duo.poller import AdaptivePoller, PollResult, age
from duo.protocol import (
    Task,
    TaskStatus,
    append_event,
    create_task,
    list_tasks,
    load_task,
    new_incarnation,
    now_iso,
    prompt_hash,
    read_ack_for_step,
    read_result_for_step,
    save_task,
    Subtask,
    transition,
)
from duo.transport import (
    diagnose_pane,
    is_process_alive,
    name_pane,
    read_pane,
    send_keys,
    send_prompt,
    type_text,
)
from duo.verifier import Correction, Pass, verify_step


DEFAULT_COPILOT_MODEL = os.environ.get("DUO_COPILOT_MODEL", "claude-opus-4.6")

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


def build_bootstrap_prompt(task: Task) -> str:
    return SESSION_BOOTSTRAP_TEMPLATE.format(
        task_dir=str(task.dir),
        incarnation=task.incarnation_id,
    )


# === Task Prompt Templates ===


def build_task_prompt(task: Task) -> str:
    """Build prompt for current step+attempt."""
    step = task.current_step
    attempt = task.current_attempt
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


# === Session Management ===


def start_session(task: Task) -> None:
    """Start a Copilot session in tmux for this task."""
    transition(task, TaskStatus.SESSION_STARTING)

    # Create tmux pane and start copilot
    # Note: tmux must already be running (user starts duo inside tmux)
    import subprocess as sp

    # Create a new window in the current tmux session
    result = sp.run(
        ["tmux", "split-window", "-h", "-P", "-F", "#{pane_id}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        append_event(task, "session_start_failed", {"error": result.stderr})
        transition(task, TaskStatus.FAILED)
        return

    pane_id = result.stdout.strip()

    # Label the pane
    name_pane(pane_id, task.pane_label)

    # Tile layout for balance
    sp.run(["tmux", "select-layout", "tiled"])

    # cd to worktree, then start copilot (no -C flag available)
    time.sleep(0.5)
    send_prompt(task.pane_label, f"cd {task.worktree}")
    time.sleep(0.3)
    copilot_cmd = f"copilot --model {DEFAULT_COPILOT_MODEL} --yolo"
    send_prompt(task.pane_label, copilot_cmd)

    append_event(task, "session_started", {
        "incarnation": task.incarnation_id,
        "pane": task.pane_label,
        "pane_id": pane_id,
    })

    # Wait for copilot to start and show its prompt
    click.echo("Waiting for Copilot to start...")
    time.sleep(8)

    # Send bootstrap prompt (this is the first message in the conversation)
    bootstrap = build_bootstrap_prompt(task)
    send_prompt(task.pane_label, bootstrap)

    transition(task, TaskStatus.PROMPT_SENT)


def restart_session(task: Task) -> None:
    """Restart a crashed session with new incarnation."""
    old_inc = task.incarnation_id
    task.incarnation_id = new_incarnation()
    task.current_attempt = 1  # reset attempt for current step
    save_task(task)

    append_event(task, "session_restarted", {
        "old_incarnation": old_inc,
        "new_incarnation": task.incarnation_id,
    })

    start_session(task)


def send_task_prompt(task: Task, prompt: str) -> None:
    """Send a prompt and record it."""
    phash = prompt_hash(prompt)

    # Save prompt to file for debug
    prompt_path = task.prompt_path(task.current_step, task.current_attempt)
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt)

    # Send via tmux-bridge
    send_prompt(task.pane_label, prompt)

    task.last_prompt_sent_at = now_iso()
    save_task(task)

    append_event(task, "prompt_sent", {
        "step": task.current_step,
        "attempt": task.current_attempt,
        "incarnation": task.incarnation_id,
        "prompt_hash": f"sha256:{phash}",
    })

    transition(task, TaskStatus.PROMPT_SENT)


def resend_last_prompt(task: Task) -> None:
    """Resend the last prompt (e.g. after ack timeout)."""
    prompt_path = task.prompt_path(task.current_step, task.current_attempt)
    if prompt_path.exists():
        prompt = prompt_path.read_text()
        send_prompt(task.pane_label, prompt)
        task.last_prompt_sent_at = now_iso()
        save_task(task)
        append_event(task, "prompt_resent", {
            "step": task.current_step,
            "attempt": task.current_attempt,
            "incarnation": task.incarnation_id,
        })


# === Verify and Advance ===


def verify_and_advance(task: Task) -> None:
    """Verify the current step result and advance or correct."""
    step = task.current_step
    attempt = task.current_attempt
    result = read_result_for_step(task, step, attempt)

    if result is None:
        return

    transition(task, TaskStatus.VERIFYING)

    # Handle blocked/error results
    if result.status in ("blocked", "error"):
        transition(task, TaskStatus.BLOCKED)
        append_event(task, "agent_blocked", {
            "step": step, "reason": result.reason,
        })
        return

    # Run verification
    verdict = verify_step(task, result)

    if isinstance(verdict, Pass):
        # Advance to next step
        if step >= len(task.subtasks):
            # All done!
            transition(task, TaskStatus.COMPLETED)
            append_event(task, "task_completed", {"id": task.id})
        else:
            # Next step
            task.current_step = step + 1
            task.current_attempt = 1
            save_task(task)

            # Ensure step dir exists
            task.step_dir(task.current_step).mkdir(parents=True, exist_ok=True)

            # Send continuation prompt (doesn't consume Premium Request!)
            prompt = build_continue_prompt(task)
            send_task_prompt(task, prompt)

    elif isinstance(verdict, Correction):
        # Check correction count
        correction_count = _count_corrections(task, step)
        if correction_count >= 3:
            transition(task, TaskStatus.ESCALATED)
            append_event(task, "escalated_to_human", {
                "step": step, "reason": f"3 corrections exhausted: {verdict.reason}",
            })
            return

        # Send correction
        task.current_attempt = attempt + 1
        save_task(task)
        task.step_dir(step).mkdir(parents=True, exist_ok=True)

        transition(task, TaskStatus.CORRECTING)
        prompt = build_correction_prompt(task, verdict.reason)
        send_task_prompt(task, prompt)

        append_event(task, "correction_sent", {
            "step": step,
            "attempt": task.current_attempt,
            "incarnation": task.incarnation_id,
            "reason": verdict.reason,
        })


def _count_corrections(task: Task, step: int) -> int:
    """Count correction events for a step from the journal."""
    from duo.protocol import read_jsonl
    events = read_jsonl(task.journal_path)
    return sum(
        1 for ev in events
        if ev.get("event") == "correction_sent"
        and ev.get("data", {}).get("step") == step
    )


# === Poll task (main scheduling loop body) ===


def poll_task(task: Task, poller: AdaptivePoller) -> PollResult:
    """Core scheduling loop body. All checks use incarnation+step+attempt."""
    step = task.current_step
    attempt = task.current_attempt
    inc = task.incarnation_id

    poll_result = poller.poll(task)

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
                append_event(task, "api_error", {
                    "incarnation": inc,
                    "terminal": terminal[-500:],
                })
                send_prompt(task.pane_label, "请重试上一个操作")

    elif poll_result == PollResult.UNKNOWN:
        # Check if ack is missing
        ack = read_ack_for_step(task, step, attempt)
        if ack is None and task.last_prompt_sent_at:
            if age(task.last_prompt_sent_at) > 30:
                resend_last_prompt(task)

    return poll_result


# === Monitor loop ===


def monitor(task_ids: list[str] | None = None) -> None:
    """Run the adaptive polling monitor loop."""
    pollers: dict[str, AdaptivePoller] = {}

    while True:
        tasks = list_tasks()
        active = [
            t for t in tasks
            if t.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ESCALATED)
            and (task_ids is None or t.id in task_ids)
        ]

        if not active:
            print("[duo] No active tasks. Monitoring stopped.")
            break

        for task in active:
            if task.id not in pollers:
                pollers[task.id] = AdaptivePoller()

            poller = pollers[task.id]
            result = poll_task(task, poller)

            if result != PollResult.WORKING:
                print(f"[duo] {task.id}: {result.name} (step={task.current_step} attempt={task.current_attempt})")

        # Use the minimum interval across all active tasks
        min_interval = min(
            (pollers[t.id].interval for t in active if t.id in pollers),
            default=5.0,
        )
        time.sleep(min_interval)
