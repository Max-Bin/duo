"""CLI entry point — thin interface to commander."""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from duo.config import get_config
from duo.errors import DuoUserError
from duo.protocol import (
    DUO_DIR,  # noqa: F401 — used by test monkeypatching
    TASKS_DIR,
    Heartbeat,
    Subtask,
    Task,
    TaskStatus,
    atomic_write_text,
    create_task,
    list_tasks,
    load_task,
    read_json,
    read_jsonl,
    replay_state,
    write_json,
)

BENCH_DIR = DUO_DIR / "bench-results"

__all__ = ["main"]

_COMMAND_SECTIONS: dict[str, list[str]] = {
    "Task Lifecycle": ["start", "send", "stop", "status", "merge", "diff", "kill"],
    "Thinking": ["think"],
    "Monitoring": ["list", "monitor", "watch", "dashboard", "logs", "inspect", "stats"],
    "Batch & Queue": ["batch", "queue"],
    "CEO Workflow": [
        "ceo-wait",
        "ceo-select",
        "ceo-approve",
        "ceo-smart",
        "ceo-smart-config",
        "ceo-dispatch",
        "ceo-status",
        "ceo-loop",
        "ceo-resume",
        "ceo-focus",
        "ceo-focus-show",
        "ceo-focus-clear",
        "ceo-now",
        "ceo-session-start",
        "ceo-session-list",
        "ceo-session-replay",
        "ceo-session-stats",
        "ceo-metrics",
    ],
    "Recovery": ["recover", "resume", "retry"],
    "Data & Audit": ["export", "audit", "cost", "cleanup", "events"],
    "Setup": ["init", "doctor", "config"],
    "Benchmarking": ["bench"],
    "Misc": ["version", "completion"],
}


class _OrderedGroup(click.Group):
    """Click group that displays commands in categorized sections."""

    def format_commands(
        self, ctx: click.Context, formatter: click.HelpFormatter
    ) -> None:
        """Format help output with categorized command sections."""
        seen: set[str] = set()
        for section, cmd_names in _COMMAND_SECTIONS.items():
            rows: list[tuple[str, str]] = []
            for name in cmd_names:
                cmd = self.get_command(ctx, name)
                if cmd is None:
                    continue  # pragma: no cover — defensive for future section edits
                seen.add(name)
                help_text = cmd.get_short_help_str(limit=60)
                rows.append((name, help_text))
            if rows:
                with formatter.section(section):
                    formatter.write_dl(rows)

        # Any commands not in a section
        extra: list[tuple[str, str]] = []
        for name in self.list_commands(ctx):
            if name not in seen:
                cmd = self.get_command(
                    ctx, name
                )  # pragma: no cover — unreachable: all commands are in COMMAND_SECTIONS
                if cmd:  # pragma: no cover — unreachable: all commands are in COMMAND_SECTIONS
                    extra.append(
                        (name, cmd.get_short_help_str(limit=60))
                    )  # pragma: no cover — unreachable: all commands are in COMMAND_SECTIONS
        if (
            extra
        ):  # pragma: no cover — unreachable: all commands are in COMMAND_SECTIONS
            with formatter.section(
                "Other"
            ):  # pragma: no cover — unreachable: all commands are in COMMAND_SECTIONS
                formatter.write_dl(
                    extra
                )  # pragma: no cover — unreachable: all commands are in COMMAND_SECTIONS


def _validate_task_name(name: str) -> None:
    """Validate that a task name contains only safe characters."""
    if len(name) > 63:
        raise click.BadParameter(
            f"Task name must be at most 63 characters, got {len(name)}"
        )
    if not re.match(r"^[a-zA-Z0-9_-]+\Z", name):
        raise click.BadParameter(
            f"Task name must contain only letters, numbers, dashes, underscores. Got: '{name}'"
        )


def _fmt_ts(ts: str) -> str:
    """Extract HH:MM:SS from ISO timestamp, or return '?' if malformed."""
    try:
        return ts.split("T", 1)[1][:8] if "T" in ts else ts[:8]
    except (IndexError, TypeError, AttributeError):
        return "?"


_GIT_TIMEOUT = 30  # seconds for git subprocess calls
_TMUX_TIMEOUT = 10  # seconds for tmux kill/health operations
_MAX_AGE_SECONDS = 1000 * 365 * 86400  # ~1000 years upper bound


def _safe_join(base: str, name: str) -> str:
    """Join base directory and name, rejecting path traversal."""
    base_path = Path(base).resolve()
    joined = (base_path / name).resolve()
    if not str(joined).startswith(str(base_path) + os.sep) and joined != base_path:
        raise click.BadParameter(f"Path traversal detected: {name}")
    return str(joined)


def _run_git(
    args: list[str], cwd: str, *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run a git command with consistent error handling.

    Args:
        args: Git arguments (without 'git' prefix), e.g. ['rev-parse', 'HEAD']
        cwd: Working directory
        check: If True, exit on failure with error message

    Returns:
        CompletedProcess result
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_GIT_TIMEOUT,
        )
    except FileNotFoundError:
        raise DuoUserError(
            "git is not installed",
            fix="Install: brew install git (macOS) or apt install git (Linux)",
        ) from None
    except subprocess.TimeoutExpired:
        cmd_str = " ".join(["git", *args])
        raise DuoUserError(
            f"`{cmd_str}` timed out after {_GIT_TIMEOUT}s",
            fix="Try 'duo doctor' to check system state.",
        ) from None
    if check and result.returncode != 0:
        cmd_str = " ".join(["git", *args])
        stderr = result.stderr.strip()[:500]
        raise DuoUserError(
            f"`{cmd_str}` failed: {stderr}",
            fix="Check the repo path is valid and you're in a git repository. Run 'duo doctor' to verify.",
        )
    return result


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


def _create_worktree(name: str, repo: str) -> tuple[str, str]:
    """Create git worktree for task. Returns (worktree_path, base_commit)."""
    if not os.path.isdir(repo):
        raise DuoUserError(
            f"repo path '{repo}' does not exist or is not a directory",
            fix="Use --repo /path/to/git/repo or run from inside a git repo.",
        )
    if not os.path.exists(os.path.join(repo, ".git")):
        raise DuoUserError(
            f"'{repo}' is not a git repository (no .git found)",
            fix="Run 'git init' first, or use --repo to point to an existing repo.",
        )

    worktree_base = get_config("worktree_base_path")
    worktree = _safe_join(worktree_base, name)
    branch = f"duo/{name}"

    result = _run_git(["rev-parse", "HEAD"], cwd=repo)
    base_commit = result.stdout.strip()

    result = _run_git(["worktree", "add", worktree, "-b", branch], cwd=repo)

    return worktree, base_commit


@main.command()
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def version(*, as_json: bool = False) -> None:
    """Show Duo version."""
    from duo import __version__

    if as_json:
        click.echo(json.dumps({"version": __version__}))
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


@main.command()
@click.argument("name")
@click.option("--repo", default=".", help="Git repo path to create worktree from")
@click.option("--desc", default="", help="Task description")
@click.option("--model", default=None, help="Override copilot model for this task")
@click.option(
    "--queue", "start_queued", is_flag=True, help="Create task in queued state"
)
@click.option(
    "--from-thinking",
    "from_thinking",
    is_flag=True,
    help="Use plan.md from a thinking session as the task description",
)
@click.option(
    "--immediate",
    is_flag=True,
    help="Send bootstrap prompt immediately (default: defer until 'duo send')",
)
@click.option(
    "--reuse-pane",
    "reuse_pane",
    default="",
    help="Reuse existing tmux pane ID instead of creating new one",
)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def start(
    name: str,
    repo: str,
    desc: str,
    model: str | None,
    start_queued: bool,
    from_thinking: bool,
    immediate: bool,
    reuse_pane: str,
    *,
    as_json: bool = False,
) -> None:
    """Create a task with worktree + Copilot session."""
    from duo.commander import start_session

    _validate_task_name(name)

    # Check for duplicate task early (before expensive repo validation)
    existing = load_task(name)
    if existing is not None:
        raise DuoUserError(
            f"task '{name}' already exists (status: {existing.status.value}, worktree: {existing.worktree})",
            fix=f"Use 'duo kill {name}' first, or choose a different task name.",
        )

    repo = os.path.abspath(repo)

    if from_thinking:
        from duo.thinking import thinking_dir

        plan_path = thinking_dir(name) / "plan.md"
        if not plan_path.exists():
            raise DuoUserError(
                f"No plan.md found for thinking session '{name}'",
                fix=f"Run 'duo think {name} --finalize' first.",
            )
        plan_content = plan_path.read_text(encoding="utf-8").strip()
        if not plan_content:
            raise DuoUserError(
                f"plan.md for '{name}' is empty",
                fix=f"Run 'duo think {name} --finalize' again.",
            )
        desc = plan_content

    if model:
        os.environ["DUO_COPILOT_MODEL"] = model

    # Acquire lockfile to prevent concurrent duplicate creation (TOCTOU)
    lock_path = TASKS_DIR / f".{name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd: Any = None
    try:
        lock_fd = open(lock_path, "w", encoding="utf-8")  # noqa: SIM115
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        if lock_fd is not None:
            lock_fd.close()
        raise DuoUserError(
            f"task '{name}' is being created by another process",
            fix="Wait and retry, or run 'duo cleanup' if stuck.",
        ) from None

    try:
        # Re-check after acquiring lock
        existing = load_task(name)
        if existing is not None:
            raise DuoUserError(
                f"task '{name}' already exists (status: {existing.status.value}, worktree: {existing.worktree})",
                fix=f"Use 'duo kill {name}' first, or choose a different task name.",
            )

        worktree, base_commit = _create_worktree(name, repo)
        branch = f"duo/{name}"

        # Create task with a placeholder subtask (user will send actual tasks)
        task = create_task(
            task_id=name,
            description=desc or f"Task {name}",
            worktree=worktree,
            branch=branch,
            base_commit=base_commit,
            subtasks=[
                Subtask(
                    step_id=1,
                    description=desc or "Awaiting instructions",
                    target_files=[],
                    writable_paths=["*"],  # permissive by default
                )
            ],
        )
    finally:
        lock_fd.close()
        lock_path.unlink(missing_ok=True)

    if not as_json:
        click.echo(f"Created task: {name}")
        click.echo(f"  Worktree: {worktree}")
        click.echo(f"  Branch: {branch}")
        click.echo(f"  Incarnation: {task.incarnation_id}")
        if from_thinking:
            click.echo("  Plan: loaded from thinking session")

    if start_queued:
        from duo.protocol import transition

        if not transition(task, TaskStatus.QUEUED):
            if as_json:
                click.echo(
                    json.dumps(
                        {
                            "created": True,
                            "task": name,
                            "status": "error",
                            "error": "transition to QUEUED failed",
                        }
                    )
                )
            else:
                click.echo(
                    f"Warning: task '{name}' created but could not transition to QUEUED.",
                    err=True,
                )
            return
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "created": True,
                        "task": name,
                        "worktree": worktree,
                        "branch": branch,
                        "incarnation": task.incarnation_id,
                        "status": "queued",
                    }
                )
            )
        else:
            click.echo(f"Task '{name}' queued.")
        return

    # Check if we should queue or start
    from duo.scheduler import enqueue_or_start, queue_status

    action = enqueue_or_start(task)

    if action == "queued":
        qs = queue_status()
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "created": True,
                        "task": name,
                        "worktree": worktree,
                        "branch": branch,
                        "incarnation": task.incarnation_id,
                        "status": "queued",
                        "queue_position": qs["queued_count"],
                    }
                )
            )
        else:
            click.echo(
                f"  Queued ({qs['queued_count']} in queue). {qs['active_count']}/{qs['max_parallel']} slots in use."
            )
            click.echo("  Task will start automatically when a slot opens.")
            click.echo("  Run 'duo monitor' to manage the queue.")
        return

    # Start Copilot session
    defer = not immediate
    if not as_json:
        click.echo("Starting Copilot session...")
    start_session(task, defer=defer, reuse_pane=reuse_pane)
    if as_json:
        click.echo(
            json.dumps(
                {
                    "created": True,
                    "task": name,
                    "worktree": worktree,
                    "branch": branch,
                    "incarnation": task.incarnation_id,
                    "pane_label": task.pane_label,
                    "status": "deferred" if defer else "started",
                }
            )
        )
    else:
        if defer:
            click.echo(f"Session ready (deferred). Pane: {task.pane_label}")
            click.echo("  Copilot is idle — no PR consumed yet.")
            click.echo(f"  Send first prompt: duo send {name} 'your instruction'")
        else:
            click.echo(f"Session started. Pane label: {task.pane_label}")


@main.command()
@click.argument("name")
@click.argument("prompt")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def send(name: str, prompt: str, *, as_json: bool = False) -> None:
    """Send a prompt to a task's Copilot session."""
    from duo.commander import send_task_prompt

    if not prompt or not prompt.strip():
        raise click.UsageError(
            'prompt cannot be empty. Usage: duo send TASK_NAME "your instruction"'
        )
    task = _load_task_or_fail(name)

    # Reject sends to terminal/dead states — give clear guidance
    _TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ESCALATED}
    if task.status in _TERMINAL:
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
        if as_json:
            click.echo(json.dumps({"sent": True, "task": name, "first_prompt": True}))
        else:
            click.echo(f"First prompt sent to '{name}' (session was deferred).")
        return

    send_task_prompt(task, prompt)
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
@click.argument("name", required=False)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def status(name: str | None = None, *, as_json: bool = False) -> None:
    """Show task status."""
    if name:
        task = _load_task_or_fail(name)
        if as_json:
            output = {
                "id": task.id,
                "status": task.status.value,
                "step": task.current_step,
                "attempt": task.current_attempt,
                "worktree": task.worktree,
                "branch": task.branch,
                "created_at": task.created_at,
                "session_started_at": task.session_started_at,
                "description": task.description,
            }
            click.echo(json.dumps(output, indent=2))
            return
        _print_task(task)
    else:
        tasks = list_tasks()
        if not tasks:
            click.echo("No tasks.")
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
    click.echo(f"    Worktree:    {task.worktree}")
    if task.session_started_at:
        click.echo(f"    Session:     {task.session_started_at[:19]}")


@main.command("list")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option(
    "--status",
    "status_filter",
    default=None,
    help="Filter by task status (e.g. running, completed, created)",
)
def list_cmd(as_json: bool, status_filter: str | None) -> None:
    """List all tasks."""
    tasks = list_tasks()

    if status_filter:
        valid_statuses = {s.value for s in TaskStatus}
        if status_filter not in valid_statuses:
            raise DuoUserError(
                f"unknown status '{status_filter}'",
                fix=f"Valid statuses: {', '.join(sorted(valid_statuses))}",
            )
        tasks = [t for t in tasks if t.status.value == status_filter]

    if not tasks:
        click.echo("No tasks.")
        return

    if as_json:
        output = [
            {
                "id": t.id,
                "status": t.status.value,
                "step": t.current_step,
                "attempt": t.current_attempt,
                "worktree": t.worktree,
                "branch": t.branch,
                "created_at": t.created_at,
                "session_started_at": t.session_started_at,
            }
            for t in tasks
        ]
        click.echo(json.dumps(output, indent=2))
        return

    click.echo(f"{'ID':<20} {'STATUS':<18} {'STEP':<8} {'INCARNATION':<12}")
    click.echo("-" * 60)
    for t in tasks:
        step_str = f"{t.current_step}/{len(t.subtasks)}"
        click.echo(
            f"{t.id:<20} {t.status.value:<18} {step_str:<8} {t.incarnation_id:<12}"
        )


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
def recover(as_json: bool) -> None:
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
            if not as_json:
                click.echo(f"  {task.id}: {task.status.value} → {actual.value}")
            task.status = actual
            from duo.protocol import save_task

            save_task(task)
            recovered += 1

    if as_json:
        click.echo(json.dumps({"recovered": recovered, "changes": changes}, indent=2))
    else:
        click.echo(
            f"Recovered {recovered} tasks." if recovered else "All tasks consistent."
        )


@main.command()
@click.argument("name")
@click.option("--dry-run", is_flag=True, help="Preview merge without executing")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def merge(name: str, dry_run: bool, *, as_json: bool = False) -> None:
    """Merge a completed task's worktree to main."""
    task = _load_task_or_fail(name)

    if task.status != TaskStatus.COMPLETED:
        raise DuoUserError(
            f"task '{name}' is '{task.status.value}', not 'completed'",
            fix=f"Check progress with 'duo status {name}' or 'duo inspect {name}'.",
        )

    worktree = task.worktree

    if dry_run:
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "dry_run": True,
                        "branch": task.branch,
                        "target": "main",
                        "worktree": task.worktree,
                    }
                )
            )
        else:
            click.echo("Would merge:")
            click.echo(f"  Branch: {task.branch}")
            click.echo("  Into: main")
            click.echo(f"  Worktree: {task.worktree}")
            click.echo("\nRun without --dry-run to execute.")
        return

    if not os.path.exists(worktree):
        raise DuoUserError(
            f"worktree '{worktree}' does not exist",
            fix="Task may have been cleaned up. Run 'duo cleanup' to remove stale references.",
        )

    # Fetch and rebase
    if not as_json:
        click.echo("Fetching and rebasing...")
    r = _run_git(["fetch", "origin", "main"], cwd=worktree, check=False)
    if r.returncode != 0:
        if not as_json:
            click.echo("Warning: fetch failed, proceeding with local state", err=True)

    r = _run_git(["rebase", "origin/main"], cwd=worktree, check=False)
    if r.returncode != 0:
        abort = _run_git(["rebase", "--abort"], cwd=worktree, check=False)
        if abort.returncode != 0:
            if not as_json:
                click.echo(
                    f"Warning: could not abort rebase: {abort.stderr.strip()}",
                    err=True,
                )
        raise DuoUserError(
            f"Rebase conflict while merging '{name}'.\n{r.stderr}",
            fix=f"Resolve conflicts manually in '{worktree}', then run 'duo merge {name}' again.",
        )

    # Get parent repo from worktree
    main_worktree: str | None = None
    r = _run_git(["worktree", "list", "--porcelain"], cwd=worktree, check=False)
    for line in r.stdout.split("\n"):
        if (
            line.startswith("worktree ")
            and get_config("worktree_base_path") not in line
        ):
            parts = line.split(" ", 1)
            main_worktree = parts[1] if len(parts) > 1 else parts[0]
            break

    if main_worktree is None:
        raise DuoUserError(
            "cannot find main worktree",
            fix="Ensure the worktree was created from a valid git repository. Run 'duo doctor' to check system state.",
        )

    # ff-only merge
    if not as_json:
        click.echo(f"Merging {task.branch} into main...")
    r = _run_git(["merge", task.branch, "--ff-only"], cwd=main_worktree, check=False)
    if r.returncode != 0:
        raise DuoUserError(
            f"Merge failed: {r.stderr}",
            fix=f"Try a manual merge: cd {main_worktree} && git merge {task.branch}",
        )

    # Cleanup
    if not as_json:
        click.echo("Cleaning up worktree and branch...")
    r = _run_git(["worktree", "remove", worktree], cwd=main_worktree, check=False)
    wt_removed = r.returncode == 0
    if not wt_removed and not as_json:
        click.echo(f"  Warning: worktree removal failed: {r.stderr.strip()}", err=True)
    r = _run_git(["branch", "-d", task.branch], cwd=main_worktree, check=False)
    branch_deleted = r.returncode == 0
    if not branch_deleted and not as_json:
        click.echo(f"  Warning: branch deletion failed: {r.stderr.strip()}", err=True)

    from duo.protocol import append_event

    append_event(task, "task_merged", {"branch": task.branch})
    if as_json:
        click.echo(
            json.dumps(
                {
                    "merged": True,
                    "branch": task.branch,
                    "worktree_removed": wt_removed,
                    "branch_deleted": branch_deleted,
                }
            )
        )
    else:
        click.echo(f"Merged {name}. Remember to `git push` when ready.")


