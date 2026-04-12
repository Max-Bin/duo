"""CLI entry point — thin interface to commander."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import click

from duo.cli._helpers import (  # noqa: F401 — re-export shared helpers
    _ALIASES as _ALIASES,
)
from duo.cli._helpers import (
    _COMMAND_SECTIONS as _COMMAND_SECTIONS,
)
from duo.cli._helpers import (
    _GIT_TIMEOUT as _GIT_TIMEOUT,
)
from duo.cli._helpers import (
    _MAX_AGE_SECONDS as _MAX_AGE_SECONDS,
)
from duo.cli._helpers import (
    _TMUX_TIMEOUT as _TMUX_TIMEOUT,
)
from duo.cli._helpers import (
    _complete_status_values as _complete_status_values,
)
from duo.cli._helpers import (
    _complete_task_names,
    _fmt_ts,
    _load_task_or_fail,
    _OrderedGroup,
    _run_git,
)
from duo.cli._helpers import (
    _complete_thinking_names as _complete_thinking_names,
)
from duo.cli._helpers import (
    _create_worktree as _create_worktree,  # noqa: F401 — re-export for tests
)
from duo.cli._helpers import (
    _fmt_age as _fmt_age,
)
from duo.cli._helpers import (
    _safe_join as _safe_join,
)
from duo.cli._helpers import (
    _validate_task_name as _validate_task_name,
)
from duo.cli.doctor import (
    CheckResult as CheckResult,  # noqa: F401 — re-export
)
from duo.cli.doctor import (
    _doctor_auto_fix as _doctor_auto_fix,  # noqa: F401 — re-export
)
from duo.cli.doctor import (
    _doctor_check_orphan_worktrees as _doctor_check_orphan_worktrees,  # noqa: F401 — re-export
)
from duo.cli.doctor import (
    _doctor_check_stale_locks as _doctor_check_stale_locks,  # noqa: F401 — re-export
)
from duo.cli.doctor import (
    _emit_restart_signal as _emit_restart_signal,  # noqa: F401 — re-export
)
from duo.cli.doctor import (
    _get_pid_child_count as _get_pid_child_count,  # noqa: F401 — re-export
)
from duo.cli.doctor import (
    _get_pid_fd_count as _get_pid_fd_count,  # noqa: F401 — re-export
)
from duo.cli.doctor import (
    _get_pid_kqueue_count as _get_pid_kqueue_count,  # noqa: F401 — re-export
)
from duo.cli.doctor import (
    doctor as doctor_command,
)
from duo.config import get_config
from duo.errors import DuoUserError
from duo.protocol import (
    DUO_DIR as DUO_DIR,  # noqa: F401 — re-export for test monkeypatching
)
from duo.protocol import (
    TASKS_DIR,
    Subtask,
    Task,
    TaskStatus,
    create_task,
    list_tasks,
    load_task,
    read_jsonl,
    replay_state,
)

_TERMINAL_STATES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ESCALATED}
)

__all__ = ["main"]


@click.group(cls=_OrderedGroup)
@click.option("-v", "--verbose", is_flag=True, help="Verbose output")
@click.version_option(package_name="duo", prog_name="duo")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """Duo — Agent Orchestration Runtime."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    if verbose:
        logging.basicConfig(
            level=logging.DEBUG, format="%(name)s %(levelname)s: %(message)s"
        )
    try:
        TASKS_DIR.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError) as e:
        raise DuoUserError(
            f"cannot create tasks directory '{TASKS_DIR}': {e}",
            fix="Check write permissions or run 'duo doctor'.",
        ) from None


main.add_command(doctor_command, "doctor")


@main.command()
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option("-q", "--quiet", is_flag=True, help="Print only the version number")
def version(*, as_json: bool = False, quiet: bool = False) -> None:
    """Show Duo version."""
    import platform
    import sys

    from duo import __version__

    if quiet:
        click.echo(__version__)
        return
    if as_json:
        click.echo(
            json.dumps(
                {
                    "version": __version__,
                    "python": sys.version.split()[0],
                    "platform": platform.system(),
                }
            )
        )
    else:
        click.echo(f"duo {__version__}")


