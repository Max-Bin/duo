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
    replay_state,
)


def _validate_task_name(name: str) -> None:
    """Validate that a task name contains only safe characters."""
    if not re.match(r"^[a-zA-Z0-9_-]+$", name):
        raise click.BadParameter(
            f"Task name must contain only letters, numbers, dashes, underscores. Got: '{name}'"
        )


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Verbose output")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """Duo — Agent Orchestration Runtime."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    if verbose:
        logging.basicConfig(
            level=logging.DEBUG, format="%(name)s %(levelname)s: %(message)s"
        )
    TASKS_DIR.mkdir(parents=True, exist_ok=True)


def _create_worktree(name: str, repo: str) -> tuple[str, str]:
    """Create git worktree for task. Returns (worktree_path, base_commit)."""
    worktree_base = get_config("worktree_base_path")
    worktree = os.path.join(worktree_base, name)
    branch = f"duo/{name}"

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        cwd=repo,
    )
    if result.returncode != 0:
        click.echo(
            f"Error: '{repo}' is not a git repository. Please provide an absolute path to a git repo, or run 'git init' first.",
            err=True,
        )
        sys.exit(1)
    base_commit = result.stdout.strip()

    result = subprocess.run(
        ["git", "worktree", "add", worktree, "-b", branch],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        click.echo(
            f"Error: failed to create worktree: {result.stderr.strip()}. Ensure the repo exists and you have write permissions.",
            err=True,
        )
        sys.exit(1)

    return worktree, base_commit


@main.command()
def version() -> None:
    """Show Duo version."""
    try:
        from importlib.metadata import version as pkg_version

        ver = pkg_version("duo")
    except (ImportError, AttributeError):
        ver = "0.5.0-dev"
    click.echo(f"duo {ver}")


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
def start(name: str, repo: str, desc: str) -> None:
    """Create a task with worktree + Copilot session."""
    from duo.commander import start_session

    _validate_task_name(name)
    repo = os.path.abspath(repo)

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
def status(name: str | None = None) -> None:
    """Show task status."""
    if name:
        task = load_task(name)
        if task is None:
            click.echo(
                f"Error: task '{name}' not found. Run 'duo list' to see available tasks.",
                err=True,
            )
            sys.exit(1)
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
def list_cmd() -> None:
    """List all tasks."""
    tasks = list_tasks()
    if not tasks:
        click.echo("No tasks.")
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
def monitor(names: tuple[str, ...]) -> None:
    """Start adaptive polling monitor."""
    from duo.commander import monitor as run_monitor

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
def merge(name: str) -> None:
    """Merge a completed task's worktree to main."""
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

    if not os.path.exists(worktree):
        click.echo(
            f"Error: worktree '{worktree}' does not exist. Task may have been cleaned up.",
            err=True,
        )
        sys.exit(1)

    # Fetch and rebase
    click.echo("Fetching and rebasing...")
    r = subprocess.run(
        ["git", "fetch", "origin", "main"], cwd=worktree, capture_output=True, text=True
    )
    if r.returncode != 0:
        click.echo("Warning: fetch failed, proceeding with local state")

    r = subprocess.run(
        ["git", "rebase", "origin/main"], cwd=worktree, capture_output=True, text=True
    )
    if r.returncode != 0:
        click.echo(f"Rebase conflict! Escalating to human.\n{r.stderr}", err=True)
        abort = subprocess.run(
            ["git", "rebase", "--abort"], cwd=worktree, capture_output=True, text=True
        )
        if abort.returncode != 0:
            click.echo(
                f"Warning: could not abort rebase: {abort.stderr.strip()}", err=True
            )
        sys.exit(1)

    # Get parent repo from worktree
    main_worktree: str | None = None
    r = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=worktree,
    )
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
    r = subprocess.run(
        ["git", "merge", task.branch, "--ff-only"],
        cwd=main_worktree,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        click.echo(f"Merge failed: {r.stderr}", err=True)
        sys.exit(1)

    # Cleanup
    click.echo("Cleaning up worktree and branch...")
    subprocess.run(["git", "worktree", "remove", worktree], cwd=main_worktree)
    subprocess.run(["git", "branch", "-d", task.branch], cwd=main_worktree)

    from duo.protocol import append_event

    append_event(task, "task_merged", {"branch": task.branch})
    click.echo(f"Merged {name}. Remember to `git push` when ready.")


@main.command()
@click.argument("name")
def kill(name: str) -> None:
    """Kill a task and clean up."""
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
    r = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=task.worktree if os.path.exists(task.worktree) else ".",
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
        subprocess.run(
            ["git", "worktree", "remove", "--force", task.worktree], cwd=repo_cwd
        )

    # Remove branch
    subprocess.run(
        ["git", "branch", "-D", task.branch],
        capture_output=True,
        cwd=repo_cwd,
    )

    from duo.protocol import append_event

    append_event(task, "task_killed", {})
    task.status = TaskStatus.FAILED
    from duo.protocol import save_task

    save_task(task)
    click.echo(f"Killed {name}.")


def _load_batch_file(file: str) -> list[dict[str, Any]]:
    """Read a JSON or YAML batch file and return the list of task definitions."""
    file_path = Path(file)
    content = file_path.read_text()

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
    else:
        tasks_data = json.loads(content)

    if not isinstance(tasks_data, dict) or "tasks" not in tasks_data:
        click.echo(
            "Error: file must contain a 'tasks' key with a list of tasks. See examples/tasks.json",
            err=True,
        )
        sys.exit(1)

    if not tasks_data.get("tasks"):
        click.echo("No tasks defined in file.", err=True)
        sys.exit(1)

    return list(tasks_data["tasks"])


def _create_task_from_batch_def(
    defn: dict[str, Any], repo: str, verbose: bool
) -> str | None:
    """Create a single task from a batch definition dict.

    Returns task name on success, None on failure (prints error).
    Handles worktree creation, task creation, and session start.
    """
    from duo.commander import start_session
    from duo.scheduler import enqueue_or_start

    name = defn["name"]
    desc = defn.get("description", f"Task {name}")
    target_files = defn.get("target_files", [])
    writable = defn.get("writable_paths", ["*"])

    worktree_base = get_config("worktree_base_path")
    worktree = os.path.join(worktree_base, name)
    branch = f"duo/{name}"

    # Get base commit
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        cwd=repo,
    )
    if result.returncode != 0:
        click.echo(
            f"Error: '{repo}' is not a git repository. Please provide an absolute path to a git repo, or run 'git init' first.",
            err=True,
        )
        sys.exit(1)
    base_commit = result.stdout.strip()

    r = subprocess.run(
        ["git", "worktree", "add", worktree, "-b", branch],
        cwd=repo,
        capture_output=True,
        text=True,
    )
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
@click.pass_context
def batch(ctx: click.Context, file: str, repo: str) -> None:
    """Create multiple tasks from a file (JSON or YAML)."""
    from duo.scheduler import queue_status

    repo = os.path.abspath(repo)
    verbose = ctx.obj.get("verbose", False)

    task_defs = _load_batch_file(file)

    created = 0
    for task_def in task_defs:
        name = _create_task_from_batch_def(task_def, repo, verbose)
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
    click.echo(f"Slots: {qs['active_count']}/{qs['max_parallel']} in use")
    if qs["active_tasks"]:
        click.echo(f"Active: {', '.join(qs['active_tasks'])}")
    if qs["queued_tasks"]:
        click.echo(f"Queued: {', '.join(qs['queued_tasks'])}")
    else:
        click.echo("Queue: empty")


