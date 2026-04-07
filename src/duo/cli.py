"""CLI entry point — thin interface to commander."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import click

from duo.protocol import (
    DUO_DIR,  # noqa: F401 — used by test monkeypatching
    Task,
    TASKS_DIR,
    TaskStatus,
    create_task,
    list_tasks,
    load_task,
    replay_state,
    Subtask,
)


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Verbose output")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """Duo — Agent Orchestration Runtime."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    TASKS_DIR.mkdir(parents=True, exist_ok=True)


@main.command()
def version() -> None:
    """Show Duo version."""
    try:
        from importlib.metadata import version as pkg_version

        ver = pkg_version("duo")
    except Exception:
        ver = "0.5.0-dev"
    click.echo(f"duo {ver}")


@main.command()
@click.argument("name")
@click.option("--repo", default=".", help="Git repo path to create worktree from")
@click.option("--desc", default="", help="Task description")
def start(name: str, repo: str, desc: str) -> None:
    """Create a task with worktree + Copilot session."""
    from duo.commander import start_session

    repo = os.path.abspath(repo)

    # Check for duplicate task
    existing = load_task(name)
    if existing is not None:
        click.echo(f"Error: task '{name}' already exists (status: {existing.status.value}). Use 'duo kill {name}' first.", err=True)
        sys.exit(1)

    worktree = f"/tmp/duo-worktrees/{name}"
    branch = f"duo/{name}"

    # Get base commit
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, cwd=repo,
    )
    if result.returncode != 0:
        click.echo(f"Error: '{repo}' is not a git repository. Run 'git init' first or specify --repo.", err=True)
        sys.exit(1)
    base_commit = result.stdout.strip()

    # Create worktree
    result = subprocess.run(
        ["git", "worktree", "add", worktree, "-b", branch],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        click.echo(f"Error: failed to create worktree: {result.stderr.strip()}", err=True)
        sys.exit(1)

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
        click.echo(f"  Queued ({qs['queued_count']} in queue). {qs['active_count']}/{qs['max_parallel']} slots in use.")
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

    task = load_task(name)
    if task is None:
        click.echo(f"Error: task '{name}' not found. Run 'duo list' to see available tasks.", err=True)
        sys.exit(1)

    if task.status == TaskStatus.QUEUED:
        click.echo(f"Warning: task '{name}' is queued and not yet started. Prompt will be sent when task starts.", err=True)

    send_task_prompt(task, prompt)
    click.echo(f"Sent to {name} (step={task.current_step} attempt={task.current_attempt})")


@main.command()
@click.argument("name", required=False)
def status(name: str | None = None) -> None:
    """Show task status."""
    if name:
        task = load_task(name)
        if task is None:
            click.echo(f"Error: task '{name}' not found. Run 'duo list' to see available tasks.", err=True)
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
        click.echo(f"Error: task '{name}' not found. Run 'duo list' to see available tasks.", err=True)
        sys.exit(1)

    if task.status != TaskStatus.COMPLETED:
        click.echo(f"Error: task '{name}' is '{task.status.value}', not 'completed'. Wait for completion or check 'duo logs {name}'.", err=True)
        sys.exit(1)

    worktree = task.worktree

    if not os.path.exists(worktree):
        click.echo(f"Error: worktree '{worktree}' does not exist. Task may have been cleaned up.", err=True)
        sys.exit(1)

    # Fetch and rebase
    click.echo("Fetching and rebasing...")
    r = subprocess.run(["git", "fetch", "origin", "main"], cwd=worktree, capture_output=True, text=True)
    if r.returncode != 0:
        click.echo("Warning: fetch failed, proceeding with local state")

    r = subprocess.run(["git", "rebase", "origin/main"], cwd=worktree, capture_output=True, text=True)
    if r.returncode != 0:
        click.echo(f"Rebase conflict! Escalating to human.\n{r.stderr}", err=True)
        abort = subprocess.run(["git", "rebase", "--abort"], cwd=worktree, capture_output=True, text=True)
        if abort.returncode != 0:
            click.echo(f"Warning: could not abort rebase: {abort.stderr.strip()}", err=True)
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
        click.echo(f"Error: task '{name}' not found. Run 'duo list' to see available tasks.", err=True)
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


@main.command()
@click.argument("file", type=click.Path(exists=True))
@click.option("--repo", default=".", help="Git repo path")
def batch(file: str, repo: str) -> None:
    """Create multiple tasks from a file (JSON or YAML)."""
    from duo.commander import start_session
    from duo.scheduler import enqueue_or_start, queue_status

    repo = os.path.abspath(repo)

    # Parse file
    file_path = Path(file)
    content = file_path.read_text()

    if file_path.suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-untyped]
            tasks_data = yaml.safe_load(content)
        except ImportError:
            click.echo("Error: PyYAML not installed. Run: uv pip install pyyaml", err=True)
            click.echo("Or use JSON format instead.", err=True)
            sys.exit(1)
    else:
        tasks_data = json.loads(content)

    if not isinstance(tasks_data, dict) or "tasks" not in tasks_data:
        click.echo("Error: file must contain a 'tasks' key with a list of tasks. See examples/tasks.json", err=True)
        sys.exit(1)

    if not tasks_data.get("tasks"):
        click.echo("No tasks defined in file.", err=True)
        sys.exit(1)

    # Get base commit
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, cwd=repo,
    )
    if result.returncode != 0:
        click.echo(f"Error: '{repo}' is not a git repository. Run 'git init' first or specify --repo.", err=True)
        sys.exit(1)
    base_commit = result.stdout.strip()

    created = 0
    for task_def in tasks_data["tasks"]:
        name = task_def["name"]
        desc = task_def.get("description", f"Task {name}")
        target_files = task_def.get("target_files", [])
        writable = task_def.get("writable_paths", ["*"])

        worktree = f"/tmp/duo-worktrees/{name}"
        branch = f"duo/{name}"

        # Create worktree
        r = subprocess.run(
            ["git", "worktree", "add", worktree, "-b", branch],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            click.echo(f"  ✗ {name}: failed to create worktree: {r.stderr.strip()}", err=True)
            continue

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
        created += 1

    qs = queue_status()
    click.echo(f"\nBatch complete: {created} tasks created")
    click.echo(f"  Active: {qs['active_count']}/{qs['max_parallel']}")
    click.echo(f"  Queued: {qs['queued_count']}")
    if qs['queued_count'] > 0:
        click.echo("Run 'duo monitor' to process the queue.")


@main.command()
def queue() -> None:
    """Show queue status."""
    from duo.scheduler import queue_status
    qs = queue_status()
    click.echo(f"Slots: {qs['active_count']}/{qs['max_parallel']} in use")
    if qs['active_tasks']:
        click.echo(f"Active: {', '.join(qs['active_tasks'])}")
    if qs['queued_tasks']:
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
                    ts = ts.split("T")[1][:8]
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
                    ts = ts.split("T")[1][:8]
                click.echo(f"  {ts} {entry.get('action', '?')} [{entry.get('label', '?')}]")


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
        click.echo(f"Error: task '{name}' not found. Run 'duo list' to see available tasks.", err=True)
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
            ts = ts.split("T")[1][:8]  # HH:MM:SS
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
    from duo.protocol import read_jsonl, read_heartbeat, read_result_for_step, read_ack_for_step

    task = load_task(name)
    if task is None:
        click.echo(f"Error: task '{name}' not found. Run 'duo list' to see available tasks.", err=True)
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
                ts = ts.split("T")[1][:8]
            click.echo(f"  {ts} {ev.get('event', '?')}")


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
    from duo.config import set_config, DEFAULTS
    if key not in DEFAULTS:
        click.echo(f"Warning: '{key}' is not a known config key", err=True)
    result = set_config(key, value)
    click.echo(f"{key} = {result}")


@config.command("list")
def config_list() -> None:
    """List all config values."""
    from duo.config import load_config, DEFAULTS
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


@main.command()
@click.argument("name")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), default="text", help="Output format")
@click.option("-o", "--output", "outfile", type=click.Path(), help="Write to file instead of stdout")
def export(name: str, fmt: str, outfile: str | None) -> None:
    """Export task report (events, files changed, summary)."""
    from duo.protocol import read_jsonl, read_result_for_step

    task = load_task(name)
    if task is None:
        click.echo(f"Error: task '{name}' not found. Run 'duo list' to see available tasks.", err=True)
        sys.exit(1)

    events = read_jsonl(task.journal_path)

    if fmt == "json":
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
        # Collect results for each step
        results = []
        for s in task.subtasks:
            max_attempt = task.current_attempt + 1 if s.step_id == task.current_step else 2
            for attempt in range(1, max_attempt):
                result = read_result_for_step(task, s.step_id, attempt)
                if result:
                    results.append({
                        "step": result.step,
                        "attempt": result.attempt,
                        "status": result.status,
                        "summary": result.summary,
                        "files_changed": result.files_changed,
                    })
        report["results"] = results

        output = json.dumps(report, ensure_ascii=False, indent=2)
    else:
        # Text format
        lines = []
        lines.append(f"Task Report: {task.id}")
        lines.append(f"{'=' * 40}")
        lines.append(f"Description: {task.description}")
        lines.append(f"Status:      {task.status.value}")
        lines.append(f"Branch:      {task.branch}")
        lines.append(f"Created:     {task.created_at}")
        lines.append(f"Step:        {task.current_step}/{len(task.subtasks)}")
        lines.append(f"Attempt:     {task.current_attempt}")
        lines.append("")

        # Steps
        lines.append("Steps:")
        for s in task.subtasks:
            lines.append(f"  {s.step_id}. {s.description}")
            if s.target_files:
                lines.append(f"     Files: {', '.join(s.target_files)}")
        lines.append("")

        # Events summary
        lines.append(f"Events ({len(events)} total):")
        for ev in events[-20:]:
            ts = ev.get("ts", "?")
            if "T" in ts:
                ts = ts.split("T")[1][:8]
            lines.append(f"  {ts} {ev.get('event', '?')}")

        output = "\n".join(lines)

    if outfile:
        Path(outfile).write_text(output + "\n")
        click.echo(f"Report written to {outfile}")
    else:
        click.echo(output)


@main.command()
@click.option("--all", "clean_all", is_flag=True, help="Clean all finished tasks (completed + failed)")
@click.option("--force", is_flag=True, help="Skip confirmation")
@click.option("--keep-journal", is_flag=True, help="Keep journal files")
def cleanup(clean_all: bool, force: bool, keep_journal: bool) -> None:
    """Clean up completed and failed tasks."""
    import shutil

    tasks = list_tasks()

    if clean_all:
        targets = [t for t in tasks if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)]
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