@main.command()
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
def completion(shell: str) -> None:
    """Generate shell completion script.

    \b
    Add to your shell profile:
      # Bash (~/.bashrc)
      eval "$(duo completion bash)"

      # Zsh (~/.zshrc)
      eval "$(duo completion zsh)"

      # Fish (~/.config/fish/config.fish)
      duo completion fish | source
    """
    scripts = {
        "bash": 'eval "$(_DUO_COMPLETE=bash_source duo)"',
        "zsh": 'eval "$(_DUO_COMPLETE=zsh_source duo)"',
        "fish": "set -x _DUO_COMPLETE fish_source\nduo | source\nset -e _DUO_COMPLETE",
    }
    click.echo(scripts[shell])


from duo.cli.lifecycle_cmd import go as go_command  # noqa: E402
from duo.cli.lifecycle_cmd import init as init_command  # noqa: E402
from duo.cli.lifecycle_cmd import start as start_command  # noqa: E402

main.add_command(start_command, "start")
main.add_command(init_command, "init")
main.add_command(go_command, "go")


@main.command()
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


@main.command()
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
    # Show heartbeat info for active tasks
    from duo.protocol import read_heartbeat

    hb = read_heartbeat(task)
    if hb and hb.current_file:
        click.echo(f"    Working on:  {hb.current_file}")
    if hb and hb.ts:
        click.echo(f"    Last pulse:  {_fmt_age(hb.ts)}")


@main.command("list")
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
        # Sort by created_at descending and take the first N
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


@main.command()
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


@main.command()
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


@main.command()
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


from duo.cli.task_mgmt_cmd import kill as kill_command  # noqa: E402
from duo.cli.task_mgmt_cmd import merge as merge_command  # noqa: E402
from duo.cli.task_mgmt_cmd import stop as stop_command  # noqa: E402

main.add_command(merge_command, "merge")
main.add_command(stop_command, "stop")
main.add_command(kill_command, "kill")


_MAX_BATCH_FILE_BYTES = 10_000_000  # 10 MB


def _load_batch_file(file: str) -> list[dict[str, Any]]:
    """Read a JSON or YAML batch file and return the list of task definitions."""
    file_path = Path(file)
    try:
        size = file_path.stat().st_size
        if size > _MAX_BATCH_FILE_BYTES:
            raise click.UsageError(
                f"batch file '{file}' too large ({size} bytes, max {_MAX_BATCH_FILE_BYTES})"
            )
        content = file_path.read_text()
    except (FileNotFoundError, PermissionError, OSError) as e:
        raise click.UsageError(f"cannot read batch file '{file}': {e}") from None

    if file_path.suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-untyped,unused-ignore]

            tasks_data = yaml.safe_load(content)
        except ImportError:
            raise click.UsageError(
                "PyYAML not installed. Run: uv pip install pyyaml\nOr use JSON format instead."
            ) from None
        except yaml.YAMLError as e:
            raise click.UsageError(
                f"invalid YAML in '{file}': {e}\n"
                "Hint: validate with `python -c \"import yaml; yaml.safe_load(open('{file}'))\"` or use JSON."
            ) from None
    else:
        try:
            tasks_data = json.loads(content)
        except json.JSONDecodeError as e:
            raise click.UsageError(
                f"invalid JSON in '{file}': {e}\nHint: validate with `python -m json.tool {file}`."
            ) from None

    if not isinstance(tasks_data, dict) or "tasks" not in tasks_data:
        if isinstance(tasks_data, list):
            tasks_data = {"tasks": tasks_data}
        else:
            raise click.UsageError(
                "file must contain a 'tasks' key with a list of tasks. See examples/tasks.json"
            )

    if not isinstance(tasks_data.get("tasks"), list):
        raise click.UsageError("'tasks' must be a list of task objects.")

    if not tasks_data.get("tasks"):
        raise click.UsageError("No tasks defined in file.")

    if not all(isinstance(t, dict) for t in tasks_data["tasks"]):
        raise click.UsageError("Each task in 'tasks' must be a JSON object.")

    tasks_list: list[dict[str, Any]] = list(tasks_data["tasks"])

    # Check for duplicate names
    names = [t.get("name", "") for t in tasks_list]
    seen: set[str] = set()
    dupes: list[str] = []
    for n in names:
        if n in seen:
            dupes.append(n)
        seen.add(n)
    if dupes:
        raise click.UsageError(
            f"duplicate task names in batch file: {', '.join(dupes)}"
        )

    return tasks_list


