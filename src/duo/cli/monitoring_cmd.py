"""Monitoring commands — ``duo status``, ``duo list``, ``duo monitor``, ``duo watch``, ``duo dashboard``."""

from __future__ import annotations

import json

import click

from duo.cli._helpers import (
    _TERMINAL_STATES,
    _complete_status_values,
    _complete_task_names,
    _fmt_age,
    _load_task_or_fail,
)
from duo.errors import DuoUserError
from duo.protocol import (
    Task,
    TaskStatus,
    list_tasks,
    load_task,
)


def _print_task(task: Task) -> None:
    click.echo(f"  {task.id}")
    click.echo(f"    Status:      {task.status.value}")
    click.echo(f"    Step:        {task.current_step}/{len(task.subtasks)}")
    click.echo(f"    Attempt:     {task.current_attempt}")
    click.echo(f"    Incarnation: {task.incarnation_id}")
    click.echo(f"    Branch:      {task.branch}")
    click.echo(f"    Worktree:    {task.worktree}")
    if task.description:
        click.echo(f"    Description: {task.description}")
    if task.created_at:
        click.echo(f"    Age:         {_fmt_age(task.created_at)}")
    if task.session_started_at:
        click.echo(f"    Session:     {task.session_started_at[:19]}")
    from duo.protocol import read_heartbeat

    hb = read_heartbeat(task)
    if hb and hb.current_file:
        click.echo(f"    Working on:  {hb.current_file}")
    if hb and hb.ts:
        click.echo(f"    Last pulse:  {_fmt_age(hb.ts)}")


@click.command()
@click.argument("name", required=False, shell_complete=_complete_task_names)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option("-q", "--quiet", is_flag=True, help="Print only the status value")
@click.option(
    "--wait",
    "wait_for",
    default=None,
    help="Block until task reaches this status (e.g. completed, failed)",
)
@click.option(
    "--timeout",
    "wait_timeout",
    default=300,
    type=int,
    help="Max seconds to wait (default: 300)",
)
def status(
    name: str | None = None,
    *,
    as_json: bool = False,
    quiet: bool = False,
    wait_for: str | None = None,
    wait_timeout: int = 300,
) -> None:
    """Show task status."""
    if wait_for is not None:
        if name is None:
            raise click.UsageError("--wait requires a task NAME")
        import time

        valid_statuses = {s.value for s in TaskStatus}
        if wait_for not in valid_statuses:
            raise DuoUserError(
                f"unknown status '{wait_for}'",
                fix=f"Valid statuses: {', '.join(sorted(valid_statuses))}",
            )
        deadline = time.monotonic() + wait_timeout
        while time.monotonic() < deadline:
            task = load_task(name)
            if task is None:
                raise DuoUserError(
                    f"task '{name}' not found",
                    fix="Run 'duo list' to see available tasks.",
                )
            if task.status.value == wait_for:
                if quiet:
                    click.echo(task.status.value)
                elif as_json:
                    click.echo(json.dumps({"id": task.id, "status": task.status.value}))
                else:
                    click.echo(f"Task '{name}' reached '{wait_for}' status.")
                return
            time.sleep(1)
        if quiet:
            click.echo(load_task(name).status.value if load_task(name) else "unknown")  # type: ignore[union-attr]
        else:
            click.echo(
                f"Timeout: task '{name}' did not reach '{wait_for}' "
                f"within {wait_timeout}s (current: {load_task(name).status.value if load_task(name) else 'unknown'})",  # type: ignore[union-attr]
                err=True,
            )
        raise SystemExit(1)

    if name:
        task = _load_task_or_fail(name)
        if quiet:
            click.echo(task.status.value)
            return
        if as_json:
            output = {
                "id": task.id,
                "status": task.status.value,
                "step": task.current_step,
                "total_steps": len(task.subtasks),
                "attempt": task.current_attempt,
                "worktree": task.worktree,
                "branch": task.branch,
                "incarnation_id": task.incarnation_id,
                "created_at": task.created_at,
                "session_started_at": task.session_started_at,
                "description": task.description,
                "age": _fmt_age(task.created_at),
            }
            click.echo(json.dumps(output, indent=2))
            return
        _print_task(task)
    else:
        tasks = list_tasks()
        if not tasks:
            if not quiet:
                click.echo("No tasks.")
            return
        if quiet:
            for t in tasks:
                click.echo(f"{t.id}\t{t.status.value}")
            return
        for t in tasks:
            _print_task(t)
            click.echo("")


