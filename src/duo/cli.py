"""CLI entry point — thin interface to commander."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import click

from duo.config import get_config
from duo.protocol import (
    DUO_DIR,  # noqa: F401 — used by test monkeypatching
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

_COMMAND_SECTIONS: dict[str, list[str]] = {
    "Task Lifecycle": ["start", "send", "stop", "status", "merge", "diff", "kill"],
    "Monitoring": ["list", "monitor", "dashboard", "logs", "inspect", "stats"],
    "Batch & Queue": ["batch", "queue"],
    "Recovery": ["recover", "resume", "retry"],
    "Data & Audit": ["export", "audit", "cleanup"],
    "Setup": ["init", "doctor", "config"],
    "Misc": ["version", "completion"],
}


class _OrderedGroup(click.Group):
    """Click group that displays commands in categorized sections."""

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
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
                cmd = self.get_command(ctx, name)  # pragma: no cover
                if cmd:  # pragma: no cover
                    extra.append((name, cmd.get_short_help_str(limit=60)))  # pragma: no cover
        if extra:  # pragma: no cover
            with formatter.section("Other"):  # pragma: no cover
                formatter.write_dl(extra)  # pragma: no cover


def _validate_task_name(name: str) -> None:
    """Validate that a task name contains only safe characters."""
    if len(name) > 63:
        raise click.BadParameter(
            f"Task name must be at most 63 characters, got {len(name)}"
        )
    if not re.match(r"^[a-zA-Z0-9_-]+$", name):
        raise click.BadParameter(
            f"Task name must contain only letters, numbers, dashes, underscores. Got: '{name}'"
        )


def _fmt_ts(ts: str) -> str:
    """Extract HH:MM:SS from ISO timestamp, or return '?' if malformed."""
    try:
        return ts.split("T", 1)[1][:8] if "T" in ts else ts[:8]
    except (IndexError, TypeError, AttributeError):
        return "?"


def _run_git(args: list[str], cwd: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a git command with consistent error handling.

    Args:
        args: Git arguments (without 'git' prefix), e.g. ['rev-parse', 'HEAD']
        cwd: Working directory
        check: If True, exit on failure with error message

    Returns:
        CompletedProcess result
    """
    try:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError:
        click.echo("Error: git is not installed. Install: brew install git (macOS) or apt install git (Linux)", err=True)
        sys.exit(1)
    if check and result.returncode != 0:
        cmd_str = " ".join(["git", *args])
        click.echo(f"Error: `{cmd_str}` failed: {result.stderr.strip()}", err=True)
        sys.exit(1)
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
        click.echo(f"Error: cannot create tasks directory '{TASKS_DIR}': {e}", err=True)
        sys.exit(1)


def _create_worktree(name: str, repo: str) -> tuple[str, str]:
    """Create git worktree for task. Returns (worktree_path, base_commit)."""
    worktree_base = get_config("worktree_base_path")
    worktree = os.path.join(worktree_base, name)
    branch = f"duo/{name}"

    result = _run_git(["rev-parse", "HEAD"], cwd=repo)
    base_commit = result.stdout.strip()

    result = _run_git(["worktree", "add", worktree, "-b", branch], cwd=repo)

    return worktree, base_commit


@main.command()
def version() -> None:
    """Show Duo version."""
    from duo import __version__

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
@click.option("--queue", "start_queued", is_flag=True, help="Create task in queued state")
def start(name: str, repo: str, desc: str, model: str | None, start_queued: bool) -> None:
    """Create a task with worktree + Copilot session."""
    from duo.commander import start_session

    _validate_task_name(name)
    repo = os.path.abspath(repo)

    if model:
        os.environ["DUO_COPILOT_MODEL"] = model

    # Check for duplicate task
    existing = load_task(name)
    if existing is not None:
        click.echo(
            f"Error: task '{name}' already exists (status: {existing.status.value}). Use 'duo kill {name}' first.",
            err=True,
        )
        sys.exit(1)

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

    click.echo(f"Created task: {name}")
    click.echo(f"  Worktree: {worktree}")
    click.echo(f"  Branch: {branch}")
    click.echo(f"  Incarnation: {task.incarnation_id}")

    if start_queued:
        from duo.protocol import transition
        transition(task, TaskStatus.QUEUED)
        click.echo(f"Task '{name}' queued.")
        return

    # Check if we should queue or start
    from duo.scheduler import enqueue_or_start, queue_status

    action = enqueue_or_start(task)

    if action == "queued":
        qs = queue_status()
        click.echo(
            f"  Queued ({qs['queued_count']} in queue). {qs['active_count']}/{qs['max_parallel']} slots in use."
        )
        click.echo("  Task will start automatically when a slot opens.")
        click.echo("  Run 'duo monitor' to manage the queue.")
        return

    # Start Copilot session
    click.echo("Starting Copilot session...")
    start_session(task)
    click.echo(f"Session started. Pane label: {task.pane_label}")