def _create_single_task(
    defn: dict[str, Any], repo: str, *, queue_only: bool = False
) -> str | None:
    """Create a single task from a batch definition dict.

    Returns task name on success, None on failure (prints error).

    When *queue_only* is False (default), the task is handed to the scheduler
    which may start it immediately or queue it.  When True, the task is placed
    directly into QUEUED state without starting a session.
    """
    name = defn.get("name")
    if not name or not isinstance(name, str):
        click.echo("  ✗ (unnamed): task missing 'name' field", err=True)
        return None
    try:
        _validate_task_name(name)
    except (SystemExit, click.BadParameter):
        click.echo(f"  ✗ {name}: invalid task name", err=True)
        return None
    desc = defn.get("description", f"Task {name}")
    target_files = defn.get("target_files", [])
    writable = defn.get("writable_paths", ["*"])
    if not isinstance(target_files, list):
        click.echo(f"  ✗ {name}: 'target_files' must be a list", err=True)
        return None
    if not isinstance(writable, list):
        click.echo(f"  ✗ {name}: 'writable_paths' must be a list", err=True)
        return None

    worktree_base = get_config("worktree_base_path")
    worktree = _safe_join(worktree_base, name)
    branch = f"duo/{name}"

    # Get base commit
    result = _run_git(["rev-parse", "HEAD"], cwd=repo)
    base_commit = result.stdout.strip()

    r = _run_git(["worktree", "add", worktree, "-b", branch], cwd=repo, check=False)
    if r.returncode != 0:
        click.echo(
            f"  ✗ {name}: failed to create worktree: {r.stderr.strip()}", err=True
        )
        return None

    task = create_task(
        task_id=name,
        description=desc,
        worktree=worktree,
        branch=branch,
        base_commit=base_commit,
        subtasks=[
            Subtask(
                step_id=1,
                description=desc,
                target_files=target_files,
                writable_paths=writable,
            )
        ],
    )

    if queue_only:
        from duo.protocol import transition

        if not transition(task, TaskStatus.QUEUED):
            click.echo(f"  ⚠ {name}: could not transition to QUEUED", err=True)
        else:
            click.echo(f"  ◷ {name}: queued")
    else:
        from duo.commander import start_session
        from duo.scheduler import enqueue_or_start

        action = enqueue_or_start(task)
        if action == "started":
            start_session(task)
            click.echo(f"  ✓ {name}: started")
        else:
            click.echo(f"  ◷ {name}: queued")
    return str(name)


@main.command()
@click.argument("file", type=click.Path(exists=True))
@click.option("--repo", default=".", help="Git repo path")
@click.option("--dry-run", is_flag=True, help="Preview tasks without creating")
@click.option(
    "--queue", "start_queued", is_flag=True, help="Create all tasks in queued state"
)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option("-q", "--quiet", is_flag=True, help="Print only the created count")
@click.pass_context
def batch(
    ctx: click.Context,
    file: str,
    repo: str,
    dry_run: bool,
    start_queued: bool,
    *,
    as_json: bool = False,
    quiet: bool = False,
) -> None:
    """Create multiple tasks from a file (JSON or YAML)."""
    from duo.scheduler import queue_status

    repo = os.path.abspath(repo)

    task_defs = _load_batch_file(file)

    if dry_run:
        if quiet:
            click.echo(str(len(task_defs)))
            return
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "dry_run": True,
                        "tasks": [
                            {
                                "name": td["name"],
                                "description": td.get("description", ""),
                            }
                            for td in task_defs
                        ],
                    }
                )
            )
        else:
            click.echo(f"Would create {len(task_defs)} tasks:")
            for i, td in enumerate(task_defs, 1):
                click.echo(
                    f"  {i}. {td['name']} — {td.get('description', '(no description)')}"
                )
        return

    created = 0
    created_names: list[str] = []
    for task_def in task_defs:
        name = _create_single_task(task_def, repo, queue_only=start_queued)
        if name is not None:
            created += 1
            created_names.append(name)

    qs = queue_status()
    if quiet:
        click.echo(str(created))
        return
    if as_json:
        click.echo(
            json.dumps(
                {
                    "created": created,
                    "tasks": created_names,
                    "active": qs["active_count"],
                    "max_parallel": qs["max_parallel"],
                    "queued": qs["queued_count"],
                }
            )
        )
    else:
        click.echo(f"\nBatch complete: {created} tasks created")
        click.echo(f"  Active: {qs['active_count']}/{qs['max_parallel']}")
        click.echo(f"  Queued: {qs['queued_count']}")
        if qs["queued_count"] > 0:
            click.echo("Run 'duo monitor' to process the queue.")


