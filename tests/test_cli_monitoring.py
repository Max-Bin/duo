"""CLI tests for monitoring commands."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import duo.cli
import duo.protocol
from duo.cli import main
from duo.protocol import (
    TaskStatus,
    save_task,
)


class TestStatus:
    def test_named_task_not_found(self, runner: CliRunner):
        result = runner.invoke(main, ["status", "nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_named_task_shows_details(self, runner: CliRunner, make_task):
        task = make_task()
        result = runner.invoke(main, ["status", "test-task"])
        assert result.exit_code == 0
        assert "test-task" in result.output
        assert task.status.value in result.output
        assert task.incarnation_id in result.output

    def test_named_task_shows_session_started(self, runner: CliRunner, make_task):
        """status displays Session line when session_started_at is set."""
        task = make_task()
        task.session_started_at = "2025-01-15T10:30:00Z"
        from duo.protocol import save_task

        save_task(task)
        result = runner.invoke(main, ["status", "test-task"])
        assert result.exit_code == 0
        assert "Session:" in result.output
        assert "2025-01-15T10:30:00" in result.output

    def test_no_name_no_tasks(self, runner: CliRunner):
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0
        assert "No tasks." in result.output

    def test_no_name_lists_all(self, runner: CliRunner, make_task):
        make_task("alpha", "Alpha task")
        make_task("beta", "Beta task")
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "beta" in result.output

    def test_status_shows_branch(self, runner: CliRunner, make_task):
        """status shows the branch name."""
        task = make_task()
        result = runner.invoke(main, ["status", "test-task"])
        assert result.exit_code == 0
        assert "Branch:" in result.output
        assert task.branch in result.output

    def test_status_shows_description(self, runner: CliRunner, make_task):
        """status shows description when set."""
        make_task("desc-task", "Fix the login bug")
        result = runner.invoke(main, ["status", "desc-task"])
        assert result.exit_code == 0
        assert "Description:" in result.output
        assert "Fix the login bug" in result.output

    def test_status_no_description(self, runner: CliRunner, make_task):
        """status omits description when empty."""
        make_task("no-desc-task", "")
        result = runner.invoke(main, ["status", "no-desc-task"])
        assert result.exit_code == 0
        assert "Description:" not in result.output

    def test_status_shows_heartbeat_file(self, runner: CliRunner, make_task):
        """status shows current file from heartbeat."""
        task = make_task("hb-task")
        import json as _json

        hb_data = {
            "ts": "2025-01-01T12:00:00Z",
            "incarnation": task.incarnation_id,
            "step": 1,
            "status": "working",
            "current_file": "src/main.py",
        }
        task.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        task.heartbeat_path.write_text(_json.dumps(hb_data), encoding="utf-8")
        result = runner.invoke(main, ["status", "hb-task"])
        assert result.exit_code == 0
        assert "Working on:" in result.output
        assert "src/main.py" in result.output
        assert "Last pulse:" in result.output

    def test_status_no_heartbeat(self, runner: CliRunner, make_task):
        """status omits heartbeat lines when no heartbeat file."""
        make_task("no-hb-task")
        result = runner.invoke(main, ["status", "no-hb-task"])
        assert result.exit_code == 0
        assert "Working on:" not in result.output
        assert "Last pulse:" not in result.output

    def test_status_quiet_single(self, runner: CliRunner, make_task):
        """status -q prints only the status value for a named task."""
        make_task("q-task")
        result = runner.invoke(main, ["status", "q-task", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "created"

    def test_status_quiet_all(self, runner: CliRunner, make_task):
        """status -q with no name prints ID\tstatus for each task."""
        make_task("alpha-q")
        make_task("beta-q")
        result = runner.invoke(main, ["status", "-q"])
        assert result.exit_code == 0
        lines = result.output.strip().splitlines()
        assert len(lines) == 2
        assert "\tcreated" in lines[0]

    def test_status_quiet_no_tasks(self, runner: CliRunner):
        """status -q with no tasks prints nothing."""
        result = runner.invoke(main, ["status", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_status_wait_already_reached(self, runner: CliRunner, make_task):
        """status --wait returns immediately if status already matches."""
        task = make_task("wait-ok")
        task.status = TaskStatus.COMPLETED
        save_task(task)
        result = runner.invoke(
            main, ["status", "wait-ok", "--wait", "completed", "--timeout", "2"]
        )
        assert result.exit_code == 0
        assert "reached" in result.output

    def test_status_wait_quiet(self, runner: CliRunner, make_task):
        """status --wait -q prints just the status value."""
        task = make_task("wait-q")
        task.status = TaskStatus.COMPLETED
        save_task(task)
        result = runner.invoke(main, ["status", "wait-q", "--wait", "completed", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "completed"

    def test_status_wait_timeout(self, runner: CliRunner, make_task):
        """status --wait times out if status doesn't match."""
        make_task("wait-to")
        result = runner.invoke(
            main, ["status", "wait-to", "--wait", "completed", "--timeout", "1"]
        )
        assert result.exit_code != 0

    def test_status_wait_timeout_quiet(self, runner: CliRunner, make_task):
        """status --wait -q on timeout prints current status."""
        make_task("wait-tq")
        result = runner.invoke(
            main,
            ["status", "wait-tq", "--wait", "completed", "--timeout", "1", "-q"],
        )
        assert result.exit_code != 0
        assert result.output.strip() == "created"

    def test_status_wait_json(self, runner: CliRunner, make_task):
        """status --wait --json-output prints JSON on success."""
        task = make_task("wait-js")
        task.status = TaskStatus.COMPLETED
        save_task(task)
        result = runner.invoke(
            main,
            ["status", "wait-js", "--wait", "completed", "--json-output"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "completed"

    def test_status_wait_task_disappears(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        make_task,
    ):
        """status --wait errors if task disappears mid-poll."""
        make_task("wait-gone")
        call_count = {"n": 0}
        original_load = duo.protocol.load_task

        def _vanishing_load(name: str) -> duo.protocol.Task | None:
            call_count["n"] += 1
            if call_count["n"] >= 2:
                return None
            return original_load(name)

        monkeypatch.setattr("duo.cli.monitoring_cmd.load_task", _vanishing_load)
        result = runner.invoke(
            main,
            ["status", "wait-gone", "--wait", "completed", "--timeout", "5"],
        )
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_status_wait_no_name(self, runner: CliRunner):
        """status --wait without name raises error."""
        result = runner.invoke(main, ["status", "--wait", "completed"])
        assert result.exit_code != 0

    def test_status_wait_invalid_status(self, runner: CliRunner, make_task):
        """status --wait with invalid status name raises error."""
        make_task("wait-inv")
        result = runner.invoke(main, ["status", "wait-inv", "--wait", "bogus"])
        assert result.exit_code != 0
        assert "unknown status" in result.output


# ---------------------------------------------------------------------------
# list command
# ---------------------------------------------------------------------------


class TestList:
    def test_no_tasks(self, runner: CliRunner):
        result = runner.invoke(main, ["list"])
        assert result.exit_code == 0
        assert "No tasks." in result.output

    def test_with_tasks(self, runner: CliRunner, make_task):
        make_task("my-task")
        result = runner.invoke(main, ["list"])
        assert result.exit_code == 0
        # Table header columns
        assert "ID" in result.output
        assert "STATUS" in result.output
        assert "STEP" in result.output
        assert "INCARNATION" in result.output
        # Task row
        assert "my-task" in result.output
        assert "created" in result.output

    def test_multiple_tasks_sorted(self, runner: CliRunner, make_task):
        make_task("zzz-task")
        make_task("aaa-task")
        result = runner.invoke(main, ["list"])
        assert result.exit_code == 0
        lines = result.output.strip().splitlines()
        # Find the task lines (skip header + separator)
        task_lines = [
            l for l in lines if "task" in l.lower() and "---" not in l and "ID" not in l
        ]
        assert len(task_lines) == 2
        # Should be sorted alphabetically
        assert task_lines[0].startswith("aaa-task")
        assert task_lines[1].startswith("zzz-task")

    def test_status_filter(self, runner: CliRunner, make_task):
        """list --status filters tasks by status."""
        make_task("created-task")
        task2 = make_task("done-task")
        task2.status = TaskStatus.COMPLETED
        save_task(task2)
        result = runner.invoke(main, ["list", "--status", "completed"])
        assert result.exit_code == 0
        assert "done-task" in result.output
        assert "created-task" not in result.output

    def test_status_filter_no_match(self, runner: CliRunner, make_task):
        """list --status with no matching tasks shows 'No tasks.'"""
        make_task("a-task")
        result = runner.invoke(main, ["list", "--status", "completed"])
        assert result.exit_code == 0
        assert "No tasks." in result.output

    def test_status_filter_invalid(self, runner: CliRunner, make_task):
        """list --status with invalid status shows error."""
        make_task("a-task")
        result = runner.invoke(main, ["list", "--status", "bogus"])
        assert result.exit_code != 0
        assert "unknown status" in result.output
        assert "Valid statuses" in result.output

    def test_sort_by_name(self, runner: CliRunner, make_task):
        """list --sort name sorts alphabetically."""
        make_task("zzz-task")
        make_task("aaa-task")
        result = runner.invoke(main, ["list", "--sort", "name"])
        assert result.exit_code == 0
        lines = [
            l
            for l in result.output.strip().splitlines()
            if "task" in l.lower() and "---" not in l and "ID" not in l
        ]
        assert lines[0].startswith("aaa-task")
        assert lines[1].startswith("zzz-task")

    def test_sort_by_name_reverse(self, runner: CliRunner, make_task):
        """list --sort name --reverse reverses order."""
        make_task("aaa-task")
        make_task("zzz-task")
        result = runner.invoke(main, ["list", "--sort", "name", "--reverse"])
        assert result.exit_code == 0
        lines = [
            l
            for l in result.output.strip().splitlines()
            if "task" in l.lower() and "---" not in l and "ID" not in l
        ]
        assert lines[0].startswith("zzz-task")
        assert lines[1].startswith("aaa-task")

    def test_sort_by_status(self, runner: CliRunner, make_task):
        """list --sort status groups by status value."""
        task_c = make_task("completed-task")
        task_c.status = TaskStatus.COMPLETED
        save_task(task_c)
        make_task("active-task")
        result = runner.invoke(main, ["list", "--sort", "status"])
        assert result.exit_code == 0
        lines = [
            l
            for l in result.output.strip().splitlines()
            if "task" in l.lower() and "---" not in l and "ID" not in l
        ]
        assert len(lines) == 2
        assert "completed" in lines[0]
        assert "created" in lines[1]

    def test_sort_by_age(self, runner: CliRunner, make_task):
        """list --sort age sorts oldest first."""
        make_task("old-task")
        make_task("new-task")
        result = runner.invoke(main, ["list", "--sort", "age"])
        assert result.exit_code == 0
        lines = [
            l
            for l in result.output.strip().splitlines()
            if "task" in l.lower() and "---" not in l and "ID" not in l
        ]
        assert len(lines) == 2

    def test_quiet_mode(self, runner: CliRunner, make_task):
        """list -q prints only task IDs."""
        make_task("alpha")
        make_task("beta")
        result = runner.invoke(main, ["list", "-q"])
        assert result.exit_code == 0
        lines = result.output.strip().splitlines()
        assert sorted(lines) == ["alpha", "beta"]

    def test_quiet_mode_empty(self, runner: CliRunner):
        """list -q with no tasks produces no output."""
        result = runner.invoke(main, ["list", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_quiet_with_status_filter(self, runner: CliRunner, make_task):
        """list -q --status filters and prints only IDs."""
        t = make_task("done-task")
        t.status = TaskStatus.COMPLETED
        save_task(t)
        make_task("open-task")
        result = runner.invoke(main, ["list", "-q", "--status", "completed"])
        assert result.exit_code == 0
        assert result.output.strip() == "done-task"

    def test_count_mode(self, runner: CliRunner, make_task):
        """list -c prints the number of tasks."""
        make_task("count-a")
        make_task("count-b")
        result = runner.invoke(main, ["list", "-c"])
        assert result.exit_code == 0
        assert result.output.strip() == "2"

    def test_count_with_filter(self, runner: CliRunner, make_task):
        """list -c --status filters then counts."""
        t = make_task("done-count")
        t.status = TaskStatus.COMPLETED
        save_task(t)
        make_task("open-count")
        result = runner.invoke(main, ["list", "-c", "--status", "completed"])
        assert result.exit_code == 0
        assert result.output.strip() == "1"

    def test_count_empty(self, runner: CliRunner):
        """list -c with no tasks prints 0."""
        result = runner.invoke(main, ["list", "-c"])
        assert result.exit_code == 0
        assert result.output.strip() == "0"

    def test_no_header(self, runner: CliRunner, make_task):
        """list --no-header omits the header row."""
        make_task("header-task")
        result = runner.invoke(main, ["list", "--no-header"])
        assert result.exit_code == 0
        assert "ID" not in result.output
        assert "STATUS" not in result.output
        assert "header-task" in result.output

    def test_recent(self, runner: CliRunner, make_task):
        """list --recent N shows only the N newest tasks."""
        for i in range(3):
            t = make_task(f"recent-{i}")
            t.created_at = f"2025-01-0{i + 1}T00:00:00Z"
            save_task(t)
        result = runner.invoke(main, ["list", "--recent", "2"])
        assert result.exit_code == 0
        assert "recent-2" in result.output
        assert "recent-1" in result.output
        assert "recent-0" not in result.output

    def test_recent_with_count(self, runner: CliRunner, make_task):
        """list --recent N --count shows filtered count."""
        for i in range(3):
            t = make_task(f"rc-{i}")
            t.created_at = f"2025-01-0{i + 1}T00:00:00Z"
            save_task(t)
        result = runner.invoke(main, ["list", "--recent", "2", "-c"])
        assert result.exit_code == 0
        assert result.output.strip() == "2"

    def test_active_flag(self, runner: CliRunner, make_task):
        """list --active shows only running/active tasks."""
        t1 = make_task("active-1")
        t1.status = TaskStatus.RUNNING
        save_task(t1)
        t2 = make_task("active-2")
        t2.status = TaskStatus.COMPLETED
        save_task(t2)
        t3 = make_task("active-3")
        t3.status = TaskStatus.ACKED
        save_task(t3)
        result = runner.invoke(main, ["list", "--active"])
        assert result.exit_code == 0
        assert "active-1" in result.output
        assert "active-3" in result.output
        assert "active-2" not in result.output

    def test_active_count(self, runner: CliRunner, make_task):
        """list --active -c shows count of active tasks."""
        t1 = make_task("ac-1")
        t1.status = TaskStatus.RUNNING
        save_task(t1)
        make_task("ac-2")  # CREATED, not active
        result = runner.invoke(main, ["list", "--active", "-c"])
        assert result.exit_code == 0
        assert result.output.strip() == "1"

    def test_finished_flag(self, runner: CliRunner, make_task):
        """list --finished shows only completed/failed/escalated tasks."""
        t1 = make_task("fin-1")
        t1.status = TaskStatus.COMPLETED
        save_task(t1)
        t2 = make_task("fin-2")
        t2.status = TaskStatus.RUNNING
        save_task(t2)
        t3 = make_task("fin-3")
        t3.status = TaskStatus.FAILED
        save_task(t3)
        result = runner.invoke(main, ["list", "--finished"])
        assert result.exit_code == 0
        assert "fin-1" in result.output
        assert "fin-3" in result.output
        assert "fin-2" not in result.output

    def test_finished_count(self, runner: CliRunner, make_task):
        """list --finished -c shows count of finished tasks."""
        t1 = make_task("fc-1")
        t1.status = TaskStatus.COMPLETED
        save_task(t1)
        make_task("fc-2")  # CREATED, not finished
        result = runner.invoke(main, ["list", "--finished", "-c"])
        assert result.exit_code == 0
        assert result.output.strip() == "1"


# ---------------------------------------------------------------------------
# recover command
# ---------------------------------------------------------------------------


class TestMonitorCommand:
    def test_monitor_invocation(self, runner: CliRunner):
        """monitor calls run_monitor and handles KeyboardInterrupt."""
        with patch("duo.commander.monitor", side_effect=KeyboardInterrupt):
            result = runner.invoke(main, ["monitor"])
            assert result.exit_code == 0
            assert "Monitor stopped" in result.output

    def test_monitor_with_names(self, runner: CliRunner):
        """monitor passes task IDs to run_monitor."""
        with patch("duo.commander.monitor") as mock_mon:
            mock_mon.return_value = None
            result = runner.invoke(main, ["monitor", "task-a", "task-b"])
            assert result.exit_code == 0
            mock_mon.assert_called_once_with(["task-a", "task-b"])

    def test_monitor_max_time_sets_config(self, runner: CliRunner):
        """--max-time sets task_timeout config before calling monitor."""
        with (
            patch("duo.commander.monitor") as mock_mon,
            patch("duo.config.set_config") as mock_set,
        ):
            mock_mon.return_value = None
            result = runner.invoke(main, ["monitor", "--max-time", "60"])
            assert result.exit_code == 0
            mock_set.assert_called_once_with("task_timeout", "60")
            mock_mon.assert_called_once()

    def test_monitor_max_time_zero_no_set(self, runner: CliRunner):
        """--max-time=0 (default) does not call set_config."""
        with (
            patch("duo.commander.monitor") as mock_mon,
            patch("duo.config.set_config") as mock_set,
        ):
            mock_mon.return_value = None
            result = runner.invoke(main, ["monitor"])
            assert result.exit_code == 0
            mock_set.assert_not_called()


# ---------------------------------------------------------------------------
# watch command
# ---------------------------------------------------------------------------


class TestWatchCommand:
    def test_watch_invocation(self, runner: CliRunner):
        """watch calls watch_tasks and handles KeyboardInterrupt."""
        with patch("duo.commander.watch_tasks", side_effect=KeyboardInterrupt):
            result = runner.invoke(main, ["watch"])
            assert result.exit_code == 0
            assert "Watch stopped" in result.output

    def test_watch_with_names(self, runner: CliRunner):
        """watch passes task IDs to watch_tasks."""
        with patch("duo.commander.watch_tasks") as mock_w:
            mock_w.return_value = 0
            result = runner.invoke(main, ["watch", "task-a", "task-b"])
            assert result.exit_code == 0
            mock_w.assert_called_once_with(
                ["task-a", "task-b"],
                timeout=300,
                interval=5.0,
                once=False,
                auto_approve=False,
            )

    def test_watch_no_names(self, runner: CliRunner):
        """watch with no names passes None."""
        with patch("duo.commander.watch_tasks") as mock_w:
            mock_w.return_value = 0
            result = runner.invoke(main, ["watch"])
            assert result.exit_code == 0
            mock_w.assert_called_once_with(
                None, timeout=300, interval=5.0, once=False, auto_approve=False
            )

    def test_watch_options(self, runner: CliRunner):
        """watch passes --timeout, --interval, --once correctly."""
        with patch("duo.commander.watch_tasks") as mock_w:
            mock_w.return_value = 1
            result = runner.invoke(
                main, ["watch", "--timeout", "60", "--interval", "2.0", "--once"]
            )
            assert result.exit_code == 0
            mock_w.assert_called_once_with(
                None, timeout=60.0, interval=2.0, once=True, auto_approve=False
            )

    def test_watch_negative_timeout(self, runner: CliRunner):
        """--timeout <= 0 rejected."""
        result = runner.invoke(main, ["watch", "--timeout", "0"])
        assert result.exit_code != 0
        assert "--timeout must be > 0" in result.output

    def test_watch_negative_interval(self, runner: CliRunner):
        """--interval <= 0 rejected."""
        result = runner.invoke(main, ["watch", "--interval", "0"])
        assert result.exit_code != 0
        assert "--interval must be > 0" in result.output

    def test_watch_auto_approve_flag(self, runner: CliRunner):
        """--auto-approve passes auto_approve=True."""
        with patch("duo.commander.watch_tasks") as mock_w:
            mock_w.return_value = 1
            result = runner.invoke(main, ["watch", "--auto-approve"])
            assert result.exit_code == 0
            mock_w.assert_called_once_with(
                None, timeout=300, interval=5.0, once=False, auto_approve=True
            )


# ---------------------------------------------------------------------------
# dashboard command
# ---------------------------------------------------------------------------


class TestDashboardCommand:
    def test_dashboard_invocation(self, runner: CliRunner):
        """dashboard calls run_dashboard."""
        # Patch the lazy import target inside the command
        with patch.dict("sys.modules", {"duo.dashboard": MagicMock()}) as _:
            import sys

            mock_dashboard_mod = sys.modules["duo.dashboard"]
            mock_dashboard_mod.run_dashboard = MagicMock()
            result = runner.invoke(main, ["dashboard", "task-x"])
            assert result.exit_code == 0
            mock_dashboard_mod.run_dashboard.assert_called_once_with(
                ["task-x"], refresh_rate=2.0
            )

    def test_dashboard_no_rich(self, runner: CliRunner):
        """dashboard shows error when rich is not installed."""
        import sys

        # Temporarily remove duo.dashboard from sys.modules to force ImportError
        saved = sys.modules.pop("duo.dashboard", None)
        with patch.dict("sys.modules", {"duo.dashboard": None}):
            result = runner.invoke(main, ["dashboard"])
            # Should get import error message
            assert result.exit_code != 0 or "rich" in result.output.lower()
        if saved is not None:
            sys.modules["duo.dashboard"] = saved

    def test_dashboard_negative_refresh(self, runner: CliRunner):
        """dashboard rejects non-positive --refresh."""
        result = runner.invoke(main, ["dashboard", "--refresh", "0"])
        assert result.exit_code != 0
        assert "--refresh must be > 0" in result.output

    def test_dashboard_negative_refresh_value(self, runner: CliRunner):
        """dashboard rejects negative --refresh."""
        result = runner.invoke(main, ["dashboard", "--refresh", "-1"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# version fallback
# ---------------------------------------------------------------------------