@main.command()
@click.argument("name")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def stop(name: str, *, as_json: bool = False) -> None:
    """Stop a task gracefully (preserves worktree for resume)."""
    from duo.protocol import append_event, transition

    task = _load_task_or_fail(name)

    terminal_states = {TaskStatus.COMPLETED, TaskStatus.FAILED}
    if task.status in terminal_states:
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "stopped": False,
                        "reason": "already_terminal",
                        "status": task.status.value,
                    }
                )
            )
            return
        click.echo(f"Task '{name}' is already in terminal state '{task.status.value}'.")
        return

    if task.status == TaskStatus.BLOCKED:
        if as_json:
            click.echo(
                json.dumps(
                    {"stopped": False, "reason": "already_stopped", "status": "BLOCKED"}
                )
            )
            return
        click.echo(f"Task '{name}' is already stopped.")
        return

    # Kill the pane but preserve worktree and branch
    from duo.transport import cleanup_pane_state, kill_pane

    pane_killed = kill_pane(task.pane_label)
    if not pane_killed:
        if not as_json:
            click.echo("Warning: failed to kill pane", err=True)

    cleanup_pane_state(task.pane_label)

    previous = task.status.value
    stopped = transition(task, TaskStatus.BLOCKED)
    if stopped:
        append_event(task, "task_stopped", {"previous_status": previous})
    else:
        if not as_json:
            click.echo(
                f"Warning: pane killed but could not transition from {previous} to BLOCKED",
                err=True,
            )
    if as_json:
        click.echo(
            json.dumps(
                {
                    "stopped": stopped,
                    "previous_status": previous,
                    "worktree": task.worktree,
                    "pane_killed": pane_killed,
                }
            )
        )
    else:
        if stopped:
            click.echo(f"Stopped '{name}'. Worktree preserved at {task.worktree}")
            click.echo(f"  Resume with: duo resume {name}")