@main.command()
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option("-q", "--quiet", is_flag=True, help="Print only the queue length")
def queue(as_json: bool, quiet: bool) -> None:
    """Show queue status."""
    from duo.scheduler import queue_status

    qs = queue_status()

    if quiet:
        click.echo(str(len(qs["queued_tasks"])))
        return

    if as_json:
        click.echo(json.dumps(qs, indent=2))
        return

    queued = qs["queued_tasks"]
    if queued:
        click.echo(f"Queue ({len(queued)} tasks):")
        for i, name in enumerate(queued, 1):
            click.echo(f"  {i}. {name}")
    else:
        click.echo("Queue: empty")

    click.echo(f"Active: {qs['active_count']} / max_parallel: {qs['max_parallel']}")


@main.command()
@click.argument("name", required=False, shell_complete=_complete_task_names)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option("-q", "--quiet", is_flag=True, help="Print only the total PR count")
def audit(
    name: str | None = None, *, as_json: bool = False, quiet: bool = False
) -> None:
    """Show Premium Request consumption audit."""
    from duo.protocol import read_jsonl
    from duo.transport import get_pr_log

    if name:
        # Single task audit
        task = _load_task_or_fail(name)
        events = read_jsonl(task.journal_path)
        pr_events = [ev for ev in events if ev.get("event") == "pr_consumed"]

        if quiet:
            click.echo(str(len(pr_events)))
            return

        if as_json:
            click.echo(
                json.dumps(
                    {
                        "task": task.id,
                        "pr_consumed": len(pr_events),
                        "events": pr_events,
                    },
                    indent=2,
                )
            )
            return

        click.echo(f"Task: {task.id}")
        click.echo(f"PR consumed: {len(pr_events)}")
        if pr_events:
            click.echo(f"\n{'TIME':<10} {'ACTION':<16} {'STEP':<6} {'ATTEMPT':<8}")
            click.echo("-" * 42)
            for ev in pr_events:
                ts = _fmt_ts(ev.get("ts", "?"))
                data = ev.get("data", {})
                click.echo(
                    f"{ts:<10} {data.get('action', '?'):<16} "
                    f"{data.get('step', '?'):<6} {data.get('attempt', '?'):<8}"
                )
    else:
        # All tasks audit
        tasks = list_tasks()
        if not tasks:
            if quiet:
                click.echo("0")
                return
            click.echo("No tasks.")
            return

        total_pr = 0
        task_rows: list[dict[str, Any]] = []
        for t in tasks:
            events = read_jsonl(t.journal_path)
            pr_count = sum(1 for ev in events if ev.get("event") == "pr_consumed")
            total_pr += pr_count
            task_rows.append(
                {"task": t.id, "status": t.status.value, "pr_count": pr_count}
            )

        if quiet:
            click.echo(str(total_pr))
            return

        if as_json:
            pr_log = get_pr_log()
            click.echo(
                json.dumps(
                    {"tasks": task_rows, "total_pr": total_pr, "session_log": pr_log},
                    indent=2,
                )
            )
            return

        click.echo(f"{'TASK':<20} {'STATUS':<14} {'PR COUNT':<10}")
        click.echo("-" * 46)
        for row in task_rows:
            click.echo(f"{row['task']:<20} {row['status']:<14} {row['pr_count']:<10}")

        click.echo("-" * 46)
        click.echo(f"{'TOTAL':<20} {'':<14} {total_pr:<10}")

        # Show session-level log
        pr_log = get_pr_log()
        if pr_log:
            click.echo(f"\nSession log ({len(pr_log)} entries):")
            for entry in pr_log[-10:]:
                ts = _fmt_ts(entry.get("ts", "?"))
                click.echo(
                    f"  {ts} {entry.get('action', '?')} [{entry.get('label', '?')}]"
                )


