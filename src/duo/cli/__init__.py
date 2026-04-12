"""CLI entry point — thin interface to commander."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import click

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
    DUO_DIR,  # used by test monkeypatching
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
)

_TERMINAL_STATES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ESCALATED}
)

__all__ = ["main"]

_COMMAND_SECTIONS: dict[str, list[str]] = {
    "Task Lifecycle": [
        "start",
        "go",
        "send",
        "stop",
        "status",
        "merge",
        "diff",
        "kill",
    ],
    "Thinking": ["think"],
    "Monitoring": ["list", "monitor", "watch", "dashboard", "logs", "inspect"],
    "Batch & Queue": ["batch", "queue"],
    "CEO Workflow": [
        "ceo-wait",
        "ceo-select",
        "ceo-approve",
        "ceo-status",
    ],
    "Recovery": ["recover", "resume", "retry"],
    "Data & Audit": ["audit", "cost", "cleanup", "events"],
    "Setup": ["init", "doctor", "config"],
    "Misc": ["version", "completion"],
}


_ALIASES: dict[str, str] = {
    "ls": "list",
    "st": "status",
    "log": "logs",
}


class _OrderedGroup(click.Group):
    """Click group that displays commands in categorized sections."""

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        """Resolve command name, supporting aliases."""
        rv = super().get_command(ctx, cmd_name)
        if rv is not None:
            return rv
        target = _ALIASES.get(cmd_name)
        if target is not None:
            return super().get_command(ctx, target)
        return None

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

        # Show aliases
        if _ALIASES:  # pragma: no branch — _ALIASES is a non-empty constant
            alias_rows = [(a, f"→ {t}") for a, t in sorted(_ALIASES.items())]
            with formatter.section("Aliases"):
                formatter.write_dl(alias_rows)


def _validate_task_name(name: str) -> None:
    """Validate that a task name contains only safe characters."""
    if len(name) > 63:
        raise click.BadParameter(
            f"Task name must be at most 63 characters, got {len(name)}"
        )
    if not re.match(r"^[a-zA-Z0-9_-]+\Z", name):
        suggested = re.sub(r"[^a-zA-Z0-9_-]", "-", name).strip("-")
        hint = f" Try: '{suggested}'" if suggested else ""
        raise click.BadParameter(
            f"Task name must contain only letters, numbers, dashes, underscores. Got: '{name}'.{hint}"
        )


def _fmt_ts(ts: str) -> str:
    """Extract HH:MM:SS from ISO timestamp, or return '?' if malformed."""
    try:
        return ts.split("T", 1)[1][:8] if "T" in ts else ts[:8]
    except (IndexError, TypeError, AttributeError):
        return "?"


def _fmt_age(created_at: str) -> str:
    """Format elapsed time since created_at as human-readable string."""
    try:
        from datetime import UTC, datetime

        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        delta = datetime.now(UTC) - created
        total_seconds = int(delta.total_seconds())
        if total_seconds < 0:
            return "0s"
        if total_seconds < 60:
            return f"{total_seconds}s"
        if total_seconds < 3600:
            return f"{total_seconds // 60}m"
        if total_seconds < 86400:
            return f"{total_seconds // 3600}h {(total_seconds % 3600) // 60}m"
        return f"{total_seconds // 86400}d {(total_seconds % 86400) // 3600}h"
    except (ValueError, TypeError, AttributeError):
        return "?"


_GIT_TIMEOUT = 30  # seconds for git subprocess calls
_TMUX_TIMEOUT = 10  # seconds for tmux kill/health operations
_MAX_AGE_SECONDS = 1000 * 365 * 86400  # ~1000 years upper bound


def _complete_task_names(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[click.shell_completion.CompletionItem]:
    from click.shell_completion import CompletionItem

    from duo.protocol import list_tasks

    try:
        tasks = list_tasks()
    except Exception:
        return []
    return [
        CompletionItem(t.id, help=t.status.value)
        for t in tasks
        if t.id.startswith(incomplete)
    ]


def _complete_thinking_names(
    _ctx: click.Context,
    _param: click.Parameter,
    incomplete: str,
) -> list[click.shell_completion.CompletionItem]:
    """Tab-complete thinking session names."""
    try:
        from click.shell_completion import CompletionItem

        from duo.thinking import THINKING_DIR

        if not THINKING_DIR.exists():
            return []
        return [
            CompletionItem(d.name)
            for d in sorted(THINKING_DIR.iterdir())
            if d.is_dir() and d.name.startswith(incomplete)
        ]
    except Exception:
        return []


def _complete_status_values(
    _ctx: click.Context,
    _param: click.Parameter,
    incomplete: str,
) -> list[click.shell_completion.CompletionItem]:
    """Tab-complete task status values."""
    try:
        from click.shell_completion import CompletionItem

        return [
            CompletionItem(s.value)
            for s in TaskStatus
            if s.value.startswith(incomplete)
        ]
    except Exception:
        return []


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


def _remove_worktree_and_branch(
    task: Task, *, cwd: str = ".", warn: bool = True
) -> tuple[bool, bool]:
    """Remove a task's git worktree and branch.

    Returns (worktree_removed, branch_deleted).
    """
    wt_removed = False
    if os.path.exists(task.worktree):
        r = _run_git(
            ["worktree", "remove", "--force", task.worktree], cwd=cwd, check=False
        )
        wt_removed = r.returncode == 0
        if not wt_removed and warn:
            click.echo(
                f"  Warning: worktree removal failed: {r.stderr.strip()}", err=True
            )
    r = _run_git(["branch", "-D", task.branch], cwd=cwd, check=False)
    branch_deleted = r.returncode == 0
    if not branch_deleted and warn:
        click.echo(f"  Warning: branch deletion failed: {r.stderr.strip()}", err=True)
    return wt_removed, branch_deleted


def _find_main_worktree(task_worktree: str) -> str | None:
    """Find the main (non-duo) worktree from a git repo.

    Returns the main worktree path, or None if not found.
    """
    cwd = task_worktree if os.path.exists(task_worktree) else "."
    r = _run_git(["worktree", "list", "--porcelain"], cwd=cwd, check=False)
    for line in r.stdout.split("\n"):
        if (
            line.startswith("worktree ")
            and get_config("worktree_base_path") not in line
        ):
            parts = line.split(" ", 1)
            return parts[1] if len(parts) > 1 else parts[0]
    return None


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
@click.option(
    "-q", "--quiet", is_flag=True, help="Print only the task name for scripting"
)
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
    quiet: bool = False,
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
        lock_fd = open(lock_path, "w", encoding="utf-8")  # noqa: SIM115 — kept open for flock
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

    if not as_json and not quiet:
        click.echo(f"Created task: {name}")
        click.echo(f"  Worktree: {worktree}")
        click.echo(f"  Branch: {branch}")
        click.echo(f"  Incarnation: {task.incarnation_id}")
        if from_thinking:
            click.echo("  Plan: loaded from thinking session")

    if start_queued:
        from duo.protocol import transition

        if not transition(task, TaskStatus.QUEUED):
            if quiet:
                click.echo(name)
                return
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
        if quiet:
            click.echo(name)
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
        if quiet:
            click.echo(name)
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
    if not as_json and not quiet:
        click.echo("Starting Copilot session...")
    start_session(task, defer=defer, reuse_pane=reuse_pane)
    if quiet:
        click.echo(name)
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


@main.command()
@click.argument("name", shell_complete=_complete_task_names)
@click.option("--dry-run", is_flag=True, help="Preview merge without executing")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option(
    "-q", "--quiet", is_flag=True, help="Print only merge result for scripting"
)
def merge(
    name: str, dry_run: bool, *, as_json: bool = False, quiet: bool = False
) -> None:
    """Merge a completed task's worktree to main."""
    task = _load_task_or_fail(name)

    if task.status != TaskStatus.COMPLETED:
        raise DuoUserError(
            f"task '{name}' is '{task.status.value}', not 'completed'",
            fix=f"Check progress with 'duo status {name}' or 'duo inspect {name}'.",
        )

    worktree = task.worktree

    if dry_run:
        commit_count = 0
        files_changed: list[str] = []
        if os.path.isdir(worktree):
            r = _run_git(["log", "--oneline", "main..HEAD"], cwd=worktree, check=False)
            if r.returncode == 0 and r.stdout.strip():
                commit_count = len(r.stdout.strip().splitlines())
            r2 = _run_git(
                ["diff", "--name-only", "main...HEAD"], cwd=worktree, check=False
            )
            if r2.returncode == 0 and r2.stdout.strip():
                files_changed = r2.stdout.strip().splitlines()
        if quiet:
            click.echo(str(len(files_changed)))
            return
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "dry_run": True,
                        "branch": task.branch,
                        "target": "main",
                        "worktree": task.worktree,
                        "commits": commit_count,
                        "files_changed": files_changed,
                    }
                )
            )
        else:
            click.echo("Would merge:")
            click.echo(f"  Branch: {task.branch}")
            click.echo("  Into: main")
            click.echo(f"  Worktree: {task.worktree}")
            if commit_count:
                click.echo(f"  Commits: {commit_count}")
            if files_changed:
                click.echo(f"  Files changed: {len(files_changed)}")
                for f in files_changed[:10]:
                    click.echo(f"    {f}")
                if len(files_changed) > 10:
                    click.echo(f"    ... and {len(files_changed) - 10} more")
            click.echo("\nRun without --dry-run to execute.")
        return

    if not os.path.exists(worktree):
        raise DuoUserError(
            f"worktree '{worktree}' does not exist",
            fix="Task may have been cleaned up. Run 'duo cleanup' to remove stale references.",
        )

    # Fetch and rebase
    if not as_json and not quiet:
        click.echo("Fetching and rebasing...")
    r = _run_git(["fetch", "origin", "main"], cwd=worktree, check=False)
    if r.returncode != 0:
        if not as_json and not quiet:
            click.echo("Warning: fetch failed, proceeding with local state", err=True)

    r = _run_git(["rebase", "origin/main"], cwd=worktree, check=False)
    if r.returncode != 0:
        abort = _run_git(["rebase", "--abort"], cwd=worktree, check=False)
        if abort.returncode != 0:
            if not as_json and not quiet:
                click.echo(
                    f"Warning: could not abort rebase: {abort.stderr.strip()}",
                    err=True,
                )
        raise DuoUserError(
            f"Rebase conflict while merging '{name}'.\n{r.stderr}",
            fix=f"Resolve conflicts manually in '{worktree}', then run 'duo merge {name}' again.",
        )

    # Get parent repo from worktree
    main_worktree = _find_main_worktree(worktree)

    if main_worktree is None:
        raise DuoUserError(
            "cannot find main worktree",
            fix="Ensure the worktree was created from a valid git repository. Run 'duo doctor' to check system state.",
        )

    # ff-only merge
    if not as_json and not quiet:
        click.echo(f"Merging {task.branch} into main...")
    r = _run_git(["merge", task.branch, "--ff-only"], cwd=main_worktree, check=False)
    if r.returncode != 0:
        raise DuoUserError(
            f"Merge failed: {r.stderr}",
            fix=f"Try a manual merge: cd {main_worktree} && git merge {task.branch}",
        )

    # Cleanup
    if not as_json and not quiet:
        click.echo("Cleaning up worktree and branch...")
    r = _run_git(["worktree", "remove", worktree], cwd=main_worktree, check=False)
    wt_removed = r.returncode == 0
    if not wt_removed and not as_json and not quiet:
        click.echo(f"  Warning: worktree removal failed: {r.stderr.strip()}", err=True)
    r = _run_git(["branch", "-d", task.branch], cwd=main_worktree, check=False)
    branch_deleted = r.returncode == 0
    if not branch_deleted and not as_json and not quiet:
        click.echo(f"  Warning: branch deletion failed: {r.stderr.strip()}", err=True)

    from duo.protocol import append_event

    append_event(task, "task_merged", {"branch": task.branch})
    if quiet:
        click.echo(task.branch)
        return
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