@main.command()
@click.argument("name")
@click.argument("prompt")
def send(name: str, prompt: str) -> None:
    """Send a prompt to a task's Copilot session."""
    from duo.commander import send_task_prompt

    _validate_task_name(name)
    if not prompt or not prompt.strip():
        click.echo("Error: prompt cannot be empty.", err=True)
        sys.exit(1)
    task = load_task(name)
    if task is None:
        click.echo(
            f"Error: task '{name}' not found. Run 'duo list' to see available tasks.",
            err=True,
        )
        sys.exit(1)

    if task.status == TaskStatus.QUEUED:
        click.echo(
            f"Warning: task '{name}' is queued and not yet started. Prompt will be sent when task starts.",
            err=True,
        )

    send_task_prompt(task, prompt)
    click.echo(
        f"Sent to {name} (step={task.current_step} attempt={task.current_attempt})"
    )


@main.command()
@click.argument("name", required=False)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def status(name: str | None = None, *, as_json: bool = False) -> None:
    """Show task status."""
    if name:
        task = load_task(name)
        if task is None:
            click.echo(
                f"Error: task '{name}' not found. Run 'duo list' to see available tasks.",
                err=True,
            )
            sys.exit(1)
        if as_json:
            output = {
                "id": task.id,
                "status": task.status.value,
                "step": task.current_step,
                "attempt": task.current_attempt,
                "worktree": task.worktree,
                "branch": task.branch,
                "created_at": task.created_at,
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


@main.command("list")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def list_cmd(as_json: bool) -> None:
    """List all tasks."""
    tasks = list_tasks()
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
def recover() -> None:
    """Recover all interrupted tasks from journals."""
    tasks = list_tasks()
    recovered = 0
    for task in tasks:
        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            continue
        actual = replay_state(task)
        if actual != task.status:
            click.echo(f"  {task.id}: {task.status.value} → {actual.value}")
            task.status = actual
            from duo.protocol import save_task

            save_task(task)
            recovered += 1
    click.echo(
        f"Recovered {recovered} tasks." if recovered else "All tasks consistent."
    )


@main.command()
@click.argument("name")
@click.option("--dry-run", is_flag=True, help="Preview merge without executing")
def merge(name: str, dry_run: bool) -> None:
    """Merge a completed task's worktree to main."""
    _validate_task_name(name)
    task = load_task(name)
    if task is None:
        click.echo(
            f"Error: task '{name}' not found. Run 'duo list' to see available tasks.",
            err=True,
        )
        sys.exit(1)

    if task.status != TaskStatus.COMPLETED:
        click.echo(
            f"Error: task '{name}' is '{task.status.value}', not 'completed'. Wait for completion or check 'duo logs {name}'.",
            err=True,
        )
        sys.exit(1)

    worktree = task.worktree

    if dry_run:
        click.echo("Would merge:")
        click.echo(f"  Branch: {task.branch}")
        click.echo("  Into: main")
        click.echo(f"  Worktree: {task.worktree}")
        click.echo("\nRun without --dry-run to execute.")
        return

    if not os.path.exists(worktree):
        click.echo(
            f"Error: worktree '{worktree}' does not exist. Task may have been cleaned up.",
            err=True,
        )
        sys.exit(1)

    # Fetch and rebase
    click.echo("Fetching and rebasing...")
    r = _run_git(["fetch", "origin", "main"], cwd=worktree, check=False)
    if r.returncode != 0:
        click.echo("Warning: fetch failed, proceeding with local state")

    r = _run_git(["rebase", "origin/main"], cwd=worktree, check=False)
    if r.returncode != 0:
        click.echo(f"Rebase conflict! Escalating to human.\n{r.stderr}", err=True)
        abort = _run_git(["rebase", "--abort"], cwd=worktree, check=False)
        if abort.returncode != 0:
            click.echo(
                f"Warning: could not abort rebase: {abort.stderr.strip()}", err=True
            )
        sys.exit(1)

    # Get parent repo from worktree
    main_worktree: str | None = None
    r = _run_git(["worktree", "list", "--porcelain"], cwd=worktree, check=False)
    for line in r.stdout.split("\n"):
        if (
            line.startswith("worktree ")
            and get_config("worktree_base_path") not in line
        ):
            main_worktree = line.split(" ", 1)[1]
            break

    if main_worktree is None:
        click.echo(
            "Error: cannot find main worktree. Ensure the task's worktree was created from a valid git repository.",
            err=True,
        )
        sys.exit(1)

    # ff-only merge
    click.echo(f"Merging {task.branch} into main...")
    r = _run_git(["merge", task.branch, "--ff-only"], cwd=main_worktree, check=False)
    if r.returncode != 0:
        click.echo(f"Merge failed: {r.stderr}", err=True)
        sys.exit(1)

    # Cleanup
    click.echo("Cleaning up worktree and branch...")
    r = _run_git(["worktree", "remove", worktree], cwd=main_worktree, check=False)
    if r.returncode != 0:
        click.echo(f"  Warning: worktree removal failed: {r.stderr.strip()}", err=True)
    r = _run_git(["branch", "-d", task.branch], cwd=main_worktree, check=False)
    if r.returncode != 0:
        click.echo(f"  Warning: branch deletion failed: {r.stderr.strip()}", err=True)

    from duo.protocol import append_event

    append_event(task, "task_merged", {"branch": task.branch})
    click.echo(f"Merged {name}. Remember to `git push` when ready.")


@main.command()
@click.argument("name")
def stop(name: str) -> None:
    """Stop a task gracefully (preserves worktree for resume)."""
    from duo.protocol import append_event, transition

    task = load_task(name)
    if task is None:
        click.echo(
            f"Error: task '{name}' not found. Run 'duo list' to see available tasks.",
            err=True,
        )
        sys.exit(1)

    terminal_states = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ESCALATED}
    if task.status in terminal_states:
        click.echo(f"Task '{name}' is already in terminal state '{task.status.value}'.")
        return

    if task.status == TaskStatus.BLOCKED:
        click.echo(f"Task '{name}' is already stopped.")
        return

    # Kill the pane but preserve worktree and branch
    subprocess.run(
        ["tmux", "kill-pane", "-t", task.pane_label],
        capture_output=True,
    )

    previous = task.status.value
    transition(task, TaskStatus.BLOCKED)
    append_event(task, "task_stopped", {"previous_status": previous})
    click.echo(f"Stopped '{name}'. Worktree preserved at {task.worktree}")
    click.echo(f"  Resume with: duo resume {name}")


