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
# status command
# ---------------------------------------------------------------------------


class TestStatus:
    def test_named_task_not_found(self, runner: CliRunner):
        result = runner.invoke(main, ["status", "nonexistent"])
        assert result.exit_code != 0
        assert "task not found" in result.output

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
        assert "task not found" in result.output


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
        assert "task not found" in result.output