@main.command()
@click.argument("name")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def kill(name: str, *, as_json: bool = False) -> None:
    """Kill a task and clean up."""
    task = _load_task_or_fail(name)

    # Try to kill the pane
    from duo.transport import cleanup_pane_state, kill_pane

    pane_killed = kill_pane(task.pane_label)
    if not pane_killed and not as_json:
        click.echo("Warning: failed to kill pane", err=True)

    cleanup_pane_state(task.pane_label)

    # Find parent repo
    main_worktree = None
    r = _run_git(
        ["worktree", "list", "--porcelain"],
        cwd=task.worktree if os.path.exists(task.worktree) else ".",
        check=False,
    )
    for line in r.stdout.split("\n"):
        if (
            line.startswith("worktree ")
            and get_config("worktree_base_path") not in line
        ):
            parts = line.split(" ", 1)
            main_worktree = parts[1] if len(parts) > 1 else parts[0]
            break
    repo_cwd = main_worktree or "."

    # Remove worktree
    wt_removed = False
    if os.path.exists(task.worktree):
        r = _run_git(
            ["worktree", "remove", "--force", task.worktree], cwd=repo_cwd, check=False
        )
        wt_removed = r.returncode == 0
        if not wt_removed and not as_json:
            click.echo(
                f"  Warning: worktree removal failed: {r.stderr.strip()}", err=True
            )

    # Remove branch
    r = _run_git(["branch", "-D", task.branch], cwd=repo_cwd, check=False)
    branch_deleted = r.returncode == 0
    if not branch_deleted and not as_json:
        click.echo(f"  Warning: branch deletion failed: {r.stderr.strip()}", err=True)

    from duo.protocol import append_event, save_task, transition

    append_event(task, "task_killed", {})
    # Use transition() when valid (emits status_changed event for journal replay).
    # For states without a FAILED transition (CREATED, COMPLETED, already FAILED),
    # fall back to direct save.
    if not transition(task, TaskStatus.FAILED):
        task.status = TaskStatus.FAILED
        save_task(task)
    if as_json:
        click.echo(
            json.dumps(
                {
                    "killed": True,
                    "pane_killed": pane_killed,
                    "worktree_removed": wt_removed,
                    "branch_deleted": branch_deleted,
                }
            )
        )
    else:
        click.echo(f"Killed {name}.")


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

    if not tasks_data.get("tasks"):
        raise click.UsageError("No tasks defined in file.")

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
@click.pass_context
def batch(
    ctx: click.Context,
    file: str,
    repo: str,
    dry_run: bool,
    start_queued: bool,
    *,
    as_json: bool = False,
) -> None:
    """Create multiple tasks from a file (JSON or YAML)."""
    from duo.scheduler import queue_status

    repo = os.path.abspath(repo)

    task_defs = _load_batch_file(file)

    if dry_run:
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
def queue(as_json: bool) -> None:
    """Show queue status."""
    from duo.scheduler import queue_status

    qs = queue_status()

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
@click.argument("name", required=False)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def audit(name: str | None = None, *, as_json: bool = False) -> None:
    """Show Premium Request consumption audit."""
    from duo.protocol import read_jsonl
    from duo.transport import get_pr_log

    if name:
        # Single task audit
        task = _load_task_or_fail(name)
        events = read_jsonl(task.journal_path)
        pr_events = [ev for ev in events if ev.get("event") == "pr_consumed"]

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
def cost(
    task_name: str | None,
    since_days: int | None,
    *,
    as_json: bool = False,
    budget: int | None = None,
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
        if as_json:
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

    if as_json:
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


@main.command()
@click.argument("name")
@click.option(
    "-n", "--lines", default=20, type=click.IntRange(1), help="Number of recent events"
)
@click.option("--all", "show_all", is_flag=True, help="Show all events")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option(
    "--filter", "event_filter", default=None, help="Filter by event type substring"
)
@click.pass_context
def logs(
    ctx: click.Context,
    name: str,
    lines: int,
    show_all: bool,
    as_json: bool,
    event_filter: str | None,
) -> None:
    """Show task journal events."""
    from duo.protocol import read_jsonl

    task = _load_task_or_fail(name)

    events = read_jsonl(task.journal_path)
    if not events:
        click.echo("No events recorded.")
        return

    if event_filter:
        events = [ev for ev in events if event_filter in ev.get("event", "")]

    if not show_all:
        events = events[-lines:]

    if as_json:
        click.echo(json.dumps(events, indent=2))
        return

    for ev in events:
        ts = _fmt_ts(ev.get("ts", "?"))
        event_type = ev.get("event", "?")
        data = ev.get("data", {})

        # Color-code by event type
        if "error" in event_type or "failed" in event_type or "violation" in event_type:
            symbol = "✗"
        elif "completed" in event_type or "passed" in event_type:
            symbol = "✓"
        elif "warning" in event_type:
            symbol = "⚠"
        else:
            symbol = "·"

        # Format data compactly
        data_str = ""
        if data:
            parts = []
            for k, v in data.items():
                if isinstance(v, list) and len(str(v)) > 40:
                    parts.append(f"{k}=[{len(v)} items]")
                elif isinstance(v, str) and len(v) > 50:
                    parts.append(f"{k}={v[:47]}...")
                else:
                    parts.append(f"{k}={v}")
            data_str = " " + " ".join(parts)

        click.echo(f"  {ts} {symbol} {event_type}{data_str}")


def _inspect_gather_worktree_files(worktree: str) -> dict[str, Any]:
    """Gather changed files, untracked files, and diff preview from a worktree."""
    r = _run_git(["diff", "--name-only", "HEAD"], cwd=worktree, check=False)
    changed = (
        [f for f in r.stdout.strip().splitlines() if f] if r.returncode == 0 else []
    )
    r2 = _run_git(
        ["ls-files", "--others", "--exclude-standard"],
        cwd=worktree,
        check=False,
    )
    untracked = (
        [f for f in r2.stdout.strip().splitlines() if f] if r2.returncode == 0 else []
    )
    r3 = _run_git(["diff", "HEAD"], cwd=worktree, check=False)
    diff_preview = r3.stdout[:500] if r3.returncode == 0 else ""
    if len(r3.stdout) > 500:
        diff_preview += "\n... (truncated)"
    return {
        "changed": changed,
        "untracked": untracked,
        "diff_preview": diff_preview,
    }


def _inspect_format_heartbeat(hb: Heartbeat) -> str:
    """Format a heartbeat for text display."""
    lines = [
        "Heartbeat:",
        f"  Timestamp:     {hb.ts}",
        f"  Status:        {hb.status}",
        f"  Current file:  {hb.current_file}",
        f"  Incarnation:   {hb.incarnation}",
    ]
    return "\n".join(lines)


def _inspect_format_ack_result(task: Task) -> str:
    """Read and format the current step's ack and result for text display."""
    from duo.protocol import read_ack_for_step, read_result_for_step

    lines: list[str] = []
    ack = read_ack_for_step(task, task.current_step, task.current_attempt)
    if ack:
        lines.append("")
        lines.append("Ack:")
        lines.append(f"  Acked at:      {ack.acked_at}")
        lines.append(f"  Prompt hash:   {ack.prompt_hash}")

    result = read_result_for_step(task, task.current_step, task.current_attempt)
    if result:
        lines.append("")
        lines.append("Result:")
        lines.append(f"  Status:        {result.status}")
        lines.append(f"  Summary:       {result.summary}")
        if result.files_changed:
            lines.append(f"  Files changed: {', '.join(result.files_changed)}")

    return "\n".join(lines)


def _inspect_build_json(task: Task, include_files: bool) -> dict[str, Any]:
    """Build the full JSON output dict for the inspect command."""
    from duo.protocol import (
        read_ack_for_step,
        read_heartbeat,
        read_result_for_step,
    )

    data: dict[str, Any] = {
        "id": task.id,
        "description": task.description,
        "status": task.status.value,
        "step": task.current_step,
        "attempt": task.current_attempt,
        "worktree": task.worktree,
        "branch": task.branch,
        "base_commit": task.base_commit,
        "created_at": task.created_at,
        "subtasks": [
            {
                "step_id": s.step_id,
                "description": s.description,
                "target_files": s.target_files,
                "writable_paths": s.writable_paths,
            }
            for s in task.subtasks
        ],
    }
    hb = read_heartbeat(task)
    if hb:
        data["heartbeat"] = {
            "ts": hb.ts,
            "status": hb.status,
            "current_file": hb.current_file,
            "incarnation": hb.incarnation,
        }
    ack = read_ack_for_step(task, task.current_step, task.current_attempt)
    if ack:
        data["ack"] = {
            "acked_at": ack.acked_at,
            "prompt_hash": ack.prompt_hash,
        }
    result = read_result_for_step(task, task.current_step, task.current_attempt)
    if result:
        data["result"] = {
            "status": result.status,
            "summary": result.summary,
            "files_changed": result.files_changed,
        }
    if include_files:
        worktree = task.worktree
        if os.path.isdir(worktree):
            files = _inspect_gather_worktree_files(worktree)
            data["changed_files"] = files["changed"]
            data["untracked_files"] = files["untracked"]
            data["diff_preview"] = files["diff_preview"]
        else:
            data["files_error"] = f"Worktree not found: {worktree}"
    return data


@main.command()
@click.argument("name")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option(
    "--include-files",
    is_flag=True,
    help="Show changed files and diff preview from worktree",
)
def inspect(name: str, as_json: bool, include_files: bool) -> None:
    """Show detailed task information."""
    from duo.protocol import (
        read_heartbeat,
        read_jsonl,
    )

    task = _load_task_or_fail(name)

    if as_json:
        data = _inspect_build_json(task, include_files)
        click.echo(json.dumps(data, indent=2))
        return

    # Task info
    click.echo(f"Task: {task.id}")
    click.echo(f"  Description:   {task.description}")
    click.echo(f"  Status:        {task.status.value}")
    click.echo(f"  Incarnation:   {task.incarnation_id}")
    click.echo(f"  Step:          {task.current_step}/{len(task.subtasks)}")
    click.echo(f"  Attempt:       {task.current_attempt}")
    click.echo(f"  Worktree:      {task.worktree}")
    click.echo(f"  Branch:        {task.branch}")
    click.echo(f"  Created:       {task.created_at}")
    if task.last_prompt_sent_at:
        click.echo(f"  Last prompt:   {task.last_prompt_sent_at}")

    # Current subtask
    if task.current_step <= len(task.subtasks):
        st = task.subtasks[task.current_step - 1]
        click.echo(f"\nCurrent Step ({task.current_step}):")
        click.echo(f"  Description:   {st.description}")
        click.echo(f"  Target files:  {', '.join(st.target_files) or '(none)'}")
        click.echo(f"  Writable:      {', '.join(st.writable_paths)}")

    # Heartbeat
    hb = read_heartbeat(task)
    if hb:
        click.echo(f"\n{_inspect_format_heartbeat(hb)}")
    else:
        click.echo("\nHeartbeat:       (none)")

    # Ack/result
    ack_result_text = _inspect_format_ack_result(task)
    if ack_result_text:
        click.echo(ack_result_text)

    # Recent events
    events = read_jsonl(task.journal_path)
    pr_count = sum(1 for ev in events if ev.get("event") == "pr_consumed")
    click.echo(f"\nPR Consumed:     {pr_count}")

    if events:
        recent = events[-5:]
        click.echo(f"\nRecent Events ({len(events)} total):")
        for ev in recent:
            ts = _fmt_ts(ev.get("ts", "?"))
            click.echo(f"  {ts} {ev.get('event', '?')}")

    if include_files:
        worktree = task.worktree
        if os.path.isdir(worktree):
            files = _inspect_gather_worktree_files(worktree)
            if files["changed"]:
                click.echo(f"\nChanged files ({len(files['changed'])}):")
                for f in files["changed"][:20]:
                    click.echo(f"  M {f}")
            if files["untracked"]:
                click.echo(f"\nUntracked files ({len(files['untracked'])}):")
                for f in files["untracked"][:20]:
                    click.echo(f"  ? {f}")
            if files["diff_preview"]:
                click.echo("\nDiff preview:")
                click.echo(files["diff_preview"])
        else:
            click.echo(f"\n⚠ Worktree not found: {worktree}")


@main.command()
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def stats(as_json: bool) -> None:
    """Show task statistics summary."""
    from collections import Counter

    tasks = list_tasks()
    counts = Counter(t.status.value for t in tasks)
    total = len(tasks)

    if as_json:
        output: dict[str, Any] = {"total": total, "by_status": dict(counts)}
        click.echo(json.dumps(output, indent=2))
        return

    click.echo(f"Tasks: {total}")
    if total == 0:
        return

    for status_val in [
        "running",
        "queued",
        "session_starting",
        "blocked",
        "prompt_sent",
        "acked",
        "result_reported",
        "verifying",
        "correcting",
        "completed",
        "failed",
        "escalated",
        "created",
    ]:
        count = counts.get(status_val, 0)
        if count > 0:
            click.echo(f"  {status_val}: {count}")


@main.command()
@click.option("--repo", default=".", help="Git repository path to initialize")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def init(repo: str, *, as_json: bool = False) -> None:
    """Initialize a project for Duo (creates .duo config and instructions)."""
    from duo.config import load_config, save_config

    repo_path = Path(repo).resolve()
    project_duo = repo_path / ".duo"

    # Already initialized?
    if project_duo.exists():
        if as_json:
            click.echo(json.dumps({"status": "already_initialized", "created": []}))
        else:
            click.echo("Already initialized.")
        return

    # Must be a git repo
    if not (repo_path / ".git").exists():
        raise DuoUserError("not a git repository", fix="Run 'git init' first.")

    created: list[str] = []

    # 1. ~/.duo/
    DUO_DIR.mkdir(parents=True, exist_ok=True)
    created.append(str(DUO_DIR))

    # 2. ~/.duo/config.json (only if missing)
    config_path = DUO_DIR / "config.json"
    if not config_path.exists():
        save_config(load_config())
        created.append(str(config_path))

    # 3. ~/.duo/tasks/
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    created.append(str(TASKS_DIR))

    # 3b. Worktree base directory (persistent, not /tmp)
    from duo.config import get_config as _gc

    worktree_base = Path(_gc("worktree_base_path"))
    worktree_base.mkdir(parents=True, exist_ok=True)
    created.append(str(worktree_base))

    # 4. .duo/ inside repo
    project_duo.mkdir(parents=True, exist_ok=True)
    created.append(str(project_duo))

    # 5. .duo/instructions.md
    instructions = project_duo / "instructions.md"
    atomic_write_text(
        instructions,
        "# Duo Project Instructions\n"
        "\n"
        "## Project Overview\n"
        "<!-- Describe your project here -->\n"
        "\n"
        "## Coding Conventions\n"
        "<!-- List your coding standards -->\n"
        "\n"
        "## Testing\n"
        "<!-- How to run tests -->\n"
        "\n"
        "## Important Notes\n"
        "<!-- Anything the executor should know -->\n",
    )
    created.append(str(instructions))

    # 6. Add .duo/ to .gitignore
    gitignore = repo_path / ".gitignore"
    needs_entry = True
    content = ""
    if gitignore.exists():
        content = gitignore.read_text()
        for line in content.splitlines():
            stripped = line.strip()
            if stripped in (".duo/", ".duo"):
                needs_entry = False
                break
    if needs_entry:
        with open(gitignore, "a", encoding="utf-8") as f:
            if content and not content.endswith("\n"):
                f.write("\n")
            f.write(".duo/\n")
            f.flush()
            os.fsync(f.fileno())
        created.append(str(gitignore) + " (updated)")

    if as_json:
        click.echo(json.dumps({"status": "initialized", "created": created}))
    else:
        click.echo("Initialized Duo project:")
        for item in created:
            click.echo(f"  ✓ {item}")


@main.command()
@click.option("--repo", default=".", help="Repository path (default: current dir)")
def go(repo: str) -> None:
    """One-command setup: CEO (Claude Code) + Executor (Copilot) side by side.

    Sets up everything needed to start working with Duo:
    1. Checks tmux is running
    2. Initializes git + duo if needed
    3. Writes project CLAUDE.md with CEO operating manual
    4. Splits a Copilot standby pane (0 PR cost)
    5. Execs Claude Code in the current pane

    After `duo go`, just chat with Claude Code about what you want to build.
    """
    import shlex

    from duo.commander import write_project_claude_md
    from duo.config import get_config
    from duo.protocol import (
        load_go_session,
        save_go_session,
    )
    from duo.transport import (
        is_at_main_prompt,
        name_pane,
        read_pane,
        send_shell_command,
        wait_for_idle,
    )

    repo_path = Path(repo).resolve()

    # 1. Check tmux
    if not os.environ.get("TMUX"):
        raise DuoUserError(
            "not inside a tmux session",
            fix="Start tmux first: tmux new -s work",
        )

    # 2. Check/init git
    if not (repo_path / ".git").exists():
        click.echo("No git repo found. Initializing...")
        result = subprocess.run(
            ["git", "init", str(repo_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise DuoUserError(
                f"git init failed: {result.stderr.strip()}",
                fix="Initialize a git repository manually.",
            )
        click.echo(f"  ✓ git init {repo_path}")

    # 3. duo init (idempotent)
    duo_project = repo_path / ".duo"
    if not duo_project.exists():
        from click.testing import CliRunner as _InternalRunner

        _InternalRunner().invoke(main, ["init", "--repo", str(repo_path)])
        click.echo("  ✓ duo init")
    else:
        # Ensure global directories exist even if .duo/ already exists
        DUO_DIR.mkdir(parents=True, exist_ok=True)
        TASKS_DIR.mkdir(parents=True, exist_ok=True)

    # 4. Write project CLAUDE.md
    write_project_claude_md(str(repo_path))
    click.echo("  ✓ project CLAUDE.md")

    # 5. Split pane for standby Copilot (or reuse existing)
    standby_label = "duo-copilot-standby"
    pane_id = ""

    # Check for existing go-session
    existing = load_go_session()
    if existing and existing.get("copilot_pane"):
        # Verify pane is alive
        try:
            check = subprocess.run(
                ["tmux", "display-message", "-t", existing["copilot_pane"], "-p", ""],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if check.returncode == 0:
                pane_id = existing["copilot_pane"]
                click.echo(f"  ✓ reusing standby pane {pane_id}")
        except (subprocess.TimeoutExpired, OSError):
            pass

    if not pane_id:
        # Create new pane
        try:
            split = subprocess.run(
                ["tmux", "split-window", "-h", "-P", "-F", "#{pane_id}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            raise DuoUserError(
                "tmux split-window timed out",
                fix="Check tmux is responding: tmux list-panes",
            ) from None
        if split.returncode != 0:
            raise DuoUserError(
                f"tmux split-window failed: {split.stderr.strip()}",
                fix="Check tmux session is healthy.",
            )
        pane_id = split.stdout.strip()

        # Name the pane
        try:
            name_pane(pane_id, standby_label)
        except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
            # Kill orphaned pane on failure
            subprocess.run(
                ["tmux", "kill-pane", "-t", pane_id],
                capture_output=True,
                timeout=5,
            )
            raise DuoUserError(
                f"failed to name standby pane: {exc}",
                fix="Retry duo go, or check tmux panes.",
            ) from None

        # Start Copilot in the standby pane
        copilot_model = get_config("copilot_model")
        copilot_cmd = f"copilot --model {shlex.quote(str(copilot_model))}"
        if get_config("bypass_permissions"):
            copilot_cmd += " --yolo"

        time.sleep(0.3)
        send_shell_command(standby_label, f"cd {shlex.quote(str(repo_path))}")
        time.sleep(0.3)
        send_shell_command(standby_label, copilot_cmd)

        click.echo("  Waiting for Copilot to start...")
        if wait_for_idle(standby_label, timeout=45, poll_interval=2.0):
            pane_content = read_pane(standby_label)
            if is_at_main_prompt(pane_content):
                # Send /allow-all
                if get_config("auto_allow_all"):
                    send_shell_command(standby_label, "/allow-all")
                    wait_for_idle(standby_label, timeout=15, poll_interval=1.0)
                click.echo(f"  ✓ Copilot standby ready ({pane_id})")
            else:
                click.echo("  ⚠ Copilot pane not at prompt (continuing anyway)")
        else:
            click.echo(
                "  ⚠ Copilot startup slow (continuing — it may still be loading)"
            )

    # 6. Save go-session state
    save_go_session(
        pane_label=standby_label,
        repo_root=str(repo_path),
        copilot_pane=pane_id,
    )
    click.echo("  ✓ go-session saved")

    # 7. Exec Claude Code (replaces this process)
    click.echo("\nLaunching Claude Code as CEO...")
    click.echo("Just tell Claude what you want to build. It knows how to use Duo.\n")

    claude_args = ["claude"]
    if get_config("bypass_permissions"):
        claude_args.append("--dangerously-skip-permissions")

    # Change to repo directory before exec
    os.chdir(repo_path)

    # os.execvp replaces this process — no return
    os.execvp("claude", claude_args)


@dataclass(slots=True)
class CheckResult:
    """Result of a single doctor diagnostic check."""

    name: str
    status: str  # "pass", "warn", "fail"
    message: str
    fix: str  # suggested fix action


def _doctor_check_python() -> CheckResult:
    """Check Python version >= 3.12."""
    vi = sys.version_info
    ver = f"{vi.major}.{vi.minor}.{vi.micro}"
    if vi >= (3, 12):
        return CheckResult("Python", "pass", ver, "")
    return CheckResult("Python", "fail", ver, "Upgrade to Python >= 3.12")


def _doctor_check_tmux() -> CheckResult:
    """Check tmux installed and version >= 3.0."""
    if shutil.which("tmux") is None:
        return CheckResult(
            "tmux",
            "fail",
            "not found",
            "Install with: brew install tmux (macOS) or apt install tmux (Linux)",
        )
    try:
        proc = subprocess.run(
            ["tmux", "-V"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_TMUX_TIMEOUT,
        )
        raw = proc.stdout.strip()
        match = re.search(r"(\d+(?:\.\d+)?)", raw)
        if match:
            ver_str = match.group(1)
            parts = ver_str.split(".")
            major = int(parts[0])
            minor = int(parts[1]) if len(parts) > 1 else 0
            if (major, minor) >= (3, 0):
                return CheckResult("tmux", "pass", f"{ver_str} (>= 3.0)", "")
            return CheckResult(
                "tmux",
                "warn",
                f"{ver_str} (< 3.0)",
                "Upgrade tmux to >= 3.0",
            )
        return CheckResult("tmux", "pass", "installed", "")
    except (subprocess.TimeoutExpired, OSError):
        return CheckResult("tmux", "pass", "installed", "")


def _doctor_check_tmux_bridge() -> CheckResult:
    """Check tmux-bridge binary exists and is executable."""
    path_loc = shutil.which("tmux-bridge")
    if path_loc is not None:
        return CheckResult("tmux-bridge", "pass", f"found at {path_loc}", "")
    smux_path = Path.home() / ".smux" / "bin" / "tmux-bridge"
    if smux_path.exists() and os.access(str(smux_path), os.X_OK):
        return CheckResult(
            "tmux-bridge",
            "pass",
            f"found at {smux_path}",
            "",
        )
    if smux_path.exists():
        return CheckResult(
            "tmux-bridge",
            "fail",
            f"found at {smux_path} but not executable",
            f"Run: chmod +x {smux_path}",
        )
    return CheckResult(
        "tmux-bridge",
        "fail",
        "not found",
        "Install from: https://github.com/anthropic-ai/tmux-bridge",
    )


def _doctor_check_claude_cli() -> CheckResult:
    """Check claude CLI available."""
    if shutil.which("claude") is not None:
        return CheckResult("claude CLI", "pass", "installed", "")
    return CheckResult(
        "claude CLI",
        "warn",
        "not found",
        "Install for 'duo think': npm i -g @anthropic-ai/claude-cli",
    )


def _doctor_check_copilot_cli() -> CheckResult:
    """Check Copilot CLI available."""
    if (
        shutil.which("github-copilot-cli") is not None
        or shutil.which("copilot") is not None
    ):
        return CheckResult("Copilot CLI", "pass", "installed", "")
    return CheckResult(
        "Copilot CLI",
        "warn",
        "not found",
        "Install from: https://github.com/github/copilot-cli",
    )


def _doctor_check_duo_dir() -> CheckResult:
    """Check ~/.duo directory writable and disk space >= 100MB."""
    if not DUO_DIR.exists():
        return CheckResult(
            "~/.duo",
            "fail",
            "missing",
            "Run: duo init",
        )
    if not os.access(str(DUO_DIR), os.W_OK):
        return CheckResult(
            "~/.duo",
            "fail",
            "not writable",
            f"Run: chmod u+w {DUO_DIR}",
        )
    try:
        usage = shutil.disk_usage(str(DUO_DIR))
        free_mb = usage.free / (1024 * 1024)
        if free_mb < 100:
            return CheckResult(
                "~/.duo",
                "warn",
                f"writable ({free_mb:.0f} MB free)",
                "Free up disk space (< 100 MB remaining)",
            )
        free_gb = free_mb / 1024
        return CheckResult(
            "~/.duo",
            "pass",
            f"writable ({free_gb:.1f} GB free)",
            "",
        )
    except OSError:
        return CheckResult("~/.duo", "pass", "writable", "")


def _doctor_check_config() -> CheckResult:
    """Check config.json exists and is valid JSON."""
    config_path = DUO_DIR / "config.json"
    if not config_path.exists():
        return CheckResult(
            "config.json",
            "warn",
            "missing",
            "Run: duo init",
        )
    try:
        json.loads(config_path.read_text())
        return CheckResult("config.json", "pass", "valid", "")
    except (json.JSONDecodeError, OSError):
        return CheckResult(
            "config.json",
            "warn",
            "invalid JSON",
            "Run: duo config reset",
        )


def _doctor_check_tmux_session() -> CheckResult:
    """Check for active tmux session."""
    if shutil.which("tmux") is None:
        return CheckResult(
            "tmux session",
            "warn",
            "tmux not installed",
            "Install tmux first",
        )
    try:
        proc = subprocess.run(
            ["tmux", "list-sessions"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_TMUX_TIMEOUT,
        )
        if proc.returncode == 0:
            return CheckResult("tmux session", "pass", "active", "")
        return CheckResult(
            "tmux session",
            "warn",
            "no active session",
            "Start: tmux new -s duo",
        )
    except (subprocess.TimeoutExpired, OSError):
        return CheckResult(
            "tmux session",
            "warn",
            "could not query tmux",
            "Check tmux installation",
        )


def _doctor_check_task_timeout() -> CheckResult:
    """Check task_timeout config value."""
    timeout_val = get_config("task_timeout")
    if isinstance(timeout_val, int) and timeout_val >= 0:
        label = f"{timeout_val}s" if timeout_val > 0 else "disabled"
        return CheckResult("task_timeout", "pass", f"configured ({label})", "")
    return CheckResult(
        "task_timeout",
        "warn",
        "invalid value",
        "Set to 0 or positive integer in config.json",
    )


def _doctor_check_corrupted() -> CheckResult:
    """Count corrupted tasks in ~/.duo/corrupted/."""
    from duo.protocol import list_corrupted

    items = list_corrupted()
    count = len(items)
    if count == 0:
        return CheckResult("corrupted tasks", "pass", "0", "")
    return CheckResult(
        "corrupted tasks",
        "warn",
        str(count),
        "Run: duo cleanup --corrupted",
    )


def _doctor_check_git() -> CheckResult:
    """Check git available."""
    if shutil.which("git") is None:
        return CheckResult(
            "git",
            "warn",
            "not found",
            "Install git: https://git-scm.com/downloads",
        )
    try:
        proc = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_TMUX_TIMEOUT,
        )
        raw = proc.stdout.strip()
        match = re.search(r"(\d+\.\d+[\.\d]*)", raw)
        ver = match.group(1) if match else "installed"
        return CheckResult("git", "pass", ver, "")
    except (subprocess.TimeoutExpired, OSError):
        return CheckResult("git", "pass", "installed", "")


_COPILOT_FD_WARN = 500
_COPILOT_FD_CRITICAL = 2000
_COPILOT_CHILD_WARN = 10
_COPILOT_KQUEUE_WARN = 50


def _get_pid_fd_count(pid: int) -> int:
    """Return total open fd count for *pid* using lsof."""
    try:
        proc = subprocess.run(
            ["lsof", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            return -1
        # lsof header is 1 line; each subsequent line = 1 fd entry
        return max(0, len(proc.stdout.strip().splitlines()) - 1)
    except (OSError, subprocess.TimeoutExpired):
        return -1


def _get_pid_kqueue_count(pid: int) -> int:
    """Return kqueue fd count for *pid* using lsof (macOS only)."""
    try:
        proc = subprocess.run(
            ["lsof", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            return -1
        return sum(1 for line in proc.stdout.splitlines() if "KQUEUE" in line)
    except (OSError, subprocess.TimeoutExpired):
        return -1


def _get_pid_child_count(pid: int) -> int:
    """Return count of child processes for *pid*."""
    try:
        proc = subprocess.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            return 0
        lines = [l for l in proc.stdout.strip().splitlines() if l.strip()]
        return len(lines)
    except (OSError, subprocess.TimeoutExpired):
        return -1


def _doctor_check_copilot_health() -> list[CheckResult]:
    """Check Copilot pane process health (fd/kqueue/child counts).

    Returns a list of CheckResult — one per active Copilot pane.
    An empty list is returned when there are no active panes.
    """
    from duo.transport import get_pane_pid

    terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ESCALATED}
    try:
        tasks = list_tasks()
    except (FileNotFoundError, OSError, ValueError):
        return []

    active = [t for t in tasks if t.status not in terminal and t.pane_label]
    if not active:
        return []

    results: list[CheckResult] = []
    for task in active:
        pid = get_pane_pid(task.pane_label)
        if pid is None:
            results.append(
                CheckResult(
                    f"pane:{task.pane_label}",
                    "warn",
                    "PID unavailable",
                    "Pane may be dead — run: duo status",
                )
            )
            continue

        fd_count = _get_pid_fd_count(pid)
        kqueue_count = _get_pid_kqueue_count(pid)
        child_count = _get_pid_child_count(pid)

        parts: list[str] = []
        worst = "pass"

        if fd_count >= 0:
            parts.append(f"fds={fd_count}")
            if fd_count >= _COPILOT_FD_CRITICAL:
                worst = "fail"
            elif fd_count >= _COPILOT_FD_WARN:
                worst = "warn" if worst != "fail" else worst
        if kqueue_count >= 0:
            parts.append(f"kqueue={kqueue_count}")
            if kqueue_count >= _COPILOT_KQUEUE_WARN:
                worst = "warn" if worst != "fail" else worst
        if child_count >= 0:
            parts.append(f"children={child_count}")
            if child_count >= _COPILOT_CHILD_WARN:
                worst = "warn" if worst != "fail" else worst

        msg = f"PID {pid}: {', '.join(parts)}" if parts else f"PID {pid}: healthy"
        fix = ""
        if worst == "fail":
            fix = "Critical — restart session: duo stop + duo start"
            _emit_restart_signal(task.id)
        elif worst == "warn":
            fix = "Run: duo ceo-cleanup"

        results.append(CheckResult(f"pane:{task.pane_label}", worst, msg, fix))

    return results


def _doctor_check_capi_error() -> list[CheckResult]:
    """Check journals for recent CAPIError events (backend context limit).

    Reads each active task's journal for ``capi_error`` events.  Unlike pane
    content reading, journal checks are race-free and replay-safe.
    """
    from duo.protocol import read_jsonl

    terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ESCALATED}
    try:
        tasks = list_tasks()
    except (FileNotFoundError, OSError, ValueError):
        return []

    active = [t for t in tasks if t.status not in terminal]
    if not active:
        return []

    results: list[CheckResult] = []
    for task in active:
        if not task.journal_path.exists():
            continue
        events = read_jsonl(task.journal_path)
        capi_events = [e for e in events if e.get("event") == "capi_error"]
        if capi_events:
            last = capi_events[-1]
            ts = last.get("ts", "unknown")[:19]
            results.append(
                CheckResult(
                    f"capi:{task.id}",
                    "fail",
                    f"CAPIError detected at {ts}",
                    "Session context limit exhausted — restart: duo stop + duo start",
                )
            )

    return results


def _emit_restart_signal(task_id: str) -> None:
    """Write a restart-recommended signal file for a task.

    The file is placed at ~/.duo/tasks/{id}/restart-recommended.
    CEO automation can check for this file and initiate orderly restart.
    """
    signal_path = TASKS_DIR / task_id / "restart-recommended"
    with contextlib.suppress(OSError):
        signal_path.write_text(
            f"Restart recommended — health check detected critical thresholds.\n"
            f"Time: {time.strftime('%Y-%m-%dT%H:%M:%S')}\n"
        )


def _doctor_check_stale_locks() -> CheckResult:
    """Check for stale .lock files from interrupted duo start commands."""
    lock_files = list(TASKS_DIR.glob(".*.lock")) if TASKS_DIR.exists() else []
    if not lock_files:
        return CheckResult("stale locks", "pass", "none", "")
    names = [f.name for f in lock_files]
    return CheckResult(
        "stale locks",
        "warn",
        f"{len(lock_files)} found: {', '.join(names)}",
        "Remove manually or run: duo cleanup",
    )


def _doctor_check_orphan_worktrees() -> CheckResult:
    """Check for git worktrees with no matching task in TASKS_DIR."""
    try:
        r = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return CheckResult("orphan worktrees", "pass", "skipped (git unavailable)", "")

    if r.returncode != 0:
        return CheckResult("orphan worktrees", "pass", "skipped (not a git repo)", "")

    known_ids = (
        {d.name for d in TASKS_DIR.iterdir() if d.is_dir()}
        if TASKS_DIR.exists()
        else set()
    )

    orphans: list[str] = []
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            wt_path = line[len("worktree ") :]
            wt_name = Path(wt_path).name
            if wt_name.startswith("duo-") and wt_name[4:] not in known_ids:
                orphans.append(wt_name)

    if not orphans:
        return CheckResult("orphan worktrees", "pass", "none", "")
    return CheckResult(
        "orphan worktrees",
        "warn",
        f"{len(orphans)} found: {', '.join(orphans[:5])}",
        "Remove with: git worktree remove <path>",
    )


_DOCTOR_CHECKS: list[Any] = [
    _doctor_check_python,
    _doctor_check_tmux,
    _doctor_check_tmux_bridge,
    _doctor_check_claude_cli,
    _doctor_check_copilot_cli,
    _doctor_check_duo_dir,
    _doctor_check_config,
    _doctor_check_tmux_session,
    _doctor_check_task_timeout,
    _doctor_check_corrupted,
    _doctor_check_stale_locks,
    _doctor_check_orphan_worktrees,
    _doctor_check_git,
]

_STATUS_ICONS: dict[str, str] = {
    "pass": "✓ PASS",
    "warn": "⚠ WARN",
    "fail": "✗ FAIL",
}

_STATUS_COLORS: dict[str, str] = {
    "pass": "green",
    "warn": "yellow",
    "fail": "red",
}


def _doctor_auto_fix() -> list[str]:
    """Attempt to auto-fix known issues. Returns list of fixed descriptions."""
    import shutil

    fixed: list[str] = []

    # Fix 1: Remove stale .lock files
    if TASKS_DIR.exists():
        for lf in TASKS_DIR.glob(".*.lock"):
            lf.unlink(missing_ok=True)
            fixed.append(f"removed stale lock: {lf.name}")

    # Fix 2: Purge quarantined (corrupted) tasks
    from duo.protocol import list_corrupted

    for p in list_corrupted():
        shutil.rmtree(p, ignore_errors=True)
        fixed.append(f"purged quarantined task: {p.name}")

    # Fix 3: Remove orphan duo-* worktrees
    try:
        r = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        r = None

    if r and r.returncode == 0:
        known_ids = (
            {d.name for d in TASKS_DIR.iterdir() if d.is_dir()}
            if TASKS_DIR.exists()
            else set()
        )
        for line in r.stdout.splitlines():
            if line.startswith("worktree "):
                wt_path = line[len("worktree ") :]
                wt_name = Path(wt_path).name
                if wt_name.startswith("duo-") and wt_name[4:] not in known_ids:
                    rm = subprocess.run(
                        ["git", "worktree", "remove", "--force", wt_path],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if rm.returncode == 0:
                        fixed.append(f"removed orphan worktree: {wt_name}")

    return fixed


@main.command()
@click.option("--json-output", is_flag=True, help="Output diagnostics as JSON.")
@click.option("--strict", is_flag=True, help="Exit non-zero on warnings too.")
@click.option("--fix", is_flag=True, help="Auto-fix issues that can be resolved.")
def doctor(json_output: bool, strict: bool, fix: bool) -> None:
    """Check environment dependencies and configuration."""
    results: list[CheckResult] = [fn() for fn in _DOCTOR_CHECKS]
    results.extend(_doctor_check_copilot_health())
    results.extend(_doctor_check_capi_error())

    fixed_items: list[str] = []
    if fix:
        fixed_items = _doctor_auto_fix()

    counts = {"pass": 0, "warn": 0, "fail": 0}
    for r in results:
        counts[r.status] += 1
    total = len(results)

    if json_output:
        payload: dict[str, Any] = {
            "checks": [
                {
                    "name": r.name,
                    "status": r.status,
                    "message": r.message,
                    "fix": r.fix,
                }
                for r in results
            ],
            "summary": {**counts, "total": total},
        }
        if fix:
            payload["fixed"] = fixed_items
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo("Duo Environment Diagnostics")
        click.echo("\u2500" * 28)
        for r in results:
            icon = _STATUS_ICONS[r.status]
            color = _STATUS_COLORS[r.status]
            suffix = ""
            if r.fix:
                suffix = f" — {r.fix}"
            line = f"  {icon}  {r.name:<16}{r.message}{suffix}"
            click.echo(click.style(line, fg=color))
        parts: list[str] = []
        parts.append(f"{counts['pass']}/{total} checks passed")
        if counts["warn"]:
            parts.append(
                f"{counts['warn']} warning{'s' if counts['warn'] != 1 else ''}"
            )
        if counts["fail"]:
            parts.append(
                f"{counts['fail']} failure{'s' if counts['fail'] != 1 else ''}"
            )
        click.echo(f"\n{', '.join(parts)}")
        if fixed_items:
            click.echo(f"\nAuto-fixed {len(fixed_items)} issue(s):")
            for item in fixed_items:
                click.echo(f"  ✓ {item}")

    has_fail = counts["fail"] > 0
    has_warn = counts["warn"] > 0
    if has_fail or (strict and has_warn):
        raise SystemExit(1)


@main.command()
@click.argument("name", required=False)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def resume(name: str | None, *, as_json: bool = False) -> None:
    """Resume interrupted task sessions."""
    from duo.commander import normalize_for_restart, restart_session, start_session
    from duo.transport import cleanup_pane_state, is_process_alive, kill_pane

    TERMINAL_STATES = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ESCALATED}
    results: list[dict[str, Any]] = []

    if name is not None:
        task = _load_task_or_fail(name)
        if task.status in TERMINAL_STATES:
            if as_json:
                click.echo(json.dumps({"resumed": [], "already_complete": [name]}))
            else:
                click.echo(f"Task '{name}' is already completed.")
            return
        targets = [task]
    else:
        all_tasks = list_tasks()
        SKIP_STATES = TERMINAL_STATES | {TaskStatus.QUEUED}
        targets = [t for t in all_tasks if t.status not in SKIP_STATES]
        if not targets:
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
            if not as_json:
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
            if not as_json:
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
            if not as_json:
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
            if not as_json:
                click.echo(f"  Replayed prompt for step {task.current_step}")
        except (RuntimeError, OSError) as exc:
            if not as_json:
                click.echo(f"  Warning: could not replay prompt: {exc}", err=True)

    if as_json:
        click.echo(json.dumps({"resumed": results}))


@main.command()
@click.argument("name")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def retry(name: str, *, as_json: bool = False) -> None:
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


@main.group()
def config() -> None:
    """Manage Duo configuration."""


@config.command("get")
@click.argument("key")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def config_get(key: str, *, as_json: bool = False) -> None:
    """Get a config value."""
    from duo.config import get_config

    value = get_config(key)
    if value is None:
        raise DuoUserError(
            f"Unknown config key: {key}",
            fix="Run 'duo config list' to see available keys.",
        )
    if as_json:
        click.echo(json.dumps({key: value}))
    else:
        click.echo(f"{key} = {value}")


@config.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def config_set(key: str, value: str, *, as_json: bool = False) -> None:
    """Set a config value."""
    from duo.config import DEFAULTS, set_config

    if key not in DEFAULTS:
        click.echo(f"Warning: '{key}' is not a known config key", err=True)
    try:
        result = set_config(key, value)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    if as_json:
        click.echo(json.dumps({key: result}))
    else:
        click.echo(f"{key} = {result}")


@config.command("list")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def config_list(*, as_json: bool = False) -> None:
    """List all config values."""
    from duo.config import DEFAULTS, load_config

    cfg = load_config()
    if as_json:
        output: dict[str, object] = {}
        for key in sorted(DEFAULTS):
            value = cfg.get(key, DEFAULTS[key])
            output[key] = value
        click.echo(json.dumps(output, indent=2))
        return
    for key in sorted(DEFAULTS):
        value = cfg.get(key, DEFAULTS[key])
        default = DEFAULTS[key]
        marker = "" if value == default else " (modified)"
        click.echo(f"  {key} = {value}{marker}")


@config.command("reset")
@click.argument("key", required=False)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def config_reset(key: str | None = None, *, as_json: bool = False) -> None:
    """Reset config to defaults (or reset a single key)."""
    from duo.config import DEFAULTS, reset_config

    if key and key not in DEFAULTS:
        raise DuoUserError(
            f"Unknown config key: {key}",
            fix="Run 'duo config list' to see available keys.",
        )
    reset_config(key)
    if as_json:
        click.echo(json.dumps({"reset": key or "all"}))
    elif key:
        click.echo(f"Reset {key} to default.")
    else:
        click.echo("All config reset to defaults.")


def _export_as_json(task: Task) -> str:
    """Generate a JSON export string for the given task."""
    from duo.protocol import read_jsonl, read_result_for_step

    events = read_jsonl(task.journal_path)
    report: dict[str, object] = {
        "task_id": task.id,
        "description": task.description,
        "status": task.status.value,
        "branch": task.branch,
        "worktree": task.worktree,
        "incarnation": task.incarnation_id,
        "created_at": task.created_at,
        "steps": task.current_step,
        "total_steps": len(task.subtasks),
        "attempt": task.current_attempt,
        "subtasks": [
            {
                "step_id": s.step_id,
                "description": s.description,
                "target_files": s.target_files,
            }
            for s in task.subtasks
        ],
        "events": events,
    }
    results = []
    for s in task.subtasks:
        if s.step_id == task.current_step:
            attempt_list = list(range(1, task.current_attempt + 1))
        else:
            step_dir = task.step_dir(s.step_id)
            attempt_list = (
                sorted(
                    int(p.stem.split("-")[-1])
                    for p in step_dir.glob("result-attempt-*.json")
                )
                if step_dir.is_dir()
                else []
            )
        for attempt in attempt_list:
            result = read_result_for_step(task, s.step_id, attempt)
            if result:
                results.append(
                    {
                        "step": result.step,
                        "attempt": result.attempt,
                        "status": result.status,
                        "summary": result.summary,
                        "files_changed": result.files_changed,
                    }
                )
    report["results"] = results
    return json.dumps(report, ensure_ascii=False, indent=2)


def _export_as_text(task: Task) -> str:
    """Generate a plain-text export string for the given task."""
    from duo.protocol import read_jsonl

    events = read_jsonl(task.journal_path)
    lines: list[str] = []
    lines.append(f"Task Report: {task.id}")
    lines.append(f"{'=' * 40}")
    lines.append(f"Description: {task.description}")
    lines.append(f"Status:      {task.status.value}")
    lines.append(f"Branch:      {task.branch}")
    lines.append(f"Created:     {task.created_at}")
    lines.append(f"Step:        {task.current_step}/{len(task.subtasks)}")
    lines.append(f"Attempt:     {task.current_attempt}")
    lines.append("")

    lines.append("Steps:")
    for s in task.subtasks:
        lines.append(f"  {s.step_id}. {s.description}")
        if s.target_files:
            lines.append(f"     Files: {', '.join(s.target_files)}")
    lines.append("")

    lines.append(f"Events ({len(events)} total):")
    for ev in events[-20:]:
        ts = _fmt_ts(ev.get("ts", "?"))
        lines.append(f"  {ts} {ev.get('event', '?')}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# duo events — watch-event signal file management
# ---------------------------------------------------------------------------

_WATCH_EVENTS_DIR = Path(os.path.expanduser("~/.duo/watch-events"))


@main.group()
def events() -> None:
    """Manage watch-event signal files."""


@events.command("list")
@click.option("-n", "--limit", default=20, help="Max events to show")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def events_list(limit: int, *, as_json: bool = False) -> None:
    """List recent watch events (newest first)."""
    if not _WATCH_EVENTS_DIR.exists():
        if as_json:
            click.echo(json.dumps({"events": [], "total": 0}))
        else:
            click.echo("No events.")
        return
    files = sorted(_WATCH_EVENTS_DIR.glob("*.json"), reverse=True)
    if not files:
        if as_json:
            click.echo(json.dumps({"events": [], "total": 0}))
        else:
            click.echo("No events.")
        return
    items: list[dict[str, str]] = []
    for f in files[:limit]:
        data = read_json(f)
        if data is None:
            continue
        if as_json:
            items.append({"file": f.name, **data})
        else:
            ts = _fmt_ts(data.get("detected_at", "?"))
            task = data.get("task_id", "?")
            click.echo(f"  {ts}  {task}  {f.name}")
    if as_json:
        click.echo(json.dumps({"events": items, "total": len(files)}))


@events.command("show")
@click.argument("name", default="latest")
def events_show(name: str) -> None:
    """Show a single event (by filename or 'latest')."""
    if not _WATCH_EVENTS_DIR.exists():
        raise DuoUserError(
            "No events directory",
            fix="Run a task with 'duo ceo-loop' to generate events.",
        )
    if name == "latest":
        files = sorted(_WATCH_EVENTS_DIR.glob("*.json"), reverse=True)
        if not files:
            raise DuoUserError(
                "No events found",
                fix="Run a task with 'duo ceo-loop' to generate events.",
            )
        target = files[0]
    else:
        target = _WATCH_EVENTS_DIR / name
        if not target.exists():
            target = _WATCH_EVENTS_DIR / f"{name}.json"
    if not target.exists():
        raise DuoUserError(
            f"Event file not found: {name}",
            fix="Run 'duo events list' to see available events.",
        )
    data = read_json(target)
    if data is None:
        raise DuoUserError(
            f"Invalid event file: {target.name}",
            fix="The file may be corrupted. Check the raw file content.",
        )
    click.echo(json.dumps(data, indent=2, ensure_ascii=False))


@events.command("tail")
@click.option("-n", "--limit", default=5, help="Initial events to show")
def events_tail(limit: int) -> None:
    """Follow watch events in real-time (Ctrl-C to stop)."""
    import time

    _WATCH_EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    # Show existing events first
    existing = sorted(_WATCH_EVENTS_DIR.glob("*.json"))
    for f in existing[-limit:]:
        data = read_json(f)
        if data:
            ts = _fmt_ts(data.get("detected_at", "?"))
            click.echo(f"  {ts}  {data.get('task_id', '?')}  {f.name}")
        seen.add(f.name)
    click.echo("--- following (Ctrl-C to stop) ---")
    try:
        while True:
            for f in sorted(_WATCH_EVENTS_DIR.glob("*.json")):
                if f.name not in seen:
                    seen.add(f.name)
                    data = read_json(f)
                    if data:
                        ts = _fmt_ts(data.get("detected_at", "?"))
                        click.echo(f"  {ts}  {data.get('task_id', '?')}  {f.name}")
            time.sleep(1)
    except KeyboardInterrupt:
        click.echo("\nStopped.")


@events.command("clear")
@click.option("--force", is_flag=True, help="Skip confirmation")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def events_clear(force: bool, *, as_json: bool = False) -> None:
    """Delete all watch-event signal files."""
    if not _WATCH_EVENTS_DIR.exists():
        if as_json:
            click.echo(json.dumps({"cleared": 0}))
        else:
            click.echo("No events to clear.")
        return
    files = list(_WATCH_EVENTS_DIR.glob("*.json"))
    if not files:
        if as_json:
            click.echo(json.dumps({"cleared": 0}))
        else:
            click.echo("No events to clear.")
        return
    if not force and not as_json:
        click.confirm(f"Delete {len(files)} event(s)?", abort=True)
    for f in files:
        f.unlink(missing_ok=True)
    if as_json:
        click.echo(json.dumps({"cleared": len(files)}))
    else:
        click.echo(f"Cleared {len(files)} event(s).")


# ---------------------------------------------------------------------------
# duo ceo-* — Ergonomic CEO workflow commands
# ---------------------------------------------------------------------------


def _load_task_or_fail(name: str) -> Task:
    """Validate task name, load it, or raise DuoUserError."""
    _validate_task_name(name)
    task = load_task(name)
    if task is None:
        raise DuoUserError(
            f"task '{name}' not found",
            fix="Run 'duo list' to see available tasks.",
        )
    return task


def _resolve_task_from_focus(task: str | None) -> str:
    """Resolve task name from argument or CEO focus."""
    if task:
        return task
    from duo.ceo_state import load_ceo_focus

    focus = load_ceo_focus()
    if focus is None:
        raise DuoUserError(
            "No task specified and no CEO focus set.",
            fix="Pass a task name or run 'duo ceo-focus <task>' first.",
        )
    return str(focus["task_id"])


def _log_pr_budget_warning(label: str, flag: str) -> None:
    """Append a warning line to ~/.duo/pr-budget.log when safety is bypassed."""
    from duo.protocol import DUO_DIR, now_iso

    log_path = DUO_DIR / "pr-budget.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"{now_iso()} WARNING {flag} used on pane '{label}'\n")
        f.flush()
        os.fsync(f.fileno())


def _enforce_not_at_main_prompt(label: str, force_new_session: bool) -> None:
    """Check main prompt guard; bypass only with force_new_session (+ log)."""
    if force_new_session:
        _log_pr_budget_warning(label, "--force-new-session")
    else:
        assert_not_at_main_prompt(label)


def assert_not_at_main_prompt(label: str) -> None:
    """Raise ClickException if the pane is at Copilot's main ❯ prompt.

    ANY input at the main prompt creates a new Premium Request.  This is a
    hard safety gate — callers must abort or require ``--force-new-session``.
    """
    from duo.transport import is_at_main_prompt, read_pane

    content = read_pane(label, 20)
    if is_at_main_prompt(content):
        raise DuoUserError(
            f"REFUSED: '{label}' is at Copilot main ❯ prompt. "
            "Sending any input here would create a NEW Premium Request "
            "and burn budget.",
            fix="Wait for a new dialog or use --force-new-session.",
        )


@main.command("ceo-wait")
@click.argument("task")
@click.option(
    "--timeout", default=300, type=float, help="Max seconds to wait (default: 300)."
)
@click.option(
    "--interval", default=5, type=float, help="Poll interval in seconds (default: 5)."
)
def ceo_wait(task: str, timeout: float, interval: float) -> None:
    """Wait for a dialog to appear in a task's pane.

    Blocks until the pane shows a stable dialog box, then prints the
    dialog content to stdout, writes a watch-event signal file, and
    exits 0. On timeout, exits 1.
    """
    from duo.commander import _write_watch_event
    from duo.transport import is_process_alive, read_pane, wait_for_dialog

    t = _load_task_or_fail(task)
    if not is_process_alive(t.pane_label):
        raise DuoUserError(
            f"Pane '{t.pane_label}' is not alive",
            fix=f"Run 'duo status {task}' to check task state, or 'duo resume {task}' to restart.",
        )
    found = wait_for_dialog(t.pane_label, timeout=timeout, interval=interval)
    if not found:
        raise DuoUserError(
            f"Timeout after {timeout}s: no dialog detected in '{task}'",
            fix=f"Check pane manually or increase --timeout. Run 'duo status {task}' for current state.",
        )
    content = read_pane(t.pane_label, 40)
    click.echo(content)
    _write_watch_event(t, content)
    _session_id = os.environ.get("DUO_CEO_SESSION")
    if _session_id:
        from duo.ceo_log import log_dialog_detected

        log_dialog_detected(_session_id, task, content, "dialog")


@main.command("ceo-select")
@click.argument("task")
@click.argument("option", required=False, default=None)
@click.option(
    "--other",
    "other_text",
    default=None,
    help="Navigate to the 'Other' option and type this text instead",
)
@click.option(
    "--force-new-session",
    is_flag=True,
    default=False,
    help="Bypass main-prompt safety check (WARNING: creates a new PR)",
)
def ceo_select(
    task: str,
    option: str | None,
    other_text: str | None,
    force_new_session: bool,
) -> None:
    """Select a dialog option in a task's pane.

    OPTION is a number (1-9) to pick that option directly.
    Use --other TEXT instead to navigate to the last option
    ("Other"/"type your answer") and type custom text.
    OPTION and --other are mutually exclusive.

    Safety: refuses to act if the pane is at the main ❯ prompt (would
    create a new Premium Request). Override with --force-new-session.
    """
    from duo.transport import (
        DialogKind,
        get_dialog_kind,
        is_in_dialog_stable,
        select_dialog_option,
        send_option_other_message,
        send_text_dialog_message,
    )

    if option is not None and other_text is not None:
        raise click.UsageError("Cannot specify both OPTION and --other. Pick one.")
    if option is None and other_text is None:
        raise click.UsageError("Must specify OPTION or --other TEXT.")

    if option is not None and not option.isdigit():
        raise DuoUserError(
            f"OPTION must be a number (1-9), got '{option}'",
            fix="Run 'duo ceo-select TASK 1' to select the first option.",
        )

    t = _load_task_or_fail(task)
    _enforce_not_at_main_prompt(t.pane_label, force_new_session)
    if not is_in_dialog_stable(t.pane_label):
        raise DuoUserError(
            f"Pane '{t.pane_label}' is not in a stable dialog",
            fix=f"Wait for the dialog to appear, then retry. Run 'duo ceo-wait {task}' to wait.",
        )
    kind = get_dialog_kind(t.pane_label)
    if kind == DialogKind.TEXT:
        # Text-input dialog: no numbered options
        if option is not None:
            raise DuoUserError(
                "This is a text-input dialog with no numbered options",
                fix="Use --other TEXT to type a response.",
            )
        if other_text is None:  # pragma: no cover — guarded by mutual-exclusion above
            raise click.ClickException(
                "Internal error: expected --other TEXT for text dialog."
            )
        success = send_text_dialog_message(t.pane_label, other_text)
        if success:
            click.echo(f"Typed text: {other_text}")
        else:
            click.echo(
                f"Typed text: {other_text} (dialog may still be active — check manually)"
            )
    elif kind == DialogKind.BULLET:
        from duo.transport import select_bullet_option

        if option is not None:
            select_bullet_option(t.pane_label, int(option))
            click.echo(f"Selected bullet option {option}")
        elif other_text is not None:
            # BULLET last item is usually "Type your answer..."
            send_text_dialog_message(t.pane_label, other_text)
            click.echo(f"Typed text in bullet dialog: {other_text}")
        else:  # pragma: no cover — unreachable: Click mutual-exclusion ensures option or other_text is set
            raise click.ClickException("Internal error: expected OPTION or --other.")
    elif other_text is not None:
        success = send_option_other_message(t.pane_label, other_text)
        if success:
            click.echo(f"Selected 'Other' with text: {other_text}")
        else:
            click.echo(
                f"Selected 'Other' with text: {other_text} (dialog may still be active)"
            )
    else:
        if option is None:  # pragma: no cover — guarded by mutual-exclusion above
            raise click.ClickException("Internal error: expected OPTION number.")
        select_dialog_option(t.pane_label, option)
        click.echo(f"Selected option {option}")
    _session_id = os.environ.get("DUO_CEO_SESSION")
    if _session_id:
        from duo.ceo_log import log_decision

        chosen = other_text if other_text is not None else (option or "?")
        log_decision(_session_id, task, "select", f"selected {chosen}", elapsed_ms=0)


@main.command("ceo-approve")
@click.argument("task")
@click.option(
    "--force-new-session",
    is_flag=True,
    default=False,
    help="Bypass main-prompt safety check (WARNING: creates a new PR)",
)
def ceo_approve(task: str, force_new_session: bool) -> None:
    """Auto-approve a permission dialog in a task's pane.

    Only works on permission dialogs (e.g. "Do you want to run this
    command?"). For ask-user dialogs, use ceo-select instead.

    Reads the dialog options and picks the "most positive" yes option:
    prefers "Yes + approve for session" over plain "Yes", skips "No".

    Safety: refuses to act if the pane is at the main ❯ prompt (would
    create a new Premium Request). Override with --force-new-session.
    """
    from duo.transport import approve_permission, is_permission_dialog

    t = _load_task_or_fail(task)
    _enforce_not_at_main_prompt(t.pane_label, force_new_session)
    if not is_permission_dialog(t.pane_label):
        raise DuoUserError(
            f"'{task}' is not showing a permission dialog",
            fix="Use 'duo ceo-select' for other dialog types, or 'duo ceo-wait' to wait for a dialog.",
        )
    approve_permission(t.pane_label)
    click.echo(f"Approved dialog in '{task}'")
    _session_id = os.environ.get("DUO_CEO_SESSION")
    if _session_id:
        from duo.ceo_log import log_decision

        log_decision(_session_id, task, "approve", "permission approved", elapsed_ms=0)


_AUTO_SELECT_KEYWORDS = [
    "continue",
    "继续",
    "继续改进",
    "yes",
    "next",
    "next step",
    "下一",
    "proceed",
    "confirm",
    "start",
    "begin",
    "ok",
    "commit first",
    "push",
    "save",
    "apply",
    "accept",
    "approve",
    "allow",
    "run",
    "execute",
    "install",
    "deploy",
    "merge",
]

_DEFER_KEYWORDS = [
    "which",
    "choose",
    "select from",
    "pick one",
    "how should",
    "what should",
]


def _load_smart_config() -> tuple[list[str], list[str]]:
    """Load user patterns from ~/.duo/ceo-smart.yaml.

    Returns (extra_auto_patterns, extra_defer_patterns).
    """
    config_path = DUO_DIR / "ceo-smart.yaml"
    if not config_path.exists():
        return [], []
    try:
        import yaml

        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return [], []
        auto = data.get("auto_select_patterns", [])
        defer = data.get("defer_patterns", [])
        if not isinstance(auto, list):
            auto = []
        if not isinstance(defer, list):
            defer = []
        return [str(p) for p in auto], [str(p) for p in defer]
    except (yaml.YAMLError, OSError, ValueError, ImportError):
        return [], []


def _is_auto_selectable(
    pane_content: str,
    *,
    verbose: bool = False,
) -> tuple[bool, str]:
    """Check if the first option in a dialog is auto-selectable.

    Returns (should_auto_select, first_option_text).
    Defer keywords take priority over auto-select.
    """
    extra_auto, extra_defer = _load_smart_config()
    all_defer = _DEFER_KEYWORDS + extra_defer
    all_auto = list(_AUTO_SELECT_KEYWORDS) + extra_auto

    content_lower = pane_content.lower()
    for kw in all_defer:
        if kw in content_lower:
            if verbose:
                click.echo(f"  [defer] matched: {kw!r}")
            return False, ""

    lines = pane_content.splitlines()
    first_option = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("1.", "❯")):
            first_option = stripped
            break

    if not first_option:
        return False, ""

    lower = first_option.lower()
    for kw in all_auto:
        if kw in lower:
            if verbose:
                click.echo(f"  [auto-select] matched: {kw!r}")
            return True, first_option

    return False, first_option


@main.command("ceo-smart")
@click.argument("task")
@click.option("--verbose", is_flag=True, help="Print decision reasoning.")
def ceo_smart(task: str, *, verbose: bool) -> None:
    """Auto-decide trivial dialogs, defer complex ones.

    Exits 0 if a decision was made automatically.
    Exits 1 if the dialog needs manual CEO intervention (prints dialog content).
    """
    from duo.transport import (
        DialogKind,
        approve_permission,
        get_dialog_kind,
        is_in_dialog,
        is_permission_dialog,
        read_pane,
        select_dialog_option,
    )

    t = _load_task_or_fail(task)

    if not is_in_dialog(t.pane_label):
        click.echo("No dialog detected.")
        return

    kind = get_dialog_kind(t.pane_label)
    content = read_pane(t.pane_label)
    session_id = os.environ.get("DUO_CEO_SESSION")

    if verbose:
        click.echo(f"Dialog kind: {kind.value}")

    if session_id:
        from duo.ceo_log import log_dialog_detected

        log_dialog_detected(session_id, task, content, kind.value)

    if is_permission_dialog(t.pane_label):
        if verbose:
            click.echo("  [reason] permission → auto-approve")
        approve_permission(t.pane_label)
        click.echo(f"Auto-approved permission dialog for '{task}'.")
        if session_id:
            from duo.ceo_log import log_decision

            log_decision(
                session_id,
                task,
                "smart-approve",
                "permission auto-approved",
                elapsed_ms=0,
            )
        return

    if kind == DialogKind.OPTION:
        auto, first_opt = _is_auto_selectable(content, verbose=verbose)
        if auto:
            select_dialog_option(t.pane_label, "1")
            click.echo(f"Auto-selected option 1 for '{task}': {first_opt[:60]}")
            if session_id:
                from duo.ceo_log import log_decision

                log_decision(
                    session_id,
                    task,
                    "smart-select",
                    f"auto-selected: {first_opt[:100]}",
                    elapsed_ms=0,
                )
            return

    click.echo(f"Dialog requires manual intervention ({kind.value}):")
    click.echo(content)
    if session_id:
        from duo.ceo_log import log_decision

        log_decision(
            session_id,
            task,
            "smart-defer",
            f"deferred {kind.value} dialog",
            elapsed_ms=0,
        )
    sys.exit(1)


@main.command("ceo-focus")
@click.argument("task")
@click.option("--session", default="", help="CEO session ID to associate.")
@click.option("--notes", default="", help="Notes about current focus.")
def ceo_focus_set(task: str, session: str, notes: str) -> None:
    """Set the current CEO focus task."""
    from duo.ceo_state import save_ceo_focus

    _validate_task_name(task)
    t = _load_task_or_fail(task)
    save_ceo_focus(task, session_id=session, notes=notes)
    click.echo(f"CEO focus set to: {task} (status: {t.status.value})")


@main.command("ceo-focus-show")
@click.option("--json-output", is_flag=True, help="Output as JSON.")
def ceo_focus_show(json_output: bool) -> None:
    """Show the current CEO focus task."""
    from duo.ceo_state import load_ceo_focus

    focus = load_ceo_focus()
    if focus is None:
        click.echo("No CEO focus set. Use 'duo ceo-focus <task>' to set one.")
        return
    if json_output:
        click.echo(json.dumps(focus, indent=2))
        return
    task_id = focus.get("task_id", "?")
    task = load_task(task_id)
    status = task.status.value if task else "unknown"
    session = focus.get("session_id", "")
    started = focus.get("started_at", "?")[:19]
    notes = focus.get("notes", "")
    click.echo("Current CEO focus:")
    click.echo(f"  Task:    {task_id} (status: {status})")
    if session:
        click.echo(f"  Session: {session}")
    click.echo(f"  Started: {started}")
    if notes:
        click.echo(f"  Notes:   {notes}")


@main.command("ceo-focus-clear")
def ceo_focus_clear() -> None:
    """Clear the current CEO focus."""
    from duo.ceo_state import clear_ceo_focus

    clear_ceo_focus()
    click.echo("CEO focus cleared.")


def _gather_task_info(task: Task, focus: dict[str, Any]) -> dict[str, Any]:
    """Build the focus/task-status section of the dashboard data."""
    return {
        "task_id": task.id,
        "status": task.status.value,
        "started_at": focus.get("started_at", ""),
    }


def _gather_pane_info(task: Task) -> dict[str, Any]:
    """Collect tmux pane liveness, dialog state, and recent output for a task."""
    pane_alive = False
    try:
        from duo.transport import (
            get_dialog_kind,
            is_in_dialog,
            resolve_label,
        )

        resolve_label(task.pane_label)
        pane_alive = True
    except (subprocess.SubprocessError, OSError, RuntimeError):
        pass

    in_dialog = False
    dialog_kind = ""
    recent_lines: list[str] = []
    if pane_alive:
        try:
            in_dialog = is_in_dialog(task.pane_label)
            if in_dialog:
                dialog_kind = get_dialog_kind(task.pane_label).value
        except (subprocess.SubprocessError, OSError, RuntimeError):
            pass
        try:
            from duo.transport import read_pane

            content = read_pane(task.pane_label, 5)
            recent_lines = [
                line.strip() for line in content.strip().splitlines() if line.strip()
            ][-5:]
        except (subprocess.SubprocessError, OSError, RuntimeError):
            pass

    return {
        "label": task.pane_label,
        "alive": pane_alive,
        "in_dialog": in_dialog,
        "dialog_kind": dialog_kind,
        "recent_lines": recent_lines,
    }


def _gather_git_info() -> dict[str, Any] | None:
    """Collect git HEAD commit, working-tree cleanliness, and last commit time."""
    try:
        result = subprocess.run(
            ["git", "--no-pager", "log", "-1", "--format=%h %s"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        git_info: dict[str, Any] | None = None
        if result.returncode == 0:
            git_info = {"head": result.stdout.strip()}
        time_result = subprocess.run(
            ["git", "--no-pager", "log", "-1", "--format=%ci"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if git_info and time_result.returncode == 0:
            git_info["last_commit_time"] = time_result.stdout.strip()
        git_status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if git_info:
            git_info["clean"] = git_status.stdout.strip() == ""
    except (subprocess.SubprocessError, OSError):
        return None
    else:
        return git_info


def _gather_recent_decisions(focus: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the last 5 decision/dialog events from the CEO session."""
    if not focus.get("session_id"):
        return []
    from duo.ceo_log import replay_session

    events_list = replay_session(focus["session_id"])
    return [
        e for e in events_list if e.get("event") in ("decision", "dialog_detected")
    ][-5:]


def _gather_budget_info(task: Task) -> dict[str, Any]:
    """Collect PR budget usage and burn rate for a task."""
    events = read_jsonl(task.journal_path)
    pr_events = [e for e in events if e.get("event") == "pr_consumed"]
    pr_count = len(pr_events)
    budget_setting = int(get_config("pr_budget") or 0)

    # Burn rate: PRs per hour over the last hour
    burn_rate = 0.0
    if pr_events:
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        one_hour_ago = now - timedelta(hours=1)
        recent_prs = 0
        for e in pr_events:
            try:
                ts_str = e.get("ts", "")
                ts = datetime.fromisoformat(ts_str)
                if ts >= one_hour_ago:
                    recent_prs += 1
            except (ValueError, TypeError):
                pass
        burn_rate = float(recent_prs)

    return {"used": pr_count, "limit": budget_setting, "burn_rate_per_hour": burn_rate}


def _gather_session_health(task: Task) -> dict[str, Any] | None:
    """Collect session age, fd count, PR usage, and session risk level."""
    from duo.transport import get_pane_pid

    pid = get_pane_pid(task.pane_label)
    if pid is None:
        return None

    fd_count = _get_pid_fd_count(pid)
    kqueue_count = _get_pid_kqueue_count(pid)
    child_count = _get_pid_child_count(pid)

    # Session age from session_started_at (preferred) or created_at
    age_seconds = 0.0
    try:
        from datetime import UTC, datetime

        ts = getattr(task, "session_started_at", None) or task.created_at
        created = datetime.fromisoformat(ts)
        age_seconds = (datetime.now(UTC) - created).total_seconds()
    except (ValueError, TypeError, AttributeError):
        pass

    # Count PR-consuming events as a proxy for session context volume
    pr_count = 0
    has_capi_error = False
    if task.journal_path.exists():
        from duo.protocol import read_jsonl

        events = read_jsonl(task.journal_path)
        pr_count = sum(1 for e in events if e.get("event") == "pr_consumed")
        has_capi_error = any(e.get("event") == "capi_error" for e in events)

    # Estimate remaining capacity based on fd growth rate
    est_remaining_hours: float | None = None
    if fd_count > 0 and age_seconds > 60:
        fd_rate_per_hour = fd_count / (age_seconds / 3600)
        remaining_fds = max(0, _COPILOT_FD_CRITICAL - fd_count)
        if fd_rate_per_hour > 0:  # pragma: no cover — always true: fd_count>0 ⇒ rate>0
            est_remaining_hours = remaining_fds / fd_rate_per_hour

    health_status = "healthy"
    if has_capi_error or fd_count >= _COPILOT_FD_CRITICAL:
        health_status = "critical"
    elif (
        fd_count >= _COPILOT_FD_WARN
        or kqueue_count >= _COPILOT_KQUEUE_WARN
        or child_count >= _COPILOT_CHILD_WARN
    ):
        health_status = "degraded"

    # Session risk: high if CAPIError seen or age > 4h with many PRs
    age_hours = age_seconds / 3600
    risk = "low"
    if has_capi_error or (age_hours > 4 and pr_count > 50):
        risk = "high"
    elif age_hours > 2 or pr_count > 30:
        risk = "medium"

    return {
        "pid": pid,
        "fd_count": fd_count,
        "kqueue_count": kqueue_count,
        "child_count": child_count,
        "age_seconds": round(age_seconds),
        "est_remaining_hours": (
            round(est_remaining_hours, 1) if est_remaining_hours is not None else None
        ),
        "status": health_status,
        "pr_count": pr_count,
        "risk": risk,
    }


@main.command("ceo-now")
@click.option("--json-output", is_flag=True, help="Output as JSON.")
def ceo_now(json_output: bool) -> None:
    """One-screen CEO status dashboard."""
    from duo.ceo_state import load_ceo_focus

    focus = load_ceo_focus()

    data: dict[str, Any] = {
        "focus": None,
        "pane": None,
        "dialog": None,
        "budget": None,
        "health": None,
        "git": None,
        "recent_decisions": [],
    }

    if focus:
        task_id = focus.get("task_id", "")
        task = load_task(task_id)
        if task:
            data["focus"] = _gather_task_info(task, focus)
            data["pane"] = _gather_pane_info(task)
            data["budget"] = _gather_budget_info(task)
            data["health"] = _gather_session_health(task)
        else:
            data["focus"] = {"task_id": task_id, "status": "not_found"}

    data["git"] = _gather_git_info()
    if focus:
        data["recent_decisions"] = _gather_recent_decisions(focus)

    if json_output:
        click.echo(json.dumps(data, indent=2))
        return

    # Pretty print
    width = 55
    click.echo(f"\u2500\u2500\u2500 Duo CEO Dashboard {'\u2500' * (width - 22)}")

    if data["focus"]:
        f = data["focus"]
        click.echo(f"Focus:     {f['task_id']} ({f['status']})")
    else:
        click.echo("Focus:     (none \u2014 run 'duo ceo-focus <task>')")

    if data["pane"]:
        p = data["pane"]
        status_parts = ["alive" if p["alive"] else "dead"]
        if p["in_dialog"]:
            status_parts.append(f"dialog: {p['dialog_kind']}")
        click.echo(f"Pane:      {p['label']} ({', '.join(status_parts)})")
        if p.get("recent_lines"):
            click.echo("  Recent output:")
            for line in p["recent_lines"]:
                click.echo(f"    {line[:60]}")

    if data["budget"]:
        b = data["budget"]
        limit_str = f"limit: {b['limit']}" if b["limit"] > 0 else "unlimited"
        burn = b.get("burn_rate_per_hour", 0)
        burn_str = f", {burn:.0f}/hr" if burn > 0 else ""
        click.echo(f"Budget:    {b['used']} PRs used ({limit_str}{burn_str})")

    if data["health"]:
        h = data["health"]
        age_hrs = h["age_seconds"] / 3600
        age_str = f"{age_hrs:.1f}h"
        status_color = {"healthy": "green", "degraded": "yellow", "critical": "red"}
        color = status_color.get(h["status"], "white")
        parts = [f"fds={h['fd_count']}", f"kqueue={h['kqueue_count']}"]
        parts.append(f"children={h['child_count']}")
        cap = h.get("est_remaining_hours")
        cap_str = f", ~{cap:.1f}h remaining" if cap is not None else ""
        line = f"Health:    {h['status']} (age {age_str}, {', '.join(parts)}{cap_str})"
        click.echo(click.style(line, fg=color))
        # Session risk line
        risk = h.get("risk", "low")
        pr_count = h.get("pr_count", 0)
        risk_color = {"low": "green", "medium": "yellow", "high": "red"}
        r_color = risk_color.get(risk, "white")
        click.echo(
            click.style(
                f"Risk:      {risk} (PRs={pr_count}, age={age_str})", fg=r_color
            )
        )

    if data["git"]:
        g = data["git"]
        clean_str = "clean" if g.get("clean") else "dirty"
        commit_time = g.get("last_commit_time", "")
        time_part = f" @ {commit_time[:19]}" if commit_time else ""
        click.echo(f"Git:       {g['head']} ({clean_str}{time_part})")

    if data["recent_decisions"]:
        last_ts = data["recent_decisions"][-1].get("ts", "")
        click.echo(f"\nLast decision: {last_ts[:19] if last_ts else 'unknown'}")
        click.echo("Recent events (last 5):")
        for ev in data["recent_decisions"]:
            ts = ev.get("ts", "?")[11:16]
            etype = ev.get("event", "?")
            task_name = ev.get("task", "")
            content = ev.get("content", ev.get("decision_type", ""))[:40]
            click.echo(f"  {ts}  {etype:20s}  {task_name:15s}  {content}")

    click.echo("\u2500" * width)


@main.command("ceo-status")
@click.argument("task")
@click.option(
    "--assert-in-dialog",
    is_flag=True,
    default=False,
    help="Exit non-zero if pane is NOT in a dialog (for scripting)",
)
def ceo_status(task: str, assert_in_dialog: bool) -> None:
    """Print the current pane state as a single JSON line.

    States: idle, processing, dialog, text_dialog, dead.

    \b
    Output examples:
      {"task":"e2e-test","state":"dialog","options":5}
      {"task":"e2e-test","state":"text_dialog"}

    Use --assert-in-dialog in scripts:
      duo ceo-status my-task --assert-in-dialog || handle_no_dialog
    """
    from duo.transport import (
        DialogKind,
        get_dialog_kind,
        is_process_alive,
        read_pane,
        strip_ansi,
    )

    t = _load_task_or_fail(task)
    label = t.pane_label

    if not is_process_alive(label):
        click.echo(json.dumps({"task": task, "state": "dead"}))
        if assert_in_dialog:
            raise SystemExit(1)
        return

    content = read_pane(label, 30)
    kind = get_dialog_kind(label)

    # Check dialog first (most specific)
    if kind == DialogKind.OPTION:
        # Count options only within the dialog box boundaries (╭─ … ╰─)
        lines = content.split("\n")
        in_box = False
        opt_count = 0
        for line in lines:  # pragma: no cover — split("\n") always yields ≥1 element
            if "╭─" in line:
                in_box = True
                continue
            if "╰─" in line:
                break
            if in_box and re.match(r"\s*[│]?\s*(❯\s*)?\d+\.\s", line):
                opt_count += 1
        click.echo(json.dumps({"task": task, "state": "dialog", "options": opt_count}))
        return

    if kind == DialogKind.TEXT:
        click.echo(json.dumps({"task": task, "state": "text_dialog"}))
        return

    if kind == DialogKind.BULLET:
        from duo.transport import count_bullet_items

        total, cursor = count_bullet_items(strip_ansi(content))
        click.echo(
            json.dumps(
                {
                    "task": task,
                    "state": "bullet_dialog",
                    "items": total,
                    "cursor": cursor,
                }
            )
        )
        return

    # Check spinner (processing)
    if any(m in content for m in ("◉ ", "◎ ", "○ ")):
        click.echo(json.dumps({"task": task, "state": "processing"}))
        if assert_in_dialog:
            raise SystemExit(1)
        return

    # Otherwise idle
    click.echo(json.dumps({"task": task, "state": "idle"}))
    if assert_in_dialog:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# duo ceo-loop / ceo-resume — automated CEO workflow
# ---------------------------------------------------------------------------

_DEFAULT_POLICY: dict[str, object] = {
    "permission_dialogs": {"auto_approve": True},
    "option_dialogs": {"default": "pause", "rules": []},
    "text_dialogs": {"action": "pause"},
}

CEO_LOOPS_DIR = DUO_DIR / "ceo-loops"


def _load_policy(policy_path: str | None) -> dict[str, object]:
    """Load a policy file (YAML or JSON) or return the default policy."""
    if policy_path is None:
        return dict(_DEFAULT_POLICY)
    import yaml

    path = Path(policy_path)
    if not path.exists():
        raise DuoUserError(
            f"Policy file not found: {policy_path}",
            fix="Check the path or remove --policy to use defaults.",
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError, ValueError) as exc:
        raise DuoUserError(
            f"Invalid policy file: {exc}",
            fix=f"Validate your YAML: python -c \"import yaml; yaml.safe_load(open('{policy_path}'))\"",
        ) from exc
    if not isinstance(data, dict):
        raise DuoUserError(
            "Policy file must be a YAML mapping at top level",
            fix="Ensure the file starts with key-value pairs, not a list or scalar.",
        )
    # Merge with defaults for missing keys
    result = dict(_DEFAULT_POLICY)
    result.update(data)
    return result


def _write_loop_state(task_id: str, state: dict[str, object]) -> None:
    """Write ceo-loop state to ~/.duo/ceo-loops/{task}.json."""
    CEO_LOOPS_DIR.mkdir(parents=True, exist_ok=True)
    state_path = CEO_LOOPS_DIR / f"{task_id}.json"
    atomic_write_text(state_path, json.dumps(state, indent=2) + "\n")


def _read_loop_state(task_id: str) -> dict[str, object] | None:
    """Read ceo-loop state, or None if not present."""
    state_path = CEO_LOOPS_DIR / f"{task_id}.json"
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        return None


def _match_option_rule(
    rules: list[dict[str, str]], content: str
) -> dict[str, str] | None:
    """Find the first matching rule for option dialog content."""
    for rule in rules:
        pattern = rule.get("match", "")
        if pattern and pattern in content:
            return rule
    return None


def _handle_dialog(
    task_id: str,
    label: str,
    policy: dict[str, object],
    content: str,
    kind: str,
) -> str:
    """Handle a dialog according to policy. Returns action taken."""
    from duo.transport import (
        approve_permission,
        is_permission_dialog,
        select_dialog_option,
        send_text_dialog_message,
    )

    # Permission dialogs
    if is_permission_dialog(label):
        perm_policy = policy.get("permission_dialogs", {})
        if isinstance(perm_policy, dict) and perm_policy.get("auto_approve", True):
            approve_permission(label)
            return "approved"

    if kind == "option":
        opt_policy = policy.get("option_dialogs", {})
        rules = []
        default_action = "pause"
        if isinstance(opt_policy, dict):
            rules = opt_policy.get("rules", [])
            default_action = opt_policy.get("default", "pause")

        # Check rules
        rule = _match_option_rule(rules, content) if isinstance(rules, list) else None
        if rule is not None:
            action = rule.get("action", "pause")
            if action == "approve":
                approve_permission(label)
                return "rule_approved"
            if action == "select_option":
                opt = rule.get("option", "1")
                select_dialog_option(label, str(opt))
                return f"rule_selected_{opt}"
            # fall through to pause
        elif default_action == "select_first":
            select_dialog_option(label, "1")
            return "selected_first"
        elif default_action == "select_last":
            # Count options only within dialog box boundaries
            import re

            from duo.transport import extract_last_box_lines, strip_ansi

            max_opt = 1
            box_lines = extract_last_box_lines(strip_ansi(content)) or []
            opt_re = re.compile(r"\s*[│]?\s*(❯\s*)?(\d+)\.\s")
            for line in box_lines:
                m = opt_re.match(line)
                if m:
                    max_opt = max(max_opt, int(m.group(2)))
            select_dialog_option(label, str(max_opt))
            return f"selected_last_{max_opt}"

    if kind == "text":
        txt_policy = policy.get("text_dialogs", {})
        if isinstance(txt_policy, dict) and txt_policy.get("action") == "auto_respond":
            resp = txt_policy.get("response", "")
            if isinstance(resp, str) and resp:
                send_text_dialog_message(label, resp)
                return "auto_responded"

    if kind == "bullet":
        # Bullet dialogs typically require human judgment — default to pause
        bullet_policy = policy.get("bullet_dialogs", {})
        if isinstance(bullet_policy, dict):
            default_action = bullet_policy.get("default", "pause")
            if default_action == "select_first":
                from duo.transport import select_bullet_option

                select_bullet_option(label, 1)
                return "bullet_selected_first"

    # Pause — wait for CEO to resume
    _write_loop_state(
        task_id,
        {
            "status": "paused",
            "dialog_kind": kind,
            "content_preview": content[:500],
        },
    )
    return "paused"


@main.command("ceo-loop")
@click.argument("task")
@click.option(
    "--policy",
    "policy_path",
    default=None,
    help="YAML policy file for auto-handling dialogs.",
)
@click.option(
    "--interval", default=5.0, type=float, help="Poll interval in seconds (default: 5)."
)
@click.option(
    "--timeout",
    default=3600.0,
    type=float,
    help="Max loop duration in seconds (default: 3600).",
)
def ceo_loop(
    task: str, policy_path: str | None, interval: float, timeout: float
) -> None:
    """Automated CEO workflow loop.

    Polls for dialogs and handles them according to the policy file.
    Permissions are auto-approved by default. Other dialogs pause
    and wait for ``duo ceo-resume`` from another terminal.

    Press Ctrl+C to stop the loop.
    """
    import time as _time

    from duo.transport import (
        DialogKind,
        TmuxServerDownError,
        get_dialog_kind,
        is_process_alive,
        read_pane,
    )

    t = _load_task_or_fail(task)
    policy = _load_policy(policy_path)
    click.echo(f"CEO loop started for '{task}'. Press Ctrl+C to stop.")
    deadline = _time.monotonic() + timeout

    try:
        while True:
            if _time.monotonic() >= deadline:
                click.echo(f"CEO loop timeout after {timeout}s. Exiting.")
                _write_loop_state(task, {"status": "stopped", "reason": "timeout"})
                break

            if not is_process_alive(t.pane_label):
                click.echo(f"Pane '{t.pane_label}' died. Exiting loop.")
                _write_loop_state(task, {"status": "stopped", "reason": "pane_died"})
                break

            kind = get_dialog_kind(t.pane_label)
            if kind == DialogKind.NONE:
                _time.sleep(interval)
                continue

            content = read_pane(t.pane_label, 40)
            kind_map = {
                DialogKind.OPTION: "option",
                DialogKind.TEXT: "text",
                DialogKind.BULLET: "bullet",
            }
            kind_str = kind_map.get(kind, "unknown")
            click.echo(f"Dialog detected ({kind_str}):")
            click.echo(content[:200])

            _session_id = os.environ.get("DUO_CEO_SESSION")
            if _session_id:
                from duo.ceo_log import log_dialog_detected

                log_dialog_detected(_session_id, task, content, kind_str)

            action = _handle_dialog(task, t.pane_label, policy, content, kind_str)
            click.echo(f"  → Action: {action}")

            if _session_id:
                from duo.ceo_log import log_decision as _log_d

                _log_d(_session_id, task, action, f"loop:{action}", elapsed_ms=0)

            if action == "paused":
                click.echo(
                    f"  Paused. Run 'duo ceo-resume {task}' from another terminal."
                )
                # Wait for resume signal
                while True:
                    state = _read_loop_state(task)
                    if state is not None and state.get("status") == "resumed":
                        resume_text = state.get("instruction", "")
                        click.echo(f"  Resumed with: {resume_text}")
                        # Clear resume state
                        _write_loop_state(task, {"status": "running"})
                        break
                    if not is_process_alive(t.pane_label):
                        click.echo(f"Pane '{t.pane_label}' died while paused. Exiting.")
                        _write_loop_state(
                            task, {"status": "stopped", "reason": "pane_died"}
                        )
                        return
                    _time.sleep(2)

            _time.sleep(interval)

    except TmuxServerDownError:
        click.echo(
            "\ntmux server is down. Start a new session and retry:\n"
            "  tmux new -s duo && duo ceo-loop " + task
        )
        _write_loop_state(task, {"status": "stopped", "reason": "tmux_server_down"})
    except KeyboardInterrupt:
        click.echo("\nCEO loop stopped by user.")
        _write_loop_state(task, {"status": "stopped", "reason": "user_interrupt"})


@main.command("ceo-resume")
@click.argument("task")
@click.argument("instruction", default="")
def ceo_resume(task: str, instruction: str) -> None:
    """Resume a paused ceo-loop for a task.

    Signals the ceo-loop (running in another terminal) to continue.
    Optional INSTRUCTION text is passed to the loop for context.
    """
    state = _read_loop_state(task)
    if state is None:
        raise DuoUserError(
            f"No ceo-loop state found for '{task}'",
            fix=f"Start a ceo-loop first: duo ceo-loop {task}",
        )
    if state.get("status") != "paused":
        raise DuoUserError(
            f"ceo-loop for '{task}' is not paused (status: {state.get('status')})",
            fix="Only paused loops can be resumed. Check the ceo-loop terminal for current state.",
        )
    _write_loop_state(task, {"status": "resumed", "instruction": instruction})
    click.echo(f"Resumed ceo-loop for '{task}'.")


# ---------------------------------------------------------------------------
# duo ceo-session-* — CEO session replay logging
# ---------------------------------------------------------------------------


@main.command("ceo-session-start")
def ceo_session_start() -> None:
    """Start a new CEO session and print export command."""
    from duo.ceo_log import start_ceo_session

    session_id = start_ceo_session()
    click.echo(f"CEO session started: {session_id}")
    click.echo(f"export DUO_CEO_SESSION={session_id}")


@main.command("ceo-session-list")
@click.option("--json-output", is_flag=True, help="Output as JSON.")
def ceo_session_list(*, json_output: bool) -> None:
    """List all CEO sessions."""
    import json

    from duo.ceo_log import list_sessions

    sessions = list_sessions()
    if not sessions:
        if json_output:
            click.echo("[]")
        else:
            click.echo("No CEO sessions found.")
        return
    if json_output:
        click.echo(json.dumps(sessions, indent=2))
    else:
        for s in sessions:
            click.echo(s)


@main.command("ceo-session-replay")
@click.argument("session_id")
def ceo_session_replay(session_id: str) -> None:
    """Replay a CEO session's events."""
    from duo.ceo_log import replay_session

    events = replay_session(session_id)
    if not events:
        raise DuoUserError(
            f"No events found for session '{session_id}'",
            fix="Run 'duo ceo-session-list' to see available sessions.",
        )
    for ev in events:
        ts = ev.get("ts", "?")[:19]
        event_type = ev.get("event", "?")
        task = ev.get("task", "")
        content = ev.get("content", ev.get("outcome", ev.get("decision_type", "")))
        line = f"  {ts}  {event_type:20s}  {task:15s}  {content}"
        click.echo(line)


@main.command("ceo-session-stats")
@click.argument("session_id")
@click.option("--json-output", is_flag=True, help="Output as JSON.")
def ceo_session_stats_cmd(session_id: str, *, json_output: bool) -> None:
    """Show stats for a CEO session."""
    from duo.ceo_log import session_stats

    stats = session_stats(session_id)
    if json_output:
        click.echo(json.dumps(stats, indent=2))
    else:
        click.echo(f"Session:    {stats['session_id']}")
        click.echo(f"Events:     {stats['total_events']}")
        click.echo(f"Dialogs:    {stats['dialogs_detected']}")
        click.echo(f"Decisions:  {stats['decisions_made']}")
        click.echo(f"Avg time:   {stats['avg_decision_ms']}ms")
        if stats["decision_types"]:
            click.echo("Types:")
            for dt, count in stats["decision_types"].items():
                click.echo(f"  {dt}: {count}")


@main.command("ceo-smart-config")
@click.option("--json-output", is_flag=True, help="Output as JSON.")
def ceo_smart_config(*, json_output: bool) -> None:
    """Show effective ceo-smart patterns (built-in + user config)."""
    extra_auto, extra_defer = _load_smart_config()
    all_auto = list(_AUTO_SELECT_KEYWORDS) + extra_auto
    all_defer = list(_DEFER_KEYWORDS) + extra_defer

    data: dict[str, Any] = {
        "auto_select_patterns": all_auto,
        "defer_patterns": all_defer,
        "user_auto_patterns": extra_auto,
        "user_defer_patterns": extra_defer,
    }

    if json_output:
        click.echo(json.dumps(data, indent=2))
        return

    click.echo("Auto-select patterns:")
    for p in all_auto:
        marker = " (user)" if p in extra_auto else ""
        click.echo(f"  {p}{marker}")
    click.echo()
    click.echo("Defer patterns:")
    for p in all_defer:
        marker = " (user)" if p in extra_defer else ""
        click.echo(f"  {p}{marker}")


@main.command("ceo-cleanup")
@click.argument("task")
@click.option(
    "--dry-run", is_flag=True, help="Show what would be killed without acting."
)
@click.option("--json-output", is_flag=True, help="Output results as JSON.")
def ceo_cleanup(task: str, *, dry_run: bool, json_output: bool) -> None:
    """Kill idle child bash processes of a Copilot pane to reclaim fds.

    Copilot CLI may leak idle bash subshells during long sessions.
    This command safely kills them — Copilot spawns fresh shells as needed.
    """
    from duo.transport import get_pane_pid

    t = _load_task_or_fail(task)
    pid = get_pane_pid(t.pane_label)
    if pid is None:
        raise DuoUserError(
            f"Cannot determine PID for pane '{t.pane_label}'",
            fix=f"Is the session alive? Check with 'duo status {task}'.",
        )

    children = _find_idle_children(pid)
    if not children:
        if json_output:
            click.echo(json.dumps({"killed": [], "total": 0}))
        else:
            click.echo("No idle child processes found.")
        return

    killed: list[int] = []
    for child_pid in children:
        if dry_run:
            killed.append(child_pid)
            continue
        try:
            os.kill(child_pid, 9)
            killed.append(child_pid)
        except OSError:
            pass

    if json_output:
        click.echo(
            json.dumps({"killed": killed, "total": len(killed), "dry_run": dry_run})
        )
    else:
        verb = "Would kill" if dry_run else "Killed"
        click.echo(f"{verb} {len(killed)} idle child process(es): {killed}")


def _find_idle_children(parent_pid: int) -> list[int]:
    """Return PIDs of idle bash children of *parent_pid*."""
    try:
        proc = subprocess.run(
            ["pgrep", "-P", str(parent_pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            return []
        child_pids = [
            int(line.strip())
            for line in proc.stdout.strip().splitlines()
            if line.strip()
        ]
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []

    idle: list[int] = []
    for cpid in child_pids:
        try:
            ps_proc = subprocess.run(
                ["ps", "-o", "comm=,state=", "-p", str(cpid)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if ps_proc.returncode != 0:
                continue
            out = ps_proc.stdout.strip()
            # idle bash: command is bash/sh AND state starts with S (sleeping)
            parts = out.split()
            if len(parts) >= 2:
                comm = parts[0].lower()
                state = parts[1]
                if ("bash" in comm or comm == "sh") and state.startswith("S"):
                    idle.append(cpid)
        except (OSError, subprocess.TimeoutExpired):
            continue
    return idle


@main.command("ceo-restart")
@click.argument("task")
@click.option(
    "--timeout",
    default=60,
    type=float,
    help="Seconds to wait for Copilot restart (default: 60).",
)
def ceo_restart(task: str, timeout: float) -> None:
    """Restart the Copilot process in a task's pane to reclaim leaked resources.

    Sends exit to the current Copilot process, waits for the shell prompt,
    then re-launches Copilot CLI with the same model. Reports health
    metrics before and after restart.

    This is the recommended way to handle degraded sessions (high fd/kqueue
    count) without losing the tmux pane or worktree context.
    """
    import time as _t

    from duo.commander import _get_copilot_model
    from duo.transport import (
        cancel_current,
        get_pane_pid,
        read_pane,
        send_shell_command,
        wait_for_idle,
    )

    _validate_task_name(task)
    t = _load_task_or_fail(task)

    # --- Pre-restart health snapshot ---
    old_pid = get_pane_pid(t.pane_label)
    old_fds = _get_pid_fd_count(old_pid) if old_pid else -1
    old_kqueue = _get_pid_kqueue_count(old_pid) if old_pid else -1

    click.echo(f"Restarting Copilot for '{task}'...")
    if old_pid:
        click.echo(f"  Pre-restart: PID={old_pid}, fds={old_fds}, kqueue={old_kqueue}")

    # --- Step 1: Clean idle children first ---
    if old_pid:
        children = _find_idle_children(old_pid)
        if children:
            for cpid in children:
                with contextlib.suppress(OSError):
                    os.kill(cpid, 9)
            click.echo(f"  Cleaned {len(children)} idle child process(es)")

    # --- Step 2: Exit Copilot gracefully ---
    try:
        cancel_current(t.pane_label)
        _t.sleep(1.0)
    except (RuntimeError, subprocess.CalledProcessError, OSError):
        pass

    try:
        send_shell_command(t.pane_label, "exit")
        _t.sleep(2.0)
    except (RuntimeError, subprocess.CalledProcessError, OSError):
        pass

    # --- Step 3: Wait for shell prompt ---
    deadline = _t.monotonic() + timeout
    shell_ready = False
    while _t.monotonic() < deadline:
        try:
            content = read_pane(t.pane_label, 5)
            # Look for shell prompt ($ or %)
            last_line = (
                content.strip().splitlines()[-1].strip() if content.strip() else ""
            )
            if last_line.endswith(("$", "%", "#")):
                shell_ready = True
                break
        except (RuntimeError, OSError):
            pass
        _t.sleep(1.0)

    if not shell_ready:
        raise DuoUserError(
            "Copilot did not exit within timeout",
            fix="Manually exit Copilot in the pane, then re-run this command.",
        )

    click.echo("  Copilot exited. Re-launching...")

    # --- Step 4: Re-launch Copilot CLI ---
    model = _get_copilot_model()
    copilot_cmd = f"copilot --model {model} --yolo"
    try:
        send_shell_command(t.pane_label, copilot_cmd)
    except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
        raise DuoUserError(
            f"Failed to re-launch Copilot: {exc}",
            fix="Manually start Copilot in the pane.",
        ) from exc

    # --- Step 5: Wait for Copilot to start ---
    click.echo("  Waiting for Copilot to start...")
    idle = wait_for_idle(t.pane_label, timeout=timeout, poll_interval=2.0)
    if not idle:
        click.echo("  Warning: Copilot may not be fully started yet.")

    # --- Step 6: Send /allow-all ---
    try:
        send_shell_command(t.pane_label, "/allow-all")
        wait_for_idle(t.pane_label, timeout=15.0, poll_interval=1.0)
        click.echo("  Sent /allow-all")
    except (RuntimeError, subprocess.CalledProcessError, OSError):
        click.echo("  Warning: /allow-all may not have been sent.")

    # --- Step 7: Post-restart health ---
    _t.sleep(1.0)
    new_pid = get_pane_pid(t.pane_label)
    new_fds = _get_pid_fd_count(new_pid) if new_pid else -1
    new_kqueue = _get_pid_kqueue_count(new_pid) if new_pid else -1

    if new_pid:
        click.echo(f"  Post-restart: PID={new_pid}, fds={new_fds}, kqueue={new_kqueue}")
    click.echo("Restart complete. Copilot is ready at the ❯ prompt.")

    # Remove restart-recommended signal if present
    signal_path = t.dir / "restart-recommended"
    signal_path.unlink(missing_ok=True)


@main.command("ceo-dispatch")
@click.argument("task")
@click.option(
    "--timeout",
    default=30,
    type=float,
    help="Seconds to wait for dialog (default: 30).",
)
@click.option(
    "--policy",
    type=click.Path(exists=True),
    default=None,
    help="YAML policy file.",
)
@click.option("--dry-run", is_flag=True, help="Show decision without executing.")
def ceo_dispatch(
    task: str,
    timeout: float,
    policy: str | None,
    *,
    dry_run: bool,
) -> None:
    """Single-shot policy-driven dialog handler.

    Wait for a dialog, classify, decide per policy, execute.
    Exit 0 = success, 1 = deferred, 2 = timeout/no dialog.
    """
    from duo.transport import (
        approve_permission,
        get_dialog_kind,
        is_in_dialog,
        is_permission_dialog,
        read_pane,
        select_dialog_option,
        send_text_dialog_message,
    )

    t = _load_task_or_fail(task)
    session_id = os.environ.get("DUO_CEO_SESSION")

    deadline = time.monotonic() + timeout
    while not is_in_dialog(t.pane_label):
        if time.monotonic() >= deadline:
            click.echo("Timeout: no dialog detected.")
            sys.exit(2)
        time.sleep(0.5)

    kind = get_dialog_kind(t.pane_label)
    content = read_pane(t.pane_label)
    is_perm = is_permission_dialog(t.pane_label)

    if session_id:
        from duo.ceo_log import log_dialog_detected

        detected_kind = "permission" if is_perm else kind.value
        log_dialog_detected(session_id, task, content, detected_kind)

    if is_perm:
        action, value = "approve", ""
    else:
        action, value = _resolve_dispatch_action(kind, content, policy)

    if dry_run:
        kind_label = "permission" if is_perm else kind.value
        click.echo(f"[dry-run] kind={kind_label} action={action} value={value}")
        if action == "defer":
            sys.exit(1)
        return

    if action == "approve" and is_perm:
        approve_permission(t.pane_label)
        click.echo(f"Dispatched: approved permission for '{task}'.")
    elif action == "select_last":
        lines = content.splitlines()
        last_num = "1"
        for line in lines:
            stripped = line.strip()
            if stripped and stripped[0].isdigit() and "." in stripped[:4]:
                last_num = stripped.split(".")[0]
        select_dialog_option(t.pane_label, last_num)
        click.echo(f"Dispatched: selected option {last_num} for '{task}'.")
    elif action.startswith("select"):
        num = value or "1"
        select_dialog_option(t.pane_label, num)
        click.echo(f"Dispatched: selected option {num} for '{task}'.")
    elif action == "type":
        send_text_dialog_message(t.pane_label, value)
        click.echo(f"Dispatched: typed '{value[:40]}' for '{task}'.")
    elif action == "defer":
        click.echo(f"Deferred: {kind.value} dialog for '{task}'.")
        if session_id:
            from duo.ceo_log import log_decision

            log_decision(
                session_id,
                task,
                "dispatch-defer",
                "policy deferred",
                elapsed_ms=0,
            )
        sys.exit(1)
    else:
        click.echo(f"Deferred: unknown action '{action}'.")
        sys.exit(1)

    if session_id and action != "defer":
        from duo.ceo_log import log_decision

        log_decision(
            session_id,
            task,
            f"dispatch-{action}",
            value or action,
            elapsed_ms=0,
        )


def _resolve_dispatch_action(
    kind: Any,
    content: str,
    policy_path: str | None,
) -> tuple[str, str]:
    """Resolve action from policy or smart defaults."""
    if policy_path:
        action, value = _match_policy(kind, content, policy_path)
        if action:
            return action, value

    from duo.transport import DialogKind as DK

    if kind == DK.OPTION:
        auto, _ = _is_auto_selectable(content)
        if auto:
            return "select", "1"
        return "defer", ""
    if kind == DK.TEXT:
        return "defer", ""
    return "defer", ""


def _match_policy(
    kind: Any,
    content: str,
    policy_path: str,
) -> tuple[str, str]:
    """Match dialog against YAML policy rules."""
    try:
        import yaml

        data = yaml.safe_load(Path(policy_path).read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError, ValueError, ImportError):
        click.echo(f"Warning: could not load policy {policy_path}")
        return "", ""

    if not isinstance(data, dict):
        return "", ""

    rules = data.get("rules", [])
    if not isinstance(rules, list):
        return "", ""

    content_lower = content.lower()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        match_kind = rule.get("match", "").lower()
        if match_kind and match_kind != kind.value.lower():
            continue
        contains = rule.get("contains", "")
        if contains and contains.lower() not in content_lower:
            continue
        action = rule.get("action", "defer")
        value = str(rule.get("value", ""))
        return action, value

    default = data.get("default", "defer")
    return str(default), ""


def _metrics_load_events(
    session_id: str | None,
    since: str | None,
) -> tuple[list[dict[str, Any]], int, list[float]]:
    """Load and filter CEO session events.

    Returns (all_events, session_count, session_durations).
    """
    from datetime import datetime

    from duo.ceo_log import list_sessions, replay_session

    session_ids = [session_id] if session_id else list_sessions()
    all_events: list[dict[str, Any]] = []
    session_count = 0
    session_durations: list[float] = []

    for sid in session_ids:
        events = replay_session(sid)
        if since:
            events = [e for e in events if e.get("ts", "") >= since]
        if not events:
            continue
        session_count += 1
        all_events.extend(events)
        timestamps = sorted(e.get("ts", "") for e in events if e.get("ts"))
        if len(timestamps) >= 2:
            try:
                t0 = datetime.fromisoformat(timestamps[0])
                t1 = datetime.fromisoformat(timestamps[-1])
                session_durations.append((t1 - t0).total_seconds())
            except (ValueError, TypeError):
                pass

    return all_events, session_count, session_durations


def _metrics_aggregate(
    all_events: list[dict[str, Any]],
    session_count: int,
    session_durations: list[float],
) -> dict[str, Any]:
    """Compute aggregate metrics from CEO session events."""
    dialogs = [e for e in all_events if e.get("event") == "dialog_detected"]
    decisions = [e for e in all_events if e.get("event") == "decision"]
    total_dialogs = len(dialogs)
    total_decisions = len(decisions)

    dialog_kinds: dict[str, int] = {}
    for d in dialogs:
        kind = d.get("dialog_kind", "unknown").upper()
        dialog_kinds[kind] = dialog_kinds.get(kind, 0) + 1

    decision_types: dict[str, int] = {}
    for d in decisions:
        dt = d.get("decision_type", "unknown")
        decision_types[dt] = decision_types.get(dt, 0) + 1

    approved_count = sum(v for k, v in decision_types.items() if k.startswith("approv"))
    approval_rate = (
        (approved_count / total_decisions * 100) if total_decisions > 0 else 0.0
    )
    avg_decisions = round(total_decisions / session_count, 1) if session_count else 0.0
    avg_duration_s = (
        sum(session_durations) / len(session_durations) if session_durations else 0.0
    )

    content_counts: dict[str, int] = {}
    for d in dialogs:
        c = d.get("content", "")[:100]
        if c:
            content_counts[c] = content_counts.get(c, 0) + 1
    top_patterns = sorted(content_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    return {
        "sessions": session_count,
        "total_dialogs": total_dialogs,
        "total_decisions": total_decisions,
        "approval_rate": round(approval_rate, 1),
        "dialog_kinds": dialog_kinds,
        "decision_types": decision_types,
        "avg_decisions_per_session": avg_decisions,
        "avg_session_duration_s": round(avg_duration_s, 1),
        "top_dialog_patterns": [{"content": c, "count": n} for c, n in top_patterns],
    }


def _metrics_format_text(
    metrics: dict[str, Any],
    session_id: str | None,
) -> None:
    """Emit human-readable metrics to stdout."""
    scope = f"session {session_id}" if session_id else "all sessions"
    click.echo(f"CEO Metrics ({scope})")
    click.echo("\u2500" * 25)
    click.echo(f"Sessions:    {metrics['sessions']}")
    click.echo(f"Dialogs:     {metrics['total_dialogs']}")
    click.echo(f"Decisions:   {metrics['total_decisions']}")
    click.echo(f"Approval rate: {metrics['approval_rate']:.1f}%")
    click.echo()

    dialog_kinds = metrics.get("dialog_kinds", {})
    total_dialogs = metrics["total_dialogs"]
    if dialog_kinds:
        click.echo("Dialog kinds:")
        for kind, count in sorted(
            dialog_kinds.items(), key=lambda x: x[1], reverse=True
        ):
            pct = count / total_dialogs * 100 if total_dialogs > 0 else 0.0
            click.echo(f"  {kind:12s} {count:4d} ({pct:.1f}%)")
        click.echo()

    decision_types = metrics.get("decision_types", {})
    total_decisions = metrics["total_decisions"]
    if decision_types:
        click.echo("Decision types:")
        for dt, count in sorted(
            decision_types.items(), key=lambda x: x[1], reverse=True
        ):
            pct = count / total_decisions * 100 if total_decisions > 0 else 0.0
            click.echo(f"  {dt:12s} {count:4d} ({pct:.1f}%)")
        click.echo()

    click.echo(f"Avg decisions/session: {metrics['avg_decisions_per_session']}")
    avg_s = metrics["avg_session_duration_s"]
    mins = int(avg_s) // 60
    secs = int(avg_s) % 60
    click.echo(f"Avg session duration:  {mins}m {secs:02d}s")

    top_patterns = metrics.get("top_dialog_patterns", [])
    if top_patterns:
        click.echo()
        click.echo("Top dialog patterns:")
        for item in top_patterns:
            click.echo(f"  [{item['count']}x] {item['content']}")


@main.command("ceo-metrics")
@click.option("--session", "session_id", default=None, help="Single session ID.")
@click.option(
    "--all", "all_sessions", is_flag=True, default=True, help="All sessions (default)."
)
@click.option("--json-output", is_flag=True, help="Output as JSON.")
@click.option(
    "--since", default=None, help="ISO datetime filter (e.g. 2025-01-01T00:00:00)."
)
def ceo_metrics_cmd(
    *,
    session_id: str | None,
    all_sessions: bool,
    json_output: bool,
    since: str | None,
) -> None:
    """Aggregate analytics across CEO sessions."""
    all_events, session_count, durations = _metrics_load_events(session_id, since)

    if session_count == 0:
        if json_output:
            click.echo(json.dumps({"error": "No CEO sessions found."}))
        else:
            click.echo("No CEO sessions found.")
        return

    metrics = _metrics_aggregate(all_events, session_count, durations)

    if json_output:
        click.echo(json.dumps(metrics, indent=2))
    else:
        _metrics_format_text(metrics, session_id)


@main.command()
@click.argument("name")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json", "jsonl"]),
    default="text",
    help="Output format",
)
@click.option(
    "-o",
    "--output",
    "outfile",
    type=click.Path(),
    help="Write to file instead of stdout",
)
def export(name: str, fmt: str, outfile: str | None) -> None:
    """Export task report (events, files changed, summary)."""
    task = _load_task_or_fail(name)

    if fmt == "jsonl":
        events = read_jsonl(task.journal_path) if task.journal_path.exists() else []
        lines = []
        for ev in events:
            line = {
                "task_id": task.id,
                "timestamp": ev.get("ts", ""),
                "event": ev.get("event", ""),
                "data": ev.get("data", {}),
            }
            lines.append(json.dumps(line, ensure_ascii=False))
        output = "\n".join(lines)
        if outfile:
            atomic_write_text(Path(outfile), output + "\n" if output else "")
            click.echo(f"Report written to {outfile}")
        else:
            for l in lines:
                click.echo(l)
        return

    output = _export_as_json(task) if fmt == "json" else _export_as_text(task)

    if outfile:
        atomic_write_text(Path(outfile), output + "\n")
        click.echo(f"Report written to {outfile}")
    else:
        click.echo(output)


def _parse_age(age_str: str) -> int:
    """Parse age string like '7d', '24h', '30m' into seconds."""
    import re

    match = re.match(r"^(\d+)([dhms])$", age_str)
    if not match:
        raise click.UsageError("invalid age format. Use: 7d, 24h, 30m, 3600s")
    value, unit = int(match.group(1)), match.group(2)
    if value == 0:
        raise click.UsageError("age value must be > 0")
    seconds = value * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    if seconds > _MAX_AGE_SECONDS:
        raise click.UsageError(f"age '{age_str}' too large (max ~1000 years).")
    return seconds


@main.command()
@click.option(
    "--all",
    "clean_all",
    is_flag=True,
    help="Clean all finished tasks (completed + failed)",
)
@click.option("--force", is_flag=True, help="Skip confirmation")
@click.option("--keep-journal", is_flag=True, help="Keep journal files")
@click.option(
    "--age",
    type=str,
    default=None,
    help="Only clean tasks older than duration (e.g., 7d, 24h, 30m)",
)
@click.option(
    "--corrupted", is_flag=True, help="List and purge quarantined corrupted tasks"
)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def cleanup(
    clean_all: bool,
    force: bool,
    keep_journal: bool,
    age: str | None,
    corrupted: bool,
    *,
    as_json: bool = False,
) -> None:
    """Clean up completed and failed tasks."""
    import shutil

    if corrupted:
        from duo.protocol import list_corrupted

        items = list_corrupted()
        if not items:
            if as_json:
                click.echo(json.dumps({"cleaned": 0, "tasks": [], "corrupted": True}))
            else:
                click.echo("No quarantined tasks.")
            return
        if not as_json:
            click.echo(f"Quarantined tasks ({len(items)}):")
            for p in items:
                click.echo(f"  {p.name}")
        if not force and not as_json:
            click.confirm("Delete all quarantined tasks?", abort=True)
        for p in items:
            shutil.rmtree(p, ignore_errors=True)
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "cleaned": len(items),
                        "tasks": [p.name for p in items],
                        "corrupted": True,
                    }
                )
            )
        else:
            click.echo(f"Purged {len(items)} quarantined task(s).")
        return

    tasks = list_tasks()

    if clean_all:
        targets = [
            t
            for t in tasks
            if t.status
            in (
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.ESCALATED,
                TaskStatus.BLOCKED,
            )
        ]
    else:
        targets = [t for t in tasks if t.status == TaskStatus.COMPLETED]

    if age:
        max_age = _parse_age(age)
        from duo.poller import age as task_age

        targets = [t for t in targets if task_age(t.created_at) > max_age]

    if not targets:
        if as_json:
            click.echo(json.dumps({"cleaned": 0, "tasks": []}))
        else:
            click.echo("No tasks to clean up.")
        return

    if not as_json:
        click.echo(f"Tasks to clean up ({len(targets)}):")
        for t in targets:
            click.echo(f"  {t.id} ({t.status.value})")

    if not force and not as_json:
        click.confirm("Proceed?", abort=True)

    cleaned = 0
    cleaned_ids: list[str] = []
    for task in targets:
        if os.path.exists(task.worktree):
            r = _run_git(
                ["worktree", "remove", "--force", task.worktree], cwd=".", check=False
            )
            if r.returncode != 0 and not as_json:
                click.echo(
                    f"  Warning: worktree removal failed: {r.stderr.strip()}", err=True
                )

        r = _run_git(["branch", "-D", task.branch], cwd=".", check=False)
        if r.returncode != 0 and not as_json:
            click.echo(
                f"  Warning: branch deletion failed: {r.stderr.strip()}", err=True
            )

        if keep_journal:
            for item in task.dir.iterdir():
                if item.name != "journal.jsonl":
                    if item.is_symlink():
                        item.unlink()
                    elif item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
        else:
            shutil.rmtree(task.dir)

        cleaned += 1
        cleaned_ids.append(task.id)
        if not as_json:
            click.echo(f"  ✓ {task.id}")

    if as_json:
        click.echo(json.dumps({"cleaned": cleaned, "tasks": cleaned_ids}))
    else:
        click.echo(f"\nCleaned {cleaned} tasks.")


@main.command("diff")
@click.argument("name")
@click.option("--stat", "show_stat", is_flag=True, help="Show diffstat summary only")
@click.option(
    "--name-only", "name_only", is_flag=True, help="List changed file names only"
)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def diff_cmd(
    name: str, *, show_stat: bool, name_only: bool, as_json: bool = False
) -> None:
    """Show git diff for a task's worktree changes."""
    task = _load_task_or_fail(name)

    if not Path(task.worktree).exists():
        raise DuoUserError(
            f"worktree '{task.worktree}' not found",
            fix="It may have been cleaned up. Run 'duo cleanup' to remove stale tasks.",
        )

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
# duo think — pre-start brainstorming with Claude Code pane
# ---------------------------------------------------------------------------


@main.command("think")
@click.argument("name")
@click.option(
    "--ask", "ask_text", default=None, help="One-shot question (CEO primary path)"
)
@click.option(
    "--finalize", is_flag=True, help="Generate plan.md from the thinking session"
)
@click.option("--close", "do_close", is_flag=True, help="Close pane, keep files")
@click.option("--delete", "do_delete", is_flag=True, help="Close pane + delete files")
def think(
    name: str,
    ask_text: str | None,
    finalize: bool,
    do_close: bool,
    do_delete: bool,
) -> None:
    """Pre-start brainstorming with Claude Code.

    NAME is the thinking session name, or 'list' to show all sessions.
    """
    # Special case: "duo think list"
    if name == "list":
        _think_list_all()
        return

    from duo.thinking import (
        ensure_pane,
        thinking_dir,
    )

    _validate_task_name(name)

    if do_delete:
        _think_delete(name)
        return
    if do_close:
        _think_close(name)
        return
    if finalize:
        _think_finalize(name)
        return
    if ask_text is not None:
        _think_ask(name, ask_text)
        return

    # Bare `duo think <name>` — info mode
    label = ensure_pane(name)
    tdir = thinking_dir(name)
    has_plan = (tdir / "plan.md").exists()
    click.echo(f"Thinking session: {name}")
    click.echo(f"  Pane: {label} (alive)")
    click.echo(f"  Dir:  {tdir}")
    click.echo(f"  Plan: {'finalized' if has_plan else 'not yet finalized'}")
    click.echo()
    click.echo(f"To brainstorm directly, switch to pane '{label}'.")
    click.echo(f'To send a one-shot message: duo think {name} --ask "..."')
    click.echo(f"To finalize: duo think {name} --finalize")


def _think_ask(name: str, text: str) -> None:
    """Handle ``duo think <name> --ask "..."``."""

    from duo.thinking import (
        append_session_log,
        ensure_pane,
        extract_response,
        wait_for_response_stable,
    )
    from duo.transport import read_pane, send_keys, type_text

    label = ensure_pane(name)

    content_before = read_pane(label, 200)
    type_text(label, text)
    send_keys(label, "Enter")

    result = wait_for_response_stable(label)

    if result == "dialog":
        raise DuoUserError(
            "Claude Code asked a question in the pane",
            fix=f"Switch to pane '{label}' to answer, then retry.",
        )
    if result == "timeout":
        raise DuoUserError(
            "Thinking pane not responding after timeout",
            fix=f"Try: duo think {name} --close",
        )

    content_after = read_pane(label, 200)
    response = extract_response(content_before, content_after, text)
    append_session_log(name, text, response)
    click.echo(response)


def _think_finalize(name: str) -> None:
    """Handle ``duo think <name> --finalize``."""
    import time as _time

    from duo.thinking import ensure_pane, thinking_dir, wait_for_response_stable
    from duo.transport import send_keys, type_text

    label = ensure_pane(name)
    tdir = thinking_dir(name)

    # Ensure pane is idle before sending finalize
    result = wait_for_response_stable(label, timeout=30)
    if result == "dialog":
        raise DuoUserError(
            "Pane is in a dialog",
            fix=f"Switch to pane '{label}' to handle it first.",
        )
    if result == "timeout":
        raise DuoUserError(
            "Pane unresponsive",
            fix=f"Try: duo think {name} --close",
        )

    finalize_msg = (
        f"Please distill our entire conversation into a plan document using the "
        f"template at {tdir}/plan-template.md. Write the result to {tdir}/plan.md. "
        f"Be concrete and specific — this plan will be the initial prompt for a "
        f"code-generating agent."
    )
    type_text(label, finalize_msg)
    send_keys(label, "Enter")

    # Poll for plan.md
    plan_path = tdir / "plan.md"
    deadline = _time.time() + 60
    last_size = -1
    stable_since: float | None = None

    while _time.time() < deadline:
        if plan_path.exists():
            size = plan_path.stat().st_size
            if size == last_size and size > 0:
                if stable_since is not None and _time.time() - stable_since > 3.0:
                    break
            else:
                last_size = size
                stable_since = _time.time()
        _time.sleep(1.0)
    else:
        raise DuoUserError(
            "Claude Code didn't produce plan.md within 60s",
            fix="Check the thinking pane manually, or run --finalize again.",
        )

    click.echo(f"Plan written to {plan_path}")
    click.echo("Review it, then run:")
    click.echo(f"  duo start {name} --from-thinking")


def _think_close(name: str) -> None:
    """Handle ``duo think <name> --close``."""
    from duo.thinking import close_pane, thinking_dir

    tdir = thinking_dir(name)
    if not tdir.exists():
        raise DuoUserError(
            f"No thinking session '{name}' found",
            fix="Run 'duo think --list' to see available sessions.",
        )
    if close_pane(name):
        click.echo(f"Closed pane for '{name}'. Files preserved in {tdir}")
    else:
        click.echo(f"No active pane for '{name}'. Files preserved in {tdir}")


def _think_delete(name: str) -> None:
    """Handle ``duo think <name> --delete``."""
    import shutil

    from duo.thinking import close_pane, thinking_dir

    tdir = thinking_dir(name)
    if not tdir.exists():
        raise DuoUserError(
            f"No thinking session '{name}' found",
            fix="Run 'duo think --list' to see available sessions.",
        )

    if not click.confirm(f"Delete thinking session '{name}'? This cannot be undone."):
        click.echo("Aborted.")
        return

    close_pane(name)
    shutil.rmtree(tdir)
    click.echo(f"Deleted thinking session '{name}'.")


def _think_list_all() -> None:
    """Handle ``duo think list``."""
    from duo.thinking import list_sessions

    sessions = list_sessions()
    if not sessions:
        click.echo("No thinking sessions.")
        return

    click.echo(f"{'NAME':<20} {'PANE':<8} {'STATUS':<12} FILES")
    for s in sessions:
        click.echo(f"{s['name']:<20} {s['pane']:<8} {s['status']:<12} {s['files']}")


# ---------------------------------------------------------------------------
# bench
# ---------------------------------------------------------------------------


def _bench_dialog_detection(iterations: int) -> dict[str, Any]:
    """Benchmark dialog detection functions."""
    from duo.transport import detect_dialog_kind, is_at_main_prompt

    # Realistic pane content samples
    option_dialog = (
        "╭─────────────────────────────────────────╮\n"
        "│ How would you like to proceed?          │\n"
        "│                                         │\n"
        "│ ❯ 1. Create a new file                  │\n"
        "│   2. Modify existing file               │\n"
        "│   3. Delete file                        │\n"
        "╰─────────────────────────────────────────╯\n"
    )
    text_dialog = (
        "╭─────────────────────────────────────────╮\n"
        "│ Type your answer below:                 │\n"
        "│                                         │\n"
        "│ Enter to submit                         │\n"
        "╰─────────────────────────────────────────╯\n"
    )
    main_prompt = (
        "─────────────────────────────────────\n"
        "  Remaining reqs: 42\n"
        "❯ Type @ to mention files\n"
    )
    spinner_content = "◉ Processing your request...\n  Working on file changes\n❯\n"

    samples: dict[str, tuple[str, str]] = {
        "option_dialog_detect": (option_dialog, "detect_dialog_kind"),
        "text_dialog_detect": (text_dialog, "detect_dialog_kind"),
        "main_prompt_detect": (main_prompt, "is_at_main_prompt"),
        "spinner_detect": (spinner_content, "is_at_main_prompt"),
    }

    results: dict[str, dict[str, float]] = {}
    total_start = time.perf_counter_ns()

    for name, (content, func_name) in samples.items():
        func = (
            detect_dialog_kind
            if func_name == "detect_dialog_kind"
            else is_at_main_prompt
        )
        timings: list[int] = []
        for _ in range(iterations):
            t0 = time.perf_counter_ns()
            func(content)
            t1 = time.perf_counter_ns()
            timings.append(t1 - t0)

        timings.sort()
        avg_ns = statistics.mean(timings)
        p99_idx = max(0, int(len(timings) * 0.99) - 1)
        p99_ns = timings[p99_idx]
        total_ns = sum(timings)
        ops = iterations / (total_ns / 1_000_000_000) if total_ns > 0 else 0.0

        results[name] = {
            "ops_per_sec": round(ops, 1),
            "avg_us": round(avg_ns / 1_000, 2),
            "p99_us": round(p99_ns / 1_000, 2),
        }

    total_end = time.perf_counter_ns()
    return {
        "suite": "dialog-detection",
        "iterations": iterations,
        "results": results,
        "total_time_sec": round((total_end - total_start) / 1_000_000_000, 4),
    }


def _bench_file_protocol(iterations: int) -> dict[str, Any]:
    """Benchmark JSON file read/write performance."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="duo-bench-"))
    sample_data: dict[str, Any] = {
        "id": "bench-task",
        "description": "Benchmark task for performance testing",
        "status": "running",
        "current_step": 1,
        "subtasks": [
            {"step_id": 1, "description": "step one", "target_files": ["a.py"]}
        ],
    }
    json_bytes = len(json.dumps(sample_data, indent=2).encode())

    results: dict[str, dict[str, float]] = {}
    total_start = time.perf_counter_ns()

    # Benchmark write_json
    write_timings: list[int] = []
    for i in range(iterations):
        p = tmp_dir / f"bench-{i}.json"
        t0 = time.perf_counter_ns()
        write_json(p, sample_data)
        t1 = time.perf_counter_ns()
        write_timings.append(t1 - t0)

    write_timings.sort()
    avg_ns = statistics.mean(write_timings)
    p99_idx = max(0, int(len(write_timings) * 0.99) - 1)
    total_ns = sum(write_timings)
    w_ops = iterations / (total_ns / 1_000_000_000) if total_ns > 0 else 0.0
    w_bps = w_ops * json_bytes

    results["write_json"] = {
        "ops_per_sec": round(w_ops, 1),
        "avg_us": round(avg_ns / 1_000, 2),
        "p99_us": round(write_timings[p99_idx] / 1_000, 2),
        "bytes_per_sec": round(w_bps, 1),
    }

    # Benchmark read_json
    read_timings: list[int] = []
    target = tmp_dir / "bench-0.json"
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        read_json(target)
        t1 = time.perf_counter_ns()
        read_timings.append(t1 - t0)

    read_timings.sort()
    avg_ns = statistics.mean(read_timings)
    p99_idx = max(0, int(len(read_timings) * 0.99) - 1)
    total_ns = sum(read_timings)
    r_ops = iterations / (total_ns / 1_000_000_000) if total_ns > 0 else 0.0
    r_bps = r_ops * json_bytes

    results["read_json"] = {
        "ops_per_sec": round(r_ops, 1),
        "avg_us": round(avg_ns / 1_000, 2),
        "p99_us": round(read_timings[p99_idx] / 1_000, 2),
        "bytes_per_sec": round(r_bps, 1),
    }

    total_end = time.perf_counter_ns()

    # Cleanup
    shutil.rmtree(tmp_dir, ignore_errors=True)

    return {
        "suite": "file-protocol",
        "iterations": iterations,
        "results": results,
        "total_time_sec": round((total_end - total_start) / 1_000_000_000, 4),
    }


def _bench_journal_append(iterations: int) -> dict[str, Any]:
    """Benchmark journal append and read performance."""
    from duo.protocol import append_event

    tmp_dir = Path(tempfile.mkdtemp(prefix="duo-bench-journal-"))
    # Temporarily override TASKS_DIR for the benchmark
    import duo.protocol

    original_tasks_dir = duo.protocol.TASKS_DIR
    duo.protocol.TASKS_DIR = tmp_dir / "tasks"
    duo.protocol.TASKS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        task = create_task(
            task_id="bench-journal",
            description="Journal benchmark task",
            worktree="/fake/bench",
            branch="duo/bench-journal",
            base_commit="abc123bench",
            subtasks=[
                Subtask(
                    step_id=1,
                    description="bench",
                    target_files=[],
                    writable_paths=["*"],
                )
            ],
        )

        results: dict[str, dict[str, float]] = {}
        total_start = time.perf_counter_ns()

        # Benchmark append_event
        append_timings: list[int] = []
        for i in range(iterations):
            t0 = time.perf_counter_ns()
            append_event(task, "bench_event", {"iteration": i, "data": "x" * 50})
            t1 = time.perf_counter_ns()
            append_timings.append(t1 - t0)

        append_timings.sort()
        avg_ns = statistics.mean(append_timings)
        p99_idx = max(0, int(len(append_timings) * 0.99) - 1)
        total_ns = sum(append_timings)
        a_ops = iterations / (total_ns / 1_000_000_000) if total_ns > 0 else 0.0

        results["append_event"] = {
            "ops_per_sec": round(a_ops, 1),
            "avg_us": round(avg_ns / 1_000, 2),
            "p99_us": round(append_timings[p99_idx] / 1_000, 2),
        }

        # Benchmark read_jsonl (reading back all events)
        journal_path = task.journal_path
        read_timings: list[int] = []
        for _ in range(iterations):
            t0 = time.perf_counter_ns()
            events = read_jsonl(journal_path)
            t1 = time.perf_counter_ns()
            read_timings.append(t1 - t0)

        read_timings.sort()
        avg_ns = statistics.mean(read_timings)
        p99_idx = max(0, int(len(read_timings) * 0.99) - 1)
        total_ns = sum(read_timings)
        r_ops = iterations / (total_ns / 1_000_000_000) if total_ns > 0 else 0.0
        event_count = len(events)
        events_per_sec = r_ops * event_count

        results["read_jsonl"] = {
            "ops_per_sec": round(r_ops, 1),
            "avg_us": round(avg_ns / 1_000, 2),
            "p99_us": round(read_timings[p99_idx] / 1_000, 2),
            "events_per_sec": round(events_per_sec, 1),
        }

        total_end = time.perf_counter_ns()

        return {
            "suite": "journal-append",
            "iterations": iterations,
            "results": results,
            "total_time_sec": round((total_end - total_start) / 1_000_000_000, 4),
        }
    finally:
        duo.protocol.TASKS_DIR = original_tasks_dir
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _compare_results(
    current: list[dict[str, Any]],
    baseline: list[dict[str, Any]],
) -> tuple[str, bool]:
    """Compare current results against a baseline.

    Returns (formatted output, has_regression) where has_regression is True
    if any metric regressed more than 20%.
    """
    baseline_map: dict[str, dict[str, dict[str, float]]] = {}
    for b in baseline:
        baseline_map[b["suite"]] = b["results"]

    lines: list[str] = []
    has_regression = False

    for cur in current:
        suite = cur["suite"]
        if suite not in baseline_map:
            continue
        for metric_name, cur_vals in cur["results"].items():
            if metric_name not in baseline_map[suite]:
                continue
            base_vals = baseline_map[suite][metric_name]
            for key in ("ops_per_sec",):
                if key not in cur_vals or key not in base_vals:
                    continue
                old_val = base_vals[key]
                new_val = cur_vals[key]
                if old_val == 0:
                    continue
                pct = ((new_val - old_val) / old_val) * 100
                sign = "+" if pct >= 0 else ""
                label = "ops/sec"
                status = "✓"
                if pct < -20:
                    status = "✗ REGRESSION"
                    has_regression = True
                elif pct < -10:
                    status = "⚠ WARNING"
                lines.append(
                    f"{suite} / {metric_name}:\n"
                    f"  {label}: {old_val:,.0f} → {new_val:,.0f} "
                    f"({sign}{pct:.1f}%) {status}"
                )

    return "\n".join(lines), has_regression


def _print_results(all_results: list[dict[str, Any]]) -> None:
    """Print human-readable benchmark results."""
    click.echo("Duo Performance Benchmark")
    click.echo("═" * 25)
    for suite_result in all_results:
        suite = suite_result["suite"]
        iters = suite_result["iterations"]
        click.echo(f"\nSuite: {suite} ({iters:,} iterations)")
        for name, metrics in suite_result["results"].items():
            ops = metrics["ops_per_sec"]
            avg = metrics["avg_us"]
            p99 = metrics["p99_us"]
            extra = ""
            if "bytes_per_sec" in metrics:
                bps = metrics["bytes_per_sec"]
                if bps >= 1_000_000:
                    extra = f"   {bps / 1_000_000:.1f} MB/s"
                else:
                    extra = f"   {bps / 1_000:.1f} KB/s"
            if "events_per_sec" in metrics:
                extra = f"   {metrics['events_per_sec']:,.0f} events/s"
            click.echo(
                f"  {name:<25} {ops:>10,.0f} ops/s   "
                f"{avg:>8,.1f} µs avg   {p99:>8,.1f} µs p99{extra}"
            )


@main.command()
@click.argument(
    "suite",
    type=click.Choice(
        ["dialog-detection", "file-protocol", "journal-append", "all"],
    ),
    default="all",
)
@click.option("--iterations", "-n", default=1000, help="Number of iterations.")
@click.option("--json-output", is_flag=True, help="Output as JSON.")
@click.option(
    "--baseline",
    type=click.Path(exists=True),
    help="Compare against baseline file.",
)
@click.option("--save", is_flag=True, help="Save results to ~/.duo/bench-results/.")
def bench(
    suite: str,
    iterations: int,
    json_output: bool,
    baseline: str | None,
    save: bool,
) -> None:
    """Run performance benchmarks."""
    from datetime import UTC, datetime

    runners: dict[str, Any] = {
        "dialog-detection": _bench_dialog_detection,
        "file-protocol": _bench_file_protocol,
        "journal-append": _bench_journal_append,
    }

    suites = list(runners.keys()) if suite == "all" else [suite]
    all_results: list[dict[str, Any]] = []
    for s in suites:
        result: dict[str, Any] = runners[s](iterations)
        all_results.append(result)

    if json_output:
        click.echo(json.dumps(all_results, indent=2))
    else:
        _print_results(all_results)

    if save:
        bench_dir = BENCH_DIR
        bench_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out_path = bench_dir / f"{ts}.json"
        out_path.write_text(json.dumps(all_results, indent=2) + "\n", encoding="utf-8")
        click.echo(f"\nResults saved to {out_path}")

    if baseline is not None:
        baseline_data: list[dict[str, Any]] = json.loads(
            Path(baseline).read_text(encoding="utf-8")
        )
        comparison, has_regression = _compare_results(all_results, baseline_data)
        if comparison:
            click.echo(f"\nBaseline comparison:\n{comparison}")
        if has_regression:
            raise SystemExit(1)