@main.command()
@click.argument("name")
def kill(name: str) -> None:
    """Kill a task and clean up."""
    _validate_task_name(name)
    task = load_task(name)
    if task is None:
        click.echo(
            f"Error: task '{name}' not found. Run 'duo list' to see available tasks.",
            err=True,
        )
        sys.exit(1)

    # Try to kill the pane
    subprocess.run(
        ["tmux", "kill-pane", "-t", task.pane_label],
        capture_output=True,
    )

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
            main_worktree = line.split(" ", 1)[1]
            break
    repo_cwd = main_worktree or "."

    # Remove worktree
    if os.path.exists(task.worktree):
        r = _run_git(["worktree", "remove", "--force", task.worktree], cwd=repo_cwd, check=False)
        if r.returncode != 0:
            click.echo(f"  Warning: worktree removal failed: {r.stderr.strip()}", err=True)

    # Remove branch
    r = _run_git(["branch", "-D", task.branch], cwd=repo_cwd, check=False)
    if r.returncode != 0:
        click.echo(f"  Warning: branch deletion failed: {r.stderr.strip()}", err=True)

    from duo.protocol import append_event

    append_event(task, "task_killed", {})
    task.status = TaskStatus.FAILED
    from duo.protocol import save_task

    save_task(task)
    click.echo(f"Killed {name}.")


