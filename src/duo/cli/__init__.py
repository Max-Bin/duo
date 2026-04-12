"""CLI entry point — thin interface to commander."""

from __future__ import annotations

import json
import logging
import os
import subprocess  # noqa: F401 — test patch target: duo.cli.subprocess.run
import sys
from pathlib import Path
from typing import Any

import click

from duo.cli._helpers import (  # noqa: F401 — re-export shared helpers
    _ALIASES as _ALIASES,
)
from duo.cli._helpers import (
    _COMMAND_SECTIONS as _COMMAND_SECTIONS,
)
from duo.cli._helpers import (
    _GIT_TIMEOUT as _GIT_TIMEOUT,
)
from duo.cli._helpers import (
    _MAX_AGE_SECONDS as _MAX_AGE_SECONDS,
)
from duo.cli._helpers import (
    _TERMINAL_STATES as _TERMINAL_STATES,  # noqa: F401 — re-export
)
from duo.cli._helpers import (
    _TMUX_TIMEOUT as _TMUX_TIMEOUT,
)
from duo.cli._helpers import (
    _complete_status_values as _complete_status_values,
)
from duo.cli._helpers import (
    _complete_task_names,
    _fmt_ts,
    _load_task_or_fail,
    _OrderedGroup,
    _run_git,
)
from duo.cli._helpers import (
    _complete_thinking_names as _complete_thinking_names,
)
from duo.cli._helpers import (
    _create_worktree as _create_worktree,  # noqa: F401 — re-export for tests
)
from duo.cli._helpers import (
    _fmt_age as _fmt_age,
)
from duo.cli._helpers import (
    _safe_join as _safe_join,
)
from duo.cli._helpers import (
    _validate_task_name as _validate_task_name,
)
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
    DUO_DIR as DUO_DIR,  # noqa: F401 — re-export for test monkeypatching
)
from duo.protocol import (
    TASKS_DIR,
    Subtask,
    Task,
    TaskStatus,
    create_task,
    list_tasks,
    read_jsonl,
)

__all__ = ["main"]


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


from duo.cli.lifecycle_cmd import go as go_command  # noqa: E402
from duo.cli.lifecycle_cmd import init as init_command  # noqa: E402
from duo.cli.lifecycle_cmd import start as start_command  # noqa: E402

main.add_command(start_command, "start")
main.add_command(init_command, "init")
main.add_command(go_command, "go")


from duo.cli.task_ops_cmd import recover as recover_command  # noqa: E402
from duo.cli.task_ops_cmd import send as send_command  # noqa: E402

main.add_command(send_command, "send")


from duo.cli.monitoring_cmd import (  # noqa: E402
    _print_task as _print_task,  # noqa: F401 — re-export
)
from duo.cli.monitoring_cmd import (  # noqa: E402
    dashboard as dashboard_command,
)
from duo.cli.monitoring_cmd import (
    list_cmd as list_command,
)
from duo.cli.monitoring_cmd import (
    monitor as monitor_command,
)
from duo.cli.monitoring_cmd import (
    status as status_command,
)
from duo.cli.monitoring_cmd import (
    watch as watch_command,
)

main.add_command(status_command, "status")
main.add_command(list_command, "list")
main.add_command(monitor_command, "monitor")
main.add_command(watch_command, "watch")


main.add_command(recover_command, "recover")


from duo.cli.task_mgmt_cmd import kill as kill_command  # noqa: E402
from duo.cli.task_mgmt_cmd import merge as merge_command  # noqa: E402
from duo.cli.task_mgmt_cmd import stop as stop_command  # noqa: E402

main.add_command(merge_command, "merge")
main.add_command(stop_command, "stop")
main.add_command(kill_command, "kill")


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


main.add_command(dashboard_command, "dashboard")


from duo.cli.logs_cmd import (  # noqa: E402
    logs as logs_command,
)

main.add_command(logs_command, "logs")


from duo.cli.inspect_cmd import inspect as inspect_command

main.add_command(inspect_command, "inspect")


from duo.cli.task_ops_cmd import resume as resume_command  # noqa: E402
from duo.cli.task_ops_cmd import retry as retry_command  # noqa: E402

main.add_command(resume_command, "resume")
main.add_command(retry_command, "retry")


# ---------------------------------------------------------------------------
# Config — extracted to duo.cli.config_cmd
# ---------------------------------------------------------------------------
from duo.cli.config_cmd import (
    _complete_config_keys as _complete_config_keys,  # noqa: F401,E402 — re-export
)
from duo.cli.config_cmd import config as config_group  # noqa: E402

main.add_command(config_group, "config")


# ---------------------------------------------------------------------------
# duo events — extracted to duo.cli.events_cmd
# ---------------------------------------------------------------------------
from duo.cli.events_cmd import (  # noqa: E402
    _WATCH_EVENTS_DIR as _WATCH_EVENTS_DIR,  # noqa: F401 — re-export
)
from duo.cli.events_cmd import events as events_group  # noqa: E402

main.add_command(events_group, "events")


# ---------------------------------------------------------------------------
# duo ceo-* — Ergonomic CEO workflow commands
# ---------------------------------------------------------------------------


from duo.cli.ceo_cmd import (  # noqa: E402
    _enforce_not_at_main_prompt as _enforce_not_at_main_prompt,  # noqa: F401 — re-export
)
from duo.cli.ceo_cmd import (  # noqa: E402
    _log_pr_budget_warning as _log_pr_budget_warning,  # noqa: F401 — re-export
)
from duo.cli.ceo_cmd import (  # noqa: E402
    assert_not_at_main_prompt as assert_not_at_main_prompt,  # noqa: F401 — re-export
)
from duo.cli.ceo_cmd import (  # noqa: E402
    ceo_approve,
    ceo_select,
    ceo_status,
    ceo_wait,
)

main.add_command(ceo_wait, "ceo-wait")
main.add_command(ceo_select, "ceo-select")
main.add_command(ceo_approve, "ceo-approve")
main.add_command(ceo_status, "ceo-status")


from duo.cli.cleanup_cmd import (  # noqa: E402
    _parse_age as _parse_age,  # noqa: F401 — re-export
)
from duo.cli.cleanup_cmd import (  # noqa: E402
    cleanup as cleanup_command,
)

main.add_command(cleanup_command, "cleanup")


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