def _stop_all_tasks(as_json: bool) -> None:
    """Stop all active (non-terminal) tasks."""
    from duo.protocol import append_event, list_tasks, transition
    from duo.transport import cleanup_pane_state, kill_pane

    terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED}
    tasks = [t for t in list_tasks() if t.status not in terminal]
    results: list[dict[str, object]] = []
    for task in tasks:
        prev = task.status.value
        kill_pane(task.pane_label)
        cleanup_pane_state(task.pane_label)
        stopped = transition(task, TaskStatus.BLOCKED)
        if stopped:
            append_event(task, "task_stopped", {"previous_status": prev})
        results.append({"id": task.id, "stopped": stopped, "previous_status": prev})
    if as_json:
        click.echo(json.dumps({"stopped_count": len(results), "tasks": results}))
    else:
        if not results:
            click.echo("No active tasks to stop.")
        else:
            for r in results:
                click.echo(f"Stopped '{r['id']}' (was {r['previous_status']})")
            click.echo(f"\n{len(results)} task(s) stopped.")


@main.command()
@click.argument(
    "name", required=False, default=None, shell_complete=_complete_task_names
)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option("--all", "stop_all", is_flag=True, help="Stop all active tasks")
@click.option(
    "-q", "--quiet", is_flag=True, help="Print only stopped count for scripting"
)
def stop(
    name: str | None,
    *,
    as_json: bool = False,
    stop_all: bool = False,
    quiet: bool = False,
) -> None:
    """Stop a task gracefully (preserves worktree for resume)."""
    from duo.protocol import append_event, transition

    if stop_all:
        _stop_all_tasks(as_json)
        return

    if name is None:
        raise click.UsageError("Provide a task NAME or use --all")

    task = _load_task_or_fail(name)

    terminal_states = {TaskStatus.COMPLETED, TaskStatus.FAILED}
    if task.status in terminal_states:
        if quiet:
            click.echo(task.status.value)
            return
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
        if quiet:
            click.echo("blocked")
            return
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
        if not as_json and not quiet:
            click.echo("Warning: failed to kill pane", err=True)

    cleanup_pane_state(task.pane_label)

    previous = task.status.value
    stopped = transition(task, TaskStatus.BLOCKED)
    if stopped:
        append_event(task, "task_stopped", {"previous_status": previous})
    else:
        if not as_json and not quiet:
            click.echo(
                f"Warning: pane killed but could not transition from {previous} to BLOCKED",
                err=True,
            )
    if quiet:
        click.echo("stopped" if stopped else "failed")
        return
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