@main.command()
@click.argument("name", required=False)
def audit(name: str | None = None) -> None:
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
        click.echo(f"Task: {task.id}")
        click.echo(f"PR consumed: {len(pr_events)}")
        if pr_events:
            click.echo(f"\n{'TIME':<10} {'ACTION':<16} {'STEP':<6} {'ATTEMPT':<8}")
            click.echo("-" * 42)
            for ev in pr_events:
                ts = ev.get("ts", "?")
                if "T" in ts:
                    ts = ts.split("T", 1)[1][:8]
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
        click.echo(f"{'TASK':<20} {'STATUS':<14} {'PR COUNT':<10}")
        click.echo("-" * 46)
        for t in tasks:
            events = read_jsonl(t.journal_path)
            pr_count = sum(1 for ev in events if ev.get("event") == "pr_consumed")
            total_pr += pr_count
            click.echo(f"{t.id:<20} {t.status.value:<14} {pr_count:<10}")

        click.echo("-" * 46)
        click.echo(f"{'TOTAL':<20} {'':<14} {total_pr:<10}")

        # Show session-level log
        pr_log = get_pr_log()
        if pr_log:
            click.echo(f"\nSession log ({len(pr_log)} entries):")
            for entry in pr_log[-10:]:
                ts = entry.get("ts", "?")
                if "T" in ts:
                    ts = ts.split("T", 1)[1][:8]
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
@click.pass_context
def logs(ctx: click.Context, name: str, lines: int, show_all: bool) -> None:
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

    for ev in events:
        ts = ev.get("ts", "?")
        # Shorten timestamp for display
        if "T" in ts:
            ts = ts.split("T", 1)[1][:8]  # HH:MM:SS
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
def inspect(name: str) -> None:
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
            ts = ev.get("ts", "?")
            if "T" in ts:
                ts = ts.split("T", 1)[1][:8]
            click.echo(f"  {ts} {ev.get('event', '?')}")


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
    if gitignore.exists():
        content = gitignore.read_text()
        for line in content.splitlines():
            stripped = line.strip()
            if stripped in (".duo/", ".duo"):
                needs_entry = False
                break
    if needs_entry:
        with open(gitignore, "a") as f:
            if gitignore.exists() and gitignore.stat().st_size > 0:
                existing = gitignore.read_text()
                if not existing.endswith("\n"):
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
            pass

        if pane_alive:
            restart_session(task)
            click.echo(f"Resumed task '{task.id}' — restarted session")
        else:
            start_session(task)
            click.echo(f"Resumed task '{task.id}' — started new session")


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
        ts = ev.get("ts", "?")
        if "T" in ts:
            ts = ts.split("T", 1)[1][:8]
        lines.append(f"  {ts} {ev.get('event', '?')}")

    return "\n".join(lines)


@main.command()
@click.argument("name")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["json", "text"]),
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

    output = _export_as_json(task) if fmt == "json" else _export_as_text(task)

    if outfile:
        Path(outfile).write_text(output + "\n")
        click.echo(f"Report written to {outfile}")
    else:
        click.echo(output)


@main.command()
@click.option(
    "--all",
    "clean_all",
    is_flag=True,
    help="Clean all finished tasks (completed + failed)",
)
@click.option("--force", is_flag=True, help="Skip confirmation")
@click.option("--keep-journal", is_flag=True, help="Keep journal files")
def cleanup(clean_all: bool, force: bool, keep_journal: bool) -> None:
    """Clean up completed and failed tasks."""
    import shutil

    tasks = list_tasks()

    if clean_all:
        targets = [
            t for t in tasks if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
        ]
    else:
        targets = [t for t in tasks if t.status == TaskStatus.COMPLETED]

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
            subprocess.run(
                ["git", "worktree", "remove", "--force", task.worktree],
                capture_output=True,
            )

        # Remove branch
        subprocess.run(
            ["git", "branch", "-D", task.branch],
            capture_output=True,
        )

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
