"""CLI entry point — thin interface to commander."""

from __future__ import annotations

import os
import subprocess
import sys

import click

from duo.protocol import (
    DUO_DIR,
    TASKS_DIR,
    TaskStatus,
    create_task,
    list_tasks,
    load_task,
    replay_state,
    Subtask,
)


@click.group()
def main() -> None:
    """Duo — Agent Orchestration Runtime."""
    TASKS_DIR.mkdir(parents=True, exist_ok=True)


@main.command()
@click.argument("name")
@click.option("--repo", default=".", help="Git repo path to create worktree from")
@click.option("--desc", default="", help="Task description")
def start(name: str, repo: str, desc: str) -> None:
    """Create a task with worktree + Copilot session."""
    from duo.commander import send_task_prompt, start_session

    repo = os.path.abspath(repo)
    worktree = f"/tmp/duo-worktrees/{name}"
    branch = f"duo/{name}"

    # Get base commit
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, cwd=repo,
    )
    if result.returncode != 0:
        click.echo(f"Error: not a git repo: {repo}", err=True)
        sys.exit(1)
    base_commit = result.stdout.strip()

    # Create worktree
    subprocess.run(
        ["git", "worktree", "add", worktree, "-b", branch],
        cwd=repo,
    )

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

    task = load_task(name)
    if task is None:
        click.echo(f"Error: task not found: {name}", err=True)
        sys.exit(1)

    send_task_prompt(task, prompt)
    click.echo(f"Sent to {name} (step={task.current_step} attempt={task.current_attempt})")


@main.command()
@click.argument("name", required=False)
def status(name: str | None = None) -> None:
    """Show task status."""
    if name:
        task = load_task(name)
        if task is None:
            click.echo(f"Error: task not found: {name}", err=True)
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


def _print_task(task) -> None:
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
            f"{t.id:<20} {t.status.value:<18} "
            f"{step_str:<8} {t.incarnation_id:<12}"
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
    click.echo(f"Recovered {recovered} tasks." if recovered else "All tasks consistent.")


@main.command()
@click.argument("name")
def merge(name: str) -> None:
    """Merge a completed task's worktree to main."""
    task = load_task(name)
    if task is None:
        click.echo(f"Error: task not found: {name}", err=True)
        sys.exit(1)

    if task.status != TaskStatus.COMPLETED:
        click.echo(f"Error: task {name} is {task.status.value}, not completed", err=True)
        sys.exit(1)

    worktree = task.worktree

    # Fetch and rebase
    click.echo("Fetching and rebasing...")
    r = subprocess.run(["git", "fetch", "origin", "main"], cwd=worktree)
    if r.returncode != 0:
        click.echo("Warning: fetch failed, proceeding with local state")

    r = subprocess.run(["git", "rebase", "origin/main"], cwd=worktree, capture_output=True, text=True)
    if r.returncode != 0:
        click.echo(f"Rebase conflict! Escalating to human.\n{r.stderr}", err=True)
        subprocess.run(["git", "rebase", "--abort"], cwd=worktree)
        sys.exit(1)

    # Get parent repo from worktree
    r = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        capture_output=True, text=True, cwd=worktree,
    )
    # Find the main worktree
    main_worktree = None
    for line in r.stdout.split("\n"):
        if line.startswith("worktree ") and "/tmp/duo-worktrees" not in line:
            main_worktree = line.split(" ", 1)[1]
            break

    if main_worktree is None:
        click.echo("Error: cannot find main worktree", err=True)
        sys.exit(1)

    # ff-only merge
    click.echo(f"Merging {task.branch} into main...")
    r = subprocess.run(
        ["git", "merge", task.branch, "--ff-only"],
        cwd=main_worktree, capture_output=True, text=True,
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
        click.echo(f"Error: task not found: {name}", err=True)
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
        capture_output=True, text=True, cwd=task.worktree if os.path.exists(task.worktree) else ".",
    )
    for line in r.stdout.split("\n"):
        if line.startswith("worktree ") and "/tmp/duo-worktrees" not in line:
            main_worktree = line.split(" ", 1)[1]
            break
    repo_cwd = main_worktree or "."

    # Remove worktree
    if os.path.exists(task.worktree):
        subprocess.run(["git", "worktree", "remove", "--force", task.worktree], cwd=repo_cwd)

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
