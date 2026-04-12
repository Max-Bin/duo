"""Batch and queue commands."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import click

from duo.cli._helpers import (
    _run_git,
    _safe_join,
    _validate_task_name,
)
from duo.config import get_config
from duo.protocol import (
    Subtask,
    TaskStatus,
    create_task,
)

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


@click.command()
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


@click.command()
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
