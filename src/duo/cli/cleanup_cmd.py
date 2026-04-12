"""Cleanup command — ``duo cleanup``."""

from __future__ import annotations

import json
import re
import shutil

import click

from duo.protocol import TaskStatus, list_tasks

_MAX_AGE_SECONDS = 1000 * 365 * 86400  # ~1000 years upper bound


def _parse_age(age_str: str) -> int:
    """Parse age string like '7d', '24h', '30m' into seconds."""
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


@click.command()
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
    from duo.cli import _remove_worktree_and_branch

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