def _kill_all_tasks(as_json: bool) -> None:
    """Kill all tasks (including terminal ones) and clean up resources."""
    from duo.protocol import append_event, list_tasks, save_task, transition
    from duo.transport import cleanup_pane_state, kill_pane

    tasks = list_tasks()
    results: list[dict[str, object]] = []
    for task in tasks:
        kill_pane(task.pane_label)
        cleanup_pane_state(task.pane_label)
        wt_removed, branch_deleted = _remove_worktree_and_branch(task, warn=False)
        append_event(task, "task_killed", {})
        if not transition(task, TaskStatus.FAILED):
            task.status = TaskStatus.FAILED
            save_task(task)
        results.append(
            {
                "id": task.id,
                "worktree_removed": wt_removed,
                "branch_deleted": branch_deleted,
            }
        )
    if as_json:
        click.echo(json.dumps({"killed_count": len(results), "tasks": results}))
    else:
        if not results:
            click.echo("No tasks to kill.")
        else:
            for entry in results:
                click.echo(f"Killed '{entry['id']}'")
            click.echo(f"\n{len(results)} task(s) killed.")


@main.command()
@click.argument(
    "name", required=False, default=None, shell_complete=_complete_task_names
)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option("--all", "kill_all", is_flag=True, help="Kill all tasks")
@click.option("-q", "--quiet", is_flag=True, help="Print only task name for scripting")
def kill(
    name: str | None,
    *,
    as_json: bool = False,
    kill_all: bool = False,
    quiet: bool = False,
) -> None:
    """Kill a task and clean up."""
    if kill_all:
        _kill_all_tasks(as_json)
        return

    if name is None:
        raise click.UsageError("Provide a task NAME or use --all")

    task = _load_task_or_fail(name)

    # Try to kill the pane
    from duo.transport import cleanup_pane_state, kill_pane

    pane_killed = kill_pane(task.pane_label)
    if not pane_killed and not as_json and not quiet:
        click.echo("Warning: failed to kill pane", err=True)

    cleanup_pane_state(task.pane_label)

    # Find parent repo
    repo_cwd = _find_main_worktree(task.worktree) or "."

    warn = not as_json and not quiet
    wt_removed, branch_deleted = _remove_worktree_and_branch(
        task, cwd=repo_cwd, warn=warn
    )

    from duo.protocol import append_event, save_task, transition

    append_event(task, "task_killed", {})
    # Use transition() when valid (emits status_changed event for journal replay).
    # For states without a FAILED transition (CREATED, COMPLETED, already FAILED),
    # fall back to direct save.
    if not transition(task, TaskStatus.FAILED):
        task.status = TaskStatus.FAILED
        save_task(task)
    if quiet:
        click.echo(name)
        return
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


