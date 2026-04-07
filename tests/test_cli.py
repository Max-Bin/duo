"""CLI integration tests for duo.cli using Click's CliRunner."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import duo.cli
import duo.protocol
from duo.cli import (
    _create_worktree,
    _load_batch_file,
    _validate_task_name,
    main,
)
from duo.protocol import (
    Subtask,
    TaskStatus,
    append_event,
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
        task_lines = [
            l for l in lines if "task" in l.lower() and "---" not in l and "ID" not in l
        ]
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

    def test_recover_skips_completed_task(self, runner: CliRunner):
        """recover() skips tasks with COMPLETED status (line 263)."""
        task = _make_task("done-task")
        task.status = TaskStatus.COMPLETED
        save_task(task)
        result = runner.invoke(main, ["recover"])
        assert result.exit_code == 0
        assert "All tasks consistent." in result.output

    def test_recover_skips_failed_task(self, runner: CliRunner):
        """recover() skips tasks with FAILED status (line 263)."""
        task = _make_task("fail-task")
        task.status = TaskStatus.FAILED
        save_task(task)
        result = runner.invoke(main, ["recover"])
        assert result.exit_code == 0
        assert "All tasks consistent." in result.output


# ---------------------------------------------------------------------------
# send command (error case)
# ---------------------------------------------------------------------------


class TestSend:
    def test_missing_task(self, runner: CliRunner):
        result = runner.invoke(main, ["send", "ghost", "hello"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_send_invalid_task_name(self, runner: CliRunner):
        result = runner.invoke(main, ["send", "bad name!", "hello"])
        assert result.exit_code != 0
        assert "Task name must contain only" in result.output


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

    def test_start_invalid_task_name(self, runner: CliRunner, tmp_path: Path):
        result = runner.invoke(main, ["start", "my task!", "--repo", str(tmp_path)])
        assert result.exit_code != 0
        assert "Task name must contain only" in result.output

    def test_start_valid_task_name_chars(self, runner: CliRunner, tmp_path: Path):
        """Names with letters, digits, dashes, underscores are accepted (repo check runs next)."""
        not_a_repo = tmp_path / "no-repo"
        not_a_repo.mkdir()
        result = runner.invoke(main, ["start", "ok-name_1", "--repo", str(not_a_repo)])
        # Should get past validation and fail on git check instead
        assert "Task name must contain only" not in result.output


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
        event_lines = [
            l
            for l in result.output.strip().splitlines()
            if "·" in l or "✓" in l or "✗" in l
        ]
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

    def test_export_collects_all_attempts_for_completed_steps(self, runner: CliRunner):
        """Export collects all attempt results for completed (non-current) steps."""
        import json

        task = create_task(
            task_id="export-attempts",
            description="Multi-attempt test",
            worktree="/fake/worktree",
            branch="duo/export-attempts",
            base_commit="abc123",
            subtasks=[
                Subtask(
                    step_id=1,
                    description="Step one",
                    target_files=[],
                    writable_paths=["*"],
                ),
                Subtask(
                    step_id=2,
                    description="Step two",
                    target_files=[],
                    writable_paths=["*"],
                ),
            ],
        )
        # Simulate: step 1 completed after 3 attempts, now on step 2 attempt 1
        task.current_step = 2
        task.current_attempt = 1
        save_task(task)

        # Write 3 result files for step 1
        step_dir = task.step_dir(1)
        step_dir.mkdir(parents=True, exist_ok=True)
        for attempt in range(1, 4):
            task.result_path(1, attempt).write_text(
                json.dumps(
                    {
                        "step": 1,
                        "attempt": attempt,
                        "incarnation": "test",
                        "status": "fail" if attempt < 3 else "pass",
                        "files_changed": [f"file{attempt}.py"],
                        "summary": f"Attempt {attempt}",
                    }
                )
            )

        result = runner.invoke(main, ["export", "export-attempts", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        step1_results = [r for r in data["results"] if r["step"] == 1]
        assert len(step1_results) == 3, (
            f"Expected 3 results for step 1, got {len(step1_results)}"
        )
        assert [r["attempt"] for r in step1_results] == [1, 2, 3]


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


class TestBatchEdgeCases:
    def test_batch_invalid_json_file(self, runner: CliRunner, tmp_path: Path):
        """batch with a malformed JSON file shows an error."""
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json!!!}")
        result = runner.invoke(main, ["batch", str(bad)])
        assert result.exit_code != 0

    def test_batch_invalid_yaml_file(self, runner: CliRunner, tmp_path: Path):
        """batch with malformed YAML shows error (if pyyaml available)."""
        try:
            import yaml  # noqa: F401
        except ImportError:
            pytest.skip("PyYAML not installed")
        bad = tmp_path / "bad.yaml"
        # Write content that parses as a string, not a dict with 'tasks'
        bad.write_text("- this: is\n  just: a list\n")
        result = runner.invoke(main, ["batch", str(bad)])
        assert result.exit_code != 0
        assert "tasks" in result.output.lower()


# ---------------------------------------------------------------------------
# export edge cases
# ---------------------------------------------------------------------------


class TestExportEdgeCases:
    def test_export_nonexistent_task(self, runner: CliRunner):
        """export with unknown task name shows error."""
        result = runner.invoke(main, ["export", "does-not-exist"])
        assert result.exit_code != 0
        assert "not found" in result.output


# ---------------------------------------------------------------------------
# audit edge cases
# ---------------------------------------------------------------------------


class TestAuditEdgeCases:
    def test_audit_no_tasks(self, runner: CliRunner):
        """audit with no tasks shows empty output."""
        result = runner.invoke(main, ["audit"])
        assert result.exit_code == 0
        assert "No tasks" in result.output


# ---------------------------------------------------------------------------
# cleanup edge cases
# ---------------------------------------------------------------------------


class TestCleanupEdgeCases:
    def test_cleanup_no_completed_tasks(self, runner: CliRunner, make_task):
        """cleanup when no tasks match shows message."""
        task = make_task("running-task")
        task.status = TaskStatus.RUNNING
        save_task(task)
        result = runner.invoke(main, ["cleanup", "--force"])
        assert "No tasks to clean up" in result.output


# ---------------------------------------------------------------------------
# config list edge cases
# ---------------------------------------------------------------------------


class TestConfigListEdgeCases:
    def test_config_list_shows_all_keys(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config list output contains all default keys."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "list"])
        assert result.exit_code == 0
        for key in config_mod.DEFAULTS:
            assert key in result.output


# ---------------------------------------------------------------------------
# _validate_task_name
# ---------------------------------------------------------------------------


class TestValidateTaskName:
    def test_valid_names(self):
        for name in ["foo", "foo-bar", "foo_bar", "Foo123", "a", "A-B_C-1"]:
            _validate_task_name(name)  # Should not raise

    def test_invalid_names(self):
        import click

        for name in ["bad name", "bad!name", "bad@name", "a/b", "a.b", ""]:
            with pytest.raises(click.BadParameter):
                _validate_task_name(name)


# ---------------------------------------------------------------------------
# _create_worktree helper
# ---------------------------------------------------------------------------


class TestCreateWorktree:
    def test_success(self, tmp_path: Path):
        """_create_worktree returns (worktree_path, base_commit) on success."""
        with (
            patch("duo.cli.get_config", return_value=str(tmp_path / "wt")),
            patch("duo.cli.subprocess.run") as mock_run,
        ):
            # First call: git rev-parse HEAD
            # Second call: git worktree add
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            worktree, base_commit = _create_worktree("my-task", "/fake/repo")
            assert base_commit == "abc123"
            assert "my-task" in worktree
            assert mock_run.call_count == 2

    def test_not_git_repo(self, tmp_path: Path):
        """_create_worktree exits if repo is not a git repository."""
        with (
            patch("duo.cli.get_config", return_value=str(tmp_path / "wt")),
            patch("duo.cli.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                returncode=128, stdout="", stderr="not a git repo"
            )
            with pytest.raises(SystemExit):
                _create_worktree("fail-task", "/not/a/repo")

    def test_worktree_add_fails(self, tmp_path: Path):
        """_create_worktree exits if 'git worktree add' fails."""
        with (
            patch("duo.cli.get_config", return_value=str(tmp_path / "wt")),
            patch("duo.cli.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\n", stderr=""),
                MagicMock(returncode=1, stdout="", stderr="branch already exists"),
            ]
            with pytest.raises(SystemExit):
                _create_worktree("dup-task", "/fake/repo")


# ---------------------------------------------------------------------------
# start command — successful path + queue path
# ---------------------------------------------------------------------------


class TestStartSuccess:
    def test_start_and_run(self, runner: CliRunner, tmp_path: Path):
        """start creates task + worktree, starts session immediately."""
        with (
            patch("duo.cli._create_worktree") as mock_wt,
            patch("duo.cli.subprocess.run"),
            patch("duo.commander.start_session") as mock_start,
            patch("duo.scheduler.enqueue_or_start", return_value="started"),
        ):
            mock_wt.return_value = (str(tmp_path / "wt" / "my-task"), "abc123")
            result = runner.invoke(
                main, ["start", "my-task", "--repo", str(tmp_path), "--desc", "hello"]
            )
            assert result.exit_code == 0
            assert "Created task: my-task" in result.output
            assert "Session started" in result.output
            mock_start.assert_called_once()

    def test_start_queued(self, runner: CliRunner, tmp_path: Path):
        """start queues task when slots are full."""
        with (
            patch("duo.cli._create_worktree") as mock_wt,
            patch("duo.cli.subprocess.run"),
            patch("duo.commander.start_session") as mock_start,
            patch("duo.scheduler.enqueue_or_start", return_value="queued"),
            patch(
                "duo.scheduler.queue_status",
                return_value={
                    "active_count": 3,
                    "queued_count": 1,
                    "max_parallel": 3,
                    "active_tasks": ["a", "b", "c"],
                    "queued_tasks": ["my-task"],
                },
            ),
        ):
            mock_wt.return_value = (str(tmp_path / "wt" / "q-task"), "abc123")
            result = runner.invoke(
                main, ["start", "q-task", "--repo", str(tmp_path), "--desc", "queued"]
            )
            assert result.exit_code == 0
            assert "Queued" in result.output
            assert "duo monitor" in result.output
            mock_start.assert_not_called()


# ---------------------------------------------------------------------------
# send command — successful path
# ---------------------------------------------------------------------------


class TestSendSuccess:
    def test_send_to_existing_task(self, runner: CliRunner):
        """send delivers prompt to existing task."""
        _make_task("send-task")
        with patch("duo.commander.send_task_prompt") as mock_send:
            result = runner.invoke(main, ["send", "send-task", "do something"])
            assert result.exit_code == 0
            assert "Sent to send-task" in result.output
            mock_send.assert_called_once()
            sent_task, sent_prompt = mock_send.call_args[0]
            assert sent_task.id == "send-task"
            assert sent_prompt == "do something"

    def test_send_nonexistent_task(self, runner: CliRunner):
        result = runner.invoke(main, ["send", "ghost", "hello"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_send_queued_task_warns(self, runner: CliRunner):
        """send to queued task prints warning but succeeds."""
        task = _make_task("q-send")
        task.status = TaskStatus.QUEUED
        save_task(task)
        with patch("duo.commander.send_task_prompt"):
            result = runner.invoke(main, ["send", "q-send", "hello"])
            assert result.exit_code == 0
            assert "queued" in result.output.lower()


# ---------------------------------------------------------------------------
# kill command — successful path
# ---------------------------------------------------------------------------


class TestKillSuccess:
    def test_kill_existing_task(self, runner: CliRunner, tmp_path: Path):
        """kill terminates pane, removes worktree, and marks task failed."""
        task = _make_task("kill-task")
        # Create fake worktree dir so os.path.exists returns True
        wt_dir = tmp_path / "fake_worktree"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        save_task(task)

        with patch("duo.cli.subprocess.run") as mock_run:
            # tmux kill-pane, git worktree list, git worktree remove, git branch -D
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="worktree /main\n  branch refs/heads/main\n\n",
                stderr="",
            )
            result = runner.invoke(main, ["kill", "kill-task"])
            assert result.exit_code == 0
            assert "Killed kill-task" in result.output
            assert mock_run.call_count >= 3  # tmux, worktree list, remove, branch

    def test_kill_nonexistent(self, runner: CliRunner):
        result = runner.invoke(main, ["kill", "nope"])
        assert result.exit_code != 0
        assert "not found" in result.output


# ---------------------------------------------------------------------------
# merge command
# ---------------------------------------------------------------------------


class TestMergeCommand:
    def test_merge_not_completed(self, runner: CliRunner):
        """merge refuses non-completed tasks."""
        task = _make_task("merge-nc")
        task.status = TaskStatus.RUNNING
        save_task(task)
        result = runner.invoke(main, ["merge", "merge-nc"])
        assert result.exit_code != 0
        assert "not 'completed'" in result.output

    def test_merge_not_found(self, runner: CliRunner):
        result = runner.invoke(main, ["merge", "ghost"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_merge_completed_task(self, runner: CliRunner, tmp_path: Path):
        """merge: happy path — fetch, rebase, ff-merge, cleanup."""
        task = _make_task("merge-ok")
        task.status = TaskStatus.COMPLETED
        wt_dir = tmp_path / "merge_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        task.branch = "duo/merge-ok"
        save_task(task)

        worktree_base = "/some/worktree/base"

        call_count = [0]

        def mock_subprocess_run(args, **kwargs):
            call_count[0] += 1
            m = MagicMock(returncode=0, stdout="", stderr="")
            # git worktree list --porcelain: return main + task worktree
            if args[:3] == ["git", "worktree", "list"]:
                m.stdout = (
                    f"worktree /main/repo\n\nworktree {worktree_base}/merge-ok\n\n"
                )
            return m

        with (
            patch("duo.cli.subprocess.run", side_effect=mock_subprocess_run),
            patch("duo.cli.get_config", return_value=worktree_base),
        ):
            result = runner.invoke(main, ["merge", "merge-ok"])
            assert result.exit_code == 0
            assert "Merged merge-ok" in result.output
            assert "git push" in result.output

    def test_merge_rebase_conflict(self, runner: CliRunner, tmp_path: Path):
        """merge exits on rebase conflict."""
        task = _make_task("merge-conflict")
        task.status = TaskStatus.COMPLETED
        wt_dir = tmp_path / "merge_conflict_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        save_task(task)

        call_idx = [0]

        def mock_subprocess_run(args, **kwargs):
            call_idx[0] += 1
            m = MagicMock(returncode=0, stdout="", stderr="")
            # fetch succeeds
            if args[:3] == ["git", "fetch", "origin"]:
                return m
            # rebase fails
            if args[:2] == ["git", "rebase"] and "--abort" not in args:
                m.returncode = 1
                m.stderr = "CONFLICT"
                return m
            # rebase --abort succeeds
            if args == ["git", "rebase", "--abort"]:
                return m
            return m

        with patch("duo.cli.subprocess.run", side_effect=mock_subprocess_run):
            result = runner.invoke(main, ["merge", "merge-conflict"])
            assert result.exit_code != 0
            assert "Rebase conflict" in result.output

    def test_merge_fetch_fails_continues(self, runner: CliRunner, tmp_path: Path):
        """merge continues when git fetch fails (line 311)."""
        task = _make_task("merge-fetch")
        task.status = TaskStatus.COMPLETED
        wt_dir = tmp_path / "merge_fetch_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        task.branch = "duo/merge-fetch"
        save_task(task)

        worktree_base = "/some/worktree/base"

        def mock_subprocess_run(args, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            if args[:3] == ["git", "fetch", "origin"]:
                m.returncode = 1
                m.stderr = "could not resolve host"
                return m
            if args[:3] == ["git", "worktree", "list"]:
                m.stdout = (
                    f"worktree /main/repo\n\nworktree {worktree_base}/merge-fetch\n\n"
                )
            return m

        with (
            patch("duo.cli.subprocess.run", side_effect=mock_subprocess_run),
            patch("duo.cli.get_config", return_value=worktree_base),
        ):
            result = runner.invoke(main, ["merge", "merge-fetch"])
            assert result.exit_code == 0
            assert "Warning: fetch failed" in result.output

    def test_merge_rebase_abort_fails(self, runner: CliRunner, tmp_path: Path):
        """merge warns when rebase --abort also fails (line 322)."""
        task = _make_task("merge-abortfail")
        task.status = TaskStatus.COMPLETED
        wt_dir = tmp_path / "merge_abortfail_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        save_task(task)

        def mock_subprocess_run(args, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            if args[:3] == ["git", "fetch", "origin"]:
                return m
            if args[:2] == ["git", "rebase"] and "--abort" not in args:
                m.returncode = 1
                m.stderr = "CONFLICT"
                return m
            if args == ["git", "rebase", "--abort"]:
                m.returncode = 1
                m.stderr = "abort failed"
                return m
            return m

        with patch("duo.cli.subprocess.run", side_effect=mock_subprocess_run):
            result = runner.invoke(main, ["merge", "merge-abortfail"])
            assert result.exit_code != 0
            assert "Rebase conflict" in result.output
            assert "could not abort rebase" in result.output

    def test_merge_no_main_worktree(self, runner: CliRunner, tmp_path: Path):
        """merge errors when no main worktree found (lines 342-347 unreachable
        due to uninitialized main_worktree; verifies the error path)."""
        task = _make_task("merge-nomain")
        task.status = TaskStatus.COMPLETED
        wt_dir = tmp_path / "merge_nomain_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        save_task(task)

        worktree_base = "/some/worktree/base"

        def mock_subprocess_run(args, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            if args[:3] == ["git", "worktree", "list"]:
                # All worktrees are under worktree_base — no main worktree
                m.stdout = f"worktree {worktree_base}/merge-nomain\n\n"
            return m

        with (
            patch("duo.cli.subprocess.run", side_effect=mock_subprocess_run),
            patch("duo.cli.get_config", return_value=worktree_base),
        ):
            result = runner.invoke(main, ["merge", "merge-nomain"])
            assert result.exit_code != 0

    def test_merge_ff_only_fails(self, runner: CliRunner, tmp_path: Path):
        """merge exits when ff-only merge fails (lines 358-359)."""
        task = _make_task("merge-ff")
        task.status = TaskStatus.COMPLETED
        wt_dir = tmp_path / "merge_ff_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        task.branch = "duo/merge-ff"
        save_task(task)

        worktree_base = "/some/worktree/base"

        def mock_subprocess_run(args, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            if args[:3] == ["git", "worktree", "list"]:
                m.stdout = (
                    f"worktree /main/repo\n\nworktree {worktree_base}/merge-ff\n\n"
                )
            if args[:2] == ["git", "merge"]:
                m.returncode = 1
                m.stderr = "not possible to fast-forward"
            return m

        with (
            patch("duo.cli.subprocess.run", side_effect=mock_subprocess_run),
            patch("duo.cli.get_config", return_value=worktree_base),
        ):
            result = runner.invoke(main, ["merge", "merge-ff"])
            assert result.exit_code != 0
            assert "Merge failed" in result.output


# ---------------------------------------------------------------------------
# _load_batch_file helper
# ---------------------------------------------------------------------------


class TestLoadBatchFile:
    def test_load_json(self, tmp_path: Path):
        """Valid JSON batch file is parsed correctly."""
        f = tmp_path / "tasks.json"
        f.write_text(json.dumps({"tasks": [{"name": "a"}, {"name": "b"}]}))
        result = _load_batch_file(str(f))
        assert len(result) == 2
        assert result[0]["name"] == "a"

    def test_load_yaml(self, tmp_path: Path):
        """Valid YAML batch file is parsed correctly."""
        try:
            import yaml  # noqa: F401
        except ImportError:
            pytest.skip("PyYAML not installed")
        f = tmp_path / "tasks.yaml"
        f.write_text("tasks:\n  - name: y1\n  - name: y2\n")
        result = _load_batch_file(str(f))
        assert len(result) == 2
        assert result[0]["name"] == "y1"

    def test_yaml_no_pyyaml(self, tmp_path: Path):
        """YAML file without pyyaml installed exits with error."""
        f = tmp_path / "tasks.yml"
        f.write_text("tasks:\n  - name: t1\n")
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("no yaml")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            with pytest.raises(SystemExit):
                _load_batch_file(str(f))

    def test_missing_tasks_key(self, tmp_path: Path):
        """JSON file without 'tasks' key exits with error."""
        f = tmp_path / "bad.json"
        f.write_text('{"items": []}')
        with pytest.raises(SystemExit):
            _load_batch_file(str(f))

    def test_empty_tasks_list(self, tmp_path: Path):
        """JSON file with empty tasks list exits."""
        f = tmp_path / "empty.json"
        f.write_text('{"tasks": []}')
        with pytest.raises(SystemExit):
            _load_batch_file(str(f))

    def test_load_yaml_safe_load_path(self, tmp_path: Path):
        """YAML safe_load path is exercised with mocked yaml module (line 439)."""
        import sys

        f = tmp_path / "tasks.yaml"
        f.write_text("tasks:\n  - name: y1\n")
        mock_yaml = MagicMock()
        mock_yaml.safe_load.return_value = {"tasks": [{"name": "y1"}]}

        with patch.dict(sys.modules, {"yaml": mock_yaml}):
            result = _load_batch_file(str(f))
            assert len(result) == 1
            assert result[0]["name"] == "y1"
            mock_yaml.safe_load.assert_called_once()


# ---------------------------------------------------------------------------
# _create_task_from_batch_def helper
# ---------------------------------------------------------------------------


class TestCreateTaskFromBatchDef:
    def test_success_started(self, runner: CliRunner, tmp_path: Path):
        """Task batch def that starts immediately."""
        from duo.cli import _create_task_from_batch_def

        defn = {"name": "batch-a", "description": "Batch A", "target_files": ["a.py"]}
        with (
            patch("duo.cli.subprocess.run") as mock_run,
            patch("duo.cli.get_config", return_value=str(tmp_path / "wt")),
            patch("duo.commander.start_session"),
            patch("duo.scheduler.enqueue_or_start", return_value="started"),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # rev-parse
                MagicMock(returncode=0, stdout="", stderr=""),  # worktree add
            ]
            result = _create_task_from_batch_def(defn, str(tmp_path), verbose=False)
            assert result == "batch-a"

    def test_success_queued(self, runner: CliRunner, tmp_path: Path):
        """Task batch def that gets queued."""
        from duo.cli import _create_task_from_batch_def

        defn = {"name": "batch-q", "description": "Queued"}
        with (
            patch("duo.cli.subprocess.run") as mock_run,
            patch("duo.cli.get_config", return_value=str(tmp_path / "wt")),
            patch("duo.commander.start_session") as mock_start,
            patch("duo.scheduler.enqueue_or_start", return_value="queued"),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            result = _create_task_from_batch_def(defn, str(tmp_path), verbose=False)
            assert result == "batch-q"
            mock_start.assert_not_called()

    def test_worktree_failure(self, runner: CliRunner, tmp_path: Path):
        """Task batch def fails when worktree creation fails."""
        from duo.cli import _create_task_from_batch_def

        defn = {"name": "batch-fail", "description": "Fail"}
        with (
            patch("duo.cli.subprocess.run") as mock_run,
            patch("duo.cli.get_config", return_value=str(tmp_path / "wt")),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\n", stderr=""),
                MagicMock(returncode=1, stdout="", stderr="error"),
            ]
            result = _create_task_from_batch_def(defn, str(tmp_path), verbose=False)
            assert result is None

    def test_revparse_fails(self, tmp_path: Path):
        """_create_task_from_batch_def exits when rev-parse fails (lines 491-495)."""
        from duo.cli import _create_task_from_batch_def

        defn = {"name": "batch-rp", "description": "RP Fail"}
        with (
            patch("duo.cli.subprocess.run") as mock_run,
            patch("duo.cli.get_config", return_value=str(tmp_path / "wt")),
        ):
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="not a git repo"
            )
            with pytest.raises(SystemExit):
                _create_task_from_batch_def(defn, str(tmp_path), verbose=False)


# ---------------------------------------------------------------------------
# batch command full flow
# ---------------------------------------------------------------------------


class TestBatchCommand:
    def test_batch_full_flow(self, runner: CliRunner, tmp_path: Path):
        """batch creates multiple tasks from a JSON file."""
        f = tmp_path / "tasks.json"
        f.write_text(
            json.dumps(
                {
                    "tasks": [
                        {"name": "b1", "description": "Task 1"},
                        {"name": "b2", "description": "Task 2"},
                    ]
                }
            )
        )

        with (
            patch("duo.cli._create_task_from_batch_def") as mock_create,
            patch(
                "duo.scheduler.queue_status",
                return_value={
                    "active_count": 2,
                    "queued_count": 0,
                    "max_parallel": 3,
                    "active_tasks": ["b1", "b2"],
                    "queued_tasks": [],
                },
            ),
        ):
            mock_create.side_effect = ["b1", "b2"]
            result = runner.invoke(main, ["batch", str(f), "--repo", str(tmp_path)])
            assert result.exit_code == 0
            assert "2 tasks created" in result.output
            assert "Active: 2/3" in result.output

    def test_batch_partial_failure(self, runner: CliRunner, tmp_path: Path):
        """batch: some tasks fail, count reflects only successes."""
        f = tmp_path / "tasks.json"
        f.write_text(
            json.dumps(
                {
                    "tasks": [
                        {"name": "ok1"},
                        {"name": "fail1"},
                    ]
                }
            )
        )

        with (
            patch("duo.cli._create_task_from_batch_def") as mock_create,
            patch(
                "duo.scheduler.queue_status",
                return_value={
                    "active_count": 1,
                    "queued_count": 0,
                    "max_parallel": 3,
                    "active_tasks": ["ok1"],
                    "queued_tasks": [],
                },
            ),
        ):
            mock_create.side_effect = ["ok1", None]  # second fails
            result = runner.invoke(main, ["batch", str(f), "--repo", str(tmp_path)])
            assert result.exit_code == 0
            assert "1 tasks created" in result.output

    def test_batch_with_queued_tasks(self, runner: CliRunner, tmp_path: Path):
        """batch shows queue message when tasks are queued (line 559)."""
        f = tmp_path / "tasks.json"
        f.write_text(json.dumps({"tasks": [{"name": "q1"}]}))

        with (
            patch("duo.cli._create_task_from_batch_def") as mock_create,
            patch(
                "duo.scheduler.queue_status",
                return_value={
                    "active_count": 2,
                    "queued_count": 3,
                    "max_parallel": 2,
                    "active_tasks": ["a1", "a2"],
                    "queued_tasks": ["q1", "q2", "q3"],
                },
            ),
        ):
            mock_create.return_value = "q1"
            result = runner.invoke(main, ["batch", str(f), "--repo", str(tmp_path)])
            assert result.exit_code == 0
            assert "Queued: 3" in result.output
            assert "duo monitor" in result.output


# ---------------------------------------------------------------------------
# queue command
# ---------------------------------------------------------------------------


class TestQueueCommand:
    def test_queue_with_active_and_queued(self, runner: CliRunner):
        """queue shows active and queued tasks."""
        with patch(
            "duo.scheduler.queue_status",
            return_value={
                "active_count": 2,
                "queued_count": 1,
                "max_parallel": 3,
                "active_tasks": ["task-a", "task-b"],
                "queued_tasks": ["task-c"],
            },
        ):
            result = runner.invoke(main, ["queue"])
            assert result.exit_code == 0
            assert "2/3" in result.output
            assert "task-a" in result.output
            assert "task-c" in result.output

    def test_queue_empty(self, runner: CliRunner):
        """queue with no tasks shows empty."""
        with patch(
            "duo.scheduler.queue_status",
            return_value={
                "active_count": 0,
                "queued_count": 0,
                "max_parallel": 3,
                "active_tasks": [],
                "queued_tasks": [],
            },
        ):
            result = runner.invoke(main, ["queue"])
            assert result.exit_code == 0
            assert "0/3" in result.output
            assert "empty" in result.output.lower()


# ---------------------------------------------------------------------------
# audit command — session log path
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


class TestInspectDetailed:
    def test_inspect_with_heartbeat(self, runner: CliRunner, make_task):
        """inspect shows heartbeat details when present."""
        task = make_task("insp-hb")
        save_task(task)

        from duo.protocol import Heartbeat

        hb = Heartbeat(
            ts="2024-01-01T12:00:00",
            incarnation="test1234",
            step=1,
            status="working",
            current_file="main.py",
        )
        with patch("duo.protocol.read_heartbeat", return_value=hb):
            result = runner.invoke(main, ["inspect", "insp-hb"])
            assert result.exit_code == 0
            assert "main.py" in result.output
            assert "working" in result.output

    def test_inspect_with_ack_and_result(self, runner: CliRunner, make_task):
        """inspect shows ack and result when available."""
        task = make_task("insp-ar")
        save_task(task)

        from duo.protocol import AckResult, StepResult

        ack = AckResult(
            step=1,
            attempt=1,
            incarnation="test1234",
            prompt_hash="h123",
            acked_at="2024-01-01T12:00:00",
        )
        result_obj = StepResult(
            step=1,
            attempt=1,
            incarnation="test1234",
            status="done",
            files_changed=["file1.py", "file2.py"],
            summary="Implemented feature",
        )
        with (
            patch("duo.protocol.read_ack_for_step", return_value=ack),
            patch("duo.protocol.read_result_for_step", return_value=result_obj),
            patch("duo.protocol.read_heartbeat", return_value=None),
        ):
            result = runner.invoke(main, ["inspect", "insp-ar"])
            assert result.exit_code == 0
            assert "Ack:" in result.output
            assert "h123" in result.output
            assert "Result:" in result.output
            assert "file1.py" in result.output
            assert "Implemented feature" in result.output

    def test_inspect_with_last_prompt(self, runner: CliRunner):
        """inspect shows last_prompt_sent_at when set."""
        task = _make_task("insp-lp")
        task.last_prompt_sent_at = "2024-01-01T12:05:00"
        save_task(task)
        result = runner.invoke(main, ["inspect", "insp-lp"])
        assert result.exit_code == 0
        assert "Last prompt:" in result.output
        assert "2024-01-01T12:05:00" in result.output


# ---------------------------------------------------------------------------
# export command — JSON / text / file
# ---------------------------------------------------------------------------


class TestExportFormats:
    def test_export_json_structure(self, runner: CliRunner, make_task):
        """export --format json produces valid JSON with expected keys."""
        make_task("exp-json")
        result = runner.invoke(main, ["export", "exp-json", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["task_id"] == "exp-json"
        assert "events" in data
        assert "subtasks" in data
        assert "results" in data
        assert data["status"] == "created"

    def test_export_text_readable(self, runner: CliRunner, make_task):
        """export default text format has human-readable sections."""
        make_task("exp-text")
        result = runner.invoke(main, ["export", "exp-text"])
        assert result.exit_code == 0
        assert "Task Report: exp-text" in result.output
        assert "Status:" in result.output
        assert "Steps:" in result.output
        assert "Events" in result.output

    def test_export_text_with_target_files(self, runner: CliRunner):
        """export text format shows target files when present."""
        create_task(
            task_id="exp-files",
            description="With files",
            worktree="/fake/wt",
            branch="duo/exp-files",
            base_commit="abc",
            subtasks=[
                Subtask(
                    step_id=1,
                    description="Step 1",
                    target_files=["app.py", "utils.py"],
                    writable_paths=["*"],
                )
            ],
        )
        result = runner.invoke(main, ["export", "exp-files"])
        assert result.exit_code == 0
        assert "app.py" in result.output

    def test_export_to_file(self, runner: CliRunner, make_task, tmp_path: Path):
        """export -o writes output to file."""
        make_task("exp-file")
        outfile = str(tmp_path / "report.json")
        result = runner.invoke(
            main, ["export", "exp-file", "--format", "json", "-o", outfile]
        )
        assert result.exit_code == 0
        assert "written to" in result.output
        content = Path(outfile).read_text()
        data = json.loads(content)
        assert data["task_id"] == "exp-file"


# ---------------------------------------------------------------------------
# cleanup command — force, keep-journal
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


# ---------------------------------------------------------------------------
# config subcommands
# ---------------------------------------------------------------------------


class TestConfigSubcommands:
    def test_config_get_known_key(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "get", "copilot_model"])
        assert result.exit_code == 0
        assert "copilot_model" in result.output

    def test_config_get_unknown_key(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "get", "nonexistent_key_xyz"])
        assert result.exit_code != 0
        assert "Unknown key" in result.output

    def test_config_set_known(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "set", "max_corrections", "5"])
        assert result.exit_code == 0
        assert "max_corrections = 5" in result.output

    def test_config_set_unknown_warns(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "set", "unknown_key", "val"])
        assert "not a known config key" in result.output

    def test_config_list(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "list"])
        assert result.exit_code == 0
        assert "copilot_model" in result.output
        assert "max_corrections" in result.output

    def test_config_list_shows_modified(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        # Set a value first
        runner.invoke(main, ["config", "set", "max_corrections", "99"])
        result = runner.invoke(main, ["config", "list"])
        assert "(modified)" in result.output

    def test_config_reset_single_key(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        # Set then reset
        runner.invoke(main, ["config", "set", "max_corrections", "99"])
        result = runner.invoke(main, ["config", "reset", "max_corrections"])
        assert result.exit_code == 0
        assert "Reset max_corrections" in result.output

    def test_config_reset_all(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "reset"])
        assert result.exit_code == 0
        assert "All config reset" in result.output


# ---------------------------------------------------------------------------
# monitor command
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


# ---------------------------------------------------------------------------
# dashboard command
# ---------------------------------------------------------------------------


class TestDashboardCommand:
    def test_dashboard_invocation(self, runner: CliRunner):
        """dashboard calls run_dashboard."""
        with patch("duo.cli.dashboard") as _:
            # We need to patch the import inside the command
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


# ---------------------------------------------------------------------------
# version fallback
# ---------------------------------------------------------------------------


class TestVersionFallback:
    def test_version_fallback(self, runner: CliRunner):
        """version command shows fallback when importlib.metadata fails."""
        with patch(
            "importlib.metadata.version", side_effect=ImportError("no metadata")
        ):
            result = runner.invoke(main, ["version"])
            assert result.exit_code == 0
            assert "duo" in result.output


# ---------------------------------------------------------------------------
# logs command edge cases — event formatting
# ---------------------------------------------------------------------------


class TestLogsFormatting:
    def test_error_event_symbol(self, runner: CliRunner):
        """Error events get ✗ symbol."""
        task = _make_task("log-err")
        append_event(task, "step_error", {"reason": "something broke"})
        result = runner.invoke(main, ["logs", "log-err"])
        assert result.exit_code == 0
        assert "✗" in result.output

    def test_completed_event_symbol(self, runner: CliRunner):
        """Completed events get ✓ symbol."""
        task = _make_task("log-done")
        append_event(task, "step_completed", {"step": 1})
        result = runner.invoke(main, ["logs", "log-done"])
        assert result.exit_code == 0
        assert "✓" in result.output

    def test_long_data_truncated(self, runner: CliRunner):
        """Long string values in event data are truncated."""
        task = _make_task("log-trunc")
        append_event(task, "test_event", {"message": "x" * 100})
        result = runner.invoke(main, ["logs", "log-trunc"])
        assert result.exit_code == 0
        assert "..." in result.output

    def test_list_data_summarized(self, runner: CliRunner):
        """Long list values in event data show item count."""
        task = _make_task("log-list")
        append_event(task, "test_event", {"files": ["f" + str(i) for i in range(20)]})
        result = runner.invoke(main, ["logs", "log-list"])
        assert result.exit_code == 0
        assert "items]" in result.output

    def test_show_all_flag(self, runner: CliRunner):
        """--all flag shows all events."""
        task = _make_task("log-all")
        for i in range(30):
            append_event(task, f"event_{i}", {})
        result = runner.invoke(main, ["logs", "log-all", "--all"])
        assert result.exit_code == 0
        # Should show more than default 20 lines
        assert "event_0" in result.output
        assert "event_29" in result.output

    def test_warning_event_symbol(self, runner: CliRunner):
        """Warning events get ⚠ symbol (line 692)."""
        task = _make_task("log-warn")
        append_event(task, "safety_warning", {"msg": "be careful"})
        result = runner.invoke(main, ["logs", "log-warn"])
        assert result.exit_code == 0
        assert "⚠" in result.output
