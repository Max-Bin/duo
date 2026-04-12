"""CLI tests for logs commands."""

from __future__ import annotations

import json

from click.testing import CliRunner

from duo.cli import main
from duo.protocol import (
    append_event,
)


class TestLogs:
    def test_task_not_found(self, runner: CliRunner):
        result = runner.invoke(main, ["logs", "nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_no_events(self, runner: CliRunner, make_task):
        task = make_task("empty-task")
        # Clear the journal
        task.journal_path.write_text("")
        result = runner.invoke(main, ["logs", "empty-task"])
        assert result.exit_code == 0
        assert "No events" in result.output

    def test_shows_events(self, runner: CliRunner, make_task):
        make_task("test-task")
        result = runner.invoke(main, ["logs", "test-task"])
        assert result.exit_code == 0
        assert "task_created" in result.output

    def test_lines_limit(self, runner: CliRunner, make_task):
        make_task("test-task")
        result = runner.invoke(main, ["logs", "test-task", "-n", "1"])
        assert result.exit_code == 0
        # With -n 1, should only show 1 event line
        event_lines = [
            l
            for l in result.output.strip().splitlines()
            if "·" in l or "✓" in l or "✗" in l
        ]
        assert len(event_lines) == 1

    def test_logs_json_output(self, runner: CliRunner, make_task):
        make_task("json-task")
        result = runner.invoke(main, ["logs", "json-task", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) >= 1
        assert data[0]["event"] == "task_created"

    def test_filter_events(self, runner: CliRunner, make_task):
        """logs --filter narrows events by event type substring."""
        task = make_task("filter-task")
        from duo.protocol import append_event

        append_event(task, "step_started", {"step": 1})
        append_event(task, "step_completed", {"step": 1})
        result = runner.invoke(main, ["logs", "filter-task", "--filter", "completed"])
        assert result.exit_code == 0
        assert "step_completed" in result.output
        assert "task_created" not in result.output

    def test_filter_no_match(self, runner: CliRunner, make_task):
        """logs --filter with no matching events shows no event lines."""
        make_task("filter-empty")
        result = runner.invoke(
            main, ["logs", "filter-empty", "--filter", "nonexistent"]
        )
        assert result.exit_code == 0
        event_lines = [
            l
            for l in result.output.strip().splitlines()
            if "·" in l or "✓" in l or "✗" in l
        ]
        assert len(event_lines) == 0


# ---------------------------------------------------------------------------
# inspect command
# ---------------------------------------------------------------------------


class TestLogsFormatting:
    def test_error_event_symbol(self, runner: CliRunner, make_task):
        """Error events get ✗ symbol."""
        task = make_task("log-err")
        append_event(task, "step_error", {"reason": "something broke"})
        result = runner.invoke(main, ["logs", "log-err"])
        assert result.exit_code == 0
        assert "✗" in result.output

    def test_completed_event_symbol(self, runner: CliRunner, make_task):
        """Completed events get ✓ symbol."""
        task = make_task("log-done")
        append_event(task, "step_completed", {"step": 1})
        result = runner.invoke(main, ["logs", "log-done"])
        assert result.exit_code == 0
        assert "✓" in result.output

    def test_long_data_truncated(self, runner: CliRunner, make_task):
        """Long string values in event data are truncated."""
        task = make_task("log-trunc")
        append_event(task, "test_event", {"message": "x" * 100})
        result = runner.invoke(main, ["logs", "log-trunc"])
        assert result.exit_code == 0
        assert "..." in result.output

    def test_list_data_summarized(self, runner: CliRunner, make_task):
        """Long list values in event data show item count."""
        task = make_task("log-list")
        append_event(task, "test_event", {"files": ["f" + str(i) for i in range(20)]})
        result = runner.invoke(main, ["logs", "log-list"])
        assert result.exit_code == 0
        assert "items]" in result.output

    def test_show_all_flag(self, runner: CliRunner, make_task):
        """--all flag shows all events."""
        task = make_task("log-all")
        for i in range(30):
            append_event(task, f"event_{i}", {})
        result = runner.invoke(main, ["logs", "log-all", "--all"])
        assert result.exit_code == 0
        # Should show more than default 20 lines
        assert "event_0" in result.output
        assert "event_29" in result.output

    def test_warning_event_symbol(self, runner: CliRunner, make_task):
        """Warning events get ⚠ symbol (line 692)."""
        task = make_task("log-warn")
        append_event(task, "safety_warning", {"msg": "be careful"})
        result = runner.invoke(main, ["logs", "log-warn"])
        assert result.exit_code == 0
        assert "⚠" in result.output

    def test_step_filter(self, runner: CliRunner, make_task):
        """--step N filters events to only those for step N."""
        task = make_task("log-step")
        append_event(task, "step_started", {"step": 1})
        append_event(task, "step_started", {"step": 2})
        append_event(task, "step_completed", {"step": 1})
        result = runner.invoke(main, ["logs", "log-step", "--step", "1", "--all"])
        assert result.exit_code == 0
        assert "step_started" in result.output
        assert "step_completed" in result.output
        lines = [l for l in result.output.splitlines() if "step_started" in l]
        assert len(lines) == 1

    def test_step_filter_json(self, runner: CliRunner, make_task):
        """--step N with --json-output filters events."""
        task = make_task("log-step-j")
        append_event(task, "ev1", {"step": 1})
        append_event(task, "ev2", {"step": 2})
        result = runner.invoke(
            main, ["logs", "log-step-j", "--step", "2", "--all", "--json-output"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["data"]["step"] == 2

    def test_count_flag(self, runner: CliRunner, make_task):
        """--count prints only the number of events."""
        task = make_task("log-cnt")
        append_event(task, "started", {"step": 1})
        append_event(task, "completed", {"step": 1})
        append_event(task, "started", {"step": 2})
        result = runner.invoke(main, ["logs", "log-cnt", "-c"])
        assert result.exit_code == 0
        count = int(result.output.strip())
        assert count >= 3

    def test_count_with_filter(self, runner: CliRunner, make_task):
        """--count with --filter counts only matching events."""
        task = make_task("log-cf")
        append_event(task, "started", {"step": 1})
        append_event(task, "completed", {"step": 1})
        result = runner.invoke(main, ["logs", "log-cf", "-c", "--filter", "started"])
        assert result.exit_code == 0
        assert result.output.strip() == "1"

    def test_quiet_prints_event_types(self, runner: CliRunner, make_task):
        """logs -q prints one event type per line."""
        task = make_task("log-q")
        append_event(task, "step_started", {"step": 1})
        append_event(task, "step_completed", {"step": 1})
        result = runner.invoke(main, ["logs", "log-q", "-q", "--all"])
        assert result.exit_code == 0
        lines = result.output.strip().splitlines()
        types = [l.strip() for l in lines]
        assert "step_started" in types
        assert "step_completed" in types


# ---------------------------------------------------------------------------
# init command
# ---------------------------------------------------------------------------