def _load_batch_file(file: str) -> list[dict[str, Any]]:
    """Read a JSON or YAML batch file and return the list of task definitions."""
    file_path = Path(file)
    try:
        content = file_path.read_text()
    except (FileNotFoundError, PermissionError, OSError) as e:
        click.echo(f"Error: cannot read batch file '{file}': {e}", err=True)
        sys.exit(1)

    if file_path.suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-untyped,unused-ignore]

            tasks_data = yaml.safe_load(content)
        except ImportError:
            click.echo(
                "Error: PyYAML not installed. Run: uv pip install pyyaml", err=True
            )
            click.echo("Or use JSON format instead.", err=True)
            sys.exit(1)
        except yaml.YAMLError as e:
            click.echo(f"Error: invalid YAML in '{file}': {e}", err=True)
            sys.exit(1)
    else:
        try:
            tasks_data = json.loads(content)
        except json.JSONDecodeError as e:
            click.echo(f"Error: invalid JSON in '{file}': {e}", err=True)
            sys.exit(1)

    if not isinstance(tasks_data, dict) or "tasks" not in tasks_data:
        if isinstance(tasks_data, list):
            tasks_data = {"tasks": tasks_data}
        else:
            click.echo(
                "Error: file must contain a 'tasks' key with a list of tasks. See examples/tasks.json",
                err=True,
            )
            sys.exit(1)

    if not tasks_data.get("tasks"):
        click.echo("No tasks defined in file.", err=True)
        sys.exit(1)

    return list(tasks_data["tasks"])


def _create_single_task(
    defn: dict[str, Any], repo: str, *, queue_only: bool = False
) -> str | None:
    """Create a single task from a batch definition dict.

    Returns task name on success, None on failure (prints error).

    When *queue_only* is False (default), the task is handed to the scheduler
    which may start it immediately or queue it.  When True, the task is placed
    directly into QUEUED state without starting a session.
    """
    name = defn["name"]
    try:
        _validate_task_name(name)
    except (SystemExit, click.BadParameter):
        click.echo(f"  ✗ {name}: invalid task name", err=True)
        return None
    desc = defn.get("description", f"Task {name}")
    target_files = defn.get("target_files", [])
    writable = defn.get("writable_paths", ["*"])

    worktree_base = get_config("worktree_base_path")
    worktree = os.path.join(worktree_base, name)
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

        transition(task, TaskStatus.QUEUED)
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
@click.option("--queue", "start_queued", is_flag=True, help="Create all tasks in queued state")
@click.pass_context
def batch(ctx: click.Context, file: str, repo: str, dry_run: bool, start_queued: bool) -> None:
    """Create multiple tasks from a file (JSON or YAML)."""
    from duo.scheduler import queue_status

    repo = os.path.abspath(repo)

    task_defs = _load_batch_file(file)

    if dry_run:
        click.echo(f"Would create {len(task_defs)} tasks:")
        for i, td in enumerate(task_defs, 1):
            click.echo(f"  {i}. {td['name']} — {td.get('description', '(no description)')}")
        return

    created = 0
    for task_def in task_defs:
        name = _create_single_task(task_def, repo, queue_only=start_queued)
        if name is not None:
            created += 1

    qs = queue_status()
    click.echo(f"\nBatch complete: {created} tasks created")
    click.echo(f"  Active: {qs['active_count']}/{qs['max_parallel']}")
    click.echo(f"  Queued: {qs['queued_count']}")
    if qs["queued_count"] > 0:
        click.echo("Run 'duo monitor' to process the queue.")