@click.command("list")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option(
    "--status",
    "status_filter",
    default=None,
    shell_complete=_complete_status_values,
    help="Filter by task status (e.g. running, completed, created)",
)
@click.option(
    "--sort",
    "sort_by",
    type=click.Choice(["name", "status", "age"], case_sensitive=False),
    default=None,
    help="Sort tasks by field",
)
@click.option("--reverse", is_flag=True, help="Reverse sort order")
@click.option("-q", "--quiet", is_flag=True, help="Only print task IDs (one per line)")
@click.option("-c", "--count", is_flag=True, help="Only print the number of tasks")
@click.option("--no-header", is_flag=True, help="Omit table header")
@click.option("--active", is_flag=True, help="Show only running/active tasks")
@click.option("--finished", is_flag=True, help="Show only completed/failed tasks")
@click.option(
    "--wide", "-w", is_flag=True, help="Show description column in table output"
)
@click.option(
    "--recent",
    type=int,
    default=None,
    help="Show only the N most recently created tasks",
)
def list_cmd(
    as_json: bool,
    status_filter: str | None,
    sort_by: str | None,
    reverse: bool,
    quiet: bool,
    count: bool,
    no_header: bool,
    active: bool,
    finished: bool,
    wide: bool,
    recent: int | None,
) -> None:
    """List all tasks."""
    tasks = list_tasks()

    if active:
        _ACTIVE_STATUSES = {
            TaskStatus.RUNNING,
            TaskStatus.SESSION_STARTING,
            TaskStatus.PROMPT_SENT,
            TaskStatus.ACKED,
            TaskStatus.RESULT_REPORTED,
            TaskStatus.VERIFYING,
            TaskStatus.CORRECTING,
        }
        tasks = [t for t in tasks if t.status in _ACTIVE_STATUSES]

    if finished:
        tasks = [t for t in tasks if t.status in _TERMINAL_STATES]

    if status_filter:
        valid_statuses = {s.value for s in TaskStatus}
        if status_filter not in valid_statuses:
            raise DuoUserError(
                f"unknown status '{status_filter}'",
                fix=f"Valid statuses: {', '.join(sorted(valid_statuses))}",
            )
        tasks = [t for t in tasks if t.status.value == status_filter]

    if sort_by == "name":
        tasks.sort(key=lambda t: t.id, reverse=reverse)
    elif sort_by == "status":
        tasks.sort(key=lambda t: t.status.value, reverse=reverse)
    elif sort_by == "age":
        tasks.sort(key=lambda t: t.created_at or "", reverse=not reverse)

    if recent is not None:
        tasks.sort(key=lambda t: t.created_at or "", reverse=True)
        tasks = tasks[:recent]

    if count:
        click.echo(len(tasks))
        return

    if not tasks:
        if not quiet:
            click.echo("No tasks.")
        return

    if quiet:
        for t in tasks:
            click.echo(t.id)
        return

    if as_json:
        output = [
            {
                "id": t.id,
                "status": t.status.value,
                "step": t.current_step,
                "total_steps": len(t.subtasks),
                "attempt": t.current_attempt,
                "worktree": t.worktree,
                "branch": t.branch,
                "incarnation_id": t.incarnation_id,
                "created_at": t.created_at,
                "session_started_at": t.session_started_at,
                "age": _fmt_age(t.created_at),
                "description": t.description,
            }
            for t in tasks
        ]
        click.echo(json.dumps(output, indent=2))
        return

    if not no_header:
        header = (
            f"{'ID':<20} {'STATUS':<18} {'STEP':<8} {'AGE':<10} {'INCARNATION':<12}"
        )
        if wide:
            header += f" {'DESCRIPTION'}"
        click.echo(header)
        click.echo("-" * (70 if not wide else 100))
    for t in tasks:
        step_str = f"{t.current_step}/{len(t.subtasks)}"
        age_str = _fmt_age(t.created_at)
        line = f"{t.id:<20} {t.status.value:<18} {step_str:<8} {age_str:<10} {t.incarnation_id:<12}"
        if wide:
            desc = (
                (t.description[:28] + "...")
                if len(t.description) > 30
                else t.description
            )
            line += f" {desc}"
        click.echo(line)