@main.command()
@click.argument("name", shell_complete=_complete_task_names)
@click.option(
    "-n", "--lines", default=20, type=click.IntRange(1), help="Number of recent events"
)
@click.option("--all", "show_all", is_flag=True, help="Show all events")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option(
    "--filter", "event_filter", default=None, help="Filter by event type substring"
)
@click.option(
    "--step", "step_filter", default=None, type=int, help="Filter events by step number"
)
@click.option(
    "-c", "--count", "show_count", is_flag=True, help="Print only the event count"
)
@click.option("-q", "--quiet", is_flag=True, help="Print one event type per line")
@click.pass_context
def logs(
    ctx: click.Context,
    name: str,
    lines: int,
    show_all: bool,
    as_json: bool,
    event_filter: str | None,
    step_filter: int | None,
    show_count: bool,
    quiet: bool,
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

    if step_filter is not None:
        events = [
            ev
            for ev in events
            if ev.get("data", {}).get("step") == step_filter
            or ev.get("step") == step_filter
        ]

    if show_count:
        click.echo(str(len(events)))
        return

    if not show_all:
        events = events[-lines:]

    if quiet:
        for ev in events:
            click.echo(ev.get("event", "unknown"))
        return

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
        "total_steps": len(task.subtasks),
        "attempt": task.current_attempt,
        "incarnation_id": task.incarnation_id,
        "worktree": task.worktree,
        "branch": task.branch,
        "base_commit": task.base_commit,
        "created_at": task.created_at,
        "session_started_at": task.session_started_at,
        "age": _fmt_age(task.created_at),
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
@click.argument("name", shell_complete=_complete_task_names)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option(
    "--include-files",
    is_flag=True,
    help="Show changed files and diff preview from worktree",
)
@click.option(
    "--events",
    "event_count",
    type=int,
    default=None,
    help="Number of recent journal events to show (default: 5, 0 for all)",
)
@click.option("-q", "--quiet", is_flag=True, help="Print only the task status")
def inspect(
    name: str,
    as_json: bool,
    include_files: bool,
    event_count: int | None,
    quiet: bool,
) -> None:
    """Show detailed task information."""
    from duo.protocol import (
        read_heartbeat,
        read_jsonl,
    )

    task = _load_task_or_fail(name)

    if quiet:
        click.echo(task.status.value)
        return

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
        show_n = event_count if event_count is not None else 5
        recent = events if show_n == 0 else events[-show_n:]
        click.echo(f"\nRecent Events ({len(events)} total, showing {len(recent)}):")
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
@click.option("--repo", default=".", help="Git repository path to initialize")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option(
    "-q", "--quiet", is_flag=True, help="Print only the number of items created"
)
def init(repo: str, *, as_json: bool = False, quiet: bool = False) -> None:
    """Initialize a project for Duo (creates .duo config and instructions)."""
    from duo.config import load_config, save_config

    repo_path = Path(repo).resolve()
    project_duo = repo_path / ".duo"

    # Already initialized?
    if project_duo.exists():
        if quiet:
            click.echo("0")
            return
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

    if quiet:
        click.echo(str(len(created)))
        return
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
        is_pane_alive,
        kill_pane,
        name_pane,
        read_pane,
        send_shell_command,
        split_window_horizontal,
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
        # Verify pane is alive via transport layer
        if is_pane_alive(existing["copilot_pane"]):
            pane_id = existing["copilot_pane"]
            click.echo(f"  ✓ reusing standby pane {pane_id}")

    if not pane_id:
        # Create new pane via transport layer
        try:
            pane_id = split_window_horizontal()
        except RuntimeError as exc:
            raise DuoUserError(
                str(exc),
                fix="Check tmux is responding: tmux list-panes",
            ) from None

        # Name the pane
        try:
            name_pane(pane_id, standby_label)
        except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
            # Kill orphaned pane on failure
            kill_pane(pane_id)
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
# duo events — watch-event signal file management
# ---------------------------------------------------------------------------

_WATCH_EVENTS_DIR = Path(os.path.expanduser("~/.duo/watch-events"))


@main.group()
def events() -> None:
    """Manage watch-event signal files."""


@events.command("list")
@click.option("-n", "--limit", default=20, help="Max events to show")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option("-q", "--quiet", is_flag=True, help="Print one event filename per line")
@click.option("-c", "--count", is_flag=True, help="Print only the event count")
def events_list(
    limit: int, *, as_json: bool = False, quiet: bool = False, count: bool = False
) -> None:
    """List recent watch events (newest first)."""
    if not _WATCH_EVENTS_DIR.exists():
        if count:
            click.echo("0")
            return
        if quiet:
            return
        if as_json:
            click.echo(json.dumps({"events": [], "total": 0}))
        else:
            click.echo("No events.")
        return
    files = sorted(_WATCH_EVENTS_DIR.glob("*.json"), reverse=True)
    if not files:
        if count:
            click.echo("0")
            return
        if quiet:
            return
        if as_json:
            click.echo(json.dumps({"events": [], "total": 0}))
        else:
            click.echo("No events.")
        return
    if count:
        click.echo(str(len(files)))
        return
    if quiet:
        for f in files[:limit]:
            click.echo(f.name)
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
        _validate_task_name(name)
        target = _WATCH_EVENTS_DIR / name
        if not target.resolve().is_relative_to(
            _WATCH_EVENTS_DIR.resolve()
        ):  # pragma: no cover — defense-in-depth; _validate_task_name rejects all traversal inputs
            raise DuoUserError(
                f"Invalid event name: {name}",
                fix="Event names must be alphanumeric with hyphens/underscores only.",
            )
        if not target.exists():
            target = _WATCH_EVENTS_DIR / f"{name}.json"
            if not target.resolve().is_relative_to(
                _WATCH_EVENTS_DIR.resolve()
            ):  # pragma: no cover — defense-in-depth; _validate_task_name rejects all traversal inputs
                raise DuoUserError(
                    f"Invalid event name: {name}",
                    fix="Event names must be alphanumeric with hyphens/underscores only.",
                )
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
@click.option("-q", "--quiet", is_flag=True, help="Print only the cleared count")
def events_clear(force: bool, *, as_json: bool = False, quiet: bool = False) -> None:
    """Delete all watch-event signal files."""
    if not _WATCH_EVENTS_DIR.exists():
        if quiet:
            click.echo("0")
            return
        if as_json:
            click.echo(json.dumps({"cleared": 0}))
        else:
            click.echo("No events to clear.")
        return
    files = list(_WATCH_EVENTS_DIR.glob("*.json"))
    if not files:
        if quiet:
            click.echo("0")
            return
        if as_json:
            click.echo(json.dumps({"cleared": 0}))
        else:
            click.echo("No events to clear.")
        return
    if not force and not as_json and not quiet:
        click.confirm(f"Delete {len(files)} event(s)?", abort=True)
    for f in files:
        f.unlink(missing_ok=True)
    if quiet:
        click.echo(str(len(files)))
        return
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
        fix = "Run 'duo list' to see available tasks."
        all_tasks = list_tasks()
        if all_tasks:
            similar = [
                t.id
                for t in all_tasks
                if name.lower() in t.id.lower() or t.id.lower() in name.lower()
            ]
            if similar:
                fix = f"Did you mean: {', '.join(similar[:3])}? Run 'duo list' to see all."
        raise DuoUserError(f"task '{name}' not found", fix=fix)
    return task


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
@click.argument("task", shell_complete=_complete_task_names)
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
@click.argument("task", shell_complete=_complete_task_names)
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
@click.argument("task", shell_complete=_complete_task_names)
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


@main.command("ceo-status")
@click.argument("task", shell_complete=_complete_task_names)
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
@click.option(
    "--dry-run", is_flag=True, help="Show what would be cleaned without doing it"
)
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
@click.option("-q", "--quiet", is_flag=True, help="Print only the cleaned count")
def cleanup(
    clean_all: bool,
    force: bool,
    dry_run: bool,
    keep_journal: bool,
    age: str | None,
    corrupted: bool,
    *,
    as_json: bool = False,
    quiet: bool = False,
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
            )
        ]
    else:
        targets = [t for t in tasks if t.status == TaskStatus.COMPLETED]

    if age:
        max_age = _parse_age(age)
        from duo.poller import age as task_age

        targets = [t for t in targets if task_age(t.created_at) > max_age]

    if not targets:
        if quiet:
            click.echo("0")
            return
        if as_json:
            click.echo(json.dumps({"cleaned": 0, "tasks": [], "dry_run": dry_run}))
        else:
            click.echo("No tasks to clean up.")
        return

    if dry_run:
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "cleaned": len(targets),
                        "tasks": [t.id for t in targets],
                        "dry_run": True,
                    }
                )
            )
        else:
            click.echo(f"Would clean up {len(targets)} task(s):")
            for t in targets:
                click.echo(f"  {t.id} ({t.status.value})")
        return

    if not as_json and not quiet:
        click.echo(f"Tasks to clean up ({len(targets)}):")
        for t in targets:
            click.echo(f"  {t.id} ({t.status.value})")

    if not force and not as_json and not quiet:
        click.confirm("Proceed?", abort=True)

    cleaned = 0
    cleaned_ids: list[str] = []
    warn = not as_json and not quiet
    for task in targets:
        _remove_worktree_and_branch(task, warn=warn)

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
        if not as_json and not quiet:
            click.echo(f"  ✓ {task.id}")

    if quiet:
        click.echo(str(cleaned))
    elif as_json:
        click.echo(json.dumps({"cleaned": cleaned, "tasks": cleaned_ids}))
    else:
        click.echo(f"\nCleaned {cleaned} tasks.")


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
