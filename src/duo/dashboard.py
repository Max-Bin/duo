"""Dashboard — real-time terminal UI using Rich.

Displays task status, queue info, and recent events in a live-updating table.
"""

from __future__ import annotations

import time
from datetime import datetime

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from duo.poller import age
from duo.protocol import Task, TaskStatus, list_tasks, read_heartbeat, read_jsonl
from duo.scheduler import queue_status

# Status colors
STATUS_COLORS: dict[str, str] = {
    "created": "dim",
    "queued": "yellow",
    "session_starting": "cyan",
    "prompt_sent": "blue",
    "acked": "blue",
    "running": "green",
    "result_reported": "green",
    "verifying": "magenta",
    "correcting": "yellow",
    "blocked": "red",
    "failed": "red bold",
    "completed": "green bold",
    "escalated": "red",
}


def _status_text(status: TaskStatus) -> Text:
    """Create colored status text."""
    color = STATUS_COLORS.get(status.value, "white")
    return Text(status.value, style=color)


def _build_tasks_table(tasks: list[Task]) -> Table:
    """Build the main tasks table."""
    table = Table(title="Tasks", expand=True, border_style="dim")
    table.add_column("ID", style="bold", min_width=15)
    table.add_column("Status", min_width=12)
    table.add_column("Step", justify="center", min_width=6)
    table.add_column("Attempt", justify="center", min_width=7)
    table.add_column("Heartbeat", min_width=10)
    table.add_column("Description", max_width=40)

    for task in tasks:
        hb = read_heartbeat(task)
        if hb and hb.incarnation == task.incarnation_id:
            hb_age = age(hb.ts)
            if hb_age < 30:
                hb_text = Text(f"{hb_age:.0f}s ago", style="green")
            elif hb_age < 90:
                hb_text = Text(f"{hb_age:.0f}s ago", style="yellow")
            else:
                hb_text = Text(f"{hb_age:.0f}s ago", style="red")
        else:
            hb_text = Text("—", style="dim")

        step_str = f"{task.current_step}/{len(task.subtasks)}"
        desc = task.description[:40] if len(task.description) > 40 else task.description

        table.add_row(
            task.id,
            _status_text(task.status),
            step_str,
            str(task.current_attempt),
            hb_text,
            desc,
        )

    return table


def _build_queue_panel() -> Panel:
    """Build the queue status panel."""
    qs = queue_status()
    lines = [
        f"[bold]Slots:[/] {qs['active_count']}/{qs['max_parallel']}",
        f"[bold]Active:[/] {', '.join(qs['active_tasks']) or '—'}",
        f"[bold]Queued:[/] {', '.join(qs['queued_tasks']) or '—'}",
    ]
    return Panel("\n".join(lines), title="Queue", border_style="dim")


def _build_events_panel(tasks: list[Task], max_events: int = 8) -> Panel:
    """Build recent events panel from all task journals."""
    all_events: list[tuple[str, str, str]] = []
    for task in tasks:
        events = read_jsonl(task.journal_path)
        for ev in events[-5:]:
            ts = ev.get("ts", "")
            event_type = ev.get("event", "")
            all_events.append((ts, task.id, event_type))

    all_events.sort(key=lambda x: x[0], reverse=True)
    all_events = all_events[:max_events]

    lines: list[str] = []
    for ts, task_id, event_type in all_events:
        short_ts = ts.split("T")[1][:8] if "T" in ts else ts[:8]
        if "error" in event_type or "failed" in event_type:
            color = "red"
        elif "completed" in event_type or "passed" in event_type:
            color = "green"
        else:
            color = "white"
        lines.append(f"[dim]{short_ts}[/] [{color}]{event_type}[/] [dim]{task_id}[/]")

    content = "\n".join(lines) if lines else "[dim]No events[/]"
    return Panel(content, title="Recent Events", border_style="dim")


def run_dashboard(task_ids: list[str] | None = None, refresh_rate: float = 2.0) -> None:
    """Run the live dashboard."""
    console = Console()

    with Live(
        console=console, refresh_per_second=1.0 / refresh_rate, screen=True
    ) as live:
        try:
            while True:
                tasks = list_tasks()
                if task_ids:
                    tasks = [t for t in tasks if t.id in task_ids]

                layout = Layout()
                layout.split_column(
                    Layout(name="header", size=1),
                    Layout(name="body"),
                    Layout(name="footer", size=max(10, min(12, len(tasks) + 4))),
                )

                now = datetime.now().strftime("%H:%M:%S")
                layout["header"].update(
                    Text(
                        f" Duo Dashboard — {now}  (Ctrl+C to exit)",
                        style="bold white on blue",
                    )
                )

                layout["body"].update(_build_tasks_table(tasks))

                layout["footer"].split_row(
                    Layout(_build_queue_panel(), name="queue", ratio=1),
                    Layout(_build_events_panel(tasks), name="events", ratio=2),
                )

                live.update(layout)
                time.sleep(refresh_rate)
        except KeyboardInterrupt:
            pass

    console.print("[bold green]Dashboard closed.[/]")
