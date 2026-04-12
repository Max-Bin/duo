"""Inspect command — show detailed task information."""

from __future__ import annotations

import json
import os
from typing import Any

import click

from duo.cli._helpers import (
    _complete_task_names,
    _fmt_age,
    _fmt_ts,
    _load_task_or_fail,
    _run_git,
)
from duo.protocol import Heartbeat, Task


def _inspect_gather_worktree_files(worktree: str) -> dict[str, Any]:
    """Gather changed files, untracked files, and diff preview from a worktree."""
    r = _run_git(["diff", "--name-only", "HEAD"], cwd=worktree, check=False)
    changed = (
        [f for f in r.stdout.strip().splitlines() if f] if r.returncode == 0 else []
    )
    r2 = _run_git(
        ["ls-files", "--others", "--exclude-standard"],
        cwd=worktree,
        check=False,
    )
    untracked = (
        [f for f in r2.stdout.strip().splitlines() if f] if r2.returncode == 0 else []
    )
    r3 = _run_git(["diff", "HEAD"], cwd=worktree, check=False)
    diff_preview = r3.stdout[:500] if r3.returncode == 0 else ""
    if len(r3.stdout) > 500:
        diff_preview += "\n... (truncated)"
    return {
        "changed": changed,
        "untracked": untracked,
        "diff_preview": diff_preview,
    }


def _inspect_format_heartbeat(hb: Heartbeat) -> str:
    """Format a heartbeat for text display."""
    lines = [
        "Heartbeat:",
        f"  Timestamp:     {hb.ts}",
        f"  Status:        {hb.status}",
        f"  Current file:  {hb.current_file}",
        f"  Incarnation:   {hb.incarnation}",
    ]
    return "\n".join(lines)


def _inspect_format_ack_result(task: Task) -> str:
    """Read and format the current step's ack and result for text display."""
    from duo.protocol import read_ack_for_step, read_result_for_step

    lines: list[str] = []
    ack = read_ack_for_step(task, task.current_step, task.current_attempt)
    if ack:
        lines.append("")
        lines.append("Ack:")
        lines.append(f"  Acked at:      {ack.acked_at}")
        lines.append(f"  Prompt hash:   {ack.prompt_hash}")

    result = read_result_for_step(task, task.current_step, task.current_attempt)
    if result:
        lines.append("")
        lines.append("Result:")
        lines.append(f"  Status:        {result.status}")
        lines.append(f"  Summary:       {result.summary}")
        if result.files_changed:
            lines.append(f"  Files changed: {', '.join(result.files_changed)}")

    return "\n".join(lines)


def _inspect_build_json(task: Task, include_files: bool) -> dict[str, Any]:
    """Build the full JSON output dict for the inspect command."""
    from duo.protocol import (
        read_ack_for_step,
        read_heartbeat,
        read_result_for_step,
    )

    data: dict[str, Any] = {
        "id": task.id,
        "description": task.description,
        "status": task.status.value,
        "step": task.current_step,
        "total_steps": len(task.subtasks),
        "attempt": task.current_attempt,
        "incarnation_id": task.incarnation_id,
        "worktree": task.worktree,
        "branch": task.branch,
        "base_commit": task.base_commit,
        "created_at": task.created_at,
        "session_started_at": task.session_started_at,
        "age": _fmt_age(task.created_at),
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
            files = _inspect_gather_worktree_files(worktree)
            data["changed_files"] = files["changed"]
            data["untracked_files"] = files["untracked"]
            data["diff_preview"] = files["diff_preview"]
        else:
            data["files_error"] = f"Worktree not found: {worktree}"
    return data


@click.command()
@click.argument("name", shell_complete=_complete_task_names)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option(
    "--include-files",
    is_flag=True,
    help="Show changed files and diff preview from worktree",
)
@click.option(
    "--events",
    "event_count",
    type=int,
    default=None,
    help="Number of recent journal events to show (default: 5, 0 for all)",
)
@click.option("-q", "--quiet", is_flag=True, help="Print only the task status")
def inspect(
    name: str,
    as_json: bool,
    include_files: bool,
    event_count: int | None,
    quiet: bool,
) -> None:
    """Show detailed task information."""
    from duo.protocol import (
        read_heartbeat,
        read_jsonl,
    )

    task = _load_task_or_fail(name)

    if quiet:
        click.echo(task.status.value)
        return

    if as_json:
        data = _inspect_build_json(task, include_files)
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
        click.echo(f"\n{_inspect_format_heartbeat(hb)}")
    else:
        click.echo("\nHeartbeat:       (none)")

    # Ack/result
    ack_result_text = _inspect_format_ack_result(task)
    if ack_result_text:
        click.echo(ack_result_text)

    # Recent events
    events = read_jsonl(task.journal_path)
    pr_count = sum(1 for ev in events if ev.get("event") == "pr_consumed")
    click.echo(f"\nPR Consumed:     {pr_count}")

    if events:
        show_n = event_count if event_count is not None else 5
        recent = events if show_n == 0 else events[-show_n:]
        click.echo(f"\nRecent Events ({len(events)} total, showing {len(recent)}):")
        for ev in recent:
            ts = _fmt_ts(ev.get("ts", "?"))
            click.echo(f"  {ts} {ev.get('event', '?')}")

    if include_files:
        worktree = task.worktree
        if os.path.isdir(worktree):
            files = _inspect_gather_worktree_files(worktree)
            if files["changed"]:
                click.echo(f"\nChanged files ({len(files['changed'])}):")
                for f in files["changed"][:20]:
                    click.echo(f"  M {f}")
            if files["untracked"]:
                click.echo(f"\nUntracked files ({len(files['untracked'])}):")
                for f in files["untracked"][:20]:
                    click.echo(f"  ? {f}")
            if files["diff_preview"]:
                click.echo("\nDiff preview:")
                click.echo(files["diff_preview"])
        else:
            click.echo(f"\n⚠ Worktree not found: {worktree}")
