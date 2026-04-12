"""Reporting commands — audit, cost, diff."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click

from duo.cli._helpers import (
    _complete_task_names,
    _fmt_ts,
    _load_task_or_fail,
    _run_git,
)
from duo.errors import DuoUserError
from duo.protocol import (
    Task,
    list_tasks,
    read_jsonl,
)


@click.command()
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


@click.command()
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


@click.command("diff")
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
