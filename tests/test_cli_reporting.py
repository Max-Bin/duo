"""CLI tests for reporting commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from duo.cli import main
from duo.protocol import (
    Subtask,
    append_event,
    create_task,
    save_task,
)


class TestAudit:
    def test_audit_no_tasks(self, runner: CliRunner):
        result = runner.invoke(main, ["audit"])
        assert result.exit_code == 0
        assert "No tasks" in result.output

    def test_audit_single_task(self, runner: CliRunner, make_task):
        from duo.protocol import append_event

        task = make_task("audit-task")
        save_task(task)
        # Simulate PR consumption events
        append_event(
            task, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )
        append_event(
            task, "pr_consumed", {"action": "task_prompt", "step": 1, "attempt": 1}
        )
        result = runner.invoke(main, ["audit", "audit-task"])
        assert result.exit_code == 0
        assert "PR consumed: 2" in result.output
        assert "bootstrap" in result.output
        assert "task_prompt" in result.output

    def test_audit_all_tasks(self, runner: CliRunner, make_task):
        from duo.protocol import append_event

        t1 = make_task("task-a")
        save_task(t1)
        append_event(
            t1, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )

        t2 = make_task("task-b")
        save_task(t2)
        append_event(
            t2, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )
        append_event(
            t2, "pr_consumed", {"action": "task_prompt", "step": 1, "attempt": 1}
        )

        result = runner.invoke(main, ["audit"])
        assert result.exit_code == 0
        assert "TOTAL" in result.output
        assert "3" in result.output  # total PR count

    def test_audit_not_found(self, runner: CliRunner):
        result = runner.invoke(main, ["audit", "nope"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_audit_json_output(self, runner: CliRunner, make_task):
        from duo.protocol import append_event

        task = make_task("json-audit")
        save_task(task)
        append_event(
            task, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )
        result = runner.invoke(main, ["audit", "json-audit", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["task"] == "json-audit"
        assert data["pr_consumed"] == 1
        assert isinstance(data["events"], list)

    def test_audit_quiet_single_task(self, runner: CliRunner, make_task):
        from duo.protocol import append_event

        task = make_task("q-audit")
        save_task(task)
        append_event(
            task, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )
        result = runner.invoke(main, ["audit", "q-audit", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "1"

    def test_audit_quiet_all_tasks(self, runner: CliRunner, make_task):
        from duo.protocol import append_event

        t = make_task("qa-task")
        save_task(t)
        append_event(t, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1})
        append_event(
            t, "pr_consumed", {"action": "task_prompt", "step": 1, "attempt": 1}
        )
        result = runner.invoke(main, ["audit", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "2"

    def test_audit_quiet_no_tasks(self, runner: CliRunner):
        result = runner.invoke(main, ["audit", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "0"


# ---------------------------------------------------------------------------
# cost
# ---------------------------------------------------------------------------


class TestAuditSessionLog:
    def test_audit_with_session_log(self, runner: CliRunner, make_task):
        """audit all tasks shows session-level PR log when available."""
        t1 = make_task("aud-task")
        save_task(t1)
        append_event(
            t1, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )

        pr_log = [
            {
                "ts": "2024-01-01T12:00:00",
                "action": "bootstrap",
                "label": "duo:aud-task",
            }
        ]
        with patch("duo.transport.get_pr_log", return_value=pr_log):
            result = runner.invoke(main, ["audit"])
            assert result.exit_code == 0
            assert "Session log" in result.output
            assert "bootstrap" in result.output

    def test_audit_single_task_pr_table(self, runner: CliRunner, make_task):
        """audit single task shows PR consumption table."""
        task = make_task("aud-single")
        save_task(task)
        append_event(
            task,
            "pr_consumed",
            {"action": "bootstrap", "step": 1, "attempt": 1},
        )
        append_event(
            task,
            "pr_consumed",
            {"action": "task_prompt", "step": 1, "attempt": 1},
        )
        result = runner.invoke(main, ["audit", "aud-single"])
        assert result.exit_code == 0
        assert "PR consumed: 2" in result.output
        assert "TIME" in result.output
        assert "ACTION" in result.output


# ---------------------------------------------------------------------------
# inspect command — detailed output
# ---------------------------------------------------------------------------


class TestAuditAllTasksJsonOutput:
    def test_audit_all_json_output(self, runner: CliRunner, make_task):
        """audit --json-output with no task name returns JSON for all tasks."""
        t1 = make_task("aj-one")
        save_task(t1)
        append_event(
            t1, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )

        t2 = make_task("aj-two")
        save_task(t2)
        append_event(
            t2, "pr_consumed", {"action": "task_prompt", "step": 1, "attempt": 1}
        )
        append_event(
            t2, "pr_consumed", {"action": "correction", "step": 1, "attempt": 2}
        )

        with patch("duo.transport.get_pr_log", return_value=[]):
            result = runner.invoke(main, ["audit", "--json-output"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "tasks" in data
        assert data["total_pr"] == 3
        assert isinstance(data["session_log"], list)
        names = {t["task"] for t in data["tasks"]}
        assert names == {"aj-one", "aj-two"}

    def test_audit_all_json_output_with_session_log(self, runner: CliRunner, make_task):
        """audit --json-output includes session_log from get_pr_log."""
        t = make_task("aj-log")
        save_task(t)

        pr_log = [{"ts": "2025-01-01T00:00:00", "action": "sent", "label": "aj-log"}]
        with patch("duo.transport.get_pr_log", return_value=pr_log):
            result = runner.invoke(main, ["audit", "--json-output"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["session_log"]) == 1
        assert data["session_log"][0]["action"] == "sent"


# ---------------------------------------------------------------------------
# inspect --json-output with heartbeat/ack/result (lines 972, 980, 986)
# ---------------------------------------------------------------------------


class TestCost:
    def test_cost_no_tasks(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["cost"])
        assert result.exit_code == 0
        assert "No tasks" in result.output

    def test_cost_no_tasks_json(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["cost", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {"tasks": [], "total_pr": 0}

    def test_cost_single_task_multiple_events(
        self, runner: CliRunner, make_task
    ) -> None:
        task = make_task("cost-task")
        save_task(task)
        append_event(
            task, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )
        append_event(
            task, "pr_consumed", {"action": "task_prompt", "step": 1, "attempt": 1}
        )
        append_event(
            task, "pr_consumed", {"action": "task_prompt", "step": 2, "attempt": 1}
        )

        result = runner.invoke(main, ["cost"])
        assert result.exit_code == 0
        assert "cost-task" in result.output
        assert "3" in result.output
        assert "task_prompt (2)" in result.output
        assert "Total" in result.output

    def test_cost_multiple_tasks(self, runner: CliRunner, make_task) -> None:
        t1 = make_task("task-x")
        save_task(t1)
        append_event(
            t1, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )

        t2 = make_task("task-y")
        save_task(t2)
        append_event(
            t2, "pr_consumed", {"action": "task_prompt", "step": 1, "attempt": 1}
        )
        append_event(
            t2, "pr_consumed", {"action": "task_prompt", "step": 1, "attempt": 2}
        )

        result = runner.invoke(main, ["cost"])
        assert result.exit_code == 0
        assert "task-x" in result.output
        assert "task-y" in result.output
        assert "Total" in result.output
        # Total should be 3
        lines = result.output.strip().splitlines()
        total_line = [l for l in lines if "Total" in l][0]
        assert "3" in total_line

    def test_cost_task_filter_existing(self, runner: CliRunner, make_task) -> None:
        t1 = make_task("alpha")
        save_task(t1)
        append_event(
            t1, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )

        t2 = make_task("beta")
        save_task(t2)
        append_event(
            t2, "pr_consumed", {"action": "task_prompt", "step": 1, "attempt": 1}
        )

        result = runner.invoke(main, ["cost", "--task", "alpha"])
        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "beta" not in result.output

    def test_cost_task_filter_not_found(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["cost", "--task", "nope"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_cost_since_filter(
        self, runner: CliRunner, make_task, tmp_path: Path
    ) -> None:
        from datetime import UTC, datetime, timedelta

        task = make_task("since-task")
        save_task(task)

        # Write journal directly with controlled timestamps
        now = datetime.now(UTC)
        old_ts = (now - timedelta(days=10)).isoformat()
        new_ts = (now - timedelta(hours=1)).isoformat()
        journal = task.journal_path
        journal.write_text(
            json.dumps(
                {
                    "ts": old_ts,
                    "event": "pr_consumed",
                    "data": {"action": "bootstrap", "step": 1, "attempt": 1},
                }
            )
            + "\n"
            + json.dumps(
                {
                    "ts": new_ts,
                    "event": "pr_consumed",
                    "data": {"action": "task_prompt", "step": 1, "attempt": 1},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        # --since 5 should only include the recent event
        result = runner.invoke(main, ["cost", "--since", "5"])
        assert result.exit_code == 0
        assert "since-task" in result.output
        lines = result.output.strip().splitlines()
        total_line = [l for l in lines if "Total" in l][0]
        assert "1" in total_line

    def test_cost_since_filter_excludes_all(self, runner: CliRunner, make_task) -> None:
        from datetime import UTC, datetime, timedelta

        task = make_task("old-task")
        save_task(task)

        old_ts = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        task.journal_path.write_text(
            json.dumps(
                {
                    "ts": old_ts,
                    "event": "pr_consumed",
                    "data": {"action": "bootstrap", "step": 1, "attempt": 1},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = runner.invoke(main, ["cost", "--since", "1"])
        assert result.exit_code == 0
        # Task appears but with 0 PRs
        assert "old-task" in result.output
        assert "Total" in result.output

    def test_cost_since_malformed_timestamp(self, runner: CliRunner, make_task) -> None:
        task = make_task("bad-ts")
        save_task(task)
        task.journal_path.write_text(
            json.dumps(
                {
                    "ts": "NOT-A-DATE",
                    "event": "pr_consumed",
                    "data": {"action": "bootstrap", "step": 1, "attempt": 1},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        result = runner.invoke(main, ["cost", "--since", "1"])
        assert result.exit_code == 0
        # Malformed ts is filtered out
        assert "bad-ts" in result.output
        lines = result.output.strip().splitlines()
        total_line = [l for l in lines if "Total" in l][0]
        assert "0" in total_line

    def test_cost_budget_no_tasks_negative(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["cost", "--budget", "-1"])
        assert result.exit_code == 1

    def test_cost_json_output(self, runner: CliRunner, make_task) -> None:
        task = make_task("json-cost")
        save_task(task)
        append_event(
            task, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )
        append_event(
            task, "pr_consumed", {"action": "task_prompt", "step": 1, "attempt": 1}
        )

        result = runner.invoke(main, ["cost", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["total_pr"] == 2
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["task"] == "json-cost"
        assert data["tasks"][0]["prs"] == 2

    def test_cost_budget_under(self, runner: CliRunner, make_task) -> None:
        task = make_task("budget-ok")
        save_task(task)
        append_event(
            task, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )

        result = runner.invoke(main, ["cost", "--budget", "5"])
        assert result.exit_code == 0

    def test_cost_budget_over(self, runner: CliRunner, make_task) -> None:
        task = make_task("budget-fail")
        save_task(task)
        append_event(
            task, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )
        append_event(
            task, "pr_consumed", {"action": "task_prompt", "step": 1, "attempt": 1}
        )
        append_event(
            task, "pr_consumed", {"action": "task_prompt", "step": 2, "attempt": 1}
        )

        result = runner.invoke(main, ["cost", "--budget", "2"])
        assert result.exit_code == 1

    def test_cost_budget_exact(self, runner: CliRunner, make_task) -> None:
        task = make_task("budget-exact")
        save_task(task)
        append_event(
            task, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )

        result = runner.invoke(main, ["cost", "--budget", "1"])
        assert result.exit_code == 0

    def test_cost_empty_journal(self, runner: CliRunner, make_task) -> None:
        task = make_task("empty-journal")
        save_task(task)
        # No pr_consumed events, just a different event
        append_event(task, "status_change", {"from": "queued", "to": "running"})

        result = runner.invoke(main, ["cost"])
        assert result.exit_code == 0
        assert "empty-journal" in result.output

    def test_cost_task_filter_json(self, runner: CliRunner, make_task) -> None:
        task = make_task("json-single")
        save_task(task)
        append_event(
            task, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )

        result = runner.invoke(main, ["cost", "--task", "json-single", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["task"] == "json-single"
        assert data["total_pr"] == 1

    def test_cost_task_with_no_journal_file(self, runner: CliRunner, make_task) -> None:
        task = make_task("no-journal")
        save_task(task)
        # Ensure journal file does not exist
        if task.journal_path.exists():
            task.journal_path.unlink()

        result = runner.invoke(main, ["cost"])
        assert result.exit_code == 0
        assert "no-journal" in result.output

    def test_cost_since_zero_days(self, runner: CliRunner, make_task) -> None:
        from datetime import UTC, datetime, timedelta

        task = make_task("since-zero")
        save_task(task)
        two_hours_ago = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        task.journal_path.write_text(
            json.dumps(
                {
                    "ts": two_hours_ago,
                    "event": "pr_consumed",
                    "data": {"action": "bootstrap", "step": 1, "attempt": 1},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = runner.invoke(main, ["cost", "--since", "0"])
        assert result.exit_code == 0
        lines = result.output.strip().splitlines()
        total_line = [l for l in lines if "Total" in l][0]
        assert "0" in total_line

    def test_cost_since_negative(self, runner: CliRunner, make_task) -> None:
        from datetime import UTC, datetime

        task = make_task("since-neg")
        save_task(task)
        now_ts = datetime.now(UTC).isoformat()
        task.journal_path.write_text(
            json.dumps(
                {
                    "ts": now_ts,
                    "event": "pr_consumed",
                    "data": {"action": "bootstrap", "step": 1, "attempt": 1},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = runner.invoke(main, ["cost", "--since", "-1"])
        assert result.exit_code == 0
        lines = result.output.strip().splitlines()
        total_line = [l for l in lines if "Total" in l][0]
        assert "0" in total_line

    def test_cost_multiple_actions_same_task(
        self, runner: CliRunner, make_task
    ) -> None:
        task = make_task("multi-action")
        save_task(task)
        append_event(
            task, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )
        append_event(
            task, "pr_consumed", {"action": "task_prompt", "step": 1, "attempt": 1}
        )
        append_event(
            task, "pr_consumed", {"action": "task_prompt", "step": 2, "attempt": 1}
        )
        append_event(
            task, "pr_consumed", {"action": "resend_prompt", "step": 1, "attempt": 1}
        )
        append_event(
            task, "pr_consumed", {"action": "error_retry", "step": 1, "attempt": 1}
        )

        result = runner.invoke(main, ["cost"])
        assert result.exit_code == 0
        assert "multi-action" in result.output
        assert "task_prompt (2)" in result.output
        lines = result.output.strip().splitlines()
        total_line = [l for l in lines if "Total" in l][0]
        assert "5" in total_line

    def test_cost_json_output_structure(self, runner: CliRunner, make_task) -> None:
        task = make_task("json-struct")
        save_task(task)
        append_event(
            task, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )

        result = runner.invoke(main, ["cost", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "tasks" in data
        assert "total_pr" in data
        assert isinstance(data["tasks"], list)
        assert isinstance(data["total_pr"], int)
        assert len(data["tasks"]) >= 1
        task_row = data["tasks"][0]
        for key in ("task", "prs", "first", "last", "top_action"):
            assert key in task_row, f"Missing key {key!r} in task row"

    def test_cost_budget_zero(self, runner: CliRunner, make_task) -> None:
        task = make_task("budget-zero")
        save_task(task)
        append_event(
            task, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )

        result = runner.invoke(main, ["cost", "--budget", "0"])
        assert result.exit_code == 1

    def test_cost_quiet(self, runner: CliRunner, make_task) -> None:
        """cost -q prints only the total PR count."""
        task = make_task("cost-q")
        save_task(task)
        append_event(
            task, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )
        result = runner.invoke(main, ["cost", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "1"

    def test_cost_quiet_no_tasks(self, runner: CliRunner) -> None:
        """cost -q with no tasks prints 0."""
        result = runner.invoke(main, ["cost", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "0"


# ---------------------------------------------------------------------------
# config set pr_budget
# ---------------------------------------------------------------------------


class TestConfigSetPRBudget:
    def test_set_pr_budget(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "set", "pr_budget", "10"])
        assert result.exit_code == 0
        assert "pr_budget = 10" in result.output


# ---------------------------------------------------------------------------
# batch edge cases
# ---------------------------------------------------------------------------


class TestPrBudgetSafety:
    """Tests for assert_not_at_main_prompt and _log_pr_budget_warning."""

    def test_log_pr_budget_warning_writes_file(self) -> None:
        """_log_pr_budget_warning writes to pr-budget.log."""
        from duo.cli import _log_pr_budget_warning
        from duo.protocol import DUO_DIR

        _log_pr_budget_warning("duo:test-label", "--force-new-session")
        log_path = DUO_DIR / "pr-budget.log"
        assert log_path.exists()
        content = log_path.read_text()
        assert "--force-new-session" in content
        assert "duo:test-label" in content

    def test_assert_not_at_main_prompt_raises(self) -> None:
        """assert_not_at_main_prompt raises when at ❯ prompt."""
        from duo.cli import assert_not_at_main_prompt

        with (
            patch("duo.transport.is_at_main_prompt", return_value=True),
            patch("duo.transport.read_pane", return_value="❯ Type @"),
        ):
            with pytest.raises(click.exceptions.ClickException, match="REFUSED"):
                assert_not_at_main_prompt("duo:test-label")

    def test_assert_not_at_main_prompt_passes(self) -> None:
        """assert_not_at_main_prompt returns None when not at prompt."""
        from duo.cli import assert_not_at_main_prompt

        with (
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.read_pane", return_value=""),
        ):
            assert assert_not_at_main_prompt("duo:test-label") is None

    def test_enforce_force_new_session_logs(self) -> None:
        """_enforce_not_at_main_prompt with force=True logs warning."""
        from duo.cli import _enforce_not_at_main_prompt
        from duo.protocol import DUO_DIR

        _enforce_not_at_main_prompt("duo:enforce-label", force_new_session=True)
        log_path = DUO_DIR / "pr-budget.log"
        assert log_path.exists()
        assert "duo:enforce-label" in log_path.read_text()

    def test_enforce_no_force_checks_prompt(self) -> None:
        """_enforce_not_at_main_prompt with force=False calls assert."""
        from duo.cli import _enforce_not_at_main_prompt

        with (
            patch("duo.transport.is_at_main_prompt", return_value=True),
            patch("duo.transport.read_pane", return_value="❯ Type @"),
        ):
            with pytest.raises(click.exceptions.ClickException, match="REFUSED"):
                _enforce_not_at_main_prompt("duo:test-label", force_new_session=False)


class TestDiffCommand:
    """Tests for duo diff command."""

    def test_diff_not_found(self, runner: CliRunner):
        """diff with unknown task shows error."""
        result = runner.invoke(main, ["diff", "nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_diff_invalid_name(self, runner: CliRunner):
        """diff rejects task names containing path traversal characters."""
        result = runner.invoke(main, ["diff", "../bad"])
        assert result.exit_code != 0

    def test_diff_invalid_name_slash(self, runner: CliRunner):
        """diff rejects task names with slashes."""
        result = runner.invoke(main, ["diff", "foo/bar"])
        assert result.exit_code != 0

    def test_diff_no_worktree(self, runner: CliRunner):
        """diff when worktree doesn't exist shows error."""
        sub = Subtask(step_id=1, description="d", target_files=[], writable_paths=[])
        create_task(
            task_id="diff-test",
            description="desc",
            worktree="/nonexistent/path",
            branch="main",
            base_commit="abc",
            subtasks=[sub],
        )

        result = runner.invoke(main, ["diff", "diff-test"])
        assert result.exit_code != 0
        assert (
            "worktree" in result.output.lower() or "not found" in result.output.lower()
        )

    def test_diff_no_changes(self, runner: CliRunner, tmp_path: Path):
        """diff with no changes shows 'No changes'."""
        wt = tmp_path / "worktree"
        wt.mkdir()

        sub = Subtask(step_id=1, description="d", target_files=[], writable_paths=[])
        create_task(
            task_id="diff-empty",
            description="desc",
            worktree=str(wt),
            branch="main",
            base_commit="abc",
            subtasks=[sub],
        )

        import subprocess

        original_run = subprocess.run

        def mock_run(cmd, **kwargs):
            if cmd[0] == "git" and "diff" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return original_run(cmd, **kwargs)

        with patch("subprocess.run", side_effect=mock_run):
            result = runner.invoke(main, ["diff", "diff-empty"])
            assert result.exit_code == 0
            assert "No changes" in result.output

    def test_diff_with_output(self, runner: CliRunner, tmp_path: Path):
        """diff shows actual git diff output."""
        wt = tmp_path / "worktree"
        wt.mkdir()

        sub = Subtask(step_id=1, description="d", target_files=[], writable_paths=[])
        create_task(
            task_id="diff-output",
            description="desc",
            worktree=str(wt),
            branch="main",
            base_commit="abc",
            subtasks=[sub],
        )

        import subprocess

        original_run = subprocess.run
        diff_text = "+++ b/file.py\n+new line\n"

        def mock_run(cmd, **kwargs):
            if cmd[0] == "git" and "diff" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=diff_text, stderr="")
            return original_run(cmd, **kwargs)

        with patch("subprocess.run", side_effect=mock_run):
            result = runner.invoke(main, ["diff", "diff-output"])
            assert result.exit_code == 0
            assert "+new line" in result.output

    def test_diff_stat(self, runner: CliRunner, tmp_path: Path):
        """diff --stat shows diffstat output."""
        wt = tmp_path / "worktree"
        wt.mkdir()

        sub = Subtask(step_id=1, description="d", target_files=[], writable_paths=[])
        create_task(
            task_id="diff-stat",
            description="desc",
            worktree=str(wt),
            branch="main",
            base_commit="abc",
            subtasks=[sub],
        )

        import subprocess

        original_run = subprocess.run
        stat_text = " file.py | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)\n"

        def mock_run(cmd, **kwargs):
            if cmd[0] == "git" and "diff" in cmd:
                assert "--stat" in cmd
                return subprocess.CompletedProcess(cmd, 0, stdout=stat_text, stderr="")
            return original_run(cmd, **kwargs)

        with patch("subprocess.run", side_effect=mock_run):
            result = runner.invoke(main, ["diff", "diff-stat", "--stat"])
            assert result.exit_code == 0
            assert "file.py" in result.output

    def test_diff_name_only(self, runner: CliRunner, tmp_path: Path):
        """diff --name-only lists changed file names."""
        wt = tmp_path / "worktree"
        wt.mkdir()

        sub = Subtask(step_id=1, description="d", target_files=[], writable_paths=[])
        create_task(
            task_id="diff-names",
            description="desc",
            worktree=str(wt),
            branch="main",
            base_commit="abc",
            subtasks=[sub],
        )

        import subprocess

        original_run = subprocess.run
        name_text = "src/foo.py\nsrc/bar.py\n"

        def mock_run(cmd, **kwargs):
            if cmd[0] == "git" and "diff" in cmd:
                assert "--name-only" in cmd
                return subprocess.CompletedProcess(cmd, 0, stdout=name_text, stderr="")
            return original_run(cmd, **kwargs)

        with patch("subprocess.run", side_effect=mock_run):
            result = runner.invoke(main, ["diff", "diff-names", "--name-only"])
            assert result.exit_code == 0
            assert "src/foo.py" in result.output
            assert "src/bar.py" in result.output

    def test_diff_json_output(self, runner: CliRunner, tmp_path: Path):
        """diff --json-output returns structured diff info."""
        wt = tmp_path / "worktree"
        wt.mkdir()

        sub = Subtask(step_id=1, description="d", target_files=[], writable_paths=[])
        create_task(
            task_id="diff-json",
            description="desc",
            worktree=str(wt),
            branch="duo/diff-json",
            base_commit="abc123",
            subtasks=[sub],
        )

        import subprocess

        original_run = subprocess.run

        def mock_run(cmd, **kwargs):
            if cmd[0] == "git" and "diff" in cmd:
                if "--name-only" in cmd:
                    return subprocess.CompletedProcess(
                        cmd, 0, stdout="src/foo.py\nsrc/bar.py\n", stderr=""
                    )
                if "--stat" in cmd:
                    return subprocess.CompletedProcess(
                        cmd, 0, stdout=" 2 files changed", stderr=""
                    )
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="diff output", stderr=""
                )
            return original_run(cmd, **kwargs)

        with patch("subprocess.run", side_effect=mock_run):
            result = runner.invoke(main, ["diff", "diff-json", "--json-output"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["task"] == "diff-json"
            assert data["branch"] == "duo/diff-json"
            assert data["base_commit"] == "abc123"
            assert data["files_changed"] == ["src/foo.py", "src/bar.py"]
            assert data["has_changes"] is True

    def test_diff_quiet(self, runner: CliRunner, tmp_path: Path):
        """diff -q prints only the changed file count."""
        wt = tmp_path / "worktree"
        wt.mkdir()

        sub = Subtask(step_id=1, description="d", target_files=[], writable_paths=[])
        create_task(
            task_id="diff-q",
            description="desc",
            worktree=str(wt),
            branch="duo/diff-q",
            base_commit="abc123",
            subtasks=[sub],
        )

        import subprocess

        original_run = subprocess.run

        def mock_run(cmd, **kwargs):
            if cmd[0] == "git" and "diff" in cmd and "--name-only" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="a.py\nb.py\nc.py\n", stderr=""
                )
            return original_run(cmd, **kwargs)

        with patch("subprocess.run", side_effect=mock_run):
            result = runner.invoke(main, ["diff", "diff-q", "-q"])
            assert result.exit_code == 0
            assert result.output.strip() == "3"


# ---------------------------------------------------------------------------
# --json-output flag
# ---------------------------------------------------------------------------
