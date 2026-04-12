"""Shared helpers used by multiple CLI submodules."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import click

from duo.config import get_config
from duo.errors import DuoUserError
from duo.protocol import (
    Task,
    TaskStatus,
    list_tasks,
    load_task,
)

_GIT_TIMEOUT = 30  # seconds for git subprocess calls
_TMUX_TIMEOUT = 10  # seconds for tmux kill/health operations
_MAX_AGE_SECONDS = 1000 * 365 * 86400  # ~1000 years upper bound

_TERMINAL_STATES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ESCALATED}
)


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
