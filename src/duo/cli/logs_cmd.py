"""Logs command — ``duo logs <name>``."""

from __future__ import annotations

import json

import click


@click.command()
@click.argument(
    "name",
    shell_complete=lambda ctx, param, incomplete: __import__(
        "duo.cli", fromlist=["_complete_task_names"]
    )._complete_task_names(ctx, param, incomplete),
)
@click.option(
    "-n", "--lines", default=20, type=click.IntRange(1), help="Number of recent events"
)
@click.option("--all", "show_all", is_flag=True, help="Show all events")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option(
    "--filter", "event_filter", default=None, help="Filter by event type substring"
)
@click.option(
    "--step", "step_filter", default=None, type=int, help="Filter events by step number"
)
@click.option(
    "-c", "--count", "show_count", is_flag=True, help="Print only the event count"
)
@click.option("-q", "--quiet", is_flag=True, help="Print one event type per line")
@click.pass_context
def logs(
    ctx: click.Context,
    name: str,
    lines: int,
    show_all: bool,
    as_json: bool,
    event_filter: str | None,
    step_filter: int | None,
    show_count: bool,
    quiet: bool,
) -> None:
    """Show task journal events."""
    from duo.cli._helpers import _fmt_ts, _load_task_or_fail
    from duo.protocol import read_jsonl

    task = _load_task_or_fail(name)

    events = read_jsonl(task.journal_path)
    if not events:
        click.echo("No events recorded.")
        return

    if event_filter:
        events = [ev for ev in events if event_filter in ev.get("event", "")]

    if step_filter is not None:
        events = [
            ev
            for ev in events
            if ev.get("data", {}).get("step") == step_filter
            or ev.get("step") == step_filter
        ]

    if show_count:
        click.echo(str(len(events)))
        return

    if not show_all:
        events = events[-lines:]

    if quiet:
        for ev in events:
            click.echo(ev.get("event", "unknown"))
        return

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