@main.command()
@click.option("--task", "task_name", default=None, help="Filter to a specific task")
@click.option(
    "--since", "since_days", default=None, type=int, help="Only show last N days"
)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option("--budget", default=None, type=int, help="Exit non-zero if total PR > N")
@click.option("-q", "--quiet", is_flag=True, help="Print only the total PR count")
def cost(
    task_name: str | None,
    since_days: int | None,
    *,
    as_json: bool = False,
    budget: int | None = None,
    quiet: bool = False,
) -> None:
    """Show Premium Request consumption across tasks."""
    from collections import Counter
    from datetime import UTC, datetime, timedelta

    cutoff: datetime | None = None
    if since_days is not None:
        cutoff = datetime.now(UTC) - timedelta(days=since_days)

    def _parse_iso(ts: str) -> datetime | None:
        try:
            return datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            return None

    def _collect_events(task: Task) -> list[dict[str, Any]]:
        events = read_jsonl(task.journal_path)
        pr_events = [ev for ev in events if ev.get("event") == "pr_consumed"]
        if cutoff is not None:
            pr_events = [
                ev
                for ev in pr_events
                if (dt := _parse_iso(ev.get("ts", ""))) is not None and dt >= cutoff
            ]
        return pr_events

    # Resolve tasks
    if task_name is not None:
        task = _load_task_or_fail(task_name)
        tasks_to_scan = [task]
    else:
        tasks_to_scan = list_tasks()

    if not tasks_to_scan:
        if quiet:
            click.echo("0")
        elif as_json:
            click.echo(json.dumps({"tasks": [], "total_pr": 0}, indent=2))
        else:
            click.echo("No tasks.")
        if budget is not None and budget < 0:
            sys.exit(1)
        return

    # Collect per-task data
    rows: list[dict[str, Any]] = []
    grand_total = 0
    for t in tasks_to_scan:
        pr_events = _collect_events(t)
        count = len(pr_events)
        grand_total += count
        if count == 0:
            rows.append(
                {"task": t.id, "prs": 0, "first": "", "last": "", "top_action": ""}
            )
            continue

        timestamps = [ev.get("ts", "") for ev in pr_events]
        first_ts = min(timestamps)
        last_ts = max(timestamps)

        actions = Counter(
            ev.get("data", {}).get("action", "unknown") for ev in pr_events
        )
        top_action, top_count = actions.most_common(1)[0]

        rows.append(
            {
                "task": t.id,
                "prs": count,
                "first": first_ts[:16].replace("T", " "),
                "last": last_ts[:16].replace("T", " "),
                "top_action": f"{top_action} ({top_count})",
            }
        )

    if quiet:
        click.echo(str(grand_total))
    elif as_json:
        click.echo(json.dumps({"tasks": rows, "total_pr": grand_total}, indent=2))
    else:
        header = (
            f"{'Task':<16}{'PRs':>5}   {'First Use':<21}{'Last Use':<21}{'Top Action'}"
        )
        click.echo(header)
        for row in rows:
            click.echo(
                f"{row['task']:<16}{row['prs']:>5}   {row['first']:<21}"
                f"{row['last']:<21}{row['top_action']}"
            )
        click.echo("\u2500" * len(header))
        click.echo(f"{'Total':<16}{grand_total:>5}")

    if budget is not None and grand_total > budget:
        click.echo(
            f"Error: PR consumption ({grand_total}) exceeds budget ({budget})",
            err=True,
        )
        sys.exit(1)


@main.command()
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


from duo.cli.logs_cmd import (  # noqa: E402
    logs as logs_command,
)

main.add_command(logs_command, "logs")


from duo.cli.inspect_cmd import inspect as inspect_command

main.add_command(inspect_command, "inspect")


@main.command()
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
        if prompt_path.exists():
            prompt = prompt_path.read_text()
        else:
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


@main.command()
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


# ---------------------------------------------------------------------------
# Config — extracted to duo.cli.config_cmd
# ---------------------------------------------------------------------------
from duo.cli.config_cmd import (
    _complete_config_keys as _complete_config_keys,  # noqa: F401,E402 — re-export
)
from duo.cli.config_cmd import config as config_group  # noqa: E402

