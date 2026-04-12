"""Task operation commands — ``duo send``, ``duo resume``, ``duo retry``, ``duo recover``."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import click

from duo.cli._helpers import (
    _TERMINAL_STATES,
    _complete_task_names,
    _load_task_or_fail,
)
from duo.errors import DuoUserError
from duo.protocol import (
    TaskStatus,
    list_tasks,
    replay_state,
)


@click.command()
@click.argument("name", shell_complete=_complete_task_names)
@click.argument("prompt", required=False, default=None)
@click.option(
    "--file",
    "-f",
    "prompt_file",
    type=click.Path(exists=True),
    help="Read prompt from file (use - for stdin)",
)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option(
    "-q", "--quiet", is_flag=True, help="Print only 'sent' or 'queued' for scripting"
)
def send(
    name: str,
    prompt: str | None,
    *,
    prompt_file: str | None = None,
    as_json: bool = False,
    quiet: bool = False,
) -> None:
    """Send a prompt to a task's Copilot session."""
    from duo.commander import send_task_prompt

    if prompt_file:
        if prompt:
            raise click.UsageError("cannot specify both PROMPT argument and --file")
        prompt = Path(prompt_file).read_text(encoding="utf-8")
    if not prompt or not prompt.strip():
        raise click.UsageError(
            'prompt cannot be empty. Usage: duo send TASK_NAME "your instruction"'
        )
    task = _load_task_or_fail(name)

    # Reject sends to terminal/dead states — give clear guidance
    if task.status in _TERMINAL_STATES:
        raise DuoUserError(
            f"task '{name}' is in terminal state '{task.status.value}'",
            fix=f"Use 'duo retry {name}' to retry, or create a new task.",
        )
    if task.status == TaskStatus.BLOCKED:
        raise DuoUserError(
            f"task '{name}' is blocked",
            fix=f"Use 'duo resume {name}' to restart it first.",
        )

    if task.status == TaskStatus.QUEUED:
        # Persist prompt for later — no pane exists yet, so don't try transport
        prompt_path = task.prompt_path(task.current_step, task.current_attempt)
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        from duo.protocol import atomic_write_text

        atomic_write_text(prompt_path, prompt)
        if quiet:
            click.echo("queued")
            return
        if as_json:
            click.echo(json.dumps({"sent": False, "queued": True, "task": name}))
        else:
            click.echo(
                f"Task '{name}' is queued — prompt saved and will be sent when task starts.",
                err=True,
            )
        return

    if task.status == TaskStatus.SESSION_STARTING:
        # Deferred start — Copilot is idle at ❯ prompt, send as bootstrap
        from duo.commander import build_bootstrap_prompt
        from duo.protocol import (
            append_event,
            atomic_write_text,
            now_iso,
            save_task,
            transition,
        )
        from duo.transport import send_bootstrap

        # Persist prompt file (like normal send path) for resume/replay
        prompt_path = task.prompt_path(task.current_step, task.current_attempt)
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(prompt_path, prompt)

        bootstrap = build_bootstrap_prompt(task, override_prompt=prompt)
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
        if quiet:
            click.echo("sent")
            return
        if as_json:
            click.echo(json.dumps({"sent": True, "task": name, "first_prompt": True}))
        else:
            click.echo(f"First prompt sent to '{name}' (session was deferred).")
        return

    send_task_prompt(task, prompt)
    if quiet:
        click.echo("sent")
        return
    if as_json:
        click.echo(
            json.dumps(
                {
                    "sent": True,
                    "task": name,
                    "step": task.current_step,
                    "attempt": task.current_attempt,
                }
            )
        )
    else:
        click.echo(
            f"Sent to {name} (step={task.current_step} attempt={task.current_attempt})"
        )


@click.command()
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option("-q", "--quiet", is_flag=True, help="Print only the recovered count")
def recover(as_json: bool, quiet: bool) -> None:
    """Recover all interrupted tasks from journals."""
    tasks = list_tasks()
    recovered = 0
    changes: list[dict[str, str]] = []
    for task in tasks:
        if task.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.ESCALATED,
        ):
            continue
        actual = replay_state(task)
        if actual != task.status:
            changes.append(
                {"task": task.id, "from": task.status.value, "to": actual.value}
            )
            if not as_json and not quiet:
                click.echo(f"  {task.id}: {task.status.value} → {actual.value}")
            task.status = actual
            from duo.protocol import save_task

            save_task(task)
            recovered += 1

    if quiet:
        click.echo(str(recovered))
        return

    if as_json:
        click.echo(json.dumps({"recovered": recovered, "changes": changes}, indent=2))
    else:
        click.echo(
            f"Recovered {recovered} tasks." if recovered else "All tasks consistent."
        )


