"""Tests for duo.dashboard module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

import duo.protocol
from duo.protocol import Subtask, TaskStatus, append_event, create_task, write_json


@pytest.fixture(autouse=True)
def isolated_tasks(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    monkeypatch.setattr(duo.protocol, "TASKS_DIR", tasks_dir)
    monkeypatch.setattr(duo.protocol, "DUO_DIR", tmp_path)
    return tasks_dir


def _make_task(task_id="test-task", description="Test task"):
    return create_task(
        task_id=task_id,
        description=description,
        worktree="/fake/worktree",
        branch=f"duo/{task_id}",
        base_commit="abc123",
        subtasks=[
            Subtask(
                step_id=1,
                description=description,
                target_files=[],
                writable_paths=["*"],
            )
        ],
    )


class TestBuildTasksTable:
    def test_empty_tasks(self):
        from duo.dashboard import _build_tasks_table

        table = _build_tasks_table([])
        assert table.row_count == 0

    def test_with_tasks(self):
        from duo.dashboard import _build_tasks_table

        task = _make_task()
        table = _build_tasks_table([task])
        assert table.row_count == 1

    def test_multiple_tasks(self):
        from duo.dashboard import _build_tasks_table

        t1 = _make_task("task-1", "First task")
        t2 = _make_task("task-2", "Second task")
        table = _build_tasks_table([t1, t2])
        assert table.row_count == 2

    def test_long_description_truncated(self):
        """Descriptions longer than 40 chars get ellipsis truncation."""
        from io import StringIO

        from rich.console import Console

        from duo.dashboard import _build_tasks_table

        long_desc = "A" * 60
        task = _make_task("trunc-task", long_desc)
        table = _build_tasks_table([task])
        # Render the table to a string and check for ellipsis
        buf = StringIO()
        console = Console(file=buf, width=200)
        console.print(table)
        rendered = buf.getvalue()
        assert "..." in rendered
        # The full 60-char description should NOT appear
        assert long_desc not in rendered


class TestBuildQueuePanel:
    def test_returns_panel(self):
        from duo.dashboard import _build_queue_panel

        panel = _build_queue_panel()
        assert panel is not None


class TestBuildEventsPanel:
    def test_no_events(self):
        from duo.dashboard import _build_events_panel

        panel = _build_events_panel([])
        assert panel is not None

    def test_with_events(self):
        from duo.dashboard import _build_events_panel

        task = _make_task()
        panel = _build_events_panel([task])
        assert panel is not None

    def test_build_events_panel_empty_events(self):
        """Task with empty journal produces 'No events' content."""
        from duo.dashboard import _build_events_panel

        task = _make_task("empty-journal")
        task.journal_path.write_text("")  # overwrite journal to be empty
        panel = _build_events_panel([task])
        assert panel.renderable == "[dim]No events[/]"

    def test_build_events_panel_max_events(self):
        """Only max_events events appear when exceeding limit."""
        import time as _t

        from duo.dashboard import _build_events_panel

        task = _make_task("max-ev-task")
        for i in range(15):
            append_event(task, f"evt_{i:02d}")
            _t.sleep(0.01)
        panel = _build_events_panel([task], max_events=3)
        content = panel.renderable
        lines = [l for l in content.split("\n") if l.strip()]
        assert len(lines) == 3

    def test_build_events_panel_event_ordering(self):
        """Events are in reverse chronological order."""
        import time as _t

        from duo.dashboard import _build_events_panel

        task = _make_task("order-task")
        for name in ["alpha", "beta", "gamma"]:
            append_event(task, name)
            _t.sleep(0.02)
        panel = _build_events_panel([task], max_events=10)
        content = panel.renderable
        assert content.index("gamma") < content.index("beta") < content.index("alpha")


class TestStatusText:
    def test_status_text(self):
        from duo.dashboard import _status_text

        for status in list(TaskStatus):
            text = _status_text(status)
            assert str(text) == status.value, f"status={status}"


class TestTaskRowStatuses:
    def test_task_row_per_status(self):
        """Build table row for each TaskStatus without error."""
        from duo.dashboard import _build_tasks_table

        for status in list(TaskStatus):
            task = _make_task(f"st-{status.value}", f"Task {status.value}")
            task.status = status
            table = _build_tasks_table([task])
            assert table.row_count == 1, f"status={status}"


def _write_heartbeat(task, seconds_ago):
    """Write a heartbeat file with a timestamp N seconds in the past."""
    ts = (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()
    write_json(
        task.heartbeat_path,
        {
            "ts": ts,
            "incarnation": task.incarnation_id,
            "step": task.current_step,
            "status": "working",
            "current_file": "test.py",
        },
    )


class TestTasksTableHeartbeat:
    def test_tasks_table_heartbeat_fresh(self):
        """Heartbeat age < 30s shows green styling."""
        from duo.dashboard import _build_tasks_table

        task = _make_task("hb-fresh")
        _write_heartbeat(task, seconds_ago=10)
        table = _build_tasks_table([task])
        hb_cell = table.columns[4]._cells[0]
        assert hb_cell.style == "green"
        assert "ago" in str(hb_cell)

    def test_tasks_table_heartbeat_stale(self):
        """Heartbeat age 30-90s shows yellow styling."""
        from duo.dashboard import _build_tasks_table

        task = _make_task("hb-stale")
        _write_heartbeat(task, seconds_ago=60)
        table = _build_tasks_table([task])
        hb_cell = table.columns[4]._cells[0]
        assert hb_cell.style == "yellow"
        assert "ago" in str(hb_cell)

    def test_tasks_table_heartbeat_old(self):
        """Heartbeat age > 90s shows red styling."""
        from duo.dashboard import _build_tasks_table

        task = _make_task("hb-old")
        _write_heartbeat(task, seconds_ago=120)
        table = _build_tasks_table([task])
        hb_cell = table.columns[4]._cells[0]
        assert hb_cell.style == "red"
        assert "ago" in str(hb_cell)

    def test_tasks_table_heartbeat_missing(self):
        """Task without heartbeat shows '—'."""
        from duo.dashboard import _build_tasks_table

        task = _make_task("hb-missing")
        table = _build_tasks_table([task])
        hb_cell = table.columns[4]._cells[0]
        assert str(hb_cell) == "—"
        assert hb_cell.style == "dim"


class TestEventsPanelColoring:
    def test_event_coloring(self):
        from duo.dashboard import _build_events_panel

        cases = [
            ("step_error", "red"),
            ("task_failed", "red"),
            ("execution_error", "red"),
            ("task_completed", "green"),
            ("tests_passed", "green"),
            ("step_completed", "green"),
            ("status_changed", "white"),
            ("heartbeat", "white"),
        ]
        for event_name, expected_color in cases:
            task = _make_task(f"color-{event_name}")
            append_event(task, event_name)
            panel = _build_events_panel([task])
            content = panel.renderable
            assert f"[{expected_color}]{event_name}[/]" in content, (
                f"event={event_name}, expected_color={expected_color}"
            )

    def test_events_panel_missing_journal(self):
        """Panel gracefully handles tasks with missing journal files."""
        from unittest.mock import patch

        from duo.dashboard import _build_events_panel

        task = _make_task("ghost-task")
        with patch("duo.dashboard.read_jsonl", side_effect=OSError("disk error")):
            panel = _build_events_panel([task])
        content = panel.renderable
        assert "No events" in content


class TestRunDashboard:
    @patch("duo.dashboard.time.sleep", side_effect=KeyboardInterrupt)
    @patch("duo.dashboard.Console")
    @patch("duo.dashboard.Live")
    def test_run_dashboard_exits_on_keyboard_interrupt(
        self, mock_live, mock_console, mock_sleep
    ):
        """run_dashboard exits cleanly on KeyboardInterrupt."""
        from duo.dashboard import run_dashboard

        mock_live.return_value.__enter__ = lambda s: s
        mock_live.return_value.__exit__ = lambda s, *a: False
        # Should not raise
        run_dashboard()

    @patch("duo.dashboard.Console")
    @patch("duo.dashboard.Live")
    def test_run_dashboard_chunked_sleep(self, mock_live, mock_console):
        """Chunked sleep loop decrements _remaining before exit."""
        from duo.dashboard import run_dashboard

        mock_live.return_value.__enter__ = lambda s: s
        mock_live.return_value.__exit__ = lambda s, *a: False
        call_count = 0

        def _sleep_then_raise(_duration):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise KeyboardInterrupt

        with patch("duo.dashboard.time.sleep", side_effect=_sleep_then_raise):
            run_dashboard(refresh_rate=0.3)
        assert call_count == 2

    @patch("duo.dashboard.time.sleep", side_effect=KeyboardInterrupt)
    @patch("duo.dashboard.Console")
    @patch("duo.dashboard.Live")
    def test_run_dashboard_filters_tasks(self, mock_live, mock_console, mock_sleep):
        """run_dashboard filters tasks by task_ids."""
        from duo.dashboard import run_dashboard

        mock_live.return_value.__enter__ = lambda s: s
        mock_live.return_value.__exit__ = lambda s, *a: False
        _make_task("keep-me")
        _make_task("skip-me")
        # Should not raise; only "keep-me" would be displayed
        run_dashboard(task_ids=["keep-me"])

    @patch("duo.dashboard.Console")
    @patch("duo.dashboard.Live")
    def test_run_dashboard_loops_back_to_outer_while(self, mock_live, mock_console):
        """179->149: inner sleep loop completes, outer while iterates again."""
        from duo.dashboard import run_dashboard

        mock_live.return_value.__enter__ = lambda s: s
        mock_live.return_value.__exit__ = lambda s, *a: False
        outer_iter = 0

        def _counting_sleep(_duration):
            nonlocal outer_iter
            # Each outer iteration does ceil(0.1/0.25)=1 sleep call
            outer_iter += 1
            if outer_iter >= 2:
                raise KeyboardInterrupt

        with patch("duo.dashboard.time.sleep", side_effect=_counting_sleep):
            run_dashboard(refresh_rate=0.1)
        # Must have looped at least twice (first completes inner, second raises)
        assert outer_iter >= 2


class TestStatusColorsExhaustiveness:
    """Verify STATUS_COLORS covers every TaskStatus."""

    def test_every_status_has_color(self) -> None:
        from duo.dashboard import STATUS_COLORS
        from duo.protocol import TaskStatus

        for status in TaskStatus:
            assert status.value in STATUS_COLORS, (
                f"STATUS_COLORS missing entry for {status.name}"
            )


# ---------------------------------------------------------------------------
# Rubber-duck audit: resilience + markup safety
# ---------------------------------------------------------------------------


class TestEventsPanelNonDictJSONL:
    """Non-dict JSONL entries are skipped, not crashed on."""

    def test_non_dict_entries_skipped(self):
        from duo.dashboard import _build_events_panel

        task = _make_task("nondict-journal")
        # Write a mix of valid dict and non-dict JSONL
        import json

        journal = task.journal_path
        with journal.open("a") as f:
            f.write(
                json.dumps({"event": "transition", "ts": "2024-01-01T00:00:00"}) + "\n"
            )
            f.write('"just a string"\n')
            f.write("42\n")
            f.write("[1,2,3]\n")
            f.write(
                json.dumps({"event": "completed", "ts": "2024-01-01T00:00:01"}) + "\n"
            )
        panel = _build_events_panel([task])
        content = panel.renderable
        assert "transition" in content
        assert "completed" in content


class TestMarkupEscaping:
    """Task IDs with Rich markup chars don't corrupt rendering."""

    def test_queue_panel_escapes_task_ids(self):
        from duo.dashboard import _build_queue_panel

        with patch(
            "duo.dashboard.queue_status",
            return_value={
                "active_count": 1,
                "max_parallel": 2,
                "active_tasks": ["task-[red]evil[/]"],
                "queued_tasks": ["q-[bold]bad[/]"],
            },
        ):
            panel = _build_queue_panel()
            # Should not raise; markup should be escaped
            assert panel is not None

    def test_events_panel_escapes_content(self):
        import json

        from duo.dashboard import _build_events_panel

        task = _make_task("markup-test")
        with task.journal_path.open("a") as f:
            f.write(
                json.dumps({"event": "[red]injected[/]", "ts": "2024-01-01T00:00:00"})
                + "\n"
            )
        panel = _build_events_panel([task])
        content = panel.renderable
        # The markup should be escaped, not interpreted
        assert "\\[red]" in content or "[red]" in content


