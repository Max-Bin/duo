"""CLI tests for cleanup commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import duo.cli
import duo.protocol
from duo.cli import main
from duo.protocol import (
    Subtask,
    TaskStatus,
    append_event,
    create_task,
    load_task,
    save_task,
)


@pytest.fixture(autouse=True)
def isolated_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect TASKS_DIR and DUO_DIR to a temporary directory."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    monkeypatch.setattr(duo.protocol, "TASKS_DIR", tasks_dir)
    monkeypatch.setattr(duo.protocol, "_CORRUPTED_DIR", tasks_dir / "_corrupted")
    monkeypatch.setattr(duo.protocol, "DUO_DIR", tmp_path)
    monkeypatch.setattr(duo.cli, "TASKS_DIR", tasks_dir)
    monkeypatch.setattr(duo.cli, "DUO_DIR", tmp_path)
    return tasks_dir


@pytest.fixture
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


@pytest.fixture
def make_task():
    """Fixture wrapper around _make_task for use in test classes."""
    return _make_task


class TestCleanup:
    def test_no_tasks(self, runner: CliRunner):
        result = runner.invoke(main, ["cleanup", "--force"])
        assert "No tasks" in result.output

    def test_cleanup_no_tasks_without_force(self, runner: CliRunner):
        """cleanup without --force still exits cleanly when there's nothing to do."""
        result = runner.invoke(main, ["cleanup"])
        assert result.exit_code == 0
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

    def test_cleanup_all_includes_escalated(self, runner: CliRunner, make_task):
        task = make_task("esc-task")
        task.status = TaskStatus.ESCALATED
        save_task(task)
        with patch("duo.cli.subprocess.run"):
            result = runner.invoke(main, ["cleanup", "--all", "--force"])
        assert "esc-task" not in result.output

    def test_cleanup_all_includes_blocked(self, runner: CliRunner, make_task):
        task = make_task("block-task")
        task.status = TaskStatus.BLOCKED
        save_task(task)
        with patch("duo.cli.subprocess.run"):
            result = runner.invoke(main, ["cleanup", "--all", "--force"])
        assert "block-task" not in result.output

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

    def test_corrupted_empty(self, runner: CliRunner):
        result = runner.invoke(main, ["cleanup", "--corrupted"])
        assert result.exit_code == 0
        assert "No quarantined" in result.output

    def test_corrupted_purge(self, runner: CliRunner, make_task):
        from duo.protocol import quarantine_task

        task = make_task("bad-task")
        quarantine_task(task.id, "test")
        result = runner.invoke(main, ["cleanup", "--corrupted", "--force"])
        assert result.exit_code == 0
        assert "Purged 1" in result.output

    def test_corrupted_confirm_abort(self, runner: CliRunner, make_task):
        from duo.protocol import quarantine_task

        task = make_task("bad2")
        quarantine_task(task.id, "test")
        result = runner.invoke(main, ["cleanup", "--corrupted"], input="n\n")
        assert result.exit_code != 0  # aborted

    def test_cleanup_symlink_in_task_dir(self, runner: CliRunner, make_task, tmp_path):
        """Symlinks in task dir are removed without following."""
        task = make_task("sym-task")
        task.status = TaskStatus.COMPLETED
        save_task(task)
        # Create a symlink inside the task dir pointing outside
        target = tmp_path / "outside"
        target.mkdir()
        (target / "precious.txt").write_text("do not delete")
        symlink = task.dir / "evil-link"
        symlink.symlink_to(target)
        with patch("duo.cli.subprocess.run"):
            result = runner.invoke(main, ["cleanup", "--force", "--keep-journal"])
        assert result.exit_code == 0
        # Symlink removed but target untouched
        assert not symlink.exists()
        assert (target / "precious.txt").exists()

    def test_cleanup_json_completed(self, runner: CliRunner, make_task):
        """--json-output returns JSON for completed cleanup."""
        task = make_task("json-done")
        task.status = TaskStatus.COMPLETED
        save_task(task)
        result = runner.invoke(main, ["cleanup", "--force", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["cleaned"] == 1
        assert "json-done" in data["tasks"]

    def test_cleanup_json_no_tasks(self, runner: CliRunner):
        """--json-output returns empty result when nothing to clean."""
        result = runner.invoke(main, ["cleanup", "--force", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["cleaned"] == 0
        assert data["tasks"] == []

    def test_cleanup_quiet(self, runner: CliRunner, make_task):
        """cleanup -q prints only the cleaned count."""
        task = make_task("q-clean")
        task.status = TaskStatus.COMPLETED
        save_task(task)
        result = runner.invoke(main, ["cleanup", "--force", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "1"

    def test_cleanup_quiet_none(self, runner: CliRunner):
        """cleanup -q with nothing to clean prints 0."""
        result = runner.invoke(main, ["cleanup", "--force", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "0"

    def test_cleanup_json_corrupted(self, runner: CliRunner, make_task):
        """--json-output with --corrupted returns purge result."""
        from duo.protocol import quarantine_task

        task = make_task("bad-task")
        quarantine_task(task.id, "corrupt")
        result = runner.invoke(
            main, ["cleanup", "--corrupted", "--force", "--json-output"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["cleaned"] == 1
        assert data["corrupted"] is True

    def test_cleanup_json_corrupted_empty(self, runner: CliRunner):
        """--json-output with --corrupted returns zero when nothing quarantined."""
        result = runner.invoke(main, ["cleanup", "--corrupted", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["cleaned"] == 0
        assert data["corrupted"] is True


class TestCleanupEdgeCases:
    def test_cleanup_no_completed_tasks(self, runner: CliRunner, make_task):
        """cleanup when no tasks match shows message."""
        task = make_task("running-task")
        task.status = TaskStatus.RUNNING
        save_task(task)
        result = runner.invoke(main, ["cleanup", "--force"])
        assert "No tasks to clean up" in result.output

    def test_cleanup_dry_run(self, runner: CliRunner, make_task, monkeypatch):
        """cleanup --dry-run shows what would be cleaned without acting."""
        task = make_task("done-dry")
        task.status = TaskStatus.COMPLETED
        save_task(task)
        monkeypatch.setattr(
            "duo.cli.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="", stderr=""),
        )
        result = runner.invoke(main, ["cleanup", "--dry-run"])
        assert result.exit_code == 0
        assert "Would clean up" in result.output
        assert "done-dry" in result.output
        assert load_task("done-dry") is not None

    def test_cleanup_dry_run_json(self, runner: CliRunner, make_task, monkeypatch):
        """cleanup --dry-run --json-output returns JSON with dry_run flag."""
        task = make_task("done-dry-json")
        task.status = TaskStatus.COMPLETED
        save_task(task)
        monkeypatch.setattr(
            "duo.cli.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="", stderr=""),
        )
        result = runner.invoke(main, ["cleanup", "--dry-run", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["dry_run"] is True
        assert "done-dry-json" in data["tasks"]

    def test_cleanup_dry_run_empty(self, runner: CliRunner):
        """cleanup --dry-run with no matching tasks."""
        result = runner.invoke(main, ["cleanup", "--dry-run"])
        assert result.exit_code == 0
        assert "No tasks to clean up" in result.output


# ---------------------------------------------------------------------------
# config list edge cases
# ---------------------------------------------------------------------------


class TestCleanupDetailed:
    def test_cleanup_with_force(self, runner: CliRunner, make_task):
        """cleanup --force removes completed tasks without confirmation."""
        task = make_task("cl-force")
        task.status = TaskStatus.COMPLETED
        save_task(task)

        with patch("duo.cli.subprocess.run"):
            result = runner.invoke(main, ["cleanup", "--force"])
            assert result.exit_code == 0
            assert "cl-force" in result.output
            assert "Cleaned 1" in result.output

    def test_cleanup_keep_journal_preserves(self, runner: CliRunner, make_task):
        """cleanup --keep-journal removes task files but keeps journal."""
        task = make_task("cl-journal")
        task.status = TaskStatus.COMPLETED
        save_task(task)
        append_event(task, "test_event", {"key": "value"})

        # Also write an extra file to be cleaned up
        extra = task.dir / "extra.txt"
        extra.write_text("extra data")

        with patch("duo.cli.subprocess.run"):
            result = runner.invoke(main, ["cleanup", "--force", "--keep-journal"])
            assert result.exit_code == 0
            assert task.journal_path.exists()
            assert not extra.exists()

    def test_cleanup_without_force_prompts(self, runner: CliRunner, make_task):
        """cleanup without --force asks for confirmation."""
        task = make_task("cl-prompt")
        task.status = TaskStatus.COMPLETED
        save_task(task)

        # Simulate user saying 'y'
        with patch("duo.cli.subprocess.run"):
            result = runner.invoke(main, ["cleanup"], input="y\n")
            assert result.exit_code == 0
            assert "Proceed?" in result.output
            assert "Cleaned 1" in result.output

    def test_cleanup_removes_existing_worktree(
        self, runner: CliRunner, make_task, tmp_path: Path
    ):
        """cleanup calls git worktree remove when worktree exists (line 1014)."""
        task = make_task("cl-wt")
        task.status = TaskStatus.COMPLETED
        wt_dir = tmp_path / "worktree_exists"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        save_task(task)

        with patch("duo.cli.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = runner.invoke(main, ["cleanup", "--force"])
            assert result.exit_code == 0
            # Verify git worktree remove was called with the worktree path
            wt_remove_calls = [
                c
                for c in mock_run.call_args_list
                if len(c[0][0]) >= 3 and c[0][0][:3] == ["git", "worktree", "remove"]
            ]
            assert len(wt_remove_calls) >= 1

    def test_cleanup_warns_on_git_failure(
        self, runner: CliRunner, make_task, tmp_path: Path
    ):
        """cleanup shows warnings when git cleanup commands fail."""
        task = make_task("cl-warn")
        task.status = TaskStatus.COMPLETED
        wt_dir = tmp_path / "wt_warn"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        task.branch = "duo/cl-warn"
        save_task(task)

        def mock_subprocess_run(args, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            if args[:3] == ["git", "worktree", "remove"]:
                m.returncode = 1
                m.stderr = "is dirty"
            if args[:3] == ["git", "branch", "-D"]:
                m.returncode = 1
                m.stderr = "not found"
            return m

        with patch("duo.cli.subprocess.run", side_effect=mock_subprocess_run):
            result = runner.invoke(main, ["cleanup", "--force"])
            assert result.exit_code == 0
            assert "Warning: worktree removal failed" in result.output


# ---------------------------------------------------------------------------
# config subcommands
# ---------------------------------------------------------------------------


class TestCleanupAge:
    def test_cleanup_with_age(self, runner: CliRunner, make_task):
        """--age filters to only tasks older than the given duration."""
        old_task = make_task("old-task")
        old_task.status = TaskStatus.COMPLETED
        old_task.created_at = "2020-01-01T00:00:00"
        save_task(old_task)

        new_task = make_task("new-task")
        new_task.status = TaskStatus.COMPLETED
        save_task(new_task)

        with patch("duo.cli.subprocess.run"):
            result = runner.invoke(main, ["cleanup", "--age", "1d", "--force"])
            assert result.exit_code == 0
            assert "old-task" in result.output
            assert "new-task" not in result.output
            assert "Cleaned 1" in result.output

    def test_cleanup_invalid_age(self, runner: CliRunner, make_task):
        """--age with invalid format shows error."""
        task = make_task("any-task")
        task.status = TaskStatus.COMPLETED
        save_task(task)

        result = runner.invoke(main, ["cleanup", "--age", "invalid", "--force"])
        assert result.exit_code != 0
        assert "invalid age format" in result.output.lower()

    def test_cleanup_age_no_match(self, runner: CliRunner, make_task):
        """--age with large duration matches nothing."""
        task = make_task("recent-task")
        task.status = TaskStatus.COMPLETED
        save_task(task)

        result = runner.invoke(main, ["cleanup", "--age", "999d", "--force"])
        assert result.exit_code == 0
        assert "No tasks" in result.output

    def test_cleanup_age_zero_rejected(self, runner: CliRunner, make_task):
        """--age 0d is rejected."""
        task = make_task("any-task")
        task.status = TaskStatus.COMPLETED
        save_task(task)

        result = runner.invoke(main, ["cleanup", "--age", "0d", "--force"])
        assert result.exit_code != 0
        assert "age value must be > 0" in result.output.lower()


# ---------------------------------------------------------------------------
# Doctor task_timeout check
# ---------------------------------------------------------------------------