@main.command()
def queue() -> None:
    """Show queue status."""
    from duo.scheduler import queue_status

    qs = queue_status()

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
        task = load_task(name)
        if task is None:
            click.echo(f"Error: task '{name}' not found.", err=True)
            sys.exit(1)
        events = read_jsonl(task.journal_path)
        pr_events = [ev for ev in events if ev.get("event") == "pr_consumed"]

        if as_json:
            click.echo(
                json.dumps(
                    {"task": task.id, "pr_consumed": len(pr_events), "events": pr_events},
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
            task_rows.append({"task": t.id, "status": t.status.value, "pr_count": pr_count})

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
@click.argument("names", nargs=-1)
@click.option("--refresh", default=2.0, help="Refresh rate in seconds")
def dashboard(names: tuple[str, ...], refresh: float) -> None:
    """Live terminal dashboard for task monitoring."""
    try:
        from duo.dashboard import run_dashboard
    except ImportError:
        click.echo("Error: 'rich' library required. Run: uv add rich", err=True)
        sys.exit(1)

    task_ids = list(names) if names else None
    run_dashboard(task_ids, refresh_rate=refresh)


@main.command()
@click.argument("name")
@click.option("-n", "--lines", default=20, help="Number of recent events to show")
@click.option("--all", "show_all", is_flag=True, help="Show all events")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def logs(ctx: click.Context, name: str, lines: int, show_all: bool, as_json: bool) -> None:
    """Show task journal events."""
    from duo.protocol import read_jsonl

    task = load_task(name)
    if task is None:
        click.echo(
            f"Error: task '{name}' not found. Run 'duo list' to see available tasks.",
            err=True,
        )
        sys.exit(1)

    events = read_jsonl(task.journal_path)
    if not events:
        click.echo("No events recorded.")
        return

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


@main.command()
@click.argument("name")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option("--include-files", is_flag=True, help="Show changed files and diff preview from worktree")
def inspect(name: str, as_json: bool, include_files: bool) -> None:
    """Show detailed task information."""
    from duo.protocol import (
        read_ack_for_step,
        read_heartbeat,
        read_jsonl,
        read_result_for_step,
    )

    task = load_task(name)
    if task is None:
        click.echo(
            f"Error: task '{name}' not found. Run 'duo list' to see available tasks.",
            err=True,
        )
        sys.exit(1)

    if as_json:
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
                r = _run_git(["diff", "--name-only", "HEAD"], cwd=worktree, check=False)
                changed = [f for f in r.stdout.strip().splitlines() if f] if r.returncode == 0 else []
                r2 = _run_git(["ls-files", "--others", "--exclude-standard"], cwd=worktree, check=False)
                untracked = [f for f in r2.stdout.strip().splitlines() if f] if r2.returncode == 0 else []
                r3 = _run_git(["diff", "HEAD"], cwd=worktree, check=False)
                diff_preview = r3.stdout[:500] if r3.returncode == 0 else ""
                if len(r3.stdout) > 500:
                    diff_preview += "\n... (truncated)"
                data["changed_files"] = changed
                data["untracked_files"] = untracked
                data["diff_preview"] = diff_preview
            else:
                data["files_error"] = f"Worktree not found: {worktree}"

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
        click.echo("\nHeartbeat:")
        click.echo(f"  Timestamp:     {hb.ts}")
        click.echo(f"  Status:        {hb.status}")
        click.echo(f"  Current file:  {hb.current_file}")
        click.echo(f"  Incarnation:   {hb.incarnation}")
    else:
        click.echo("\nHeartbeat:       (none)")

    # Latest ack/result
    ack = read_ack_for_step(task, task.current_step, task.current_attempt)
    if ack:
        click.echo("\nAck:")
        click.echo(f"  Acked at:      {ack.acked_at}")
        click.echo(f"  Prompt hash:   {ack.prompt_hash}")

    result = read_result_for_step(task, task.current_step, task.current_attempt)
    if result:
        click.echo("\nResult:")
        click.echo(f"  Status:        {result.status}")
        click.echo(f"  Summary:       {result.summary}")
        if result.files_changed:
            click.echo(f"  Files changed: {', '.join(result.files_changed)}")

    # Recent events
    events = read_jsonl(task.journal_path)

    # PR consumption count
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
            r = _run_git(["diff", "--name-only", "HEAD"], cwd=worktree, check=False)
            changed = [f for f in r.stdout.strip().splitlines() if f] if r.returncode == 0 else []
            r2 = _run_git(["ls-files", "--others", "--exclude-standard"], cwd=worktree, check=False)
            untracked = [f for f in r2.stdout.strip().splitlines() if f] if r2.returncode == 0 else []
            r3 = _run_git(["diff", "HEAD"], cwd=worktree, check=False)
            diff_preview = r3.stdout[:500] if r3.returncode == 0 else ""
            if len(r3.stdout) > 500:
                diff_preview += "\n... (truncated)"
            if changed:
                click.echo(f"\nChanged files ({len(changed)}):")
                for f in changed[:20]:
                    click.echo(f"  M {f}")
            if untracked:
                click.echo(f"\nUntracked files ({len(untracked)}):")
                for f in untracked[:20]:
                    click.echo(f"  ? {f}")
            if diff_preview:
                click.echo("\nDiff preview:")
                click.echo(diff_preview)
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
        "blocked",
        "prompt_sent",
        "acked",
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
def init(repo: str) -> None:
    """Initialize a project for Duo (creates .duo config and instructions)."""
    from duo.config import load_config, save_config

    repo_path = Path(repo).resolve()
    project_duo = repo_path / ".duo"

    # Already initialized?
    if project_duo.exists():
        click.echo("Already initialized.")
        return

    # Must be a git repo
    if not (repo_path / ".git").exists():
        click.echo("Error: not a git repository. Run 'git init' first.", err=True)
        sys.exit(1)

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

    # 4. .duo/ inside repo
    project_duo.mkdir(parents=True, exist_ok=True)
    created.append(str(project_duo))

    # 5. .duo/instructions.md
    instructions = project_duo / "instructions.md"
    instructions.write_text(
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
        "<!-- Anything the executor should know -->\n"
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
        with open(gitignore, "a") as f:
            if content and not content.endswith("\n"):
                f.write("\n")
            f.write(".duo/\n")
        created.append(str(gitignore) + " (updated)")

    click.echo("Initialized Duo project:")
    for item in created:
        click.echo(f"  ✓ {item}")


@main.command()
def doctor() -> None:
    """Check environment dependencies and configuration."""
    checks_passed = 0
    checks_total = 0
    critical_failed = False

    def _check(
        name: str,
        ok: bool,
        ok_msg: str,
        fail_msg: str,
        *,
        critical: bool = False,
    ) -> None:
        nonlocal checks_passed, checks_total, critical_failed
        checks_total += 1
        if ok:
            checks_passed += 1
            click.echo(click.style(f"  ✓ {name}: {ok_msg}", fg="green"))
        else:
            click.echo(click.style(f"  ✗ {name}: {fail_msg}", fg="red"))
            if critical:
                critical_failed = True

    # 1. Python version
    vi = sys.version_info
    _check(
        "Python",
        vi >= (3, 12),
        f"{vi.major}.{vi.minor}.{vi.micro}",
        f"{vi.major}.{vi.minor}.{vi.micro} — Requires >= 3.12",
    )

    # 2. tmux
    _check(
        "tmux",
        shutil.which("tmux") is not None,
        "installed",
        "not found — Install with: brew install tmux (macOS) or apt install tmux (Linux)",
        critical=True,
    )

    # 3. tmux-bridge
    bridge_found = shutil.which("tmux-bridge") is not None
    if not bridge_found:
        smux_path = Path.home() / ".smux" / "bin" / "tmux-bridge"
        bridge_found = smux_path.exists()
    _check(
        "tmux-bridge",
        bridge_found,
        "installed",
        "not found — Install from: https://github.com/anthropic-ai/tmux-bridge",
        critical=True,
    )

    # 4. Copilot CLI
    copilot_found = (
        shutil.which("github-copilot-cli") is not None
        or shutil.which("copilot") is not None
    )
    _check(
        "Copilot CLI",
        copilot_found,
        "installed",
        "not found — Install from: https://github.com/github/copilot-cli",
    )

    # 5. uv
    _check(
        "uv",
        shutil.which("uv") is not None,
        "installed",
        "not found — Install with: curl -LsSf https://astral.sh/uv/install.sh | sh",
    )

    # 6. ~/.duo directory
    _check(
        "~/.duo directory",
        DUO_DIR.exists(),
        "exists",
        "missing — Run: duo init",
    )

    # 7. Config file
    config_ok = False
    config_path = DUO_DIR / "config.json"
    if config_path.exists():
        try:
            json.loads(config_path.read_text())
            config_ok = True
        except (json.JSONDecodeError, OSError):
            pass
    _check(
        "Config file",
        config_ok,
        "valid",
        "missing or invalid — Run: duo config reset",
    )

    # 8. Active tmux session
    tmux_ok = False
    if shutil.which("tmux"):
        result = subprocess.run(
            ["tmux", "list-sessions"],
            capture_output=True,
            text=True,
        )
        tmux_ok = result.returncode == 0
    _check(
        "tmux session",
        tmux_ok,
        "active",
        "no active session — Start tmux first",
    )

    # 9. Task timeout config
    timeout_val = get_config("task_timeout")
    if isinstance(timeout_val, int) and timeout_val >= 0:
        label = f"{timeout_val}s" if timeout_val > 0 else "disabled"
        _check("task_timeout", True, f"configured ({label})", "")
    else:
        _check("task_timeout", False, "", "invalid value — must be 0 or positive integer")

    click.echo(f"\n{checks_passed}/{checks_total} checks passed")
    if critical_failed:
        sys.exit(1)


@main.command()
@click.argument("name", required=False)
def resume(name: str | None) -> None:
    """Resume interrupted task sessions."""
    from duo.commander import restart_session, start_session
    from duo.transport import is_process_alive

    TERMINAL_STATES = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ESCALATED}

    if name is not None:
        task = load_task(name)
        if task is None:
            click.echo(f"Error: task '{name}' not found.", err=True)
            sys.exit(1)
        if task.status in TERMINAL_STATES:
            click.echo(f"Task '{name}' is already completed.")
            return
        targets = [task]
    else:
        all_tasks = list_tasks()
        targets = [t for t in all_tasks if t.status not in TERMINAL_STATES]
        if not targets:
            click.echo("No interrupted tasks found.")
            return

    for task in targets:
        pane_alive = False
        try:
            pane_alive = is_process_alive(task.pane_label)
        except Exception:
            click.echo(f"  Warning: could not check pane status for '{task.id}', assuming dead", err=True)

        if pane_alive:
            restart_session(task)
            click.echo(f"Resumed task '{task.id}' — restarted session")
        else:
            start_session(task)
            click.echo(f"Resumed task '{task.id}' — started new session")


@main.command()
@click.argument("name")
def retry(name: str) -> None:
    """Retry a failed or blocked task from its current step."""
    from duo.protocol import transition

    _validate_task_name(name)
    task = load_task(name)
    if task is None:
        click.echo(f"Error: task '{name}' not found.", err=True)
        sys.exit(1)
    if task.status not in (TaskStatus.FAILED, TaskStatus.BLOCKED):
        click.echo(
            f"Error: task '{name}' is '{task.status.value}', not retryable."
            " Only FAILED or BLOCKED tasks can be retried.",
            err=True,
        )
        sys.exit(1)
    transition(task, TaskStatus.SESSION_STARTING)
    click.echo(f"Task '{name}' queued for retry from step {task.current_step}.")


@main.group()
def config() -> None:
    """Manage Duo configuration."""
    pass


@config.command("get")
@click.argument("key")
def config_get(key: str) -> None:
    """Get a config value."""
    from duo.config import get_config

    value = get_config(key)
    if value is None:
        click.echo(f"Unknown key: {key}", err=True)
        sys.exit(1)
    click.echo(f"{key} = {value}")


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Set a config value."""
    from duo.config import DEFAULTS, set_config

    if key not in DEFAULTS:
        click.echo(f"Warning: '{key}' is not a known config key", err=True)
    result = set_config(key, value)
    click.echo(f"{key} = {result}")


@config.command("list")
def config_list() -> None:
    """List all config values."""
    from duo.config import DEFAULTS, load_config

    config = load_config()
    for key in sorted(DEFAULTS):
        value = config.get(key, DEFAULTS[key])
        default = DEFAULTS[key]
        marker = "" if value == default else " (modified)"
        click.echo(f"  {key} = {value}{marker}")


@config.command("reset")
@click.argument("key", required=False)
def config_reset(key: str | None = None) -> None:
    """Reset config to defaults (or reset a single key)."""
    from duo.config import reset_config

    reset_config(key)
    if key:
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
    task = load_task(name)
    if task is None:
        click.echo(
            f"Error: task '{name}' not found. Run 'duo list' to see available tasks.",
            err=True,
        )
        sys.exit(1)

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
            Path(outfile).write_text(output + "\n" if output else "")
            click.echo(f"Report written to {outfile}")
        else:
            for l in lines:
                click.echo(l)
        return

    output = _export_as_json(task) if fmt == "json" else _export_as_text(task)

    if outfile:
        Path(outfile).write_text(output + "\n")
        click.echo(f"Report written to {outfile}")
    else:
        click.echo(output)


def _parse_age(age_str: str) -> int:
    """Parse age string like '7d', '24h', '30m' into seconds."""
    import re
    match = re.match(r"^(\d+)([dhms])$", age_str)
    if not match:
        click.echo("Error: invalid age format. Use: 7d, 24h, 30m, 3600s", err=True)
        sys.exit(1)
    value, unit = int(match.group(1)), match.group(2)
    return value * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]


@main.command()
@click.option(
    "--all",
    "clean_all",
    is_flag=True,
    help="Clean all finished tasks (completed + failed)",
)
@click.option("--force", is_flag=True, help="Skip confirmation")
@click.option("--keep-journal", is_flag=True, help="Keep journal files")
@click.option("--age", type=str, default=None, help="Only clean tasks older than duration (e.g., 7d, 24h, 30m)")
def cleanup(clean_all: bool, force: bool, keep_journal: bool, age: str | None) -> None:
    """Clean up completed and failed tasks."""
    import shutil

    tasks = list_tasks()

    if clean_all:
        targets = [
            t for t in tasks if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
        ]
    else:
        targets = [t for t in tasks if t.status == TaskStatus.COMPLETED]

    if age:
        max_age = _parse_age(age)
        from duo.poller import age as task_age
        targets = [t for t in targets if task_age(t.created_at) > max_age]

    if not targets:
        click.echo("No tasks to clean up.")
        return

    click.echo(f"Tasks to clean up ({len(targets)}):")
    for t in targets:
        click.echo(f"  {t.id} ({t.status.value})")

    if not force:
        click.confirm("Proceed?", abort=True)

    cleaned = 0
    for task in targets:
        # Remove worktree if it exists
        if os.path.exists(task.worktree):
            r = _run_git(["worktree", "remove", "--force", task.worktree], cwd=".", check=False)
            if r.returncode != 0:
                click.echo(f"  Warning: worktree removal failed: {r.stderr.strip()}", err=True)

        # Remove branch
        r = _run_git(["branch", "-D", task.branch], cwd=".", check=False)
        if r.returncode != 0:
            click.echo(f"  Warning: branch deletion failed: {r.stderr.strip()}", err=True)

        # Remove task directory (or just non-journal files)
        if keep_journal:
            for item in task.dir.iterdir():
                if item.name != "journal.jsonl":
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
        else:
            shutil.rmtree(task.dir)

        cleaned += 1
        click.echo(f"  ✓ {task.id}")

    click.echo(f"\nCleaned {cleaned} tasks.")


@main.command("diff")
@click.argument("name")
def diff_cmd(name: str) -> None:
    """Show git diff for a task's worktree changes."""
    _validate_task_name(name)
    task = load_task(name)
    if task is None:
        click.echo(f"Error: task '{name}' not found.", err=True)
        sys.exit(1)

    if not Path(task.worktree).exists():
        click.echo(f"Error: worktree '{task.worktree}' not found.", err=True)
        sys.exit(1)

    result = _run_git(["diff", task.base_commit], cwd=task.worktree, check=False)
    if result.stdout:
        click.echo(result.stdout)
    else:
        click.echo("No changes.")