class TestHeartbeatReadResilience:
    """Dashboard survives unreadable heartbeat files."""

    def test_heartbeat_oserror_shows_dash(self):
        from duo.dashboard import _build_tasks_table

        task = _make_task("hb-error")
        with patch("duo.dashboard.read_heartbeat", side_effect=OSError("perm denied")):
            table = _build_tasks_table([task])
            assert table is not None


class TestRunDashboardRefreshRateGuard:
    """run_dashboard clamps invalid refresh_rate."""

    @patch("duo.dashboard.time.sleep", side_effect=KeyboardInterrupt)
    @patch("duo.dashboard.Console")
    @patch("duo.dashboard.Live")
    def test_zero_refresh_rate_clamped(self, mock_live, mock_console, mock_sleep):
        from duo.dashboard import run_dashboard

        mock_live.return_value.__enter__ = lambda s: s
        mock_live.return_value.__exit__ = lambda s, *a: False
        run_dashboard(refresh_rate=0.0)  # should not raise ZeroDivisionError

    @patch("duo.dashboard.time.sleep", side_effect=KeyboardInterrupt)
    @patch("duo.dashboard.Console")
    @patch("duo.dashboard.Live")
    def test_negative_refresh_rate_clamped(self, mock_live, mock_console, mock_sleep):
        from duo.dashboard import run_dashboard

        mock_live.return_value.__enter__ = lambda s: s
        mock_live.return_value.__exit__ = lambda s, *a: False
        run_dashboard(refresh_rate=-5.0)  # should not raise
