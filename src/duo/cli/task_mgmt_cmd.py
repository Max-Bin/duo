"""Task management commands — ``duo stop``, ``duo kill``, ``duo merge``."""

from __future__ import annotations

import json
import os

import click

from duo.cli._helpers import (
    _complete_task_names,
    _find_main_worktree,
    _load_task_or_fail,
    _remove_worktree_and_branch,
    _run_git,
)
from duo.errors import DuoUserError
from duo.protocol import TaskStatus


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


@click.command()
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


@click.command()
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


@click.command()
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