main.add_command(config_group, "config")


# ---------------------------------------------------------------------------
# duo events — extracted to duo.cli.events_cmd
# ---------------------------------------------------------------------------
from duo.cli.events_cmd import (  # noqa: E402
    _WATCH_EVENTS_DIR as _WATCH_EVENTS_DIR,  # noqa: F401 — re-export
)
from duo.cli.events_cmd import events as events_group  # noqa: E402

main.add_command(events_group, "events")


# ---------------------------------------------------------------------------
# duo ceo-* — Ergonomic CEO workflow commands
# ---------------------------------------------------------------------------


from duo.cli.ceo_cmd import (  # noqa: E402
    _enforce_not_at_main_prompt as _enforce_not_at_main_prompt,  # noqa: F401 — re-export
)
from duo.cli.ceo_cmd import (  # noqa: E402
    _log_pr_budget_warning as _log_pr_budget_warning,  # noqa: F401 — re-export
)
from duo.cli.ceo_cmd import (  # noqa: E402
    assert_not_at_main_prompt as assert_not_at_main_prompt,  # noqa: F401 — re-export
)
from duo.cli.ceo_cmd import (  # noqa: E402
    ceo_approve,
    ceo_select,
    ceo_status,
    ceo_wait,
)

main.add_command(ceo_wait, "ceo-wait")
main.add_command(ceo_select, "ceo-select")
main.add_command(ceo_approve, "ceo-approve")
main.add_command(ceo_status, "ceo-status")


from duo.cli.cleanup_cmd import (  # noqa: E402
    _parse_age as _parse_age,  # noqa: F401 — re-export
)
from duo.cli.cleanup_cmd import (  # noqa: E402
    cleanup as cleanup_command,
)

main.add_command(cleanup_command, "cleanup")


@main.command("diff")
@click.argument("name", shell_complete=_complete_task_names)
@click.option("--stat", "show_stat", is_flag=True, help="Show diffstat summary only")
@click.option(
    "--name-only", "name_only", is_flag=True, help="List changed file names only"
)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option("-q", "--quiet", is_flag=True, help="Print only the changed file count")
def diff_cmd(
    name: str,
    *,
    show_stat: bool,
    name_only: bool,
    as_json: bool = False,
    quiet: bool = False,
) -> None:
    """Show git diff for a task's worktree changes."""
    task = _load_task_or_fail(name)

    if not Path(task.worktree).exists():
        raise DuoUserError(
            f"worktree '{task.worktree}' not found",
            fix="It may have been cleaned up. Run 'duo cleanup' to remove stale tasks.",
        )

    if quiet:
        r_names = _run_git(
            ["diff", task.base_commit, "--name-only"], cwd=task.worktree, check=False
        )
        files = [f for f in r_names.stdout.strip().splitlines() if f]
        click.echo(str(len(files)))
        return

    if as_json:
        r_names = _run_git(
            ["diff", task.base_commit, "--name-only"], cwd=task.worktree, check=False
        )
        r_stat = _run_git(
            ["diff", task.base_commit, "--stat"], cwd=task.worktree, check=False
        )
        files = [f for f in r_names.stdout.strip().splitlines() if f]
        click.echo(
            json.dumps(
                {
                    "task": task.id,
                    "branch": task.branch,
                    "base_commit": task.base_commit,
                    "files_changed": files,
                    "stat": r_stat.stdout.strip(),
                    "has_changes": len(files) > 0,
                }
            )
        )
        return

    git_args = ["diff", task.base_commit]
    if show_stat:
        git_args.append("--stat")
    if name_only:
        git_args.append("--name-only")
    result = _run_git(git_args, cwd=task.worktree, check=False)
    if result.stdout:
        click.echo(result.stdout)
    else:
        click.echo("No changes.")


# ---------------------------------------------------------------------------
# Think — extracted to duo.cli.think_cmd
# ---------------------------------------------------------------------------
from duo.cli.think_cmd import think as think_command  # noqa: E402

main.add_command(think_command, "think")


# ---------------------------------------------------------------------------
# bench
# ---------------------------------------------------------------------------