@click.command()
@click.argument("names", nargs=-1)
@click.option(
    "--max-time",
    type=int,
    default=0,
    help="Max seconds before timing out tasks (overrides config).",
)
def monitor(names: tuple[str, ...], max_time: int) -> None:
    """Start adaptive polling monitor."""
    from duo.commander import monitor as run_monitor
    from duo.config import set_config

    if max_time > 0:
        set_config("task_timeout", str(max_time))
    task_ids = list(names) if names else None
    click.echo("[duo] Starting monitor...")
    try:
        run_monitor(task_ids)
    except KeyboardInterrupt:
        click.echo("\n[duo] Monitor stopped.")


@click.command()
@click.argument("names", nargs=-1)
@click.option(
    "--timeout",
    type=float,
    default=300,
    help="Seconds to wait for each dialog (default: 300).",
)
@click.option(
    "--interval",
    type=float,
    default=5.0,
    help="Poll interval in seconds (default: 5).",
)
@click.option("--once", is_flag=True, help="Exit after detecting one dialog.")
@click.option(
    "--auto-approve",
    is_flag=True,
    default=False,
    help="Auto-approve permission dialogs (legacy). Default: detect and report only.",
)
def watch(
    names: tuple[str, ...],
    timeout: float,
    interval: float,
    once: bool,
    auto_approve: bool,
) -> None:
    """Pane dialog detector — monitors tasks and reports dialogs.

    Default mode: watches task panes for permission dialogs. When a dialog
    is detected, prints its content, writes a signal file to
    ``~/.duo/watch-events/``, and exits so the CEO process can decide
    what to do next.

    With ``--auto-approve``: automatically approves permission dialogs
    (unattended mode).

    \b
    Example:
      duo watch                # detect dialog → print → signal → exit
      duo watch --auto-approve # auto-approve dialogs (legacy behavior)
      duo watch --once         # exit after first detection
    """
    if timeout <= 0:
        raise click.UsageError("--timeout must be > 0. Example: --timeout 60")
    if interval <= 0:
        raise click.UsageError("--interval must be > 0. Example: --interval 5")
    from duo.commander import watch_tasks

    task_ids = list(names) if names else None
    try:
        watch_tasks(
            task_ids,
            timeout=timeout,
            interval=interval,
            once=once,
            auto_approve=auto_approve,
        )
    except KeyboardInterrupt:
        click.echo("\n[duo] Watch stopped.")


@click.command()
@click.argument("names", nargs=-1)
@click.option("--refresh", default=2.0, help="Refresh rate in seconds")
def dashboard(names: tuple[str, ...], refresh: float) -> None:
    """Live terminal dashboard for task monitoring."""
    if refresh <= 0:
        raise click.UsageError("--refresh must be > 0. Example: --refresh 2")
    try:
        from duo.dashboard import run_dashboard
    except ImportError:
        raise DuoUserError(
            "'rich' library required for dashboard",
            fix="Run: uv add rich",
        ) from None

    task_ids = list(names) if names else None
    run_dashboard(task_ids, refresh_rate=refresh)
