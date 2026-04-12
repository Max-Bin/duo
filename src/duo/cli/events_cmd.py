"""Events subcommands — ``duo events {list,show,tail,clear}``."""

from __future__ import annotations

import json
import os
from pathlib import Path

import click

from duo.errors import DuoUserError
from duo.protocol import read_json

_WATCH_EVENTS_DIR = Path(os.path.expanduser("~/.duo/watch-events"))


@click.group()
def events() -> None:
    """Manage watch-event signal files."""


@events.command("list")
@click.option("-n", "--limit", default=20, help="Max events to show")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option("-q", "--quiet", is_flag=True, help="Print one event filename per line")
@click.option("-c", "--count", is_flag=True, help="Print only the event count")
def events_list(
    limit: int, *, as_json: bool = False, quiet: bool = False, count: bool = False
) -> None:
    """List recent watch events (newest first)."""
    from duo.cli._helpers import _fmt_ts

    if not _WATCH_EVENTS_DIR.exists():
        if count:
            click.echo("0")
            return
        if quiet:
            return
        if as_json:
            click.echo(json.dumps({"events": [], "total": 0}))
        else:
            click.echo("No events.")
        return
    files = sorted(_WATCH_EVENTS_DIR.glob("*.json"), reverse=True)
    if not files:
        if count:
            click.echo("0")
            return
        if quiet:
            return
        if as_json:
            click.echo(json.dumps({"events": [], "total": 0}))
        else:
            click.echo("No events.")
        return
    if count:
        click.echo(str(len(files)))
        return
    if quiet:
        for f in files[:limit]:
            click.echo(f.name)
        return
    items: list[dict[str, str]] = []
    for f in files[:limit]:
        data = read_json(f)
        if data is None:
            continue
        if as_json:
            items.append({"file": f.name, **data})
        else:
            ts = _fmt_ts(data.get("detected_at", "?"))
            task = data.get("task_id", "?")
            click.echo(f"  {ts}  {task}  {f.name}")
    if as_json:
        click.echo(json.dumps({"events": items, "total": len(files)}))


@events.command("show")
@click.argument("name", default="latest")
def events_show(name: str) -> None:
    """Show a single event (by filename or 'latest')."""
    from duo.cli._helpers import _validate_task_name

    if not _WATCH_EVENTS_DIR.exists():
        raise DuoUserError(
            "No events directory",
            fix="Run a task with 'duo ceo-loop' to generate events.",
        )
    if name == "latest":
        files = sorted(_WATCH_EVENTS_DIR.glob("*.json"), reverse=True)
        if not files:
            raise DuoUserError(
                "No events found",
                fix="Run a task with 'duo ceo-loop' to generate events.",
            )
        target = files[0]
    else:
        _validate_task_name(name)
        target = _WATCH_EVENTS_DIR / name
        if not target.resolve().is_relative_to(
            _WATCH_EVENTS_DIR.resolve()
        ):  # pragma: no cover — defense-in-depth; _validate_task_name rejects all traversal inputs
            raise DuoUserError(
                f"Invalid event name: {name}",
                fix="Event names must be alphanumeric with hyphens/underscores only.",
            )
        if not target.exists():
            target = _WATCH_EVENTS_DIR / f"{name}.json"
            if not target.resolve().is_relative_to(
                _WATCH_EVENTS_DIR.resolve()
            ):  # pragma: no cover — defense-in-depth; _validate_task_name rejects all traversal inputs
                raise DuoUserError(
                    f"Invalid event name: {name}",
                    fix="Event names must be alphanumeric with hyphens/underscores only.",
                )
    if not target.exists():
        raise DuoUserError(
            f"Event file not found: {name}",
            fix="Run 'duo events list' to see available events.",
        )
    data = read_json(target)
    if data is None:
        raise DuoUserError(
            f"Invalid event file: {target.name}",
            fix="The file may be corrupted. Check the raw file content.",
        )
    click.echo(json.dumps(data, indent=2, ensure_ascii=False))


@events.command("tail")
@click.option("-n", "--limit", default=5, help="Initial events to show")
def events_tail(limit: int) -> None:
    """Follow watch events in real-time (Ctrl-C to stop)."""
    import time

    from duo.cli._helpers import _fmt_ts

    _WATCH_EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    # Show existing events first
    existing = sorted(_WATCH_EVENTS_DIR.glob("*.json"))
    for f in existing[-limit:]:
        data = read_json(f)
        if data:
            ts = _fmt_ts(data.get("detected_at", "?"))
            click.echo(f"  {ts}  {data.get('task_id', '?')}  {f.name}")
        seen.add(f.name)
    click.echo("--- following (Ctrl-C to stop) ---")
    try:
        while True:
            for f in sorted(_WATCH_EVENTS_DIR.glob("*.json")):
                if f.name not in seen:
                    seen.add(f.name)
                    data = read_json(f)
                    if data:
                        ts = _fmt_ts(data.get("detected_at", "?"))
                        click.echo(f"  {ts}  {data.get('task_id', '?')}  {f.name}")
            time.sleep(1)
    except KeyboardInterrupt:
        click.echo("\nStopped.")


@events.command("clear")
@click.option("--force", is_flag=True, help="Skip confirmation")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option("-q", "--quiet", is_flag=True, help="Print only the cleared count")
def events_clear(force: bool, *, as_json: bool = False, quiet: bool = False) -> None:
    """Delete all watch-event signal files."""
    if not _WATCH_EVENTS_DIR.exists():
        if quiet:
            click.echo("0")
            return
        if as_json:
            click.echo(json.dumps({"cleared": 0}))
        else:
            click.echo("No events to clear.")
        return
    files = list(_WATCH_EVENTS_DIR.glob("*.json"))
    if not files:
        if quiet:
            click.echo("0")
            return
        if as_json:
            click.echo(json.dumps({"cleared": 0}))
        else:
            click.echo("No events to clear.")
        return
    if not force and not as_json and not quiet:
        click.confirm(f"Delete {len(files)} event(s)?", abort=True)
    for f in files:
        f.unlink(missing_ok=True)
    if quiet:
        click.echo(str(len(files)))
        return
    if as_json:
        click.echo(json.dumps({"cleared": len(files)}))
    else:
        click.echo(f"Cleared {len(files)} event(s).")
