"""CLI integration tests for duo.cli using Click's CliRunner."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

import duo.cli
import duo.protocol
from duo.cli import main
from duo.protocol import (
    Subtask,
    TaskStatus,
    create_task,
    save_task,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect TASKS_DIR and DUO_DIR to a temporary directory."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    monkeypatch.setattr(duo.protocol, "TASKS_DIR", tasks_dir)
    monkeypatch.setattr(duo.protocol, "DUO_DIR", tmp_path)
    monkeypatch.setattr(duo.cli, "TASKS_DIR", tasks_dir)
    monkeypatch.setattr(duo.cli, "DUO_DIR", tmp_path)
    return tasks_dir


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _make_task(task_id: str = "test-task", description: str = "Test task"):
    """Create a task in the isolated TASKS_DIR and return it."""
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


@pytest.fixture()
def make_task():
    """Fixture wrapper around _make_task for use in test classes."""
    return _make_task


# ---------------------------------------------------------------------------
# main group
# ---------------------------------------------------------------------------


class TestMainGroup:
    def test_help(self, runner: CliRunner):
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "Duo" in result.output

    def test_creates_tasks_dir(self, tmp_path: Path, runner: CliRunner, monkeypatch):
        new_dir = tmp_path / "fresh" / "tasks"
        monkeypatch.setattr(duo.protocol, "TASKS_DIR", new_dir)
        monkeypatch.setattr(duo.cli, "TASKS_DIR", new_dir)
        # Invoke a subcommand so the group callback runs
        result = runner.invoke(main, ["list"])
        assert result.exit_code == 0
        assert new_dir.exists()


# ---------------------------------------------------------------------------
# version command
# ---------------------------------------------------------------------------


class TestVersion:
    def test_version_output(self, runner: CliRunner):
        result = runner.invoke(main, ["version"])
        assert result.exit_code == 0
        assert "duo" in result.output


# ---------------------------------------------------------------------------
# status command
# ---------------------------------------------------------------------------


class TestStatus:
    def test_named_task_not_found(self, runner: CliRunner):
        result = runner.invoke(main, ["status", "nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_named_task_shows_details(self, runner: CliRunner):
        task = _make_task()
        result = runner.invoke(main, ["status", "test-task"])
        assert result.exit_code == 0
        assert "test-task" in result.output
        assert task.status.value in result.output
        assert task.incarnation_id in result.output

    def test_no_name_no_tasks(self, runner: CliRunner):
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0
        assert "No tasks." in result.output

    def test_no_name_lists_all(self, runner: CliRunner):
        _make_task("alpha", "Alpha task")
        _make_task("beta", "Beta task")
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "beta" in result.output


# ---------------------------------------------------------------------------
# list command
# ---------------------------------------------------------------------------


class TestList:
    def test_no_tasks(self, runner: CliRunner):
        result = runner.invoke(main, ["list"])
        assert result.exit_code == 0
        assert "No tasks." in result.output

    def test_with_tasks(self, runner: CliRunner):
        _make_task("my-task")
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

    def test_multiple_tasks_sorted(self, runner: CliRunner):
        _make_task("zzz-task")
        _make_task("aaa-task")
        result = runner.invoke(main, ["list"])
        assert result.exit_code == 0
        lines = result.output.strip().splitlines()
        # Find the task lines (skip header + separator)
        task_lines = [l for l in lines if "task" in l.lower() and "---" not in l and "ID" not in l]
        assert len(task_lines) == 2
        # Should be sorted alphabetically
        assert task_lines[0].startswith("aaa-task")
        assert task_lines[1].startswith("zzz-task")


# ---------------------------------------------------------------------------
# recover command
# ---------------------------------------------------------------------------


class TestRecover:
    def test_no_tasks(self, runner: CliRunner):
        result = runner.invoke(main, ["recover"])
        assert result.exit_code == 0
        assert "All tasks consistent." in result.output

    def test_consistent_tasks(self, runner: CliRunner):
        _make_task()
        result = runner.invoke(main, ["recover"])
        assert result.exit_code == 0
        assert "All tasks consistent." in result.output

    def test_recovers_inconsistent_task(self, runner: CliRunner):
        task = _make_task()
        # Manually corrupt status to something the journal doesn't support
        # The journal says "created" but we'll set task.json to "running"
        task.status = TaskStatus.RUNNING
        save_task(task)
        result = runner.invoke(main, ["recover"])
        assert result.exit_code == 0
        assert "test-task" in result.output
        assert "running" in result.output
        assert "created" in result.output


# ---------------------------------------------------------------------------
# send command (error case)
# ---------------------------------------------------------------------------


class TestSend:
    def test_missing_task(self, runner: CliRunner):
        result = runner.invoke(main, ["send", "ghost", "hello"])
        assert result.exit_code != 0
        assert "not found" in result.output


# ---------------------------------------------------------------------------
# start command (error case)
# ---------------------------------------------------------------------------


class TestStart:
    def test_not_a_git_repo(self, runner: CliRunner, tmp_path: Path):
        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()
        result = runner.invoke(main, ["start", "fail-task", "--repo", str(not_a_repo)])
        assert result.exit_code != 0
        assert "not a git repo" in result.output


# ---------------------------------------------------------------------------
# kill command (error case)
# ---------------------------------------------------------------------------


class TestKill:
    def test_missing_task(self, runner: CliRunner):
        result = runner.invoke(main, ["kill", "nope"])
        assert result.exit_code != 0
        assert "not found" in result.output


# ---------------------------------------------------------------------------
# logs command
# ---------------------------------------------------------------------------


class TestLogs:
    def test_task_not_found(self, runner: CliRunner):
        result = runner.invoke(main, ["logs", "nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_no_events(self, runner: CliRunner):
        task = _make_task("empty-task")
        # Clear the journal
        task.journal_path.write_text("")
        result = runner.invoke(main, ["logs", "empty-task"])
        assert result.exit_code == 0
        assert "No events" in result.output

    def test_shows_events(self, runner: CliRunner):
        _make_task("test-task")
        result = runner.invoke(main, ["logs", "test-task"])
        assert result.exit_code == 0
        assert "task_created" in result.output

    def test_lines_limit(self, runner: CliRunner):
        _make_task("test-task")
        result = runner.invoke(main, ["logs", "test-task", "-n", "1"])
        assert result.exit_code == 0
        # With -n 1, should only show 1 event line
        event_lines = [l for l in result.output.strip().splitlines() if "·" in l or "✓" in l or "✗" in l]
        assert len(event_lines) == 1


# ---------------------------------------------------------------------------
# inspect command
# ---------------------------------------------------------------------------


class TestInspect:
    def test_task_not_found(self, runner: CliRunner):
        result = runner.invoke(main, ["inspect", "nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_shows_task_details(self, runner: CliRunner):
        _make_task("test-task")
        result = runner.invoke(main, ["inspect", "test-task"])
        assert result.exit_code == 0
        assert "test-task" in result.output
        assert "Status:" in result.output
        assert "Incarnation:" in result.output
        assert "Step:" in result.output

    def test_shows_recent_events(self, runner: CliRunner):
        _make_task("test-task")
        result = runner.invoke(main, ["inspect", "test-task"])
        assert "Recent Events" in result.output


# ---------------------------------------------------------------------------
# verbose flag
# ---------------------------------------------------------------------------


class TestVerbose:
    def test_verbose_flag_accepted(self, runner: CliRunner):
        result = runner.invoke(main, ["-v", "list"])
        assert result.exit_code == 0

    def test_help_shows_verbose(self, runner: CliRunner):
        result = runner.invoke(main, ["--help"])
        assert "--verbose" in result.output or "-v" in result.output


# ---------------------------------------------------------------------------
# improved error messages
# ---------------------------------------------------------------------------


class TestErrorMessages:
    def test_send_not_found_helpful(self, runner: CliRunner):
        result = runner.invoke(main, ["send", "nope", "hello"])
        assert "duo list" in result.output

    def test_status_not_found_helpful(self, runner: CliRunner):
        result = runner.invoke(main, ["status", "nope"])
        assert "duo list" in result.output

    def test_kill_not_found_helpful(self, runner: CliRunner):
        result = runner.invoke(main, ["kill", "nope"])
        assert "duo list" in result.output

    def test_logs_not_found_helpful(self, runner: CliRunner):
        result = runner.invoke(main, ["logs", "nope"])
        assert "duo list" in result.output

    def test_inspect_not_found_helpful(self, runner: CliRunner):
        result = runner.invoke(main, ["inspect", "nope"])
        assert "duo list" in result.output


# ---------------------------------------------------------------------------
# edge-case tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_start_duplicate_task(self, runner: CliRunner):
        _make_task("dupe-task")
        result = runner.invoke(main, ["start", "dupe-task", "--repo", "/tmp"])
        assert result.exit_code != 0
        assert "already exists" in result.output

    def test_batch_empty_tasks(self, runner: CliRunner, tmp_path: Path):
        f = tmp_path / "empty.json"
        f.write_text('{"tasks": []}')
        result = runner.invoke(main, ["batch", str(f)])
        assert result.exit_code != 0
        assert "No tasks" in result.output

    def test_send_queued_task_warning(self, runner: CliRunner):
        task = _make_task("q-task")
        task.status = TaskStatus.QUEUED
        save_task(task)
        result = runner.invoke(main, ["send", "q-task", "hello"])
        assert "queued" in result.output.lower()

    def test_merge_missing_worktree(self, runner: CliRunner):
        task = _make_task("merge-task")
        task.status = TaskStatus.COMPLETED
        save_task(task)
        result = runner.invoke(main, ["merge", "merge-task"])
        assert result.exit_code != 0
        assert "does not exist" in result.output


# ---------------------------------------------------------------------------
# export command
# ---------------------------------------------------------------------------


class TestExport:
    def test_task_not_found(self, runner: CliRunner):
        result = runner.invoke(main, ["export", "nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_text_format(self, runner: CliRunner, make_task):
        make_task("export-test")
        result = runner.invoke(main, ["export", "export-test"])
        assert result.exit_code == 0
        assert "Task Report: export-test" in result.output
        assert "Status:" in result.output

    def test_json_format(self, runner: CliRunner, make_task):
        import json

        make_task("export-json")
        result = runner.invoke(main, ["export", "export-json", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["task_id"] == "export-json"
        assert "events" in data

    def test_output_to_file(self, runner: CliRunner, make_task, tmp_path: Path):
        make_task("export-file")
        outfile = str(tmp_path / "report.txt")
        result = runner.invoke(main, ["export", "export-file", "-o", outfile])
        assert result.exit_code == 0
        assert "written to" in result.output
        assert (tmp_path / "report.txt").exists()


# ---------------------------------------------------------------------------
# cleanup command
# ---------------------------------------------------------------------------


class TestCleanup:
    def test_no_tasks(self, runner: CliRunner):
        result = runner.invoke(main, ["cleanup", "--force"])
        assert "No tasks" in result.output

    def test_cleanup_completed(self, runner: CliRunner, make_task):
        task = make_task("done-task")
        task.status = TaskStatus.COMPLETED
        save_task(task)
        result = runner.invoke(main, ["cleanup", "--force"])
        assert result.exit_code == 0
        assert "done-task" in result.output
        assert "Cleaned 1" in result.output

    def test_cleanup_all_includes_failed(self, runner: CliRunner, make_task):
        task = make_task("fail-task")
        task.status = TaskStatus.FAILED
        save_task(task)
        # Without --all, failed tasks not cleaned
        result = runner.invoke(main, ["cleanup", "--force"])
        assert "No tasks" in result.output
        # With --all, failed included
        result = runner.invoke(main, ["cleanup", "--all", "--force"])
        assert "fail-task" in result.output

    def test_keep_journal(self, runner: CliRunner, make_task):
        from duo.protocol import append_event

        task = make_task("journal-task")
        task.status = TaskStatus.COMPLETED
        save_task(task)
        append_event(task, "test_event", {})
        result = runner.invoke(main, ["cleanup", "--force", "--keep-journal"])
        assert result.exit_code == 0
        # Journal should still exist
        assert task.journal_path.exists()


# ---------------------------------------------------------------------------
# audit command
# ---------------------------------------------------------------------------


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
        append_event(task, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1})
        append_event(task, "pr_consumed", {"action": "task_prompt", "step": 1, "attempt": 1})
        result = runner.invoke(main, ["audit", "audit-task"])
        assert result.exit_code == 0
        assert "PR consumed: 2" in result.output
        assert "bootstrap" in result.output
        assert "task_prompt" in result.output

    def test_audit_all_tasks(self, runner: CliRunner, make_task):
        from duo.protocol import append_event

        t1 = make_task("task-a")
        save_task(t1)
        append_event(t1, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1})

        t2 = make_task("task-b")
        save_task(t2)
        append_event(t2, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1})
        append_event(t2, "pr_consumed", {"action": "task_prompt", "step": 1, "attempt": 1})

        result = runner.invoke(main, ["audit"])
        assert result.exit_code == 0
        assert "TOTAL" in result.output
        assert "3" in result.output  # total PR count

    def test_audit_not_found(self, runner: CliRunner):
        result = runner.invoke(main, ["audit", "nope"])
        assert result.exit_code != 0
        assert "not found" in result.output