@click.command()
@click.argument("name", required=False, shell_complete=_complete_task_names)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option("-q", "--quiet", is_flag=True, help="Print only the resumed count")
def resume(name: str | None, *, as_json: bool = False, quiet: bool = False) -> None:
    """Resume interrupted task sessions."""
    from duo.commander import normalize_for_restart, restart_session, start_session
    from duo.transport import cleanup_pane_state, is_process_alive, kill_pane

    results: list[dict[str, Any]] = []

    if name is not None:
        task = _load_task_or_fail(name)
        if task.status in _TERMINAL_STATES:
            if quiet:
                click.echo("0")
                return
            if as_json:
                click.echo(json.dumps({"resumed": [], "already_complete": [name]}))
            else:
                click.echo(f"Task '{name}' is already completed.")
            return
        targets = [task]
    else:
        all_tasks = list_tasks()
        _SKIP_STATES = _TERMINAL_STATES | {TaskStatus.QUEUED}
        targets = [t for t in all_tasks if t.status not in _SKIP_STATES]
        if not targets:
            if quiet:
                click.echo("0")
                return
            if as_json:
                click.echo(
                    json.dumps({"resumed": [], "message": "no interrupted tasks"})
                )
            else:
                click.echo("No interrupted tasks found.")
            return

    for task in targets:
        pane_alive = False
        try:
            pane_alive = is_process_alive(task.pane_label)
        except (RuntimeError, OSError):
            if not as_json and not quiet:
                click.echo(
                    f"  Warning: could not check pane status for '{task.id}', assuming dead",
                    err=True,
                )

        if pane_alive:
            if kill_pane(task.pane_label):
                cleanup_pane_state(task.pane_label)
            try:
                restart_session(task)
            except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
                if as_json:
                    results.append(
                        {"task": task.id, "resumed": False, "error": str(exc)}
                    )
                else:
                    click.echo(f"  Failed to resume '{task.id}': {exc}", err=True)
                continue
            if not as_json and not quiet:
                click.echo(f"Resumed task '{task.id}' — restarted session")
            results.append({"task": task.id, "resumed": True, "method": "restart"})
        else:
            if not normalize_for_restart(task):
                msg = (
                    f"Cannot normalize '{task.id}' from {task.status.value} for restart"
                )
                if as_json:
                    results.append({"task": task.id, "resumed": False, "error": msg})
                else:
                    click.echo(f"  {msg}", err=True)
                continue
            try:
                start_session(task)
            except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
                if as_json:
                    results.append(
                        {"task": task.id, "resumed": False, "error": str(exc)}
                    )
                else:
                    click.echo(f"  Failed to resume '{task.id}': {exc}", err=True)
                continue
            if not as_json and not quiet:
                click.echo(f"Resumed task '{task.id}' — started new session")
            results.append({"task": task.id, "resumed": True, "method": "new_session"})

        from duo.commander import build_task_prompt, send_task_prompt

        prompt_path = task.prompt_path(task.current_step, task.current_attempt)
        try:
            if prompt_path.exists():
                prompt = prompt_path.read_text()
            else:
                prompt = build_task_prompt(task)
        except (OSError, UnicodeDecodeError):
            prompt = build_task_prompt(task)
        try:
            send_task_prompt(task, prompt)
            if not as_json and not quiet:
                click.echo(f"  Replayed prompt for step {task.current_step}")
        except (RuntimeError, OSError) as exc:
            if not as_json and not quiet:
                click.echo(f"  Warning: could not replay prompt: {exc}", err=True)

    if quiet:
        resumed_count = sum(1 for r in results if r.get("resumed"))
        click.echo(str(resumed_count))
        return
    if as_json:
        click.echo(json.dumps({"resumed": results}))


@click.command()
@click.argument("name", shell_complete=_complete_task_names)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option("-q", "--quiet", is_flag=True, help="Print only the new status value")
def retry(name: str, *, as_json: bool = False, quiet: bool = False) -> None:
    """Retry a failed, blocked, or escalated task from its current step."""
    from duo.protocol import transition

    task = _load_task_or_fail(name)
    retryable = (TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.ESCALATED)
    if task.status not in retryable:
        raise DuoUserError(
            f"task '{name}' is '{task.status.value}', not retryable",
            fix=(
                f"Only FAILED, BLOCKED, or ESCALATED tasks can be retried. "
                f"Check with 'duo status {name}'."
            ),
        )
    # ESCALATED → PROMPT_SENT (re-send current step prompt)
    # FAILED/BLOCKED → SESSION_STARTING (restart session)
    if task.status == TaskStatus.ESCALATED:
        target = TaskStatus.PROMPT_SENT
    else:
        target = TaskStatus.SESSION_STARTING
    previous = task.status.value
    if not transition(task, target):
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "retried": False,
                        "error": f"Cannot transition from {previous} to {target.value}",
                    }
                )
            )
        else:
            click.echo(
                f"Error: cannot retry task '{name}' — illegal transition {previous} → {target.value}.",
                err=True,
            )
        raise SystemExit(1)
    if quiet:
        click.echo(target.value)
        return
    if as_json:
        click.echo(
            json.dumps(
                {
                    "retried": True,
                    "previous_status": previous,
                    "new_status": target.value,
                    "step": task.current_step,
                }
            )
        )
    else:
        click.echo(f"Task '{name}' queued for retry from step {task.current_step}.")
