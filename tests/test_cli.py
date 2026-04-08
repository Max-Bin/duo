"""CLI integration tests for duo.cli using Click's CliRunner."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import duo.cli
import duo.protocol
from duo.cli import (
    _create_worktree,
    _fmt_ts,
    _load_batch_file,
    _validate_task_name,
    main,
)
from duo.protocol import (
    Subtask,
    TaskStatus,
    append_event,
    create_task,
    load_task,
    read_jsonl,
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
# completion command
# ---------------------------------------------------------------------------


class TestCompletion:
    def test_completion_bash(self, runner: CliRunner):
        result = runner.invoke(main, ["completion", "bash"])
        assert result.exit_code == 0
        assert "_DUO_COMPLETE" in result.output
        assert "bash_source" in result.output

    def test_completion_zsh(self, runner: CliRunner):
        result = runner.invoke(main, ["completion", "zsh"])
        assert result.exit_code == 0
        assert "_DUO_COMPLETE" in result.output
        assert "zsh_source" in result.output

    def test_completion_fish(self, runner: CliRunner):
        result = runner.invoke(main, ["completion", "fish"])
        assert result.exit_code == 0
        assert "_DUO_COMPLETE" in result.output
        assert "fish_source" in result.output

    def test_completion_invalid_shell(self, runner: CliRunner):
        result = runner.invoke(main, ["completion", "powershell"])
        assert result.exit_code != 0

    @pytest.mark.parametrize("shell,keyword", [
        ("bash", "bash_source"),
        ("zsh", "zsh_source"),
        ("fish", "fish_source"),
    ])
    def test_completion_shells(self, runner: CliRunner, shell: str, keyword: str):
        result = runner.invoke(main, ["completion", shell])
        assert result.exit_code == 0
        assert "_DUO_COMPLETE" in result.output
        assert keyword in result.output


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

    def test_send_empty_prompt(self, runner: CliRunner):
        """Verify send() rejects empty prompts."""
        _make_task("empty-prompt-task")
        result = runner.invoke(main, ["send", "empty-prompt-task", ""])
        assert result.exit_code != 0
        assert "empty" in result.output.lower() or "empty" in (result.output + str(result.exception)).lower()

    def test_send_whitespace_prompt(self, runner: CliRunner):
        """Verify send() rejects whitespace-only prompts."""
        _make_task("ws-prompt-task")
        result = runner.invoke(main, ["send", "ws-prompt-task", "   "])
        assert result.exit_code != 0
        assert "empty" in result.output.lower() or "empty" in (result.output + str(result.exception)).lower()


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

    def test_start_rejects_emoji_name(self, runner: CliRunner, tmp_path: Path):
        """Task names with emoji should be rejected."""
        result = runner.invoke(main, ["start", "task-🚀", "--repo", str(tmp_path)])
        assert result.exit_code != 0

    def test_start_rejects_slash_in_name(self, runner: CliRunner, tmp_path: Path):
        """Task names with slashes should be rejected (path traversal)."""
        result = runner.invoke(main, ["start", "../evil", "--repo", str(tmp_path)])
        assert result.exit_code != 0

    @pytest.mark.parametrize("invalid_name", [
        "tâche",
        "任务",
        "タスク",
        "задача",
        "name with space",
        "name\twith\ttab",
    ])
    def test_start_rejects_unicode_names(self, runner: CliRunner, tmp_path: Path, invalid_name: str):
        result = runner.invoke(main, ["start", invalid_name, "--repo", str(tmp_path), "--desc", "test"])
        assert result.exit_code != 0

    def test_start_concurrent_lock(self, runner: CliRunner, tmp_path: Path):
        """Concurrent start attempts are protected by lockfile."""
        import fcntl

        import duo.protocol

        lock_path = duo.protocol.TASKS_DIR / ".lock-task.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = open(lock_path, "w")  # noqa: SIM115
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = runner.invoke(
                main, ["start", "lock-task", "--repo", str(tmp_path), "--desc", "t"]
            )
            assert result.exit_code != 0
            assert "another process" in result.output.lower()
        finally:
            lock_fd.close()
            lock_path.unlink(missing_ok=True)

    def test_start_race_recheck_after_lock(self, runner: CliRunner, tmp_path: Path):
        """Re-check after lock detects task created by another process."""
        from unittest.mock import patch

        call_count = 0

        def load_side_effect(name: str):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return None  # first check passes
            return _make_task(name)  # re-check finds task

        with patch("duo.cli.load_task", side_effect=load_side_effect):
            result = runner.invoke(
                main, ["start", "race-task", "--repo", str(tmp_path), "--desc", "t"]
            )
            assert result.exit_code != 0
            assert "already exists" in result.output


# ---------------------------------------------------------------------------
# kill command (error case)
# ---------------------------------------------------------------------------


class TestKill:
    def test_missing_task(self, runner: CliRunner):
        result = runner.invoke(main, ["kill", "nope"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_kill_invalid_name(self, runner: CliRunner):
        """kill rejects task names containing path traversal characters."""
        result = runner.invoke(main, ["kill", "../bad"])
        assert result.exit_code != 0

    def test_kill_invalid_name_slash(self, runner: CliRunner):
        """kill rejects task names with slashes."""
        result = runner.invoke(main, ["kill", "foo/bar"])
        assert result.exit_code != 0


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

    def test_logs_json_output(self, runner: CliRunner):
        _make_task("json-task")
        result = runner.invoke(main, ["logs", "json-task", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) >= 1
        assert data[0]["event"] == "task_created"


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

    def test_inspect_json_output(self, runner: CliRunner):
        _make_task("json-inspect")
        result = runner.invoke(main, ["inspect", "json-inspect", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == "json-inspect"
        assert data["status"] == "created"
        assert "subtasks" in data
        assert isinstance(data["subtasks"], list)
        assert data["branch"] == "duo/json-inspect"


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
        assert "esc-task" in result.output

    def test_cleanup_all_includes_blocked(self, runner: CliRunner, make_task):
        task = make_task("block-task")
        task.status = TaskStatus.BLOCKED
        save_task(task)
        with patch("duo.cli.subprocess.run"):
            result = runner.invoke(main, ["cleanup", "--all", "--force"])
        assert "block-task" in result.output

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

    def test_task_name_too_long(self, runner: CliRunner, tmp_path: Path):
        """A 100-character name exceeds the 63-char limit and is rejected."""
        long_name = "a" * 100
        result = runner.invoke(main, ["start", long_name, "--repo", str(tmp_path)])
        assert result.exit_code != 0
        assert "at most 63 characters" in result.output

    def test_boundary_length_63_accepted(self):
        """Exactly 63 characters should be accepted."""
        _validate_task_name("a" * 63)

    def test_boundary_length_64_rejected(self):
        """64 characters should be rejected."""
        import click
        with pytest.raises(click.BadParameter, match="at most 63"):
            _validate_task_name("a" * 64)

    @pytest.mark.parametrize("char", list("!@#$%^&*()+=[]{}|\\:;\"'<>,./? \t\n"))
    def test_special_characters_rejected(self, char):
        """Each special character in task name is rejected."""
        import click
        with pytest.raises(click.BadParameter):
            _validate_task_name(f"task{char}name")


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
# stop command
# ---------------------------------------------------------------------------


class TestStop:
    def test_stop_not_found(self, runner: CliRunner):
        result = runner.invoke(main, ["stop", "ghost"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_stop_running_task(self, runner: CliRunner):
        """stop transitions task to BLOCKED and preserves worktree."""
        task = _make_task("stop-running")
        task.status = TaskStatus.RUNNING
        save_task(task)

        with patch("duo.cli.subprocess.run"):
            result = runner.invoke(main, ["stop", "stop-running"])
            assert result.exit_code == 0
            assert "Stopped" in result.output
            assert "resume" in result.output

        reloaded = load_task("stop-running")
        assert reloaded is not None
        assert reloaded.status == TaskStatus.BLOCKED

    def test_stop_already_completed(self, runner: CliRunner):
        """stop on completed task shows message."""
        task = _make_task("stop-done")
        task.status = TaskStatus.COMPLETED
        save_task(task)

        result = runner.invoke(main, ["stop", "stop-done"])
        assert result.exit_code == 0
        assert "terminal state" in result.output

    def test_stop_already_blocked(self, runner: CliRunner):
        """stop on already blocked task shows message."""
        task = _make_task("stop-blocked")
        task.status = TaskStatus.BLOCKED
        save_task(task)

        result = runner.invoke(main, ["stop", "stop-blocked"])
        assert result.exit_code == 0
        assert "already stopped" in result.output

    def test_stop_pane_kill_failure_warns(self, runner: CliRunner):
        """stop shows warning when pane kill fails."""
        task = _make_task("stop-pane-fail")
        task.status = TaskStatus.RUNNING
        save_task(task)

        def mock_subprocess_run(args, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            if args[:2] == ["tmux", "kill-pane"]:
                m.returncode = 1
                m.stderr = "no pane found"
            return m

        with patch("duo.cli.subprocess.run", side_effect=mock_subprocess_run):
            result = runner.invoke(main, ["stop", "stop-pane-fail"])
            assert result.exit_code == 0
            assert "Warning" in result.output
            assert "Stopped" in result.output

    def test_stop_records_event(self, runner: CliRunner):
        """stop logs task_stopped event with previous status."""
        task = _make_task("stop-event")
        task.status = TaskStatus.RUNNING
        save_task(task)

        with patch("duo.cli.subprocess.run"):
            runner.invoke(main, ["stop", "stop-event"])

        events = read_jsonl(task.journal_path)
        stopped_events = [e for e in events if e.get("event") == "task_stopped"]
        assert len(stopped_events) == 1
        assert stopped_events[0]["data"]["previous_status"] == "running"


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

    def test_kill_pane_kill_failure_warns(self, runner: CliRunner, tmp_path: Path):
        """kill shows warning when tmux kill-pane fails."""
        task = _make_task("kill-pane-fail")
        wt_dir = tmp_path / "kill_pane_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        save_task(task)

        def mock_subprocess_run(args, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            if args[:2] == ["tmux", "kill-pane"]:
                m.returncode = 1
                m.stderr = "no pane found"
            if args[:3] == ["git", "worktree", "list"]:
                m.stdout = "worktree /main\n  branch refs/heads/main\n\n"
            return m

        with patch("duo.cli.subprocess.run", side_effect=mock_subprocess_run):
            result = runner.invoke(main, ["kill", "kill-pane-fail"])
            assert result.exit_code == 0
            assert "Warning" in result.output

    def test_kill_cleanup_warnings(self, runner: CliRunner, tmp_path: Path):
        """kill shows warnings when git cleanup fails."""
        task = _make_task("kill-warn")
        wt_dir = tmp_path / "kill_warn_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        task.branch = "duo/kill-warn"
        save_task(task)

        def mock_subprocess_run(args, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            if args[:3] == ["git", "worktree", "list"]:
                m.stdout = "worktree /main/repo\n\n"
            if args[:3] == ["git", "worktree", "remove"]:
                m.returncode = 1
                m.stderr = "is dirty"
            if args[:3] == ["git", "branch", "-D"]:
                m.returncode = 1
                m.stderr = "not found"
            return m

        with patch("duo.cli.subprocess.run", side_effect=mock_subprocess_run):
            result = runner.invoke(main, ["kill", "kill-warn"])
            assert result.exit_code == 0
            assert "Warning: worktree removal failed" in result.output
            assert "Warning: branch deletion failed" in result.output


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

    def test_merge_invalid_name(self, runner: CliRunner):
        """merge rejects task names containing path traversal characters."""
        result = runner.invoke(main, ["merge", "../bad"])
        assert result.exit_code != 0

    def test_merge_invalid_name_slash(self, runner: CliRunner):
        """merge rejects task names with slashes."""
        result = runner.invoke(main, ["merge", "foo/bar"])
        assert result.exit_code != 0

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

    def test_merge_cleanup_warnings(self, runner: CliRunner, tmp_path: Path):
        """merge shows warnings when worktree/branch cleanup fails."""
        task = _make_task("merge-warn")
        task.status = TaskStatus.COMPLETED
        wt_dir = tmp_path / "merge_warn_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        task.branch = "duo/merge-warn"
        save_task(task)

        worktree_base = "/some/worktree/base"

        def mock_subprocess_run(args, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            if args[:3] == ["git", "worktree", "list"]:
                m.stdout = (
                    f"worktree /main/repo\n\nworktree {worktree_base}/merge-warn\n\n"
                )
            # Cleanup fails
            if args[:3] == ["git", "worktree", "remove"]:
                m.returncode = 1
                m.stderr = "dirty worktree"
            if args[:3] == ["git", "branch", "-d"]:
                m.returncode = 1
                m.stderr = "branch not found"
            return m

        with (
            patch("duo.cli.subprocess.run", side_effect=mock_subprocess_run),
            patch("duo.cli.get_config", return_value=worktree_base),
        ):
            result = runner.invoke(main, ["merge", "merge-warn"])
            assert result.exit_code == 0
            assert "Warning: worktree removal failed" in result.output
            assert "Warning: branch deletion failed" in result.output

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

    def test_load_batch_file_read_error(self):
        """_load_batch_file exits when the file cannot be read."""
        with pytest.raises(SystemExit):
            _load_batch_file("/no/such/path/batch.json")

    def test_load_batch_yaml_parse_error(self, tmp_path: Path):
        """_load_batch_file exits on yaml.YAMLError."""
        import sys

        f = tmp_path / "bad.yaml"
        f.write_text("{: bad yaml:}")
        mock_yaml = MagicMock()
        yaml_error = type("YAMLError", (Exception,), {})
        mock_yaml.YAMLError = yaml_error
        mock_yaml.safe_load.side_effect = yaml_error("parse error")

        with patch.dict(sys.modules, {"yaml": mock_yaml}):
            with pytest.raises(SystemExit):
                _load_batch_file(str(f))


# ---------------------------------------------------------------------------
# _create_single_task helper
# ---------------------------------------------------------------------------


class TestCreateTaskFromBatchDef:
    def test_success_started(self, runner: CliRunner, tmp_path: Path):
        """Task batch def that starts immediately."""
        from duo.cli import _create_single_task

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
            result = _create_single_task(defn, str(tmp_path))
            assert result == "batch-a"

    def test_success_queued(self, runner: CliRunner, tmp_path: Path):
        """Task batch def that gets queued."""
        from duo.cli import _create_single_task

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
            result = _create_single_task(defn, str(tmp_path))
            assert result == "batch-q"
            mock_start.assert_not_called()

    def test_worktree_failure(self, runner: CliRunner, tmp_path: Path):
        """Task batch def fails when worktree creation fails."""
        from duo.cli import _create_single_task

        defn = {"name": "batch-fail", "description": "Fail"}
        with (
            patch("duo.cli.subprocess.run") as mock_run,
            patch("duo.cli.get_config", return_value=str(tmp_path / "wt")),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\n", stderr=""),
                MagicMock(returncode=1, stdout="", stderr="error"),
            ]
            result = _create_single_task(defn, str(tmp_path))
            assert result is None

    def test_revparse_fails(self, tmp_path: Path):
        """_create_single_task exits when rev-parse fails."""
        from duo.cli import _create_single_task

        defn = {"name": "batch-rp", "description": "RP Fail"}
        with (
            patch("duo.cli.subprocess.run") as mock_run,
            patch("duo.cli.get_config", return_value=str(tmp_path / "wt")),
        ):
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="not a git repo"
            )
            with pytest.raises(SystemExit):
                _create_single_task(defn, str(tmp_path))


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
            patch("duo.cli._create_single_task") as mock_create,
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
            patch("duo.cli._create_single_task") as mock_create,
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
            patch("duo.cli._create_single_task") as mock_create,
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

    def test_batch_queue_flag(self, runner: CliRunner, tmp_path: Path):
        """batch --queue creates tasks in QUEUED state."""
        f = tmp_path / "tasks.json"
        f.write_text(
            json.dumps(
                {
                    "tasks": [
                        {"name": "q1", "description": "Task 1"},
                        {"name": "q2", "description": "Task 2"},
                    ]
                }
            )
        )

        with (
            patch("duo.cli._create_single_task") as mock_create,
            patch(
                "duo.scheduler.queue_status",
                return_value={
                    "active_count": 0,
                    "queued_count": 2,
                    "max_parallel": 3,
                    "active_tasks": [],
                    "queued_tasks": ["q1", "q2"],
                },
            ),
        ):
            mock_create.side_effect = ["q1", "q2"]
            result = runner.invoke(
                main, ["batch", str(f), "--queue", "--repo", str(tmp_path)]
            )
            assert result.exit_code == 0
            assert "2 tasks created" in result.output
            assert mock_create.call_count == 2


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
            assert "2" in result.output
            assert "3" in result.output
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
            assert "0" in result.output
            assert "empty" in result.output.lower()

    def test_queue_positions(self, runner: CliRunner):
        """queue shows numbered positions for queued tasks."""
        with patch(
            "duo.scheduler.queue_status",
            return_value={
                "active_count": 2,
                "queued_count": 3,
                "max_parallel": 4,
                "active_tasks": ["run-a", "run-b"],
                "queued_tasks": ["task-a", "task-b", "task-c"],
            },
        ):
            result = runner.invoke(main, ["queue"])
            assert result.exit_code == 0
            assert "Queue (3 tasks):" in result.output
            assert "1. task-a" in result.output
            assert "2. task-b" in result.output
            assert "3. task-c" in result.output
            assert "Active: 2 / max_parallel: 4" in result.output


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


class TestExportJsonl:
    def test_export_jsonl_format(self, runner: CliRunner):
        """export --format jsonl outputs each event as a structured JSON line."""
        task = create_task(
            task_id="exp-jsonl",
            description="JSONL test",
            worktree="/fake/wt",
            branch="duo/exp-jsonl",
            base_commit="abc",
            subtasks=[
                Subtask(
                    step_id=1,
                    description="Step 1",
                    target_files=[],
                    writable_paths=["*"],
                )
            ],
        )
        append_event(task, "started", {"step": 1})
        append_event(task, "completed", {"step": 1, "result": "pass"})

        result = runner.invoke(main, ["export", "exp-jsonl", "--format", "jsonl"])
        assert result.exit_code == 0
        lines = [l for l in result.output.strip().splitlines() if l.strip()]
        assert len(lines) >= 3  # task_created + started + completed
        for line in lines:
            obj = json.loads(line)
            assert obj["task_id"] == "exp-jsonl"
            assert "timestamp" in obj
            assert "event" in obj
            assert "data" in obj
        events = [json.loads(l)["event"] for l in lines]
        assert "task_created" in events
        assert "started" in events
        assert "completed" in events

    def test_export_jsonl_empty_journal(self, runner: CliRunner, make_task):
        """export --format jsonl with only task_created event outputs one line."""
        make_task("exp-jsonl-empty")
        result = runner.invoke(main, ["export", "exp-jsonl-empty", "--format", "jsonl"])
        assert result.exit_code == 0
        lines = [l for l in result.output.strip().splitlines() if l.strip()]
        # create_task appends a task_created event automatically
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["event"] == "task_created"

    def test_export_jsonl_outfile(self, runner: CliRunner, make_task, tmp_path: Path):
        """export --format jsonl --outfile writes JSONL to disk."""
        make_task("exp-jsonl-file")
        outfile = str(tmp_path / "export.jsonl")
        result = runner.invoke(main, ["export", "exp-jsonl-file", "--format", "jsonl", "-o", outfile])
        assert result.exit_code == 0
        assert "written to" in result.output
        content = Path(outfile).read_text()
        assert content.endswith("\n")
        lines = [l for l in content.strip().splitlines() if l.strip()]
        assert len(lines) >= 1
        obj = json.loads(lines[0])
        assert obj["task_id"] == "exp-jsonl-file"
        assert obj["event"] == "task_created"

    def test_export_json_format(self, runner: CliRunner, make_task):
        """export --format json produces valid JSON with task_id and events."""
        make_task("exp-json-fmt")
        result = runner.invoke(main, ["export", "exp-json-fmt", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["task_id"] == "exp-json-fmt"
        assert "events" in data
        assert "subtasks" in data

    def test_export_text_format_default(self, runner: CliRunner, make_task):
        """export without --format defaults to text output."""
        make_task("exp-text-def")
        result = runner.invoke(main, ["export", "exp-text-def"])
        assert result.exit_code == 0
        assert "Task Report: exp-text-def" in result.output
        assert "Status:" in result.output


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
                ["task-a", "task-b"], timeout=300, interval=5.0, once=False
            )

    def test_watch_no_names(self, runner: CliRunner):
        """watch with no names passes None."""
        with patch("duo.commander.watch_tasks") as mock_w:
            mock_w.return_value = 0
            result = runner.invoke(main, ["watch"])
            assert result.exit_code == 0
            mock_w.assert_called_once_with(
                None, timeout=300, interval=5.0, once=False
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
                None, timeout=60.0, interval=2.0, once=True
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


# ---------------------------------------------------------------------------
# init command
# ---------------------------------------------------------------------------


class TestInit:
    def test_init_creates_duo_dir(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """duo init creates ~/.duo directory."""
        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        result = runner.invoke(main, ["init", "--repo", str(repo)])
        assert result.exit_code == 0
        assert "Initialized" in result.output
        # DUO_DIR is monkeypatched to tmp_path which should exist
        assert (tmp_path).exists()

    def test_init_creates_project_duo_dir(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """duo init creates .duo/ inside the repo."""
        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        result = runner.invoke(main, ["init", "--repo", str(repo)])
        assert result.exit_code == 0
        assert (repo / ".duo").is_dir()

    def test_init_creates_instructions(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """duo init creates .duo/instructions.md with template content."""
        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        result = runner.invoke(main, ["init", "--repo", str(repo)])
        assert result.exit_code == 0
        instructions = repo / ".duo" / "instructions.md"
        assert instructions.exists()
        content = instructions.read_text()
        assert "# Duo Project Instructions" in content
        assert "## Project Overview" in content
        assert "## Coding Conventions" in content
        assert "## Testing" in content
        assert "## Important Notes" in content

    def test_init_adds_gitignore(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """duo init adds .duo/ to .gitignore."""
        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        result = runner.invoke(main, ["init", "--repo", str(repo)])
        assert result.exit_code == 0
        gitignore = repo / ".gitignore"
        assert gitignore.exists()
        assert ".duo/" in gitignore.read_text()

    def test_init_already_initialized(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Re-running duo init shows 'Already initialized'."""
        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / ".duo").mkdir()
        result = runner.invoke(main, ["init", "--repo", str(repo)])
        assert result.exit_code == 0
        assert "Already initialized" in result.output

    def test_init_not_git_repo(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """duo init in a non-git directory shows error."""
        repo = tmp_path / "not-a-repo"
        repo.mkdir()
        result = runner.invoke(main, ["init", "--repo", str(repo)])
        assert result.exit_code != 0
        assert "git init" in result.output or "git init" in (result.output + str(result.exception or ""))

    def test_init_gitignore_no_duplicate(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """duo init does not duplicate .duo/ entry in .gitignore."""
        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / ".gitignore").write_text("node_modules/\n.duo/\n")
        # First init
        result = runner.invoke(main, ["init", "--repo", str(repo)])
        assert result.exit_code == 0
        content = (repo / ".gitignore").read_text()
        assert content.count(".duo/") == 1

    def test_init_creates_tasks_dir(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """duo init creates ~/.duo/tasks/ directory."""
        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        result = runner.invoke(main, ["init", "--repo", str(repo)])
        assert result.exit_code == 0
        # TASKS_DIR is monkeypatched to tmp_path / "tasks"
        assert (tmp_path / "tasks").is_dir()

    def test_init_gitignore_no_trailing_newline(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """When .gitignore exists without trailing newline, a newline is prepended before .duo/."""
        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / ".gitignore").write_text("foo")
        result = runner.invoke(main, ["init", "--repo", str(repo)])
        assert result.exit_code == 0
        content = (repo / ".gitignore").read_text()
        assert content == "foo\n.duo/\n"


# ---------------------------------------------------------------------------
# doctor command
# ---------------------------------------------------------------------------


class TestDoctor:
    def test_doctor_all_pass(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """All checks pass when everything is available."""
        # Write valid config
        config_path = tmp_path / "config.json"
        config_path.write_text('{"copilot_model": "claude-opus-4.6"}\n')

        def fake_which(name: str) -> str | None:
            return f"/usr/bin/{name}"

        monkeypatch.setattr("shutil.which", fake_which)
        monkeypatch.setattr("duo.cli.shutil.which", fake_which)
        monkeypatch.setattr(
            "duo.cli.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0),
        )
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0
        assert "✓" in result.output
        assert "9/9 checks passed" in result.output

    def test_doctor_missing_tmux(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Missing tmux shows fix suggestion and exits 1."""
        config_path = tmp_path / "config.json"
        config_path.write_text('{"copilot_model": "claude-opus-4.6"}\n')

        def fake_which(name: str) -> str | None:
            if name == "tmux":
                return None
            return f"/usr/bin/{name}"

        monkeypatch.setattr("duo.cli.shutil.which", fake_which)
        monkeypatch.setattr(
            "duo.cli.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=1),
        )
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code != 0
        assert "brew install tmux" in result.output

    def test_doctor_missing_uv(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Missing uv shows fix suggestion."""
        config_path = tmp_path / "config.json"
        config_path.write_text('{"copilot_model": "claude-opus-4.6"}\n')

        def fake_which(name: str) -> str | None:
            if name == "uv":
                return None
            return f"/usr/bin/{name}"

        monkeypatch.setattr("duo.cli.shutil.which", fake_which)
        monkeypatch.setattr(
            "duo.cli.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0),
        )
        result = runner.invoke(main, ["doctor"])
        assert "astral.sh" in result.output

    def test_doctor_summary_count(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Doctor output ends with X/Y checks passed."""
        config_path = tmp_path / "config.json"
        config_path.write_text('{"copilot_model": "claude-opus-4.6"}\n')

        def fake_which(name: str) -> str | None:
            return f"/usr/bin/{name}"

        monkeypatch.setattr("duo.cli.shutil.which", fake_which)
        monkeypatch.setattr(
            "duo.cli.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0),
        )
        result = runner.invoke(main, ["doctor"])
        assert "/9 checks passed" in result.output

    def test_doctor_tmux_bridge_fallback_path(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """tmux-bridge found via ~/.smux/bin/tmux-bridge when not in PATH."""
        config_path = tmp_path / "config.json"
        config_path.write_text('{"copilot_model": "claude-opus-4.6"}\n')

        def fake_which(name: str) -> str | None:
            if name == "tmux-bridge":
                return None
            return f"/usr/bin/{name}"

        monkeypatch.setattr("duo.cli.shutil.which", fake_which)
        monkeypatch.setattr(
            "duo.cli.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0),
        )
        # Create the fallback path
        smux_bin = tmp_path / "fakehome" / ".smux" / "bin"
        smux_bin.mkdir(parents=True)
        (smux_bin / "tmux-bridge").touch()
        monkeypatch.setattr("duo.cli.Path.home", lambda: tmp_path / "fakehome")
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0
        # tmux-bridge should show as installed via fallback
        assert "tmux-bridge" in result.output
        assert "not found" not in result.output

    def test_doctor_invalid_config_json(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Invalid JSON in config.json shows 'missing or invalid' message."""
        config_path = tmp_path / "config.json"
        config_path.write_text("not valid json {{{")

        def fake_which(name: str) -> str | None:
            return f"/usr/bin/{name}"

        monkeypatch.setattr("duo.cli.shutil.which", fake_which)
        monkeypatch.setattr(
            "duo.cli.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0),
        )
        result = runner.invoke(main, ["doctor"])
        assert "missing or invalid" in result.output


# ---------------------------------------------------------------------------
# resume command
# ---------------------------------------------------------------------------


class TestResume:
    def test_resume_specific_task(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Resume a named task calls restart or start session."""
        task = _make_task("resume-me")
        # Set to a non-terminal state
        task.status = TaskStatus.RUNNING
        save_task(task)

        monkeypatch.setattr(
            "duo.transport.is_process_alive",
            lambda label: False,
        )
        mock_start = MagicMock()
        monkeypatch.setattr("duo.commander.start_session", mock_start)
        mock_restart = MagicMock()
        monkeypatch.setattr("duo.commander.restart_session", mock_restart)
        result = runner.invoke(main, ["resume", "resume-me"])
        assert result.exit_code == 0
        assert "Resumed task 'resume-me'" in result.output
        mock_start.assert_called_once()

    def test_resume_no_tasks(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """No interrupted tasks shows appropriate message."""
        result = runner.invoke(main, ["resume"])
        assert result.exit_code == 0
        assert "No interrupted tasks found" in result.output

    def test_resume_completed_task(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Resuming a completed task shows already completed."""
        task = _make_task("done-task")
        task.status = TaskStatus.COMPLETED
        save_task(task)
        result = runner.invoke(main, ["resume", "done-task"])
        assert result.exit_code == 0
        assert "already completed" in result.output

    def test_resume_all_interrupted(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Resume without name finds and resumes all non-terminal tasks."""
        t1 = _make_task("task-a", "Task A")
        t1.status = TaskStatus.RUNNING
        save_task(t1)

        t2 = _make_task("task-b", "Task B")
        t2.status = TaskStatus.PROMPT_SENT
        save_task(t2)

        t3 = _make_task("task-c", "Task C")
        t3.status = TaskStatus.COMPLETED
        save_task(t3)

        monkeypatch.setattr("duo.transport.is_process_alive", lambda label: False)
        mock_start = MagicMock()
        monkeypatch.setattr("duo.commander.start_session", mock_start)
        mock_restart = MagicMock()
        monkeypatch.setattr("duo.commander.restart_session", mock_restart)
        result = runner.invoke(main, ["resume"])
        assert result.exit_code == 0
        assert "task-a" in result.output
        assert "task-b" in result.output
        assert "task-c" not in result.output
        assert mock_start.call_count == 2

    def test_resume_task_not_found(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Resuming a non-existent task shows error."""
        result = runner.invoke(main, ["resume", "nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_resume_pane_alive_restarts(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """When pane is alive, restart_session is called instead of start_session."""
        task = _make_task("alive-task")
        task.status = TaskStatus.RUNNING
        save_task(task)

        monkeypatch.setattr("duo.transport.is_process_alive", lambda label: True)
        mock_restart = MagicMock()
        monkeypatch.setattr("duo.commander.restart_session", mock_restart)
        mock_start = MagicMock()
        monkeypatch.setattr("duo.commander.start_session", mock_start)
        result = runner.invoke(main, ["resume", "alive-task"])
        assert result.exit_code == 0
        assert "restarted session" in result.output
        mock_restart.assert_called_once()

    def test_resume_is_process_alive_exception(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """When is_process_alive raises, pane_alive stays False and start_session is called."""
        task = _make_task("error-task")
        task.status = TaskStatus.RUNNING
        save_task(task)

        def raise_error(label: str) -> bool:
            raise RuntimeError("tmux not available")

        monkeypatch.setattr("duo.transport.is_process_alive", raise_error)
        mock_start = MagicMock()
        monkeypatch.setattr("duo.commander.start_session", mock_start)
        mock_restart = MagicMock()
        monkeypatch.setattr("duo.commander.restart_session", mock_restart)
        result = runner.invoke(main, ["resume", "error-task"])
        assert result.exit_code == 0
        assert "started new session" in result.output
        mock_start.assert_called_once()
        mock_restart.assert_not_called()


# ---------------------------------------------------------------------------
# Help-text smoke tests
# ---------------------------------------------------------------------------


class TestHelpTexts:
    """Verify --help works for every registered command."""

    @pytest.mark.parametrize(
        "cmd",
        [
            ["version", "--help"],
            ["completion", "--help"],
            ["start", "--help"],
            ["send", "--help"],
            ["status", "--help"],
            ["list", "--help"],
            ["monitor", "--help"],
            ["recover", "--help"],
            ["merge", "--help"],
            ["stop", "--help"],
            ["kill", "--help"],
            ["batch", "--help"],
            ["queue", "--help"],
            ["audit", "--help"],
            ["dashboard", "--help"],
            ["logs", "--help"],
            ["inspect", "--help"],
            ["init", "--help"],
            ["doctor", "--help"],
            ["resume", "--help"],
            ["diff", "--help"],
            ["config", "--help"],
            ["export", "--help"],
            ["cleanup", "--help"],
            ["retry", "--help"],
            ["config", "get", "--help"],
            ["config", "set", "--help"],
            ["config", "list", "--help"],
            ["config", "reset", "--help"],
        ],
    )
    def test_help_exits_zero(self, cmd):
        runner = CliRunner()
        result = runner.invoke(main, cmd)
        assert result.exit_code == 0, f"{cmd} failed: {result.output}"
        assert "Usage:" in result.output

    def test_main_help_shows_sections(self):
        """Main --help displays categorized command sections."""
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "Task Lifecycle:" in result.output
        assert "Monitoring:" in result.output
        assert "Setup:" in result.output
        assert "Recovery:" in result.output


# ---------------------------------------------------------------------------
# Batch validation edge cases
# ---------------------------------------------------------------------------


class TestBatchValidation:
    """Additional edge cases for batch file validation."""

    def test_batch_tasks_null(self, tmp_path: Path):
        """_load_batch_file exits when 'tasks' value is null."""
        f = tmp_path / "bad.json"
        f.write_text(json.dumps({"tasks": None}))
        with pytest.raises(SystemExit):
            _load_batch_file(str(f))

    def test_batch_missing_name_in_task_def(
        self, runner: CliRunner, tmp_path: Path
    ):
        """batch task missing 'name' key causes an error for that task."""
        f = tmp_path / "noname.json"
        f.write_text(json.dumps({"tasks": [{"description": "no name field"}]}))
        result = runner.invoke(main, ["batch", str(f), "--repo", str(tmp_path)])
        assert result.exit_code != 0

    def test_batch_nonexistent_file(self, runner: CliRunner):
        """batch with a path that doesn't exist fails."""
        result = runner.invoke(main, ["batch", "/no/such/file.json"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# --dry-run flags
# ---------------------------------------------------------------------------


class TestDryRun:
    """Tests for --dry-run flags."""

    def test_batch_dry_run(self, runner: CliRunner, tmp_path: Path):
        """batch --dry-run previews tasks without creating them."""
        batch_file = tmp_path / "tasks.json"
        batch_file.write_text(
            json.dumps(
                [
                    {
                        "name": "task-a",
                        "description": "First task",
                        "subtasks": [{"step_id": 1, "description": "do a"}],
                    },
                    {
                        "name": "task-b",
                        "description": "Second task",
                        "subtasks": [{"step_id": 1, "description": "do b"}],
                    },
                ]
            )
        )

        result = runner.invoke(main, ["batch", str(batch_file), "--dry-run"])
        assert result.exit_code == 0
        assert "Would create 2 tasks" in result.output
        assert "task-a" in result.output
        assert "task-b" in result.output
        # Verify no tasks were actually created
        tasks_dir = duo.protocol.TASKS_DIR
        assert len(list(tasks_dir.iterdir())) == 0

    def test_merge_dry_run(self, runner: CliRunner):
        """merge --dry-run previews without merging."""
        task = _make_task("dry-merge")
        task.status = TaskStatus.COMPLETED
        save_task(task)

        result = runner.invoke(main, ["merge", "dry-merge", "--dry-run"])
        assert result.exit_code == 0
        assert "Would merge" in result.output
        assert "dry-merge" in result.output or task.branch in result.output


# ---------------------------------------------------------------------------
# diff command
# ---------------------------------------------------------------------------


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
        sub = Subtask(
            step_id=1, description="d", target_files=[], writable_paths=[]
        )
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
            "worktree" in result.output.lower()
            or "not found" in result.output.lower()
        )

    def test_diff_no_changes(self, runner: CliRunner, tmp_path: Path):
        """diff with no changes shows 'No changes'."""
        wt = tmp_path / "worktree"
        wt.mkdir()

        sub = Subtask(
            step_id=1, description="d", target_files=[], writable_paths=[]
        )
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
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="", stderr=""
                )
            return original_run(cmd, **kwargs)

        with patch("subprocess.run", side_effect=mock_run):
            result = runner.invoke(main, ["diff", "diff-empty"])
            assert result.exit_code == 0
            assert "No changes" in result.output

    def test_diff_with_output(self, runner: CliRunner, tmp_path: Path):
        """diff shows actual git diff output."""
        wt = tmp_path / "worktree"
        wt.mkdir()

        sub = Subtask(
            step_id=1, description="d", target_files=[], writable_paths=[]
        )
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
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=diff_text, stderr=""
                )
            return original_run(cmd, **kwargs)

        with patch("subprocess.run", side_effect=mock_run):
            result = runner.invoke(main, ["diff", "diff-output"])
            assert result.exit_code == 0
            assert "+new line" in result.output


# ---------------------------------------------------------------------------
# --json-output flag
# ---------------------------------------------------------------------------


class TestJsonOutput:
    def test_list_json(self, runner: CliRunner):
        """list --json-output returns JSON array."""
        _make_task("json-task", "A JSON task")
        result = runner.invoke(main, ["list", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["id"] == "json-task"
        assert data[0]["status"] == "created"
        assert "step" in data[0]
        assert "attempt" in data[0]
        assert "worktree" in data[0]
        assert "branch" in data[0]
        assert "created_at" in data[0]

    def test_list_json_empty(self, runner: CliRunner):
        """list --json-output with no tasks still shows 'No tasks.'."""
        result = runner.invoke(main, ["list", "--json-output"])
        assert result.exit_code == 0
        assert "No tasks." in result.output

    def test_list_json_multiple(self, runner: CliRunner):
        """list --json-output with multiple tasks returns sorted array."""
        _make_task("beta-task", "Beta")
        _make_task("alpha-task", "Alpha")
        result = runner.invoke(main, ["list", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 2
        assert data[0]["id"] == "alpha-task"
        assert data[1]["id"] == "beta-task"

    def test_status_json(self, runner: CliRunner):
        """status --json-output returns JSON object."""
        _make_task("json-status", "Status JSON task")
        result = runner.invoke(main, ["status", "json-status", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, dict)
        assert data["id"] == "json-status"
        assert data["status"] == "created"
        assert data["description"] == "Status JSON task"
        assert "step" in data
        assert "attempt" in data
        assert "worktree" in data
        assert "branch" in data
        assert "created_at" in data

    def test_status_json_not_found(self, runner: CliRunner):
        """status --json-output with unknown task shows error."""
        result = runner.invoke(main, ["status", "nope", "--json-output"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_status_json_no_name(self, runner: CliRunner):
        """status --json-output without name falls back to normal output."""
        _make_task("fallback-task", "Fallback")
        result = runner.invoke(main, ["status", "--json-output"])
        assert result.exit_code == 0
        assert "fallback-task" in result.output

    def test_list_without_json_flag(self, runner: CliRunner):
        """list without --json-output returns table format."""
        _make_task("table-task", "Table task")
        result = runner.invoke(main, ["list"])
        assert result.exit_code == 0
        assert "ID" in result.output
        assert "STATUS" in result.output
        assert "table-task" in result.output

    def test_status_without_json_flag(self, runner: CliRunner):
        """status without --json-output returns normal format."""
        _make_task("normal-task", "Normal task")
        result = runner.invoke(main, ["status", "normal-task"])
        assert result.exit_code == 0
        assert "normal-task" in result.output
        assert "Status:" in result.output


class TestStats:
    def test_stats_empty(self, runner: CliRunner):
        result = runner.invoke(main, ["stats"])
        assert result.exit_code == 0
        assert "Tasks: 0" in result.output

    def test_stats_with_tasks(self, runner: CliRunner):
        t1 = _make_task("stats-1")
        t1.status = TaskStatus.RUNNING
        save_task(t1)
        t2 = _make_task("stats-2")
        t2.status = TaskStatus.COMPLETED
        save_task(t2)
        result = runner.invoke(main, ["stats"])
        assert result.exit_code == 0
        assert "Tasks: 2" in result.output
        assert "running: 1" in result.output
        assert "completed: 1" in result.output

    def test_stats_json(self, runner: CliRunner):
        _make_task("stats-j")
        result = runner.invoke(main, ["stats", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["total"] == 1
        assert "by_status" in data


class TestStartFlags:
    def test_start_with_queue(self, runner: CliRunner, tmp_path: Path):
        """start --queue creates task in QUEUED state without starting a session."""
        with (
            patch("duo.cli._create_worktree") as mock_wt,
            patch("duo.commander.start_session") as mock_start,
        ):
            mock_wt.return_value = (str(tmp_path / "wt" / "q-task"), "abc123")
            result = runner.invoke(
                main,
                ["start", "q-task", "--queue", "--repo", str(tmp_path)],
            )
            assert result.exit_code == 0
            assert "queued" in result.output.lower()
            mock_start.assert_not_called()

        task = load_task("q-task")
        assert task is not None
        assert task.status == TaskStatus.QUEUED

    def test_start_with_model(self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """start --model sets DUO_COPILOT_MODEL env var."""
        with (
            patch("duo.cli._create_worktree") as mock_wt,
            patch("duo.commander.start_session"),
            patch("duo.scheduler.enqueue_or_start", return_value="started"),
        ):
            mock_wt.return_value = (str(tmp_path / "wt" / "m-task"), "abc123")
            result = runner.invoke(
                main,
                ["start", "m-task", "--model", "gpt-4", "--repo", str(tmp_path)],
            )
            assert result.exit_code == 0
            assert os.environ.get("DUO_COPILOT_MODEL") == "gpt-4"

        # Clean up env var
        monkeypatch.delenv("DUO_COPILOT_MODEL", raising=False)


# ---------------------------------------------------------------------------
# _create_single_task queue_only=True path
# ---------------------------------------------------------------------------


class TestCreateTaskQueued:
    def test_success(self, tmp_path: Path):
        """_create_single_task(queue_only=True) creates task and transitions to QUEUED."""
        from duo.cli import _create_single_task

        defn = {
            "name": "cq-ok",
            "description": "Queued task",
            "target_files": ["x.py"],
            "writable_paths": ["src/"],
        }
        with (
            patch("duo.cli.subprocess.run") as mock_run,
            patch("duo.cli.get_config", return_value=str(tmp_path / "wt")),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # rev-parse
                MagicMock(returncode=0, stdout="", stderr=""),  # worktree add
            ]
            result = _create_single_task(defn, str(tmp_path), queue_only=True)
            assert result == "cq-ok"

        task = load_task("cq-ok")
        assert task is not None
        assert task.status == TaskStatus.QUEUED

    def test_worktree_failure_returns_none(self, tmp_path: Path):
        """_create_single_task(queue_only=True) returns None when worktree add fails."""
        from duo.cli import _create_single_task

        defn = {"name": "cq-fail", "description": "Fail"}
        with (
            patch("duo.cli.subprocess.run") as mock_run,
            patch("duo.cli.get_config", return_value=str(tmp_path / "wt")),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # rev-parse
                MagicMock(returncode=1, stdout="", stderr="already exists"),
            ]
            result = _create_single_task(defn, str(tmp_path), queue_only=True)
            assert result is None

    def test_default_description_and_writable(self, tmp_path: Path):
        """_create_single_task(queue_only=True) uses defaults when description/writable_paths omitted."""
        from duo.cli import _create_single_task

        defn = {"name": "cq-defaults"}
        with (
            patch("duo.cli.subprocess.run") as mock_run,
            patch("duo.cli.get_config", return_value=str(tmp_path / "wt")),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="def456\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            result = _create_single_task(defn, str(tmp_path), queue_only=True)
            assert result == "cq-defaults"

        task = load_task("cq-defaults")
        assert task.description == "Task cq-defaults"
        assert task.subtasks[0].writable_paths == ["*"]


# ---------------------------------------------------------------------------
# audit --json-output for all tasks (lines 820-827)
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


class TestInspectJsonWithFiles:
    def test_inspect_json_with_heartbeat(self, runner: CliRunner):
        """inspect --json-output includes heartbeat data when file exists."""
        from duo.protocol import write_json

        task = _make_task("ins-hb")
        write_json(
            task.heartbeat_path,
            {
                "ts": "2025-01-01T12:00:00",
                "incarnation": "inc-1",
                "step": 1,
                "status": "running",
                "current_file": "src/app.py",
            },
        )

        result = runner.invoke(main, ["inspect", "ins-hb", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "heartbeat" in data
        assert data["heartbeat"]["ts"] == "2025-01-01T12:00:00"
        assert data["heartbeat"]["status"] == "running"
        assert data["heartbeat"]["current_file"] == "src/app.py"
        assert data["heartbeat"]["incarnation"] == "inc-1"

    def test_inspect_json_with_ack(self, runner: CliRunner):
        """inspect --json-output includes ack data when file exists."""
        from duo.protocol import write_json

        task = _make_task("ins-ack")
        write_json(
            task.ack_path(task.current_step, task.current_attempt),
            {
                "step": 1,
                "attempt": 1,
                "incarnation": "inc-2",
                "prompt_hash": "hash123",
                "acked_at": "2025-01-01T12:01:00",
            },
        )

        result = runner.invoke(main, ["inspect", "ins-ack", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "ack" in data
        assert data["ack"]["acked_at"] == "2025-01-01T12:01:00"
        assert data["ack"]["prompt_hash"] == "hash123"

    def test_inspect_json_with_result(self, runner: CliRunner):
        """inspect --json-output includes result data when file exists."""
        from duo.protocol import write_json

        task = _make_task("ins-res")
        write_json(
            task.result_path(task.current_step, task.current_attempt),
            {
                "step": 1,
                "attempt": 1,
                "incarnation": "inc-3",
                "status": "completed",
                "files_changed": ["a.py", "b.py"],
                "summary": "Done",
                "reason": "",
            },
        )

        result = runner.invoke(main, ["inspect", "ins-res", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "result" in data
        assert data["result"]["status"] == "completed"
        assert data["result"]["files_changed"] == ["a.py", "b.py"]
        assert data["result"]["summary"] == "Done"

    def test_inspect_json_with_all_files(self, runner: CliRunner):
        """inspect --json-output includes all three when all files exist."""
        from duo.protocol import write_json

        task = _make_task("ins-all")
        write_json(
            task.heartbeat_path,
            {
                "ts": "2025-01-01T12:00:00",
                "incarnation": "inc-a",
                "step": 1,
                "status": "running",
                "current_file": "main.py",
            },
        )
        write_json(
            task.ack_path(1, 1),
            {
                "step": 1,
                "attempt": 1,
                "incarnation": "inc-a",
                "prompt_hash": "ph1",
                "acked_at": "2025-01-01T12:01:00",
            },
        )
        write_json(
            task.result_path(1, 1),
            {
                "step": 1,
                "attempt": 1,
                "incarnation": "inc-a",
                "status": "completed",
                "files_changed": ["f.py"],
                "summary": "All done",
                "reason": "",
            },
        )

        result = runner.invoke(main, ["inspect", "ins-all", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "heartbeat" in data
        assert "ack" in data
        assert "result" in data


# ---------------------------------------------------------------------------
# inspect --include-files
# ---------------------------------------------------------------------------


class TestInspectIncludeFiles:
    def test_inspect_include_files(self, runner: CliRunner):
        """inspect --include-files shows changed and untracked files."""
        _make_task("incl-files")
        changed = subprocess.CompletedProcess(args=[], returncode=0, stdout="src/a.py\nsrc/b.py\n", stderr="")
        untracked = subprocess.CompletedProcess(args=[], returncode=0, stdout="new.txt\n", stderr="")
        diff = subprocess.CompletedProcess(args=[], returncode=0, stdout="diff --git a/src/a.py\n+hello\n", stderr="")

        def fake_run_git(args, cwd, *, check=True):
            if args[:2] == ["diff", "--name-only"]:
                return changed
            if args[0] == "ls-files":
                return untracked
            return diff

        with patch("duo.cli._run_git", side_effect=fake_run_git), \
             patch("os.path.isdir", return_value=True):
            result = runner.invoke(main, ["inspect", "incl-files", "--include-files"])
        assert result.exit_code == 0
        assert "Changed files (2):" in result.output
        assert "M src/a.py" in result.output
        assert "Untracked files (1):" in result.output
        assert "? new.txt" in result.output
        assert "Diff preview:" in result.output

    def test_inspect_include_files_no_worktree(self, runner: CliRunner):
        """inspect --include-files warns when worktree does not exist."""
        _make_task("incl-nodir")
        result = runner.invoke(main, ["inspect", "incl-nodir", "--include-files"])
        assert result.exit_code == 0
        assert "Worktree not found" in result.output

    def test_inspect_include_files_json(self, runner: CliRunner):
        """inspect --json-output --include-files populates JSON keys."""
        _make_task("incl-json")
        changed = subprocess.CompletedProcess(args=[], returncode=0, stdout="x.py\n", stderr="")
        untracked = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        diff = subprocess.CompletedProcess(args=[], returncode=0, stdout="diff content", stderr="")

        def fake_run_git(args, cwd, *, check=True):
            if args[:2] == ["diff", "--name-only"]:
                return changed
            if args[0] == "ls-files":
                return untracked
            return diff

        with patch("duo.cli._run_git", side_effect=fake_run_git), \
             patch("os.path.isdir", return_value=True):
            result = runner.invoke(main, ["inspect", "incl-json", "--json-output", "--include-files"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["changed_files"] == ["x.py"]
        assert data["untracked_files"] == []
        assert "diff content" in data["diff_preview"]

    def test_inspect_json_include_files_truncates_diff(self, runner: CliRunner):
        """inspect --json-output --include-files truncates diff > 500 chars."""
        _make_task("incl-trunc")
        big_diff = "x" * 600
        changed = subprocess.CompletedProcess(args=[], returncode=0, stdout="a.py\n", stderr="")
        untracked = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        diff = subprocess.CompletedProcess(args=[], returncode=0, stdout=big_diff, stderr="")

        def fake_run_git(args, cwd, *, check=True):
            if args[:2] == ["diff", "--name-only"]:
                return changed
            if args[0] == "ls-files":
                return untracked
            return diff

        with patch("duo.cli._run_git", side_effect=fake_run_git), \
             patch("os.path.isdir", return_value=True):
            result = runner.invoke(main, ["inspect", "incl-trunc", "--json-output", "--include-files"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["diff_preview"].endswith("... (truncated)")
        assert len(data["diff_preview"].split("\n... (truncated)")[0]) == 500

    def test_inspect_json_include_files_no_worktree(self, runner: CliRunner):
        """inspect --json-output --include-files sets files_error when worktree missing."""
        _make_task("incl-nodir-json")
        result = runner.invoke(main, ["inspect", "incl-nodir-json", "--json-output", "--include-files"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "files_error" in data
        assert "Worktree not found" in data["files_error"]

    def test_inspect_text_include_files_truncates_diff(self, runner: CliRunner):
        """inspect --include-files (text) truncates diff > 500 chars."""
        _make_task("incl-trunc-text")
        big_diff = "y" * 600
        changed = subprocess.CompletedProcess(args=[], returncode=0, stdout="b.py\n", stderr="")
        untracked = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        diff = subprocess.CompletedProcess(args=[], returncode=0, stdout=big_diff, stderr="")

        def fake_run_git(args, cwd, *, check=True):
            if args[:2] == ["diff", "--name-only"]:
                return changed
            if args[0] == "ls-files":
                return untracked
            return diff

        with patch("duo.cli._run_git", side_effect=fake_run_git), \
             patch("os.path.isdir", return_value=True):
            result = runner.invoke(main, ["inspect", "incl-trunc-text", "--include-files"])
        assert result.exit_code == 0
        assert "... (truncated)" in result.output


# ---------------------------------------------------------------------------
# _fmt_ts helper
# ---------------------------------------------------------------------------


class TestFmtTs:
    """Tests for the _fmt_ts timestamp formatting helper."""

    def test_valid_iso_timestamp(self):
        assert _fmt_ts("2025-01-15T14:30:45.123Z") == "14:30:45"

    def test_no_t_separator(self):
        assert _fmt_ts("14:30:45") == "14:30:45"

    def test_empty_string(self):
        assert _fmt_ts("") == ""

    def test_none_input(self):
        assert _fmt_ts(None) == "?"

    def test_numeric_input(self):
        assert _fmt_ts(12345) == "?"

    def test_t_at_end(self):
        """Timestamp ending with T and nothing after → IndexError → '?'."""
        assert _fmt_ts("2025-01-15T") == ""

    def test_short_time_part(self):
        """Time portion shorter than 8 chars returns what's available."""
        assert _fmt_ts("2025-01-15T14:30") == "14:30"


# ---------------------------------------------------------------------------
# batch: invalid task name in batch file
# ---------------------------------------------------------------------------


class TestBatchInvalidName:
    """Batch rejects task definitions with invalid names."""

    def test_batch_invalid_name_traversal(self, runner: CliRunner, tmp_path: Path):
        """Batch file with path-traversal task name is rejected."""
        batch_file = tmp_path / "bad-names.json"
        batch_file.write_text(
            json.dumps(
                {"tasks": [{"name": "../evil", "description": "bad"}]}
            )
        )
        result = runner.invoke(main, ["batch", str(batch_file), "--repo", str(tmp_path)])
        assert "invalid task name" in result.output.lower() or result.exit_code != 0


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


class TestBatchCorruptedJson:
    def test_batch_corrupted_json(self, runner: CliRunner, tmp_path: Path):
        """batch with corrupted JSON content shows a friendly error."""
        bad = tmp_path / "corrupt.json"
        bad.write_text("{bad json")
        result = runner.invoke(main, ["batch", str(bad), "--repo", "."])
        assert result.exit_code != 0
        out = result.output.lower() + (result.stderr if result.stderr else "").lower()
        assert "invalid json" in out or "error" in out


class TestBatchCorruptedYaml:
    def test_batch_corrupted_yaml(self, runner: CliRunner, tmp_path: Path):
        """batch with corrupted YAML content shows a friendly error."""
        pytest.importorskip("yaml")
        bad = tmp_path / "tasks.yaml"
        bad.write_text("{: bad yaml:}")
        result = runner.invoke(main, ["batch", str(bad), "--repo", "."])
        assert result.exit_code != 0
        out = result.output + (result.stderr or "")
        assert "Error" in out or "invalid" in out.lower() or "error" in out.lower()


class TestBatchFileNotFound:
    def test_batch_file_not_found(self, runner: CliRunner):
        """batch with a nonexistent file shows a friendly error."""
        result = runner.invoke(main, ["batch", "/nonexistent/file.json", "--repo", "."])
        assert result.exit_code != 0
        out = result.output + (result.stderr or "")
        assert "Error" in out or "No such file" in out or "error" in out.lower()


class TestRunGitNotInstalled:
    def test_run_git_not_installed(self, runner: CliRunner, tmp_path: Path):
        """When git is not found, a friendly error is shown."""
        with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
            result = runner.invoke(main, ["start", "sometask", "--repo", str(tmp_path)])
        assert result.exit_code != 0
        out = result.output + (result.stderr or "")
        assert "git is not installed" in out

    def test_run_git_timeout(self, runner: CliRunner, tmp_path: Path):
        """When git times out, a timeout error is shown."""
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
        ):
            result = runner.invoke(main, ["start", "sometask", "--repo", str(tmp_path)])
        assert result.exit_code != 0
        out = result.output + (result.stderr or "")
        assert "timed out" in out


class TestMainTasksDirPermissionDenied:
    def test_main_tasks_dir_permission_denied(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ):
        """When TASKS_DIR.mkdir raises PermissionError, a friendly error is shown."""
        original_mkdir = Path.mkdir

        def _raise_permission(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, N805
            if "tasks" in str(self):
                raise PermissionError("Permission denied")
            return original_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", _raise_permission)
        result = runner.invoke(main, ["status"])
        assert result.exit_code != 0
        out = result.output + (result.stderr or "")
        assert "Error" in out or "Permission" in out


class TestRetry:
    def test_retry_success(self, runner: CliRunner):
        """retry a FAILED task succeeds."""
        task = _make_task("retry-ok")
        task.status = TaskStatus.FAILED
        save_task(task)
        result = runner.invoke(main, ["retry", "retry-ok"])
        assert result.exit_code == 0
        assert "retry" in result.output.lower()

    def test_retry_not_retryable(self, runner: CliRunner):
        """retry a RUNNING task shows not-retryable error."""
        task = _make_task("retry-running")
        task.status = TaskStatus.RUNNING
        save_task(task)
        result = runner.invoke(main, ["retry", "retry-running"])
        assert result.exit_code != 0
        out = result.output + (result.stderr or "")
        assert "not retryable" in out.lower() or "not retryable" in out

    def test_retry_not_found(self, runner: CliRunner):
        """retry a nonexistent task shows not-found error."""
        result = runner.invoke(main, ["retry", "nonexistent"])
        assert result.exit_code != 0
        out = result.output + (result.stderr or "")
        assert "not found" in out.lower()

    def test_retry_invalid_name(self, runner: CliRunner):
        """retry with a path-traversal name is rejected."""
        result = runner.invoke(main, ["retry", "../bad"])
        assert result.exit_code != 0


class TestNotFoundParametrized:
    """Parametrized 'not found' tests covering all task-based commands."""

    @pytest.mark.parametrize("args", [
        ["status", "nonexistent"],
        ["logs", "nonexistent"],
        ["inspect", "nonexistent"],
        ["export", "nonexistent"],
        ["audit", "nonexistent"],
        ["kill", "nonexistent"],
        ["stop", "nonexistent"],
        ["merge", "nonexistent"],
        ["diff", "nonexistent"],
        ["retry", "nonexistent"],
        ["resume", "nonexistent"],
        ["send", "nonexistent", "hello"],
    ])
    def test_command_task_not_found(self, runner: CliRunner, args: list[str]):
        result = runner.invoke(main, args)
        assert result.exit_code != 0
        assert "not found" in result.output.lower()


# ---------------------------------------------------------------------------
# Cleanup --age tests
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


class TestDoctorTaskTimeout:
    def test_doctor_shows_task_timeout(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """doctor output includes task_timeout check."""
        config_path = tmp_path / "config.json"
        config_path.write_text('{"copilot_model": "claude-opus-4.6"}\n')

        def fake_which(name: str) -> str | None:
            return f"/usr/bin/{name}"

        monkeypatch.setattr("duo.cli.shutil.which", fake_which)
        monkeypatch.setattr(
            "duo.cli.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0),
        )
        result = runner.invoke(main, ["doctor"])
        assert "task_timeout" in result.output

    def test_doctor_invalid_task_timeout(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """doctor reports invalid task_timeout value."""
        monkeypatch.setattr("duo.cli.shutil.which", lambda _: "/usr/bin/fake")
        monkeypatch.setattr(
            "duo.cli.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0),
        )
        monkeypatch.setattr("duo.cli.get_config", lambda k: -1 if k == "task_timeout" else 0)
        result = runner.invoke(main, ["doctor"])
        assert "task_timeout" in result.output
        assert "invalid" in result.output.lower()


# ── Bare array batch file auto-wrapping ──────────────────────────────


class TestBatchBareArray:
    def test_batch_bare_array_auto_wrapped(self, runner: CliRunner, tmp_path: Path):
        """Batch file with bare JSON array (not wrapped in {tasks:...}) is auto-wrapped."""
        batch_file = tmp_path / "bare-array.json"
        batch_file.write_text(json.dumps([
            {"name": "task-a", "description": "first task"},
            {"name": "task-b", "description": "second task"},
        ]))
        with (
            patch("duo.cli._create_single_task") as mock_create,
            patch(
                "duo.scheduler.queue_status",
                return_value={
                    "active_count": 2,
                    "queued_count": 0,
                    "max_parallel": 3,
                    "active_tasks": ["task-a", "task-b"],
                    "queued_tasks": [],
                },
            ),
        ):
            mock_create.side_effect = ["task-a", "task-b"]
            result = runner.invoke(main, ["batch", str(batch_file), "--repo", str(tmp_path)])
        # Should succeed, not error about missing 'tasks' key
        assert result.exit_code == 0
        assert "2 tasks created" in result.output


class TestBatchDuplicateNames:
    def test_batch_duplicate_names_rejected(self, runner: CliRunner, tmp_path: Path):
        """Batch file with duplicate task names is rejected."""
        batch_file = tmp_path / "dupes.json"
        batch_file.write_text(json.dumps({
            "tasks": [
                {"name": "task-a", "description": "first"},
                {"name": "task-b", "description": "second"},
                {"name": "task-a", "description": "duplicate"},
            ]
        }))
        result = runner.invoke(main, ["batch", str(batch_file), "--repo", str(tmp_path)])
        assert result.exit_code != 0
        assert "duplicate" in result.output.lower()
        assert "task-a" in result.output
