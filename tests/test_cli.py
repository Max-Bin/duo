"""CLI integration tests for duo.cli using Click's CliRunner."""

from __future__ import annotations

import builtins
import json
import os
import re
import string
import subprocess
import time
from datetime import UTC
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner
from hypothesis import given
from hypothesis import strategies as st

import duo.cli
import duo.cli.doctor
import duo.protocol
from duo.ceo_log import start_ceo_session
from duo.cli import (
    _create_worktree,
    _fmt_ts,
    _load_batch_file,
    _parse_age,
    _safe_join,
    _validate_task_name,
    main,
)
from duo.cli.doctor import (
    CheckResult,
    _doctor_check_capi_error,
    _doctor_check_claude_cli,
    _doctor_check_config,
    _doctor_check_copilot_cli,
    _doctor_check_copilot_health,
    _doctor_check_corrupted,
    _doctor_check_duo_dir,
    _doctor_check_git,
    _doctor_check_python,
    _doctor_check_task_timeout,
    _doctor_check_tmux,
    _doctor_check_tmux_bridge,
    _doctor_check_tmux_session,
    _emit_restart_signal,
    _get_pid_child_count,
    _get_pid_fd_count,
    _get_pid_kqueue_count,
)
from duo.errors import DuoUserError
from duo.protocol import (
    Subtask,
    TaskStatus,
    append_event,
    create_task,
    load_task,
    read_jsonl,
    save_task,
)
from duo.transport import DialogKind

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    monkeypatch.setattr(duo.cli.doctor, "TASKS_DIR", tasks_dir)
    monkeypatch.setattr(duo.cli.doctor, "DUO_DIR", tmp_path)
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


# ---------------------------------------------------------------------------
# main group
# ---------------------------------------------------------------------------


class TestMainGroup:
    def test_help(self, runner: CliRunner):
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "Duo" in result.output

    def test_help_shows_aliases(self, runner: CliRunner):
        result = runner.invoke(main, ["--help"])
        assert "Aliases" in result.output
        assert "ls" in result.output
        assert "→ list" in result.output

    def test_alias_ls(self, runner: CliRunner):
        result = runner.invoke(main, ["ls"])
        assert result.exit_code == 0

    def test_alias_st(self, runner: CliRunner, _isolate_tasks_dir: Path):
        task = _make_task("alias-test")
        result = runner.invoke(main, ["st", task.id])
        assert result.exit_code == 0
        assert task.id in result.output

    def test_alias_log(self, runner: CliRunner, _isolate_tasks_dir: Path):
        task = _make_task("log-alias")
        result = runner.invoke(main, ["log", task.id])
        assert result.exit_code == 0

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

    def test_version_json(self, runner: CliRunner):
        result = runner.invoke(main, ["version", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "version" in data
        assert "python" in data
        assert "platform" in data

    def test_version_quiet(self, runner: CliRunner):
        result = runner.invoke(main, ["version", "-q"])
        assert result.exit_code == 0
        # Should be just the version number, no "duo " prefix
        assert "duo" not in result.output
        assert result.output.strip() != ""


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

    def test_completion_shells(self, runner: CliRunner):
        for shell, keyword in [
            ("bash", "bash_source"),
            ("zsh", "zsh_source"),
            ("fish", "fish_source"),
        ]:
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

    def test_named_task_shows_session_started(self, runner: CliRunner):
        """status displays Session line when session_started_at is set."""
        task = _make_task()
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

    def test_no_name_lists_all(self, runner: CliRunner):
        _make_task("alpha", "Alpha task")
        _make_task("beta", "Beta task")
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "beta" in result.output

    def test_status_shows_branch(self, runner: CliRunner):
        """status shows the branch name."""
        task = _make_task()
        result = runner.invoke(main, ["status", "test-task"])
        assert result.exit_code == 0
        assert "Branch:" in result.output
        assert task.branch in result.output

    def test_status_shows_description(self, runner: CliRunner):
        """status shows description when set."""
        _make_task("desc-task", "Fix the login bug")
        result = runner.invoke(main, ["status", "desc-task"])
        assert result.exit_code == 0
        assert "Description:" in result.output
        assert "Fix the login bug" in result.output

    def test_status_no_description(self, runner: CliRunner):
        """status omits description when empty."""
        _make_task("no-desc-task", "")
        result = runner.invoke(main, ["status", "no-desc-task"])
        assert result.exit_code == 0
        assert "Description:" not in result.output

    def test_status_shows_heartbeat_file(self, runner: CliRunner):
        """status shows current file from heartbeat."""
        task = _make_task("hb-task")
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

    def test_status_no_heartbeat(self, runner: CliRunner):
        """status omits heartbeat lines when no heartbeat file."""
        _make_task("no-hb-task")
        result = runner.invoke(main, ["status", "no-hb-task"])
        assert result.exit_code == 0
        assert "Working on:" not in result.output
        assert "Last pulse:" not in result.output

    def test_status_quiet_single(self, runner: CliRunner):
        """status -q prints only the status value for a named task."""
        _make_task("q-task")
        result = runner.invoke(main, ["status", "q-task", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "created"

    def test_status_quiet_all(self, runner: CliRunner):
        """status -q with no name prints ID\tstatus for each task."""
        _make_task("alpha-q")
        _make_task("beta-q")
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

    def test_status_wait_already_reached(self, runner: CliRunner):
        """status --wait returns immediately if status already matches."""
        task = _make_task("wait-ok")
        task.status = TaskStatus.COMPLETED
        save_task(task)
        result = runner.invoke(
            main, ["status", "wait-ok", "--wait", "completed", "--timeout", "2"]
        )
        assert result.exit_code == 0
        assert "reached" in result.output

    def test_status_wait_quiet(self, runner: CliRunner):
        """status --wait -q prints just the status value."""
        task = _make_task("wait-q")
        task.status = TaskStatus.COMPLETED
        save_task(task)
        result = runner.invoke(main, ["status", "wait-q", "--wait", "completed", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "completed"

    def test_status_wait_timeout(self, runner: CliRunner):
        """status --wait times out if status doesn't match."""
        _make_task("wait-to")
        result = runner.invoke(
            main, ["status", "wait-to", "--wait", "completed", "--timeout", "1"]
        )
        assert result.exit_code != 0

    def test_status_wait_timeout_quiet(self, runner: CliRunner):
        """status --wait -q on timeout prints current status."""
        _make_task("wait-tq")
        result = runner.invoke(
            main,
            ["status", "wait-tq", "--wait", "completed", "--timeout", "1", "-q"],
        )
        assert result.exit_code != 0
        assert result.output.strip() == "created"

    def test_status_wait_json(self, runner: CliRunner):
        """status --wait --json-output prints JSON on success."""
        task = _make_task("wait-js")
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
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ):
        """status --wait errors if task disappears mid-poll."""
        _make_task("wait-gone")
        call_count = {"n": 0}
        original_load = duo.protocol.load_task

        def _vanishing_load(name: str) -> duo.protocol.Task | None:
            call_count["n"] += 1
            if call_count["n"] >= 2:
                return None
            return original_load(name)

        monkeypatch.setattr("duo.cli.load_task", _vanishing_load)
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

    def test_status_wait_invalid_status(self, runner: CliRunner):
        """status --wait with invalid status name raises error."""
        _make_task("wait-inv")
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

    def test_status_filter(self, runner: CliRunner):
        """list --status filters tasks by status."""
        _make_task("created-task")
        task2 = _make_task("done-task")
        task2.status = TaskStatus.COMPLETED
        save_task(task2)
        result = runner.invoke(main, ["list", "--status", "completed"])
        assert result.exit_code == 0
        assert "done-task" in result.output
        assert "created-task" not in result.output

    def test_status_filter_no_match(self, runner: CliRunner):
        """list --status with no matching tasks shows 'No tasks.'"""
        _make_task("a-task")
        result = runner.invoke(main, ["list", "--status", "completed"])
        assert result.exit_code == 0
        assert "No tasks." in result.output

    def test_status_filter_invalid(self, runner: CliRunner):
        """list --status with invalid status shows error."""
        _make_task("a-task")
        result = runner.invoke(main, ["list", "--status", "bogus"])
        assert result.exit_code != 0
        assert "unknown status" in result.output
        assert "Valid statuses" in result.output

    def test_sort_by_name(self, runner: CliRunner):
        """list --sort name sorts alphabetically."""
        _make_task("zzz-task")
        _make_task("aaa-task")
        result = runner.invoke(main, ["list", "--sort", "name"])
        assert result.exit_code == 0
        lines = [
            l
            for l in result.output.strip().splitlines()
            if "task" in l.lower() and "---" not in l and "ID" not in l
        ]
        assert lines[0].startswith("aaa-task")
        assert lines[1].startswith("zzz-task")

    def test_sort_by_name_reverse(self, runner: CliRunner):
        """list --sort name --reverse reverses order."""
        _make_task("aaa-task")
        _make_task("zzz-task")
        result = runner.invoke(main, ["list", "--sort", "name", "--reverse"])
        assert result.exit_code == 0
        lines = [
            l
            for l in result.output.strip().splitlines()
            if "task" in l.lower() and "---" not in l and "ID" not in l
        ]
        assert lines[0].startswith("zzz-task")
        assert lines[1].startswith("aaa-task")

    def test_sort_by_status(self, runner: CliRunner):
        """list --sort status groups by status value."""
        task_c = _make_task("completed-task")
        task_c.status = TaskStatus.COMPLETED
        save_task(task_c)
        _make_task("active-task")
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

    def test_sort_by_age(self, runner: CliRunner):
        """list --sort age sorts oldest first."""
        _make_task("old-task")
        _make_task("new-task")
        result = runner.invoke(main, ["list", "--sort", "age"])
        assert result.exit_code == 0
        lines = [
            l
            for l in result.output.strip().splitlines()
            if "task" in l.lower() and "---" not in l and "ID" not in l
        ]
        assert len(lines) == 2

    def test_quiet_mode(self, runner: CliRunner):
        """list -q prints only task IDs."""
        _make_task("alpha")
        _make_task("beta")
        result = runner.invoke(main, ["list", "-q"])
        assert result.exit_code == 0
        lines = result.output.strip().splitlines()
        assert sorted(lines) == ["alpha", "beta"]

    def test_quiet_mode_empty(self, runner: CliRunner):
        """list -q with no tasks produces no output."""
        result = runner.invoke(main, ["list", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_quiet_with_status_filter(self, runner: CliRunner):
        """list -q --status filters and prints only IDs."""
        t = _make_task("done-task")
        t.status = TaskStatus.COMPLETED
        save_task(t)
        _make_task("open-task")
        result = runner.invoke(main, ["list", "-q", "--status", "completed"])
        assert result.exit_code == 0
        assert result.output.strip() == "done-task"

    def test_count_mode(self, runner: CliRunner):
        """list -c prints the number of tasks."""
        _make_task("count-a")
        _make_task("count-b")
        result = runner.invoke(main, ["list", "-c"])
        assert result.exit_code == 0
        assert result.output.strip() == "2"

    def test_count_with_filter(self, runner: CliRunner):
        """list -c --status filters then counts."""
        t = _make_task("done-count")
        t.status = TaskStatus.COMPLETED
        save_task(t)
        _make_task("open-count")
        result = runner.invoke(main, ["list", "-c", "--status", "completed"])
        assert result.exit_code == 0
        assert result.output.strip() == "1"

    def test_count_empty(self, runner: CliRunner):
        """list -c with no tasks prints 0."""
        result = runner.invoke(main, ["list", "-c"])
        assert result.exit_code == 0
        assert result.output.strip() == "0"

    def test_no_header(self, runner: CliRunner):
        """list --no-header omits the header row."""
        _make_task("header-task")
        result = runner.invoke(main, ["list", "--no-header"])
        assert result.exit_code == 0
        assert "ID" not in result.output
        assert "STATUS" not in result.output
        assert "header-task" in result.output

    def test_recent(self, runner: CliRunner):
        """list --recent N shows only the N newest tasks."""
        for i in range(3):
            t = _make_task(f"recent-{i}")
            t.created_at = f"2025-01-0{i + 1}T00:00:00Z"
            save_task(t)
        result = runner.invoke(main, ["list", "--recent", "2"])
        assert result.exit_code == 0
        assert "recent-2" in result.output
        assert "recent-1" in result.output
        assert "recent-0" not in result.output

    def test_recent_with_count(self, runner: CliRunner):
        """list --recent N --count shows filtered count."""
        for i in range(3):
            t = _make_task(f"rc-{i}")
            t.created_at = f"2025-01-0{i + 1}T00:00:00Z"
            save_task(t)
        result = runner.invoke(main, ["list", "--recent", "2", "-c"])
        assert result.exit_code == 0
        assert result.output.strip() == "2"

    def test_active_flag(self, runner: CliRunner):
        """list --active shows only running/active tasks."""
        t1 = _make_task("active-1")
        t1.status = TaskStatus.RUNNING
        save_task(t1)
        t2 = _make_task("active-2")
        t2.status = TaskStatus.COMPLETED
        save_task(t2)
        t3 = _make_task("active-3")
        t3.status = TaskStatus.ACKED
        save_task(t3)
        result = runner.invoke(main, ["list", "--active"])
        assert result.exit_code == 0
        assert "active-1" in result.output
        assert "active-3" in result.output
        assert "active-2" not in result.output

    def test_active_count(self, runner: CliRunner):
        """list --active -c shows count of active tasks."""
        t1 = _make_task("ac-1")
        t1.status = TaskStatus.RUNNING
        save_task(t1)
        _make_task("ac-2")  # CREATED, not active
        result = runner.invoke(main, ["list", "--active", "-c"])
        assert result.exit_code == 0
        assert result.output.strip() == "1"

    def test_finished_flag(self, runner: CliRunner):
        """list --finished shows only completed/failed/escalated tasks."""
        t1 = _make_task("fin-1")
        t1.status = TaskStatus.COMPLETED
        save_task(t1)
        t2 = _make_task("fin-2")
        t2.status = TaskStatus.RUNNING
        save_task(t2)
        t3 = _make_task("fin-3")
        t3.status = TaskStatus.FAILED
        save_task(t3)
        result = runner.invoke(main, ["list", "--finished"])
        assert result.exit_code == 0
        assert "fin-1" in result.output
        assert "fin-3" in result.output
        assert "fin-2" not in result.output

    def test_finished_count(self, runner: CliRunner):
        """list --finished -c shows count of finished tasks."""
        t1 = _make_task("fc-1")
        t1.status = TaskStatus.COMPLETED
        save_task(t1)
        _make_task("fc-2")  # CREATED, not finished
        result = runner.invoke(main, ["list", "--finished", "-c"])
        assert result.exit_code == 0
        assert result.output.strip() == "1"


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

    def test_recover_skips_escalated_task(self, runner: CliRunner):
        """recover() skips tasks with ESCALATED status — human decision should be preserved."""
        task = _make_task("esc-task")
        task.status = TaskStatus.ESCALATED
        save_task(task)
        result = runner.invoke(main, ["recover"])
        assert result.exit_code == 0
        assert "All tasks consistent." in result.output

    def test_recover_json_output(self, runner: CliRunner):
        """recover --json-output returns structured JSON."""
        task = _make_task("json-recover")
        task.status = TaskStatus.RUNNING
        save_task(task)
        result = runner.invoke(main, ["recover", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["recovered"] == 1
        assert len(data["changes"]) == 1
        assert data["changes"][0]["task"] == "json-recover"
        assert data["changes"][0]["from"] == "running"
        assert data["changes"][0]["to"] == "created"

    def test_recover_json_output_consistent(self, runner: CliRunner):
        """recover --json-output with no changes returns empty list."""
        _make_task("ok-task")
        result = runner.invoke(main, ["recover", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["recovered"] == 0
        assert data["changes"] == []

    def test_recover_quiet(self, runner: CliRunner):
        """recover -q prints only the recovered count."""
        task = _make_task("q-rec")
        task.status = TaskStatus.RUNNING
        save_task(task)
        result = runner.invoke(main, ["recover", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "1"

    def test_recover_quiet_zero(self, runner: CliRunner):
        """recover -q with no recoveries prints 0."""
        _make_task("ok-rec")
        result = runner.invoke(main, ["recover", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "0"


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
        assert "Try: 'bad-name'" in result.output

    def test_send_empty_prompt(self, runner: CliRunner):
        """Verify send() rejects empty prompts."""
        _make_task("empty-prompt-task")
        result = runner.invoke(main, ["send", "empty-prompt-task", ""])
        assert result.exit_code != 0
        assert (
            "empty" in result.output.lower()
            or "empty" in (result.output + str(result.exception)).lower()
        )

    def test_send_whitespace_prompt(self, runner: CliRunner):
        """Verify send() rejects whitespace-only prompts."""
        _make_task("ws-prompt-task")
        result = runner.invoke(main, ["send", "ws-prompt-task", "   "])
        assert result.exit_code != 0
        assert (
            "empty" in result.output.lower()
            or "empty" in (result.output + str(result.exception)).lower()
        )

    def test_send_rejects_dead_states(self, runner: CliRunner):
        """send() rejects prompts to tasks in terminal or blocked states."""
        cases = [
            (TaskStatus.COMPLETED, "terminal state"),
            (TaskStatus.FAILED, "terminal state"),
            (TaskStatus.ESCALATED, "terminal state"),
            (TaskStatus.BLOCKED, "blocked"),
        ]
        for status, expected_msg in cases:
            task = _make_task(f"dead-{status.value}")
            task.status = status
            save_task(task)
            result = runner.invoke(main, ["send", f"dead-{status.value}", "hello"])
            assert result.exit_code != 0, f"{status.value} should be rejected"
            assert expected_msg in result.output

    def test_send_from_file(self, runner: CliRunner, tmp_path: Path):
        """send --file reads prompt from a file."""
        _make_task("file-prompt")
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("hello from file", encoding="utf-8")
        result = runner.invoke(
            main, ["send", "file-prompt", "--file", str(prompt_file)]
        )
        # Will fail at transport layer but should get past prompt parsing
        assert "empty" not in result.output.lower()

    def test_send_file_and_prompt_conflict(self, runner: CliRunner, tmp_path: Path):
        """send rejects both PROMPT argument and --file."""
        _make_task("conflict-prompt")
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("hello", encoding="utf-8")
        result = runner.invoke(
            main, ["send", "conflict-prompt", "inline", "--file", str(prompt_file)]
        )
        assert result.exit_code != 0
        assert "cannot specify both" in result.output.lower()

    def test_send_no_prompt_no_file(self, runner: CliRunner):
        """send with neither prompt nor --file shows usage error."""
        _make_task("no-prompt")
        result = runner.invoke(main, ["send", "no-prompt"])
        assert result.exit_code != 0


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

    def test_git_worktree_dot_git_file_accepted(
        self, runner: CliRunner, tmp_path: Path
    ):
        """Repos where .git is a file (git worktrees) should pass the .git check."""
        repo = tmp_path / "worktree-repo"
        repo.mkdir()
        (repo / ".git").write_text("gitdir: /somewhere/.git/worktrees/x\n")
        result = runner.invoke(main, ["start", "wt-task", "--repo", str(repo)])
        # Should pass the .git existence check — not get "no .git found"
        assert "no .git found" not in result.output

    def test_repo_path_does_not_exist(self, runner: CliRunner, tmp_path: Path):
        result = runner.invoke(
            main, ["start", "t", "--repo", str(tmp_path / "nonexistent")]
        )
        assert result.exit_code != 0
        assert "does not exist" in result.output

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

    def test_start_rejects_unicode_names(self, runner: CliRunner, tmp_path: Path):
        """Unicode names in start command are rejected."""
        for invalid_name in [
            "tâche",
            "任务",
            "タスク",
            "задача",
            "name with space",
            "name\twith\ttab",
        ]:
            result = runner.invoke(
                main,
                ["start", invalid_name, "--repo", str(tmp_path), "--desc", "test"],
            )
            assert result.exit_code != 0, f"{invalid_name!r} should be rejected"

    def test_start_concurrent_lock(self, runner: CliRunner, tmp_path: Path):
        """Concurrent start attempts are protected by lockfile."""
        import fcntl
        import subprocess

        import duo.protocol

        # Create a real git repo so validation passes
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "--allow-empty", "-m", "init"],
            capture_output=True,
            check=True,
        )

        lock_path = duo.protocol.TASKS_DIR / ".lock-task.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = open(lock_path, "w")  # noqa: SIM115
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = runner.invoke(
                main, ["start", "lock-task", "--repo", str(repo), "--desc", "t"]
            )
            assert result.exit_code != 0
            assert "another process" in result.output.lower()
        finally:
            lock_fd.close()
            lock_path.unlink(missing_ok=True)

    def test_start_race_recheck_after_lock(self, runner: CliRunner, tmp_path: Path):
        """Re-check after lock detects task created by another process."""
        import subprocess
        from unittest.mock import patch

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "--allow-empty", "-m", "init"],
            capture_output=True,
            check=True,
        )

        call_count = 0

        def load_side_effect(name: str):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return None  # first check passes
            return _make_task(name)  # re-check finds task

        with patch("duo.cli.lifecycle_cmd.load_task", side_effect=load_side_effect):
            result = runner.invoke(
                main, ["start", "race-task", "--repo", str(repo), "--desc", "t"]
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

    def test_kill_empty_worktree_list(self, runner: CliRunner):
        """kill handles empty git worktree list gracefully."""
        task = _make_task("kill-empty-wt")
        task.status = TaskStatus.RUNNING
        save_task(task)

        proc = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("duo.cli.subprocess.run", return_value=proc),
            patch("duo.transport.kill_pane", return_value=True),
            patch("duo.transport.cleanup_pane_state"),
        ):
            result = runner.invoke(main, ["kill", "kill-empty-wt"])
            assert result.exit_code == 0

    def test_kill_worktree_list_no_match(self, runner: CliRunner):
        """kill handles worktree list where no line matches worktree pattern."""
        task = _make_task("kill-no-match")
        task.status = TaskStatus.RUNNING
        save_task(task)

        # All lines contain worktree_base_path or don't start with 'worktree '
        wt_output = "branch refs/heads/main\nbare\n"
        proc = MagicMock(returncode=0, stdout=wt_output, stderr="")
        with (
            patch("duo.cli.subprocess.run", return_value=proc),
            patch("duo.transport.kill_pane", return_value=True),
            patch("duo.transport.cleanup_pane_state"),
        ):
            result = runner.invoke(main, ["kill", "kill-no-match"])
            assert result.exit_code == 0


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

    def test_filter_events(self, runner: CliRunner):
        """logs --filter narrows events by event type substring."""
        task = _make_task("filter-task")
        from duo.protocol import append_event

        append_event(task, "step_started", {"step": 1})
        append_event(task, "step_completed", {"step": 1})
        result = runner.invoke(main, ["logs", "filter-task", "--filter", "completed"])
        assert result.exit_code == 0
        assert "step_completed" in result.output
        assert "task_created" not in result.output

    def test_filter_no_match(self, runner: CliRunner):
        """logs --filter with no matching events shows no event lines."""
        _make_task("filter-empty")
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

    def test_inspect_events_count(self, runner: CliRunner):
        """inspect --events N shows N events."""
        from duo.protocol import append_event

        task = _make_task("ev-count")
        for i in range(10):
            append_event(task, f"event_{i}", {"i": i})
        result = runner.invoke(main, ["inspect", "ev-count", "--events", "3"])
        assert result.exit_code == 0
        assert "showing 3" in result.output

    def test_inspect_events_zero_shows_all(self, runner: CliRunner):
        """inspect --events 0 shows all events."""
        from duo.protocol import append_event

        task = _make_task("ev-all")
        for i in range(5):
            append_event(task, f"event_{i}", {"i": i})
        result = runner.invoke(main, ["inspect", "ev-all", "--events", "0"])
        assert result.exit_code == 0
        # should show all events (task_created + 5 custom = 6 total)
        assert "showing 6" in result.output


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

    def test_inspect_quiet(self, runner: CliRunner):
        """inspect -q prints only the status value."""
        _make_task("q-insp")
        result = runner.invoke(main, ["inspect", "q-insp", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "created"


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
        # Prompt should be persisted to file, not sent via transport
        prompt_path = task.prompt_path(task.current_step, task.current_attempt)
        assert prompt_path.exists()
        assert prompt_path.read_text() == "hello"

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


class TestBatchEdgeCases:
    def test_batch_invalid_json_file(self, runner: CliRunner, tmp_path: Path):
        """batch with a malformed JSON file shows an error."""
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json!!!}")
        result = runner.invoke(main, ["batch", str(bad)])
        assert result.exit_code != 0

    def test_batch_invalid_yaml_file(self, runner: CliRunner, tmp_path: Path):
        """batch with non-mapping YAML shows error (if pyyaml available)."""
        try:
            import yaml  # noqa: F401
        except ImportError:
            pytest.skip("PyYAML not installed")
        bad = tmp_path / "bad.yaml"
        # Write content that parses as a plain string, not a dict or list
        bad.write_text("just a plain string\n")
        result = runner.invoke(main, ["batch", str(bad)])
        assert result.exit_code != 0
        assert "tasks" in result.output.lower()


# ---------------------------------------------------------------------------
# export edge cases
# ---------------------------------------------------------------------------


# TestExportEdgeCases — deleted: duplicate of TestExport.test_task_not_found
# TestAuditEdgeCases — deleted: duplicate of TestAudit.test_audit_no_tasks


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

    def test_config_list_shows_descriptions(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config list text output includes inline descriptions."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "list"])
        assert result.exit_code == 0
        assert "# " in result.output
        assert "AI model" in result.output

    def test_config_descriptions_cover_all_keys(self):
        """Every DEFAULTS key has a CONFIG_DESCRIPTIONS entry."""
        import duo.config as config_mod

        for key in config_mod.DEFAULTS:
            assert key in config_mod.CONFIG_DESCRIPTIONS, (
                f"Missing description for config key: {key}"
            )

    def test_config_key_completion(self):
        """_complete_config_keys returns matching keys with help text."""
        from duo.cli import _complete_config_keys

        items = _complete_config_keys(None, None, "poll_")  # type: ignore[arg-type]
        names = [i.value for i in items]
        assert "poll_base_interval" in names
        assert "poll_max_interval" in names
        assert "copilot_model" not in names
        assert all(i.help for i in items)

    def test_config_key_completion_empty_prefix(self):
        """Empty prefix returns all keys."""
        from duo.cli import _complete_config_keys

        items = _complete_config_keys(None, None, "")  # type: ignore[arg-type]
        assert len(items) == 12


class TestTaskNameCompletion:
    def test_task_name_completion(self):
        """_complete_task_names returns matching task names."""
        from duo.cli import _complete_task_names

        _make_task("alpha-task")
        _make_task("beta-task")
        items = _complete_task_names(None, None, "alpha")  # type: ignore[arg-type]
        names = [i.value for i in items]
        assert "alpha-task" in names
        assert "beta-task" not in names

    def test_task_name_completion_empty(self):
        """Empty prefix returns all task names."""
        from duo.cli import _complete_task_names

        _make_task("first")
        _make_task("second")
        items = _complete_task_names(None, None, "")  # type: ignore[arg-type]
        names = [i.value for i in items]
        assert "first" in names
        assert "second" in names
        assert all(i.help for i in items)

    def test_task_name_completion_error(self, monkeypatch: pytest.MonkeyPatch):
        """Completion returns empty list on error."""
        from duo.cli import _complete_task_names

        monkeypatch.setattr(
            "duo.protocol.list_tasks", lambda: (_ for _ in ()).throw(OSError("fail"))
        )
        items = _complete_task_names(None, None, "")  # type: ignore[arg-type]
        assert items == []


class TestThinkingNameCompletion:
    def test_thinking_name_completion(self, tmp_path: Path, monkeypatch):
        """_complete_thinking_names returns matching session names."""
        from duo.cli import _complete_thinking_names

        tdir = tmp_path / "thinking"
        tdir.mkdir()
        (tdir / "alpha-session").mkdir()
        (tdir / "beta-session").mkdir()
        monkeypatch.setattr("duo.thinking.THINKING_DIR", tdir)
        items = _complete_thinking_names(None, None, "alpha")  # type: ignore[arg-type]
        names = [i.value for i in items]
        assert "alpha-session" in names
        assert "beta-session" not in names

    def test_thinking_name_completion_empty_prefix(self, tmp_path: Path, monkeypatch):
        """Empty prefix returns all session names."""
        from duo.cli import _complete_thinking_names

        tdir = tmp_path / "thinking"
        tdir.mkdir()
        (tdir / "first").mkdir()
        (tdir / "second").mkdir()
        monkeypatch.setattr("duo.thinking.THINKING_DIR", tdir)
        items = _complete_thinking_names(None, None, "")  # type: ignore[arg-type]
        names = [i.value for i in items]
        assert "first" in names
        assert "second" in names

    def test_thinking_name_completion_no_dir(self, tmp_path: Path, monkeypatch):
        """Returns empty list when thinking dir doesn't exist."""
        from duo.cli import _complete_thinking_names

        monkeypatch.setattr("duo.thinking.THINKING_DIR", tmp_path / "nonexistent")
        items = _complete_thinking_names(None, None, "")  # type: ignore[arg-type]
        assert items == []

    def test_thinking_name_completion_error(self, monkeypatch: pytest.MonkeyPatch):
        """Returns empty list on error."""
        from duo.cli import _complete_thinking_names

        monkeypatch.setattr(
            "duo.thinking.THINKING_DIR",
            property(lambda self: (_ for _ in ()).throw(OSError("fail"))),
        )
        items = _complete_thinking_names(None, None, "")  # type: ignore[arg-type]
        assert items == []


class TestStatusValueCompletion:
    def test_status_completion_prefix(self):
        """_complete_status_values returns matching status values."""
        from duo.cli import _complete_status_values

        items = _complete_status_values(None, None, "run")  # type: ignore[arg-type]
        names = [i.value for i in items]
        assert "running" in names
        assert "completed" not in names

    def test_status_completion_all(self):
        """Empty prefix returns all status values."""
        from duo.cli import _complete_status_values

        items = _complete_status_values(None, None, "")  # type: ignore[arg-type]
        names = [i.value for i in items]
        assert "running" in names
        assert "completed" in names
        assert "created" in names
        assert len(names) == len(TaskStatus)

    def test_status_completion_error(self, monkeypatch: pytest.MonkeyPatch):
        """Returns empty list on error."""
        from duo.cli import _complete_status_values

        monkeypatch.setattr(
            "duo.cli._helpers.TaskStatus",
            property(lambda self: (_ for _ in ()).throw(RuntimeError("fail"))),
        )
        items = _complete_status_values(None, None, "")  # type: ignore[arg-type]
        assert items == []


# ---------------------------------------------------------------------------
# _safe_join
# ---------------------------------------------------------------------------


class TestSafeJoin:
    def test_safe_join_normal(self, tmp_path: Path) -> None:
        """Normal name is joined correctly."""
        result = _safe_join(str(tmp_path), "my-task")
        assert result == str(tmp_path / "my-task")

    def test_safe_join_traversal_rejected(self, tmp_path: Path) -> None:
        """Various path traversal attempts are rejected."""
        import click

        malicious_paths = [
            "../../../etc",
            "/etc/passwd",
            "foo/../../../etc",
            "foo/../../bar",
            "../",
            "..",
            "a/../b/../../../etc",
        ]
        for malicious in malicious_paths:
            with pytest.raises(click.BadParameter, match="traversal"):
                _safe_join(str(tmp_path), malicious)

    def test_safe_join_valid_names(self, tmp_path: Path) -> None:
        """Valid task names are joined correctly."""
        for safe_name in ["my-task", "task_123", "FooBar", "a", "x-y-z_0"]:
            result = _safe_join(str(tmp_path), safe_name)
            assert result == str(tmp_path / safe_name)


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

    def test_special_characters_rejected(self):
        """Each special character in task name is rejected."""
        import click

        for char in list("!@#$%^&*()+=[]{}|\\:;\"'<>,./? \t\n"):
            with pytest.raises(click.BadParameter):
                _validate_task_name(f"task{char}name")


# ---------------------------------------------------------------------------
# Hypothesis property-based tests
# ---------------------------------------------------------------------------


class TestPropertyBased:
    """Property-based tests using hypothesis for validation functions."""

    @given(
        st.text(
            alphabet=string.ascii_letters + string.digits + "_-",
            min_size=1,
            max_size=63,
        )
    )
    def test_valid_task_names_always_accepted(self, name: str):
        """Any string of valid characters ≤63 chars is accepted."""
        _validate_task_name(name)  # Should not raise

    @given(st.text(min_size=64, max_size=200))
    def test_long_names_always_rejected(self, name: str):
        """Names >63 chars are always rejected."""
        import click

        with pytest.raises(click.BadParameter, match="at most 63"):
            _validate_task_name(name)

    @given(
        st.sampled_from(["d", "h", "m", "s"]), st.integers(min_value=1, max_value=999)
    )
    def test_parse_age_unit_conversion(self, unit: str, value: int):
        """All valid age strings produce correct seconds."""
        expected = value * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
        assert _parse_age(f"{value}{unit}") == expected

    @given(st.text().filter(lambda s: not re.match(r"^\d+[dhms]$", s)))
    def test_parse_age_rejects_invalid(self, age_str: str):
        """Invalid age strings raise UsageError."""
        with pytest.raises(click.UsageError):
            _parse_age(age_str)

    def test_parse_age_rejects_too_large(self):
        """Age exceeding ~1000 years is rejected."""
        with pytest.raises(click.UsageError):
            _parse_age("999999d")

    @given(
        st.dictionaries(
            st.text(min_size=1, max_size=20),
            st.one_of(st.integers(), st.text(max_size=50), st.booleans()),
            max_size=5,
        )
    )
    def test_json_roundtrip(self, data: dict):
        """write_json → read_json preserves data."""
        import tempfile

        from duo.protocol import read_json, write_json

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "test.json"
            write_json(p, data)
            result = read_json(p)
            assert result == data


# ---------------------------------------------------------------------------
# _create_worktree helper
# ---------------------------------------------------------------------------


class TestCreateWorktree:
    def test_success(self, tmp_path: Path):
        """_create_worktree returns (worktree_path, base_commit) on success."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        with (
            patch("duo.config.get_config", return_value=str(tmp_path / "wt")),
            patch("duo.cli.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            worktree, base_commit = _create_worktree("my-task", str(repo))
            assert base_commit == "abc123"
            assert "my-task" in worktree
            assert mock_run.call_count == 2

    def test_not_git_repo(self, tmp_path: Path):
        """_create_worktree exits if repo has no .git directory."""
        repo = tmp_path / "no-git"
        repo.mkdir()
        with pytest.raises(DuoUserError, match="not a git repository"):
            _create_worktree("fail-task", str(repo))

    def test_repo_path_missing(self, tmp_path: Path):
        """_create_worktree exits if repo path doesn't exist."""
        with pytest.raises(DuoUserError, match="does not exist"):
            _create_worktree("fail-task", str(tmp_path / "nonexistent"))

    def test_worktree_add_fails(self, tmp_path: Path):
        """_create_worktree exits if 'git worktree add' fails."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        with (
            patch("duo.config.get_config", return_value=str(tmp_path / "wt")),
            patch("duo.cli.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\n", stderr=""),
                MagicMock(returncode=1, stdout="", stderr="branch already exists"),
            ]
            with pytest.raises(click.ClickException):
                _create_worktree("dup-task", str(repo))


# ---------------------------------------------------------------------------
# start command — successful path + queue path
# ---------------------------------------------------------------------------


class TestStartSuccess:
    def test_start_and_run(self, runner: CliRunner, tmp_path: Path):
        """start creates task + worktree, starts session in defer mode by default."""
        with (
            patch("duo.cli.lifecycle_cmd._create_worktree") as mock_wt,
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
            assert "Session ready (deferred)" in result.output
            assert "duo send my-task" in result.output
            mock_start.assert_called_once()
            # Verify defer=True is passed by default
            _, kwargs = mock_start.call_args
            assert kwargs.get("defer") is True

    def test_start_immediate(self, runner: CliRunner, tmp_path: Path):
        """start --immediate sends bootstrap immediately (old behavior)."""
        with (
            patch("duo.cli.lifecycle_cmd._create_worktree") as mock_wt,
            patch("duo.cli.subprocess.run"),
            patch("duo.commander.start_session") as mock_start,
            patch("duo.scheduler.enqueue_or_start", return_value="started"),
        ):
            mock_wt.return_value = (str(tmp_path / "wt" / "my-task"), "abc123")
            result = runner.invoke(
                main,
                [
                    "start",
                    "my-task",
                    "--repo",
                    str(tmp_path),
                    "--desc",
                    "hello",
                    "--immediate",
                ],
            )
            assert result.exit_code == 0
            assert "Session started" in result.output
            mock_start.assert_called_once()
            _, kwargs = mock_start.call_args
            assert kwargs.get("defer") is False

    def test_start_queued(self, runner: CliRunner, tmp_path: Path):
        """start queues task when slots are full."""
        with (
            patch("duo.cli.lifecycle_cmd._create_worktree") as mock_wt,
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
        """send to queued task persists prompt but doesn't call transport."""
        task = _make_task("q-send")
        task.status = TaskStatus.QUEUED
        save_task(task)
        with patch("duo.commander.send_task_prompt") as mock_send:
            result = runner.invoke(main, ["send", "q-send", "hello"])
            assert result.exit_code == 0
            assert "queued" in result.output.lower()
            mock_send.assert_not_called()  # no transport for queued tasks

    def test_send_json_output(self, runner: CliRunner):
        """send --json-output returns structured JSON."""
        _make_task("send-json")
        with patch("duo.commander.send_task_prompt"):
            result = runner.invoke(
                main, ["send", "send-json", "do it", "--json-output"]
            )
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["sent"] is True
            assert data["task"] == "send-json"
            assert "step" in data

    def test_send_queued_json_output(self, runner: CliRunner):
        """send --json-output on queued task returns queued status."""
        task = _make_task("send-q-json")
        task.status = TaskStatus.QUEUED
        save_task(task)
        with patch("duo.commander.send_task_prompt") as mock_send:
            result = runner.invoke(
                main, ["send", "send-q-json", "hello", "--json-output"]
            )
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["sent"] is False
            assert data["queued"] is True
            mock_send.assert_not_called()

    def test_send_deferred_task_uses_bootstrap(self, runner: CliRunner):
        """send to SESSION_STARTING task sends bootstrap instead of dialog prompt."""
        task = _make_task("deferred-task")
        task.status = TaskStatus.SESSION_STARTING
        task.pane_label = "deferred-task"
        save_task(task)
        with (
            patch("duo.transport.send_bootstrap") as mock_boot,
            patch(
                "duo.commander.build_bootstrap_prompt",
                return_value="bootstrap+prompt",
            ),
        ):
            result = runner.invoke(main, ["send", "deferred-task", "my instruction"])
            assert result.exit_code == 0
            assert "First prompt sent" in result.output
            mock_boot.assert_called_once_with("deferred-task", "bootstrap+prompt")

    def test_send_deferred_task_json_output(self, runner: CliRunner):
        """send --json-output to deferred task returns first_prompt: true."""
        task = _make_task("defer-json")
        task.status = TaskStatus.SESSION_STARTING
        task.pane_label = "defer-json"
        save_task(task)
        with (
            patch("duo.transport.send_bootstrap"),
            patch("duo.commander.build_bootstrap_prompt", return_value="bootstrap"),
        ):
            result = runner.invoke(
                main, ["send", "defer-json", "hello", "--json-output"]
            )
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["sent"] is True
            assert data["first_prompt"] is True

    def test_send_deferred_persists_prompt_file(self, runner: CliRunner):
        """Deferred send persists prompt file for resume/replay safety."""
        task = _make_task("defer-persist")
        task.status = TaskStatus.SESSION_STARTING
        task.pane_label = "defer-persist"
        save_task(task)
        with (
            patch("duo.transport.send_bootstrap"),
            patch("duo.commander.build_bootstrap_prompt", return_value="bootstrap"),
        ):
            result = runner.invoke(
                main, ["send", "defer-persist", "my important instruction"]
            )
            assert result.exit_code == 0
            prompt_path = task.prompt_path(task.current_step, task.current_attempt)
            assert prompt_path.exists()
            assert prompt_path.read_text() == "my important instruction"

    def test_send_quiet_normal(self, runner: CliRunner):
        """send -q prints 'sent' on normal send."""
        task = _make_task("send-q-ok")
        task.status = TaskStatus.RUNNING
        save_task(task)
        with patch("duo.commander.send_task_prompt"):
            result = runner.invoke(main, ["send", "send-q-ok", "do something", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "sent"

    def test_send_quiet_queued(self, runner: CliRunner):
        """send -q prints 'queued' for queued task."""
        task = _make_task("send-q-queue")
        task.status = TaskStatus.QUEUED
        save_task(task)
        result = runner.invoke(main, ["send", "send-q-queue", "do something", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "queued"

    def test_send_quiet_deferred(self, runner: CliRunner):
        """send -q prints 'sent' for deferred session."""
        task = _make_task("send-q-def")
        task.status = TaskStatus.SESSION_STARTING
        task.pane_label = "send-q-def"
        save_task(task)
        with (
            patch("duo.transport.send_bootstrap"),
            patch("duo.commander.build_bootstrap_prompt", return_value="bootstrap"),
        ):
            result = runner.invoke(main, ["send", "send-q-def", "do something", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "sent"


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
        """stop shows warning when kill_pane returns False."""
        task = _make_task("stop-pane-fail")
        task.status = TaskStatus.RUNNING
        save_task(task)

        with (
            patch("duo.transport.kill_pane", return_value=False),
            patch("duo.transport.cleanup_pane_state"),
        ):
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

    def test_stop_json_output(self, runner: CliRunner):
        """stop --json-output returns structured JSON."""
        task = _make_task("stop-json")
        task.status = TaskStatus.RUNNING
        save_task(task)

        with (
            patch("duo.transport.kill_pane", return_value=True),
            patch("duo.transport.cleanup_pane_state"),
        ):
            result = runner.invoke(main, ["stop", "stop-json", "--json-output"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["stopped"] is True
            assert data["previous_status"] == "running"
            assert "worktree" in data

    def test_stop_json_pane_kill_failure_silent(self, runner: CliRunner):
        """stop --json-output with kill_pane failure doesn't print warning text."""
        task = _make_task("stop-json-fail")
        task.status = TaskStatus.RUNNING
        save_task(task)

        with (
            patch("duo.transport.kill_pane", return_value=False),
            patch("duo.transport.cleanup_pane_state"),
        ):
            result = runner.invoke(main, ["stop", "stop-json-fail", "--json-output"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["stopped"] is True
            # No text warning in JSON mode
            assert "Warning" not in result.output.split("\n")[0]

    def test_stop_json_already_terminal(self, runner: CliRunner):
        """stop --json-output on terminal task returns reason."""
        task = _make_task("stop-json-term")
        task.status = TaskStatus.COMPLETED
        save_task(task)

        result = runner.invoke(main, ["stop", "stop-json-term", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["stopped"] is False
        assert data["reason"] == "already_terminal"

    def test_stop_json_already_blocked(self, runner: CliRunner):
        """stop --json-output on blocked task returns reason."""
        task = _make_task("stop-json-blk")
        task.status = TaskStatus.BLOCKED
        save_task(task)

        result = runner.invoke(main, ["stop", "stop-json-blk", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["stopped"] is False
        assert data["reason"] == "already_stopped"

    def test_stop_transition_failure_warns(self, runner: CliRunner):
        """stop warns when BLOCKED transition fails (text mode)."""
        task = _make_task("stop-trans-fail")
        task.status = TaskStatus.RUNNING
        save_task(task)

        with (
            patch("duo.cli.subprocess.run"),
            patch("duo.protocol.transition", return_value=False),
        ):
            result = runner.invoke(main, ["stop", "stop-trans-fail"])
            assert "Warning" in result.output
            assert "could not transition" in result.output

    def test_stop_transition_failure_json(self, runner: CliRunner):
        """stop --json-output reports stopped=False when transition fails."""
        task = _make_task("stop-trans-fail-j")
        task.status = TaskStatus.RUNNING
        save_task(task)

        with (
            patch("duo.cli.subprocess.run"),
            patch("duo.protocol.transition", return_value=False),
        ):
            result = runner.invoke(main, ["stop", "stop-trans-fail-j", "--json-output"])
            data = json.loads(result.output)
            assert data["stopped"] is False

    def test_stop_from_all_non_terminal_states(self, runner: CliRunner):
        """stop successfully transitions to BLOCKED from every non-terminal state."""
        non_terminal = [
            TaskStatus.CREATED,
            TaskStatus.QUEUED,
            TaskStatus.SESSION_STARTING,
            TaskStatus.PROMPT_SENT,
            TaskStatus.ACKED,
            TaskStatus.RUNNING,
            TaskStatus.RESULT_REPORTED,
            TaskStatus.VERIFYING,
            TaskStatus.CORRECTING,
            TaskStatus.ESCALATED,
        ]
        for state in non_terminal:
            tid = f"stop-{state.value}"
            task = _make_task(tid)
            task.status = state
            save_task(task)

            with patch("duo.cli.subprocess.run"):
                result = runner.invoke(main, ["stop", tid])
                assert result.exit_code == 0, f"stop failed for {state.value}"
                assert "Stopped" in result.output

            reloaded = load_task(tid)
            assert reloaded is not None
            assert reloaded.status == TaskStatus.BLOCKED

    def test_stop_all_stops_active_tasks(self, runner: CliRunner):
        """stop --all stops all non-terminal tasks."""
        t1 = _make_task("stop-all-1")
        t1.status = TaskStatus.RUNNING
        save_task(t1)
        t2 = _make_task("stop-all-2")
        t2.status = TaskStatus.ACKED
        save_task(t2)
        # completed task should be skipped
        t3 = _make_task("stop-all-3")
        t3.status = TaskStatus.COMPLETED
        save_task(t3)

        with patch("duo.cli.subprocess.run"):
            result = runner.invoke(main, ["stop", "--all"])
            assert result.exit_code == 0
            assert "stop-all-1" in result.output
            assert "stop-all-2" in result.output
            assert "stop-all-3" not in result.output
            assert "2 task(s) stopped" in result.output

    def test_stop_all_no_active(self, runner: CliRunner):
        """stop --all with no active tasks prints message."""
        result = runner.invoke(main, ["stop", "--all"])
        assert result.exit_code == 0
        assert "No active tasks" in result.output

    def test_stop_all_json(self, runner: CliRunner):
        """stop --all --json-output returns structured JSON."""
        t = _make_task("stop-all-j")
        t.status = TaskStatus.RUNNING
        save_task(t)

        with patch("duo.cli.subprocess.run"):
            result = runner.invoke(main, ["stop", "--all", "--json-output"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["stopped_count"] == 1
            assert len(data["tasks"]) == 1

    def test_stop_no_name_no_all(self, runner: CliRunner):
        """stop without name or --all shows error."""
        result = runner.invoke(main, ["stop"])
        assert result.exit_code != 0
        assert "Provide a task NAME or use --all" in result.output

    def test_stop_all_transition_failure(self, runner: CliRunner):
        """stop --all handles tasks where transition fails."""
        t = _make_task("stop-all-tf")
        t.status = TaskStatus.RUNNING
        save_task(t)

        with (
            patch("duo.cli.subprocess.run"),
            patch("duo.protocol.transition", return_value=False),
        ):
            result = runner.invoke(main, ["stop", "--all"])
            assert result.exit_code == 0
            assert "stop-all-tf" in result.output

    def test_stop_quiet_success(self, runner: CliRunner):
        """stop -q prints 'stopped' on success."""
        t = _make_task("stop-q-ok")
        t.status = TaskStatus.RUNNING
        save_task(t)
        with patch("duo.cli.subprocess.run"):
            result = runner.invoke(main, ["stop", "stop-q-ok", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "stopped"

    def test_stop_quiet_already_terminal(self, runner: CliRunner):
        """stop -q on completed task prints status value."""
        t = _make_task("stop-q-done")
        t.status = TaskStatus.COMPLETED
        save_task(t)
        result = runner.invoke(main, ["stop", "stop-q-done", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "completed"

    def test_stop_quiet_already_blocked(self, runner: CliRunner):
        """stop -q on blocked task prints 'blocked'."""
        t = _make_task("stop-q-blk")
        t.status = TaskStatus.BLOCKED
        save_task(t)
        result = runner.invoke(main, ["stop", "stop-q-blk", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "blocked"


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
        """kill shows warning when kill_pane returns False."""
        task = _make_task("kill-pane-fail")
        wt_dir = tmp_path / "kill_pane_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        save_task(task)

        def mock_subprocess_run(args, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            if args[:3] == ["git", "worktree", "list"]:
                m.stdout = "worktree /main\n  branch refs/heads/main\n\n"
            return m

        with (
            patch("duo.transport.kill_pane", return_value=False),
            patch("duo.transport.cleanup_pane_state"),
            patch("duo.cli.subprocess.run", side_effect=mock_subprocess_run),
        ):
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

    def test_kill_json_output(self, runner: CliRunner, tmp_path: Path):
        """kill --json-output returns structured JSON."""
        task = _make_task("kill-json")
        wt_dir = tmp_path / "kill_json_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        save_task(task)

        with patch("duo.cli.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="worktree /main\n  branch refs/heads/main\n\n",
                stderr="",
            )
            result = runner.invoke(main, ["kill", "kill-json", "--json-output"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["killed"] is True
            assert "pane_killed" in data
            assert "worktree_removed" in data
            assert "branch_deleted" in data

    def test_kill_emits_status_changed_event(self, runner: CliRunner, tmp_path: Path):
        """kill uses transition() to emit status_changed for journal replay."""
        from duo.protocol import TaskStatus, read_jsonl, transition

        task = _make_task("kill-trans")
        transition(task, TaskStatus.SESSION_STARTING)
        wt_dir = tmp_path / "kill_trans_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        save_task(task)

        with patch("duo.cli.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="worktree /main\n  branch refs/heads/main\n\n",
                stderr="",
            )
            result = runner.invoke(main, ["kill", "kill-trans"])
            assert result.exit_code == 0
        events = read_jsonl(task.journal_path)
        status_events = [e for e in events if e.get("event") == "status_changed"]
        assert any(e["data"]["to"] == "failed" for e in status_events)

    def test_kill_completed_task_uses_fallback(self, runner: CliRunner, tmp_path: Path):
        """kill from COMPLETED falls back to direct save (no FAILED transition)."""
        from duo.protocol import TaskStatus, transition

        task = _make_task("kill-comp")
        transition(task, TaskStatus.SESSION_STARTING)
        transition(task, TaskStatus.PROMPT_SENT)
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)
        transition(task, TaskStatus.VERIFYING)
        transition(task, TaskStatus.COMPLETED)
        wt_dir = tmp_path / "kill_comp_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        save_task(task)

        with patch("duo.cli.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="worktree /main\n  branch refs/heads/main\n\n",
                stderr="",
            )
            result = runner.invoke(main, ["kill", "kill-comp"])
            assert result.exit_code == 0
        task_reloaded = load_task("kill-comp")
        assert task_reloaded.status == TaskStatus.FAILED


class TestKillAll:
    def test_kill_all(self, runner: CliRunner):
        """kill --all kills all tasks."""
        _make_task("kill-all-1")
        _make_task("kill-all-2")

        proc = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("duo.cli.subprocess.run", return_value=proc),
            patch("duo.transport.kill_pane", return_value=True),
            patch("duo.transport.cleanup_pane_state"),
        ):
            result = runner.invoke(main, ["kill", "--all"])
            assert result.exit_code == 0
            assert "kill-all-1" in result.output
            assert "kill-all-2" in result.output
            assert "2 task(s) killed" in result.output

    def test_kill_all_empty(self, runner: CliRunner):
        """kill --all with no tasks prints message."""
        result = runner.invoke(main, ["kill", "--all"])
        assert result.exit_code == 0
        assert "No tasks to kill" in result.output

    def test_kill_all_json(self, runner: CliRunner):
        """kill --all --json-output returns structured JSON."""
        _make_task("kill-all-j")

        proc = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("duo.cli.subprocess.run", return_value=proc),
            patch("duo.transport.kill_pane", return_value=True),
            patch("duo.transport.cleanup_pane_state"),
        ):
            result = runner.invoke(main, ["kill", "--all", "--json-output"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["killed_count"] == 1

    def test_kill_no_name_no_all(self, runner: CliRunner):
        """kill without name or --all shows error."""
        result = runner.invoke(main, ["kill"])
        assert result.exit_code != 0
        assert "Provide a task NAME or use --all" in result.output

    def test_kill_all_with_worktree(self, runner: CliRunner, tmp_path: Path):
        """kill --all removes worktrees that exist on disk."""
        task = _make_task("kill-all-wt")
        wt_dir = tmp_path / "fake_worktree"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        task.status = TaskStatus.RUNNING
        save_task(task)

        proc = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("duo.cli.subprocess.run", return_value=proc),
            patch("duo.transport.kill_pane", return_value=True),
            patch("duo.transport.cleanup_pane_state"),
        ):
            result = runner.invoke(main, ["kill", "--all"])
            assert result.exit_code == 0
            assert "kill-all-wt" in result.output

    def test_kill_all_transition_fallback(self, runner: CliRunner):
        """kill --all falls back to direct save when transition fails."""
        task = _make_task("kill-all-fb")
        task.status = TaskStatus.CREATED
        save_task(task)

        proc = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("duo.cli.subprocess.run", return_value=proc),
            patch("duo.transport.kill_pane", return_value=True),
            patch("duo.transport.cleanup_pane_state"),
        ):
            result = runner.invoke(main, ["kill", "--all"])
            assert result.exit_code == 0
        reloaded = load_task("kill-all-fb")
        assert reloaded.status == TaskStatus.FAILED

    def test_kill_quiet(self, runner: CliRunner, tmp_path: Path):
        """kill -q prints only the task name."""
        task = _make_task("kill-q")
        wt_dir = tmp_path / "kq_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        task.status = TaskStatus.RUNNING
        save_task(task)

        proc = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("duo.cli.subprocess.run", return_value=proc),
            patch("duo.transport.kill_pane", return_value=True),
            patch("duo.transport.cleanup_pane_state"),
        ):
            result = runner.invoke(main, ["kill", "kill-q", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "kill-q"


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
            patch("duo.cli._helpers.get_config", return_value=worktree_base),
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
            patch("duo.cli._helpers.get_config", return_value=worktree_base),
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
            patch("duo.cli._helpers.get_config", return_value=worktree_base),
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
            patch("duo.cli._helpers.get_config", return_value=worktree_base),
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
            patch("duo.cli._helpers.get_config", return_value=worktree_base),
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
            with pytest.raises(click.UsageError):
                _load_batch_file(str(f))

    def test_missing_tasks_key(self, tmp_path: Path):
        """JSON file without 'tasks' key raises UsageError."""
        f = tmp_path / "bad.json"
        f.write_text('{"items": []}')
        with pytest.raises(click.UsageError):
            _load_batch_file(str(f))

    def test_empty_tasks_list(self, tmp_path: Path):
        """JSON file with empty tasks list raises UsageError."""
        f = tmp_path / "empty.json"
        f.write_text('{"tasks": []}')
        with pytest.raises(click.UsageError):
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
        """_load_batch_file raises UsageError when the file cannot be read."""
        with pytest.raises(click.UsageError):
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
            with pytest.raises(click.UsageError):
                _load_batch_file(str(f))

    def test_tasks_not_list(self, tmp_path: Path):
        """tasks key that is not a list raises UsageError."""
        f = tmp_path / "bad.json"
        f.write_text('{"tasks": "not-a-list"}')
        with pytest.raises(click.UsageError, match="must be a list"):
            _load_batch_file(str(f))

    def test_tasks_items_not_dicts(self, tmp_path: Path):
        """tasks list containing non-dict items raises UsageError."""
        f = tmp_path / "bad.json"
        f.write_text('{"tasks": ["string-item"]}')
        with pytest.raises(click.UsageError, match="must be a JSON object"):
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
            patch("duo.config.get_config", return_value=str(tmp_path / "wt")),
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
            patch("duo.config.get_config", return_value=str(tmp_path / "wt")),
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
            patch("duo.config.get_config", return_value=str(tmp_path / "wt")),
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
            patch("duo.config.get_config", return_value=str(tmp_path / "wt")),
        ):
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="not a git repo"
            )
            with pytest.raises(click.ClickException):
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

    def test_batch_json_output(self, runner: CliRunner, tmp_path: Path):
        """batch --json-output returns structured creation result."""
        f = tmp_path / "tasks.json"
        f.write_text(json.dumps({"tasks": [{"name": "j1"}, {"name": "j2"}]}))

        with (
            patch("duo.cli._create_single_task") as mock_create,
            patch(
                "duo.scheduler.queue_status",
                return_value={
                    "active_count": 2,
                    "queued_count": 0,
                    "max_parallel": 3,
                    "active_tasks": ["j1", "j2"],
                    "queued_tasks": [],
                },
            ),
        ):
            mock_create.side_effect = ["j1", "j2"]
            result = runner.invoke(
                main, ["batch", str(f), "--repo", str(tmp_path), "--json-output"]
            )
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["created"] == 2
            assert data["tasks"] == ["j1", "j2"]
            assert data["active"] == 2

    def test_batch_json_dry_run(self, runner: CliRunner, tmp_path: Path):
        """batch --dry-run --json-output returns task preview."""
        f = tmp_path / "tasks.json"
        f.write_text(json.dumps({"tasks": [{"name": "d1", "description": "Desc 1"}]}))
        result = runner.invoke(main, ["batch", str(f), "--dry-run", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["dry_run"] is True
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["name"] == "d1"

    def test_batch_quiet_dry_run(self, runner: CliRunner, tmp_path: Path):
        """batch --dry-run -q prints only the task count."""
        f = tmp_path / "tasks.json"
        f.write_text(
            json.dumps(
                {
                    "tasks": [
                        {"name": "q1", "description": "A"},
                        {"name": "q2", "description": "B"},
                    ]
                }
            )
        )
        result = runner.invoke(main, ["batch", str(f), "--dry-run", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "2"

    def test_batch_quiet(self, runner: CliRunner, tmp_path: Path):
        """batch -q prints only the created count."""
        f = tmp_path / "tasks.json"
        f.write_text(json.dumps({"tasks": [{"name": "bq1", "description": "D"}]}))
        with (
            patch("duo.cli._create_single_task", return_value="bq1"),
            patch(
                "duo.scheduler.queue_status",
                return_value={
                    "active_count": 1,
                    "max_parallel": 3,
                    "queued_count": 0,
                    "active_tasks": ["bq1"],
                    "queued_tasks": [],
                },
            ),
        ):
            result = runner.invoke(
                main, ["batch", str(f), "--repo", str(tmp_path), "-q"]
            )
        assert result.exit_code == 0
        assert result.output.strip() == "1"


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

    def test_queue_json_output(self, runner: CliRunner):
        """queue --json-output returns JSON."""
        mock_qs = {
            "active_count": 1,
            "queued_count": 2,
            "max_parallel": 3,
            "active_tasks": ["run-a"],
            "queued_tasks": ["q-a", "q-b"],
        }
        with patch("duo.scheduler.queue_status", return_value=mock_qs):
            result = runner.invoke(main, ["queue", "--json-output"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["active_count"] == 1
            assert data["queued_count"] == 2
            assert data["queued_tasks"] == ["q-a", "q-b"]

    def test_queue_quiet(self, runner: CliRunner):
        """queue -q prints only the queue length."""
        mock_qs = {
            "active_count": 1,
            "queued_count": 2,
            "max_parallel": 3,
            "active_tasks": ["run-a"],
            "queued_tasks": ["q-a", "q-b"],
        }
        with patch("duo.scheduler.queue_status", return_value=mock_qs):
            result = runner.invoke(main, ["queue", "-q"])
            assert result.exit_code == 0
            assert result.output.strip() == "2"

    def test_queue_quiet_empty(self, runner: CliRunner):
        """queue -q with empty queue prints 0."""
        mock_qs = {
            "active_count": 0,
            "queued_count": 0,
            "max_parallel": 3,
            "active_tasks": [],
            "queued_tasks": [],
        }
        with patch("duo.scheduler.queue_status", return_value=mock_qs):
            result = runner.invoke(main, ["queue", "-q"])
            assert result.exit_code == 0
            assert result.output.strip() == "0"


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
        assert "Unknown config key" in result.output

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

    def test_config_set_invalid_bool_error(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "set", "auto_allow_all", "maybe"])
        assert result.exit_code != 0
        assert "Cannot convert" in result.output

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

    def test_config_get_json(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        """config get --json-output returns JSON."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(
            main, ["config", "get", "copilot_model", "--json-output"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "copilot_model" in data

    def test_config_get_quiet(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        """config get -q prints only the raw value."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "get", "copilot_model", "-q"])
        assert result.exit_code == 0
        val = result.output.strip()
        assert "=" not in val
        assert val  # not empty

    def test_config_list_json(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        """config list --json-output returns all config as JSON with descriptions."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "list", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "copilot_model" in data
        assert "max_corrections" in data
        entry = data["copilot_model"]
        assert "value" in entry
        assert "default" in entry
        assert "modified" in entry
        assert "description" in entry
        assert isinstance(entry["description"], str)

    def test_config_reset_unknown_key(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config reset with unknown key raises DuoUserError."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "reset", "totally_bogus_key"])
        assert result.exit_code != 0
        assert "Unknown config key" in result.output

    def test_config_set_json(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        """config set --json-output returns JSON."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(
            main, ["config", "set", "max_corrections", "5", "--json-output"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["max_corrections"] == 5

    def test_config_reset_json_single(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config reset KEY --json-output returns JSON."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(
            main, ["config", "reset", "max_corrections", "--json-output"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["reset"] == "max_corrections"

    def test_config_reset_json_all(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config reset --json-output returns JSON."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "reset", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["reset"] == "all"

    def test_config_path(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        """config path shows the config file location."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "path"])
        assert result.exit_code == 0
        assert str(fake_config) in result.output

    def test_config_edit_creates_file(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config edit creates config file if missing."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        monkeypatch.setenv("EDITOR", "true")
        result = runner.invoke(main, ["config", "edit"])
        assert result.exit_code == 0
        assert fake_config.exists()

    def test_config_edit_opens_existing(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config edit opens existing config file."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        fake_config.write_text('{"copilot_model": "test"}', encoding="utf-8")
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        monkeypatch.setenv("EDITOR", "true")
        result = runner.invoke(main, ["config", "edit"])
        assert result.exit_code == 0

    def test_config_edit_visual_fallback(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config edit falls back to $VISUAL when $EDITOR not set."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        fake_config.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        monkeypatch.delenv("EDITOR", raising=False)
        monkeypatch.setenv("VISUAL", "true")
        result = runner.invoke(main, ["config", "edit"])
        assert result.exit_code == 0

    def test_config_edit_default_editor(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config edit defaults to vi when no EDITOR or VISUAL set."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        fake_config.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        monkeypatch.delenv("EDITOR", raising=False)
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.setattr("click.edit", lambda **kw: None)
        result = runner.invoke(main, ["config", "edit"])
        assert result.exit_code == 0

    def test_config_validate_valid(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config validate reports valid when config is correct."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        fake_config.write_text('{"max_corrections": 5}', encoding="utf-8")
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "validate"])
        assert result.exit_code == 0
        assert "valid" in result.output

    def test_config_validate_no_file(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config validate with no config file reports valid (defaults used)."""
        import duo.config as config_mod

        fake_config = tmp_path / "no-such-config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "validate"])
        assert result.exit_code == 0
        assert "valid" in result.output

    def test_config_validate_invalid_json(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config validate detects invalid JSON."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        fake_config.write_text("{bad json", encoding="utf-8")
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "validate"])
        assert result.exit_code == 1
        assert "Invalid JSON" in result.output

    def test_config_validate_wrong_type(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config validate detects type mismatches."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        fake_config.write_text('{"max_corrections": "not_a_number"}', encoding="utf-8")
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "validate"])
        assert result.exit_code == 1
        assert "expected number" in result.output

    def test_config_validate_unknown_key(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config validate detects unknown keys."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        fake_config.write_text('{"mystery_key": 42}', encoding="utf-8")
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "validate"])
        assert result.exit_code == 1
        assert "Unknown key" in result.output

    def test_config_validate_json_output(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config validate --json-output returns structured result."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        fake_config.write_text('{"max_corrections": "bad"}', encoding="utf-8")
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "validate", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["valid"] is False
        assert len(data["issues"]) > 0

    def test_config_validate_cross_key_invariant(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config validate detects poll_max < poll_base."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        fake_config.write_text(
            '{"poll_base_interval": 100.0, "poll_max_interval": 10.0}',
            encoding="utf-8",
        )
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "validate"])
        assert result.exit_code == 1
        assert "poll_max_interval" in result.output


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

    def test_step_filter(self, runner: CliRunner):
        """--step N filters events to only those for step N."""
        task = _make_task("log-step")
        append_event(task, "step_started", {"step": 1})
        append_event(task, "step_started", {"step": 2})
        append_event(task, "step_completed", {"step": 1})
        result = runner.invoke(main, ["logs", "log-step", "--step", "1", "--all"])
        assert result.exit_code == 0
        assert "step_started" in result.output
        assert "step_completed" in result.output
        lines = [l for l in result.output.splitlines() if "step_started" in l]
        assert len(lines) == 1

    def test_step_filter_json(self, runner: CliRunner):
        """--step N with --json-output filters events."""
        task = _make_task("log-step-j")
        append_event(task, "ev1", {"step": 1})
        append_event(task, "ev2", {"step": 2})
        result = runner.invoke(
            main, ["logs", "log-step-j", "--step", "2", "--all", "--json-output"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["data"]["step"] == 2

    def test_count_flag(self, runner: CliRunner):
        """--count prints only the number of events."""
        task = _make_task("log-cnt")
        append_event(task, "started", {"step": 1})
        append_event(task, "completed", {"step": 1})
        append_event(task, "started", {"step": 2})
        result = runner.invoke(main, ["logs", "log-cnt", "-c"])
        assert result.exit_code == 0
        count = int(result.output.strip())
        assert count >= 3

    def test_count_with_filter(self, runner: CliRunner):
        """--count with --filter counts only matching events."""
        task = _make_task("log-cf")
        append_event(task, "started", {"step": 1})
        append_event(task, "completed", {"step": 1})
        result = runner.invoke(main, ["logs", "log-cf", "-c", "--filter", "started"])
        assert result.exit_code == 0
        assert result.output.strip() == "1"

    def test_quiet_prints_event_types(self, runner: CliRunner):
        """logs -q prints one event type per line."""
        task = _make_task("log-q")
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
        assert "git init" in result.output or "git init" in (
            result.output + str(result.exception or "")
        )

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

    def test_init_json_output(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """duo init --json-output returns JSON with status and created list."""
        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        result = runner.invoke(main, ["init", "--repo", str(repo), "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "initialized"
        assert isinstance(data["created"], list)
        assert len(data["created"]) > 0

    def test_init_json_already_initialized(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Re-running init with --json-output returns already_initialized status."""
        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / ".duo").mkdir()
        result = runner.invoke(main, ["init", "--repo", str(repo), "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "already_initialized"
        assert data["created"] == []

    def test_init_quiet_already_initialized(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / ".duo").mkdir()
        result = runner.invoke(main, ["init", "--repo", str(repo), "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "0"

    def test_init_quiet_success(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        monkeypatch.setattr(duo.protocol, "DUO_DIR", tmp_path / ".duo")
        monkeypatch.setattr(duo.cli, "DUO_DIR", tmp_path / ".duo")
        monkeypatch.setattr(duo.protocol, "TASKS_DIR", tmp_path / ".duo" / "tasks")
        monkeypatch.setattr(duo.cli, "TASKS_DIR", tmp_path / ".duo" / "tasks")
        result = runner.invoke(main, ["init", "--repo", str(repo), "-q"])
        assert result.exit_code == 0
        count = int(result.output.strip())
        assert count > 0


# ---------------------------------------------------------------------------
# doctor command
# ---------------------------------------------------------------------------


class TestDoctor:
    """Tests for the overhauled doctor command and individual check functions."""

    # ── Individual check function tests ──────────────────────────────

    def test_check_python_pass(self):
        """Python check passes on current interpreter (>= 3.12)."""
        r = _doctor_check_python()
        assert r.status == "pass"
        assert r.name == "Python"

    def test_check_python_fail(self, monkeypatch: pytest.MonkeyPatch):
        """Python check fails when version < 3.12."""
        from collections import namedtuple

        FakeVI = namedtuple(
            "version_info", ["major", "minor", "micro", "releaselevel", "serial"]
        )
        fake_vi = FakeVI(3, 11, 0, "final", 0)
        monkeypatch.setattr("duo.cli.doctor.sys.version_info", fake_vi)
        r = _doctor_check_python()
        assert r.status == "fail"
        assert "3.11" in r.message

    def test_check_tmux_pass(self, monkeypatch: pytest.MonkeyPatch):
        """tmux check passes with version >= 3.0."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: "/usr/bin/tmux" if n == "tmux" else None,
        )
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(stdout="tmux 3.4\n", returncode=0),
        )
        r = _doctor_check_tmux()
        assert r.status == "pass"
        assert "3.4" in r.message

    def test_check_tmux_warn_old_version(self, monkeypatch: pytest.MonkeyPatch):
        """tmux check warns when version < 3.0."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: "/usr/bin/tmux" if n == "tmux" else None,
        )
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(stdout="tmux 2.9\n", returncode=0),
        )
        r = _doctor_check_tmux()
        assert r.status == "warn"
        assert "2.9" in r.message

    def test_check_tmux_fail_missing(self, monkeypatch: pytest.MonkeyPatch):
        """tmux check fails when not installed."""
        monkeypatch.setattr("duo.cli.doctor.shutil.which", lambda n: None)
        r = _doctor_check_tmux()
        assert r.status == "fail"
        assert "not found" in r.message

    def test_check_tmux_timeout(self, monkeypatch: pytest.MonkeyPatch):
        """tmux check passes (graceful) on subprocess timeout."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: "/usr/bin/tmux" if n == "tmux" else None,
        )

        def _timeout(*a: object, **kw: object) -> None:
            raise subprocess.TimeoutExpired("tmux", 10)

        monkeypatch.setattr("duo.cli.doctor.subprocess.run", _timeout)
        r = _doctor_check_tmux()
        assert r.status == "pass"
        assert r.message == "installed"

    def test_check_tmux_unparseable_version(self, monkeypatch: pytest.MonkeyPatch):
        """tmux check passes (graceful) when version string cannot be parsed."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: "/usr/bin/tmux" if n == "tmux" else None,
        )
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(stdout="tmux next-server\n", returncode=0),
        )
        r = _doctor_check_tmux()
        assert r.status == "pass"
        assert r.message == "installed"

    def test_check_tmux_bridge_in_path(self, monkeypatch: pytest.MonkeyPatch):
        """tmux-bridge found in PATH."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: "/usr/bin/tmux-bridge" if n == "tmux-bridge" else None,
        )
        r = _doctor_check_tmux_bridge()
        assert r.status == "pass"

    def test_check_tmux_bridge_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """tmux-bridge found at ~/.smux/bin/tmux-bridge fallback."""
        monkeypatch.setattr("duo.cli.doctor.shutil.which", lambda n: None)
        smux_bin = tmp_path / ".smux" / "bin"
        smux_bin.mkdir(parents=True)
        bridge = smux_bin / "tmux-bridge"
        bridge.touch()
        bridge.chmod(0o755)
        monkeypatch.setattr("duo.cli.doctor.Path.home", lambda: tmp_path)
        r = _doctor_check_tmux_bridge()
        assert r.status == "pass"
        assert "found at" in r.message

    def test_check_tmux_bridge_not_executable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """tmux-bridge found but not executable."""
        monkeypatch.setattr("duo.cli.doctor.shutil.which", lambda n: None)
        smux_bin = tmp_path / ".smux" / "bin"
        smux_bin.mkdir(parents=True)
        bridge = smux_bin / "tmux-bridge"
        bridge.touch()
        bridge.chmod(0o644)
        monkeypatch.setattr("duo.cli.doctor.Path.home", lambda: tmp_path)
        monkeypatch.setattr("duo.cli.doctor.os.access", lambda p, m: False)
        r = _doctor_check_tmux_bridge()
        assert r.status == "fail"
        assert "not executable" in r.message

    def test_check_tmux_bridge_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """tmux-bridge missing everywhere."""
        monkeypatch.setattr("duo.cli.doctor.shutil.which", lambda n: None)
        monkeypatch.setattr("duo.cli.doctor.Path.home", lambda: tmp_path)
        r = _doctor_check_tmux_bridge()
        assert r.status == "fail"
        assert "not found" in r.message

    def test_check_claude_cli_pass(self, monkeypatch: pytest.MonkeyPatch):
        """claude CLI found."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: "/usr/bin/claude" if n == "claude" else None,
        )
        r = _doctor_check_claude_cli()
        assert r.status == "pass"

    def test_check_claude_cli_warn(self, monkeypatch: pytest.MonkeyPatch):
        """claude CLI not found → warn."""
        monkeypatch.setattr("duo.cli.doctor.shutil.which", lambda n: None)
        r = _doctor_check_claude_cli()
        assert r.status == "warn"

    def test_check_copilot_cli_pass_copilot(self, monkeypatch: pytest.MonkeyPatch):
        """Copilot CLI found via 'copilot'."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: "/usr/bin/copilot" if n == "copilot" else None,
        )
        r = _doctor_check_copilot_cli()
        assert r.status == "pass"

    def test_check_copilot_cli_pass_github(self, monkeypatch: pytest.MonkeyPatch):
        """Copilot CLI found via 'github-copilot-cli'."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: (
                "/usr/bin/github-copilot-cli" if n == "github-copilot-cli" else None
            ),
        )
        r = _doctor_check_copilot_cli()
        assert r.status == "pass"

    def test_check_copilot_cli_warn(self, monkeypatch: pytest.MonkeyPatch):
        """Copilot CLI not found → warn."""
        monkeypatch.setattr("duo.cli.doctor.shutil.which", lambda n: None)
        r = _doctor_check_copilot_cli()
        assert r.status == "warn"

    def test_check_duo_dir_pass(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """~/.duo directory exists, writable, with space."""
        monkeypatch.setattr(duo.cli, "DUO_DIR", tmp_path)
        monkeypatch.setattr(duo.cli.doctor, "DUO_DIR", tmp_path)
        monkeypatch.setattr("duo.cli.doctor.os.access", lambda p, m: True)
        usage = MagicMock(free=500 * 1024 * 1024)  # 500MB
        monkeypatch.setattr("duo.cli.doctor.shutil.disk_usage", lambda p: usage)
        r = _doctor_check_duo_dir()
        assert r.status == "pass"
        assert "writable" in r.message

    def test_check_duo_dir_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """~/.duo directory missing → fail."""
        monkeypatch.setattr(duo.cli, "DUO_DIR", tmp_path / "nonexistent")
        monkeypatch.setattr(duo.cli.doctor, "DUO_DIR", tmp_path / "nonexistent")
        r = _doctor_check_duo_dir()
        assert r.status == "fail"
        assert "missing" in r.message

    def test_check_duo_dir_not_writable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """~/.duo directory not writable → fail."""
        monkeypatch.setattr(duo.cli, "DUO_DIR", tmp_path)
        monkeypatch.setattr(duo.cli.doctor, "DUO_DIR", tmp_path)
        monkeypatch.setattr("duo.cli.doctor.os.access", lambda p, m: False)
        r = _doctor_check_duo_dir()
        assert r.status == "fail"
        assert "not writable" in r.message

    def test_check_duo_dir_low_space(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """~/.duo directory low disk space → warn."""
        monkeypatch.setattr(duo.cli, "DUO_DIR", tmp_path)
        monkeypatch.setattr(duo.cli.doctor, "DUO_DIR", tmp_path)
        monkeypatch.setattr("duo.cli.doctor.os.access", lambda p, m: True)
        usage = MagicMock(free=50 * 1024 * 1024)  # 50MB
        monkeypatch.setattr("duo.cli.doctor.shutil.disk_usage", lambda p: usage)
        r = _doctor_check_duo_dir()
        assert r.status == "warn"
        assert "50 MB" in r.message

    def test_check_duo_dir_disk_usage_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """~/.duo disk_usage raises OSError → pass gracefully."""
        monkeypatch.setattr(duo.cli, "DUO_DIR", tmp_path)
        monkeypatch.setattr(duo.cli.doctor, "DUO_DIR", tmp_path)
        monkeypatch.setattr("duo.cli.doctor.os.access", lambda p, m: True)

        def _raise(*a: object) -> None:
            raise OSError("disk error")

        monkeypatch.setattr("duo.cli.doctor.shutil.disk_usage", _raise)
        r = _doctor_check_duo_dir()
        assert r.status == "pass"
        assert r.message == "writable"

    def test_check_config_pass(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Valid config.json → pass."""
        import duo.config as config_mod

        monkeypatch.setattr(duo.cli, "DUO_DIR", tmp_path)
        monkeypatch.setattr(duo.cli.doctor, "DUO_DIR", tmp_path)
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.json")
        (tmp_path / "config.json").write_text('{"max_corrections": 5}')
        r = _doctor_check_config()
        assert r.status == "pass"
        assert r.message == "valid"

    def test_check_config_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Missing config.json → warn."""
        monkeypatch.setattr(duo.cli, "DUO_DIR", tmp_path)
        monkeypatch.setattr(duo.cli.doctor, "DUO_DIR", tmp_path)
        r = _doctor_check_config()
        assert r.status == "warn"
        assert "missing" in r.message

    def test_check_config_invalid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Invalid JSON → warn."""
        monkeypatch.setattr(duo.cli, "DUO_DIR", tmp_path)
        monkeypatch.setattr(duo.cli.doctor, "DUO_DIR", tmp_path)
        (tmp_path / "config.json").write_text("{{{invalid")
        r = _doctor_check_config()
        assert r.status == "warn"
        assert "invalid" in r.message.lower()

    def test_check_config_validation_issues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Config with validation issues → warn with issue count."""
        import duo.config as config_mod

        monkeypatch.setattr(duo.cli, "DUO_DIR", tmp_path)
        monkeypatch.setattr(duo.cli.doctor, "DUO_DIR", tmp_path)
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.json")
        (tmp_path / "config.json").write_text('{"unknown_key": true}')
        r = _doctor_check_config()
        assert r.status == "warn"
        assert "issue" in r.message
        assert "duo config validate" in r.fix

    def test_check_tmux_session_pass(self, monkeypatch: pytest.MonkeyPatch):
        """Active tmux session → pass."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: "/usr/bin/tmux" if n == "tmux" else None,
        )
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0),
        )
        r = _doctor_check_tmux_session()
        assert r.status == "pass"

    def test_check_tmux_session_warn_no_session(self, monkeypatch: pytest.MonkeyPatch):
        """No active tmux session → warn."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: "/usr/bin/tmux" if n == "tmux" else None,
        )
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=1),
        )
        r = _doctor_check_tmux_session()
        assert r.status == "warn"
        assert "no active" in r.message

    def test_check_tmux_session_warn_not_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """tmux not installed → warn for session check."""
        monkeypatch.setattr("duo.cli.doctor.shutil.which", lambda n: None)
        r = _doctor_check_tmux_session()
        assert r.status == "warn"
        assert "not installed" in r.message

    def test_check_tmux_session_timeout(self, monkeypatch: pytest.MonkeyPatch):
        """tmux list-sessions times out → warn."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: "/usr/bin/tmux" if n == "tmux" else None,
        )

        def _timeout(*a: object, **kw: object) -> None:
            raise subprocess.TimeoutExpired("tmux", 10)

        monkeypatch.setattr("duo.cli.doctor.subprocess.run", _timeout)
        r = _doctor_check_tmux_session()
        assert r.status == "warn"

    def test_check_task_timeout_pass(self, monkeypatch: pytest.MonkeyPatch):
        """Valid task_timeout → pass."""
        monkeypatch.setattr("duo.cli.doctor.get_config", lambda k: 300)
        r = _doctor_check_task_timeout()
        assert r.status == "pass"
        assert "300s" in r.message

    def test_check_task_timeout_pass_disabled(self, monkeypatch: pytest.MonkeyPatch):
        """task_timeout = 0 → pass (disabled)."""
        monkeypatch.setattr("duo.cli.doctor.get_config", lambda k: 0)
        r = _doctor_check_task_timeout()
        assert r.status == "pass"
        assert "disabled" in r.message

    def test_check_task_timeout_warn_invalid(self, monkeypatch: pytest.MonkeyPatch):
        """Invalid task_timeout → warn."""
        monkeypatch.setattr("duo.cli.doctor.get_config", lambda k: -1)
        r = _doctor_check_task_timeout()
        assert r.status == "warn"
        assert "invalid" in r.message

    def test_check_task_timeout_warn_none(self, monkeypatch: pytest.MonkeyPatch):
        """task_timeout is None → warn."""
        monkeypatch.setattr("duo.cli.doctor.get_config", lambda k: None)
        r = _doctor_check_task_timeout()
        assert r.status == "warn"

    def test_check_corrupted_pass(self, monkeypatch: pytest.MonkeyPatch):
        """No corrupted tasks → pass."""
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])
        r = _doctor_check_corrupted()
        assert r.status == "pass"
        assert r.message == "0"

    def test_check_corrupted_warn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Corrupted tasks present → warn."""
        monkeypatch.setattr(
            "duo.protocol.list_corrupted", lambda: [tmp_path / "a", tmp_path / "b"]
        )
        r = _doctor_check_corrupted()
        assert r.status == "warn"
        assert r.message == "2"

    def test_check_git_pass(self, monkeypatch: pytest.MonkeyPatch):
        """git found with version → pass."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: "/usr/bin/git" if n == "git" else None,
        )
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(stdout="git version 2.44.0\n", returncode=0),
        )
        r = _doctor_check_git()
        assert r.status == "pass"
        assert "2.44.0" in r.message

    def test_check_git_warn_missing(self, monkeypatch: pytest.MonkeyPatch):
        """git not found → warn."""
        monkeypatch.setattr("duo.cli.doctor.shutil.which", lambda n: None)
        r = _doctor_check_git()
        assert r.status == "warn"
        assert "not found" in r.message

    def test_check_git_timeout(self, monkeypatch: pytest.MonkeyPatch):
        """git --version times out → pass gracefully."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: "/usr/bin/git" if n == "git" else None,
        )

        def _timeout(*a: object, **kw: object) -> None:
            raise subprocess.TimeoutExpired("git", 10)

        monkeypatch.setattr("duo.cli.doctor.subprocess.run", _timeout)
        r = _doctor_check_git()
        assert r.status == "pass"
        assert r.message == "installed"

    # ── Integration tests: doctor command ──────────────────────────────

    def _setup_all_pass(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Configure monkeypatches for all checks to pass."""
        monkeypatch.setattr("duo.cli.doctor.DUO_DIR", tmp_path)
        config_path = tmp_path / "config.json"
        config_path.write_text('{"copilot_model": "claude-opus-4.6"}\n')

        def fake_which(name: str) -> str | None:
            return f"/usr/bin/{name}"

        monkeypatch.setattr("duo.cli.doctor.shutil.which", fake_which)
        monkeypatch.setattr("duo.cli.doctor.os.access", lambda p, m: True)
        usage = MagicMock(free=5 * 1024 * 1024 * 1024)
        monkeypatch.setattr("duo.cli.doctor.shutil.disk_usage", lambda p: usage)
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(
                returncode=0,
                stdout="tmux 3.4\n"
                if a and a[0] and a[0][0] == "tmux"
                else "git version 2.44.0\n",
            ),
        )
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])

    def test_doctor_all_pass(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """All checks pass when everything is available."""
        self._setup_all_pass(monkeypatch, tmp_path)
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0
        assert "PASS" in result.output
        assert "13/13 checks passed" in result.output

    def test_doctor_missing_tmux(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Missing tmux shows fix suggestion and exits 1."""
        self._setup_all_pass(monkeypatch, tmp_path)

        def fake_which(name: str) -> str | None:
            if name == "tmux":
                return None
            return f"/usr/bin/{name}"

        monkeypatch.setattr("duo.cli.doctor.shutil.which", fake_which)
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code != 0
        assert "brew install tmux" in result.output

    def test_doctor_summary_count(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Doctor output ends with X/Y checks passed."""
        self._setup_all_pass(monkeypatch, tmp_path)
        result = runner.invoke(main, ["doctor"])
        assert "/13 checks passed" in result.output

    def test_doctor_tmux_bridge_fallback_path(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """tmux-bridge found via ~/.smux/bin/tmux-bridge when not in PATH."""
        self._setup_all_pass(monkeypatch, tmp_path)

        def fake_which(name: str) -> str | None:
            if name == "tmux-bridge":
                return None
            return f"/usr/bin/{name}"

        monkeypatch.setattr("duo.cli.doctor.shutil.which", fake_which)
        smux_bin = tmp_path / "fakehome" / ".smux" / "bin"
        smux_bin.mkdir(parents=True)
        bridge = smux_bin / "tmux-bridge"
        bridge.touch()
        bridge.chmod(0o755)
        monkeypatch.setattr("duo.cli.doctor.Path.home", lambda: tmp_path / "fakehome")
        monkeypatch.setattr("duo.cli.doctor.os.access", lambda p, m: True)
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0
        assert "tmux-bridge" in result.output
        assert "not found" not in result.output

    def test_doctor_invalid_config_json(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Invalid JSON in config.json shows warn."""
        self._setup_all_pass(monkeypatch, tmp_path)
        (tmp_path / "config.json").write_text("not valid json {{{")
        result = runner.invoke(main, ["doctor"])
        assert "invalid" in result.output.lower()

    def test_doctor_json_output(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """--json-output produces valid JSON with checks and summary."""
        self._setup_all_pass(monkeypatch, tmp_path)
        result = runner.invoke(main, ["doctor", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "checks" in data
        assert "summary" in data
        assert len(data["checks"]) == 13
        assert data["summary"]["total"] == 13
        for check in data["checks"]:
            assert "name" in check
            assert "status" in check
            assert "message" in check
            assert "fix" in check

    def test_doctor_strict_with_warnings(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """--strict exits non-zero when warnings exist."""
        self._setup_all_pass(monkeypatch, tmp_path)

        def fake_which(name: str) -> str | None:
            if name == "claude":
                return None
            return f"/usr/bin/{name}"

        monkeypatch.setattr("duo.cli.doctor.shutil.which", fake_which)
        result = runner.invoke(main, ["doctor", "--strict"])
        assert result.exit_code != 0

    def test_doctor_strict_all_pass(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """--strict exits 0 when all checks pass."""
        self._setup_all_pass(monkeypatch, tmp_path)
        result = runner.invoke(main, ["doctor", "--strict"])
        assert result.exit_code == 0

    def test_doctor_exit_0_with_warnings_no_strict(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Warnings without --strict → exit 0."""
        self._setup_all_pass(monkeypatch, tmp_path)

        def fake_which(name: str) -> str | None:
            if name == "claude":
                return None
            return f"/usr/bin/{name}"

        monkeypatch.setattr("duo.cli.doctor.shutil.which", fake_which)
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0
        assert "warning" in result.output.lower()

    def test_doctor_json_with_failure(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """JSON output with a failure includes fail count and exits non-zero."""
        self._setup_all_pass(monkeypatch, tmp_path)

        def fake_which(name: str) -> str | None:
            if name == "tmux":
                return None
            return f"/usr/bin/{name}"

        monkeypatch.setattr("duo.cli.doctor.shutil.which", fake_which)
        result = runner.invoke(main, ["doctor", "--json-output"])
        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["summary"]["fail"] > 0

    def test_doctor_header_present(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Default output includes header."""
        self._setup_all_pass(monkeypatch, tmp_path)
        result = runner.invoke(main, ["doctor"])
        assert "Duo Environment Diagnostics" in result.output

    def test_check_result_dataclass(self):
        """CheckResult dataclass fields are accessible."""
        cr = CheckResult(name="test", status="pass", message="ok", fix="")
        assert cr.name == "test"
        assert cr.status == "pass"
        assert cr.message == "ok"
        assert cr.fix == ""

    def test_doctor_quiet_all_pass(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """--quiet with all pass prints nothing."""
        self._setup_all_pass(monkeypatch, tmp_path)
        result = runner.invoke(main, ["doctor", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_doctor_quiet_with_failure(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """--quiet with failures shows only failure lines."""
        self._setup_all_pass(monkeypatch, tmp_path)

        def fake_which(name: str) -> str | None:
            if name == "tmux":
                return None
            return f"/usr/bin/{name}"

        monkeypatch.setattr("duo.cli.doctor.shutil.which", fake_which)
        result = runner.invoke(main, ["doctor", "-q"])
        assert result.exit_code != 0
        assert "tmux" in result.output.lower()


# ---------------------------------------------------------------------------
# resume command
# ---------------------------------------------------------------------------


class TestResume:
    def test_resume_specific_task(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Resume a named task calls restart or start session + replays prompt."""
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
        mock_send = MagicMock()
        monkeypatch.setattr("duo.commander.send_task_prompt", mock_send)
        result = runner.invoke(main, ["resume", "resume-me"])
        assert result.exit_code == 0
        assert "Resumed task 'resume-me'" in result.output
        mock_start.assert_called_once()
        mock_send.assert_called_once()

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

    def test_resume_quiet_completed(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """resume -q on completed task prints 0."""
        task = _make_task("rq-done")
        task.status = TaskStatus.COMPLETED
        save_task(task)
        result = runner.invoke(main, ["resume", "rq-done", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "0"

    def test_resume_quiet_no_tasks(self, runner: CliRunner):
        """resume -q with no interrupted tasks prints 0."""
        result = runner.invoke(main, ["resume", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "0"

    def test_resume_quiet_success(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """resume -q prints resumed count."""
        task = _make_task("rq-ok")
        task.status = TaskStatus.RUNNING
        save_task(task)
        monkeypatch.setattr("duo.transport.is_process_alive", lambda label: False)
        monkeypatch.setattr("duo.commander.start_session", MagicMock())
        monkeypatch.setattr("duo.commander.send_task_prompt", MagicMock())
        result = runner.invoke(main, ["resume", "rq-ok", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "1"

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
        mock_send = MagicMock()
        monkeypatch.setattr("duo.commander.send_task_prompt", mock_send)
        result = runner.invoke(main, ["resume"])
        assert result.exit_code == 0
        assert "task-a" in result.output
        assert "task-b" in result.output
        assert "task-c" not in result.output
        assert mock_start.call_count == 2
        assert mock_send.call_count == 2

    def test_resume_skips_queued_tasks(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Bare resume skips QUEUED tasks (they're intentionally deferred)."""
        t1 = _make_task("active-one", "Active")
        t1.status = TaskStatus.RUNNING
        save_task(t1)

        t2 = _make_task("queued-one", "Queued")
        t2.status = TaskStatus.QUEUED
        save_task(t2)

        monkeypatch.setattr("duo.transport.is_process_alive", lambda label: False)
        mock_start = MagicMock()
        monkeypatch.setattr("duo.commander.start_session", mock_start)
        mock_send = MagicMock()
        monkeypatch.setattr("duo.commander.send_task_prompt", mock_send)
        result = runner.invoke(main, ["resume"])
        assert result.exit_code == 0
        assert "active-one" in result.output
        assert "queued-one" not in result.output
        assert mock_start.call_count == 1

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
        """When pane is alive, old pane is killed before restart_session."""
        task = _make_task("alive-task")
        task.status = TaskStatus.RUNNING
        save_task(task)

        monkeypatch.setattr("duo.transport.is_process_alive", lambda label: True)
        mock_restart = MagicMock()
        monkeypatch.setattr("duo.commander.restart_session", mock_restart)
        mock_start = MagicMock()
        monkeypatch.setattr("duo.commander.start_session", mock_start)
        mock_send = MagicMock()
        monkeypatch.setattr("duo.commander.send_task_prompt", mock_send)
        mock_cleanup = MagicMock()
        monkeypatch.setattr("duo.transport.cleanup_pane_state", mock_cleanup)
        mock_kill = MagicMock(return_value=True)
        monkeypatch.setattr("duo.transport.kill_pane", mock_kill)
        result = runner.invoke(main, ["resume", "alive-task"])
        assert result.exit_code == 0
        assert "restarted session" in result.output
        mock_restart.assert_called_once()
        mock_send.assert_called_once()
        mock_kill.assert_called_once_with(task.pane_label)
        mock_cleanup.assert_called_once_with(task.pane_label)

    def test_resume_alive_kill_pane_error(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """When kill_pane returns False, cleanup is skipped but restart proceeds."""
        task = _make_task("kill-fail")
        task.status = TaskStatus.RUNNING
        save_task(task)

        monkeypatch.setattr("duo.transport.is_process_alive", lambda label: True)
        mock_restart = MagicMock()
        monkeypatch.setattr("duo.commander.restart_session", mock_restart)
        mock_send = MagicMock()
        monkeypatch.setattr("duo.commander.send_task_prompt", mock_send)
        monkeypatch.setattr("duo.transport.kill_pane", MagicMock(return_value=False))
        mock_cleanup = MagicMock()
        monkeypatch.setattr("duo.transport.cleanup_pane_state", mock_cleanup)
        result = runner.invoke(main, ["resume", "kill-fail"])
        assert result.exit_code == 0
        assert "restarted session" in result.output
        mock_restart.assert_called_once()
        mock_cleanup.assert_not_called()

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
        mock_send = MagicMock()
        monkeypatch.setattr("duo.commander.send_task_prompt", mock_send)
        result = runner.invoke(main, ["resume", "error-task"])
        assert result.exit_code == 0
        assert "started new session" in result.output
        mock_start.assert_called_once()
        mock_restart.assert_not_called()
        mock_send.assert_called_once()

    def test_resume_replays_persisted_prompt(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Resume uses persisted prompt file if available."""
        task = _make_task("resume-persisted")
        task.status = TaskStatus.RUNNING
        save_task(task)
        # Persist a prompt
        prompt_path = task.prompt_path(task.current_step, task.current_attempt)
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text("saved user prompt")

        monkeypatch.setattr("duo.transport.is_process_alive", lambda label: False)
        mock_start = MagicMock()
        monkeypatch.setattr("duo.commander.start_session", mock_start)
        mock_send = MagicMock()
        monkeypatch.setattr("duo.commander.send_task_prompt", mock_send)
        result = runner.invoke(main, ["resume", "resume-persisted"])
        assert result.exit_code == 0
        # Should have sent the persisted prompt
        sent_prompt = mock_send.call_args[0][1]
        assert sent_prompt == "saved user prompt"

    def test_resume_prompt_replay_failure_nonfatal(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """If prompt replay fails, resume still succeeds with a warning."""
        task = _make_task("resume-fail-prompt")
        task.status = TaskStatus.RUNNING
        save_task(task)

        monkeypatch.setattr("duo.transport.is_process_alive", lambda label: False)
        mock_start = MagicMock()
        monkeypatch.setattr("duo.commander.start_session", mock_start)
        monkeypatch.setattr(
            "duo.commander.send_task_prompt",
            MagicMock(side_effect=RuntimeError("dialog timeout")),
        )
        result = runner.invoke(main, ["resume", "resume-fail-prompt"])
        assert result.exit_code == 0
        assert "could not replay prompt" in result.output

    def test_resume_restart_session_error_continues(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """If restart_session raises, resume logs error and continues."""
        task = _make_task("restart-err")
        task.status = TaskStatus.RUNNING
        save_task(task)

        monkeypatch.setattr("duo.transport.is_process_alive", lambda label: True)
        monkeypatch.setattr(
            "duo.commander.restart_session",
            MagicMock(side_effect=RuntimeError("pane creation failed")),
        )
        monkeypatch.setattr("duo.transport.cleanup_pane_state", MagicMock())
        monkeypatch.setattr(
            "duo.cli.subprocess.run", MagicMock(return_value=MagicMock(returncode=0))
        )
        result = runner.invoke(main, ["resume", "restart-err"])
        assert result.exit_code == 0
        assert "Failed to resume" in result.output

    def test_resume_start_session_error_continues(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """If start_session raises, resume logs error and continues."""
        task = _make_task("start-err")
        task.status = TaskStatus.RUNNING
        save_task(task)

        monkeypatch.setattr("duo.transport.is_process_alive", lambda label: False)
        monkeypatch.setattr(
            "duo.commander.start_session",
            MagicMock(side_effect=OSError("tmux not running")),
        )
        result = runner.invoke(main, ["resume", "start-err"])
        assert result.exit_code == 0
        assert "Failed to resume" in result.output

    def test_resume_completed_json(self, runner: CliRunner):
        """resume --json-output on completed task returns already_complete."""
        task = _make_task("resume-done-json")
        task.status = TaskStatus.COMPLETED
        save_task(task)
        result = runner.invoke(main, ["resume", "resume-done-json", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["already_complete"] == ["resume-done-json"]

    def test_resume_no_tasks_json(self, runner: CliRunner):
        """resume --json-output with no interrupted tasks returns empty."""
        result = runner.invoke(main, ["resume", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["resumed"] == []
        assert "message" in data

    def test_resume_json_output(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """resume --json-output returns structured result."""
        task = _make_task("resume-json")
        task.status = TaskStatus.RUNNING
        save_task(task)

        monkeypatch.setattr("duo.transport.is_process_alive", lambda label: False)
        monkeypatch.setattr("duo.commander.start_session", MagicMock())
        monkeypatch.setattr("duo.commander.send_task_prompt", MagicMock())
        result = runner.invoke(main, ["resume", "resume-json", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["resumed"]) == 1
        assert data["resumed"][0]["task"] == "resume-json"
        assert data["resumed"][0]["resumed"] is True

    def test_resume_restart_error_json(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """resume --json-output captures restart errors in JSON."""
        task = _make_task("resume-rerr-json")
        task.status = TaskStatus.RUNNING
        save_task(task)

        monkeypatch.setattr("duo.transport.is_process_alive", lambda label: True)
        monkeypatch.setattr("duo.cli.subprocess.run", MagicMock(returncode=0))
        monkeypatch.setattr(
            "duo.commander.restart_session",
            MagicMock(side_effect=OSError("restart failed")),
        )
        result = runner.invoke(main, ["resume", "resume-rerr-json", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["resumed"][0]["resumed"] is False
        assert "restart failed" in data["resumed"][0]["error"]

    def test_resume_start_error_json(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """resume --json-output captures start errors in JSON."""
        task = _make_task("resume-serr-json")
        task.status = TaskStatus.RUNNING
        save_task(task)

        monkeypatch.setattr("duo.transport.is_process_alive", lambda label: False)
        monkeypatch.setattr(
            "duo.commander.start_session",
            MagicMock(side_effect=OSError("start failed")),
        )
        result = runner.invoke(main, ["resume", "resume-serr-json", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["resumed"][0]["resumed"] is False
        assert "start failed" in data["resumed"][0]["error"]

    def test_resume_dead_pane_normalize_failure_text(self, runner: CliRunner):
        """resume reports error when normalize_for_restart fails (text mode)."""
        task = _make_task("resume-norm-fail")
        task.status = TaskStatus.RUNNING
        save_task(task)

        with (
            patch("duo.transport.is_process_alive", return_value=False),
            patch("duo.commander.normalize_for_restart", return_value=False),
        ):
            result = runner.invoke(main, ["resume", "resume-norm-fail"])
            assert result.exit_code == 0
            assert "Cannot normalize" in result.output

    def test_resume_dead_pane_normalize_failure_json(self, runner: CliRunner):
        """resume --json-output reports normalize failure."""
        task = _make_task("resume-norm-j")
        task.status = TaskStatus.RUNNING
        save_task(task)

        with (
            patch("duo.transport.is_process_alive", return_value=False),
            patch("duo.commander.normalize_for_restart", return_value=False),
        ):
            result = runner.invoke(main, ["resume", "resume-norm-j", "--json-output"])
            data = json.loads(result.output)
            assert data["resumed"][0]["resumed"] is False
            assert "Cannot normalize" in data["resumed"][0]["error"]

    def test_resume_dead_pane_normalizes_active_states(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """resume with dead pane normalizes active states to FAILED before start."""
        active_states = [
            TaskStatus.PROMPT_SENT,
            TaskStatus.ACKED,
            TaskStatus.RUNNING,
            TaskStatus.RESULT_REPORTED,
            TaskStatus.VERIFYING,
            TaskStatus.CORRECTING,
        ]
        for state in active_states:
            tid = f"resume-{state.value}"
            task = _make_task(tid)
            task.status = state
            save_task(task)

            monkeypatch.setattr("duo.transport.is_process_alive", lambda label: False)
            mock_start = MagicMock()
            monkeypatch.setattr("duo.commander.start_session", mock_start)
            monkeypatch.setattr("duo.commander.send_task_prompt", MagicMock())
            result = runner.invoke(main, ["resume", tid])
            assert result.exit_code == 0, f"resume failed for {state.value}"
            assert "Resumed" in result.output
            mock_start.assert_called_once()


# ---------------------------------------------------------------------------
# Batch validation edge cases
# ---------------------------------------------------------------------------


class TestBatchValidation:
    """Additional edge cases for batch file validation."""

    def test_batch_tasks_null(self, tmp_path: Path):
        """_load_batch_file raises UsageError when 'tasks' value is null."""
        f = tmp_path / "bad.json"
        f.write_text(json.dumps({"tasks": None}))
        with pytest.raises(click.UsageError):
            _load_batch_file(str(f))

    def test_batch_missing_name_in_task_def(self, runner: CliRunner, tmp_path: Path):
        """batch task missing 'name' key prints error and skips that task."""
        f = tmp_path / "noname.json"
        f.write_text(json.dumps({"tasks": [{"description": "no name field"}]}))
        result = runner.invoke(main, ["batch", str(f), "--repo", str(tmp_path)])
        assert "(unnamed)" in result.output
        assert "missing 'name'" in result.output

    def test_batch_nonexistent_file(self, runner: CliRunner):
        """batch with a path that doesn't exist fails."""
        result = runner.invoke(main, ["batch", "/no/such/file.json"])
        assert result.exit_code != 0

    def test_batch_target_files_not_list_rejected(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """target_files as string instead of list is rejected."""
        f = tmp_path / "bad_tf.json"
        f.write_text(
            json.dumps({"tasks": [{"name": "t1", "target_files": "not-a-list"}]})
        )
        result = runner.invoke(main, ["batch", str(f), "--repo", str(tmp_path)])
        assert "'target_files' must be a list" in result.output

    def test_batch_writable_paths_not_list_rejected(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """writable_paths as dict instead of list is rejected."""
        f = tmp_path / "bad_wp.json"
        f.write_text(
            json.dumps({"tasks": [{"name": "t1", "writable_paths": {"a": 1}}]})
        )
        result = runner.invoke(main, ["batch", str(f), "--repo", str(tmp_path)])
        assert "'writable_paths' must be a list" in result.output


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

    def test_merge_dry_run_json(self, runner: CliRunner):
        """merge --dry-run --json-output returns structured preview."""
        task = _make_task("dry-merge-json")
        task.status = TaskStatus.COMPLETED
        save_task(task)

        result = runner.invoke(
            main, ["merge", "dry-merge-json", "--dry-run", "--json-output"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["dry_run"] is True
        assert data["target"] == "main"
        assert "branch" in data
        assert "commits" in data
        assert "files_changed" in data

    def test_merge_dry_run_with_worktree(self, runner: CliRunner, tmp_path: Path):
        """merge --dry-run shows commit count and files when worktree exists."""
        task = _make_task("dry-wt")
        task.status = TaskStatus.COMPLETED
        wt_dir = tmp_path / "wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        save_task(task)

        with patch("duo.cli.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(
                    returncode=0, stdout="abc1234 first\ndef5678 second\n", stderr=""
                ),
                MagicMock(returncode=0, stdout="file1.py\nfile2.py\n", stderr=""),
            ]
            result = runner.invoke(main, ["merge", "dry-wt", "--dry-run"])
            assert result.exit_code == 0
            assert "Commits: 2" in result.output
            assert "Files changed: 2" in result.output
            assert "file1.py" in result.output

    def test_merge_dry_run_many_files(self, runner: CliRunner, tmp_path: Path):
        """merge --dry-run truncates file list at 10."""
        task = _make_task("dry-many")
        task.status = TaskStatus.COMPLETED
        wt_dir = tmp_path / "wt2"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        save_task(task)

        files = "\n".join(f"file{i}.py" for i in range(15))
        with patch("duo.cli.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="abc commit\n", stderr=""),
                MagicMock(returncode=0, stdout=files, stderr=""),
            ]
            result = runner.invoke(main, ["merge", "dry-many", "--dry-run"])
            assert result.exit_code == 0
            assert "... and 5 more" in result.output

    def test_merge_dry_run_json_with_commits(self, runner: CliRunner, tmp_path: Path):
        """merge --dry-run --json-output includes commit count."""
        task = _make_task("dry-jc")
        task.status = TaskStatus.COMPLETED
        wt_dir = tmp_path / "wt3"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        save_task(task)

        with patch("duo.cli.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="abc first\n", stderr=""),
                MagicMock(returncode=0, stdout="app.py\n", stderr=""),
            ]
            result = runner.invoke(
                main, ["merge", "dry-jc", "--dry-run", "--json-output"]
            )
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["commits"] == 1
            assert data["files_changed"] == ["app.py"]

    def test_merge_dry_run_git_fails(self, runner: CliRunner, tmp_path: Path):
        """merge --dry-run handles git errors gracefully."""
        task = _make_task("dry-fail")
        task.status = TaskStatus.COMPLETED
        wt_dir = tmp_path / "wt4"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        save_task(task)

        with patch("duo.cli.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=1, stdout="", stderr="no upstream"),
                MagicMock(returncode=1, stdout="", stderr="no upstream"),
            ]
            result = runner.invoke(main, ["merge", "dry-fail", "--dry-run"])
            assert result.exit_code == 0
            assert "Would merge" in result.output

    def test_merge_dry_run_quiet(self, runner: CliRunner):
        """merge --dry-run -q prints only the changed file count."""
        task = _make_task("dry-q")
        task.status = TaskStatus.COMPLETED
        save_task(task)

        result = runner.invoke(main, ["merge", "dry-q", "--dry-run", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "0"

    def test_merge_quiet(self, runner: CliRunner, tmp_path: Path):
        """merge -q prints only the branch name."""
        task = _make_task("merge-q")
        task.status = TaskStatus.COMPLETED
        wt_dir = tmp_path / "mq_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        save_task(task)

        with patch("duo.cli.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=f"worktree /main\n  branch refs/heads/main\n\nworktree {wt_dir}\n  branch refs/heads/{task.branch}\n",
                stderr="",
            )
            result = runner.invoke(main, ["merge", "merge-q", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == task.branch

    def test_merge_json_output(self, runner: CliRunner, tmp_path: Path):
        """merge --json-output returns structured result."""
        task = _make_task("merge-json")
        task.status = TaskStatus.COMPLETED
        wt_dir = tmp_path / "merge_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        save_task(task)

        with patch("duo.cli.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=f"worktree /main\n  branch refs/heads/main\n\nworktree {wt_dir}\n  branch refs/heads/{task.branch}\n",
                stderr="",
            )
            result = runner.invoke(main, ["merge", "merge-json", "--json-output"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["merged"] is True
            assert data["branch"] == task.branch
            assert "worktree_removed" in data
            assert "branch_deleted" in data


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
        assert "total_steps" in data[0]
        assert "attempt" in data[0]
        assert "worktree" in data[0]
        assert "branch" in data[0]
        assert "incarnation_id" in data[0]
        assert "created_at" in data[0]
        assert "session_started_at" in data[0]
        assert "age" in data[0]
        assert data[0]["description"] == "A JSON task"

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
        assert "total_steps" in data
        assert "attempt" in data
        assert "worktree" in data
        assert "branch" in data
        assert "incarnation_id" in data
        assert "created_at" in data
        assert "session_started_at" in data
        assert "age" in data

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

    def test_list_wide_shows_description(self, runner: CliRunner):
        """list --wide includes DESCRIPTION column."""
        _make_task("wide-task", "Fix the login bug")
        result = runner.invoke(main, ["list", "--wide"])
        assert result.exit_code == 0
        assert "DESCRIPTION" in result.output
        assert "Fix the login bug" in result.output

    def test_list_wide_truncates_long_desc(self, runner: CliRunner):
        """list --wide truncates descriptions longer than 30 chars."""
        _make_task("long-desc", "A" * 50)
        result = runner.invoke(main, ["list", "--wide"])
        assert result.exit_code == 0
        assert "..." in result.output

    def test_status_without_json_flag(self, runner: CliRunner):
        """status without --json-output returns normal format."""
        _make_task("normal-task", "Normal task")
        result = runner.invoke(main, ["status", "normal-task"])
        assert result.exit_code == 0
        assert "normal-task" in result.output
        assert "Status:" in result.output
        assert "Age:" in result.output

    def test_status_no_created_at(self, runner: CliRunner):
        """status handles task without created_at."""
        t = _make_task("no-date-task")
        t.created_at = ""
        save_task(t)
        result = runner.invoke(main, ["status", "no-date-task"])
        assert result.exit_code == 0
        assert "Age:" not in result.output


class TestStartFlags:
    def test_start_with_queue(self, runner: CliRunner, tmp_path: Path):
        """start --queue creates task in QUEUED state without starting a session."""
        with (
            patch("duo.cli.lifecycle_cmd._create_worktree") as mock_wt,
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

    def test_start_queue_transition_failure(self, runner: CliRunner, tmp_path: Path):
        """start --queue with failed transition warns the user."""
        with (
            patch("duo.cli.lifecycle_cmd._create_worktree") as mock_wt,
            patch("duo.protocol.transition", return_value=False),
        ):
            mock_wt.return_value = (str(tmp_path / "wt" / "qtf"), "abc123")
            result = runner.invoke(
                main, ["start", "qtf", "--queue", "--repo", str(tmp_path)]
            )
            assert result.exit_code == 0
            out = result.output + (result.stderr or "")
            assert "could not transition" in out.lower() or "warning" in out.lower()

    def test_start_queue_transition_failure_json(
        self, runner: CliRunner, tmp_path: Path
    ):
        """start --queue --json-output with failed transition returns error."""
        with (
            patch("duo.cli.lifecycle_cmd._create_worktree") as mock_wt,
            patch("duo.protocol.transition", return_value=False),
        ):
            mock_wt.return_value = (str(tmp_path / "wt" / "qtfj"), "abc123")
            result = runner.invoke(
                main,
                ["start", "qtfj", "--queue", "--json-output", "--repo", str(tmp_path)],
            )
            assert result.exit_code == 0
            # Find the JSON line in output
            for line in result.output.splitlines():
                line = line.strip()
                if line.startswith("{"):
                    data = json.loads(line)
                    assert data["status"] == "error"
                    break
            else:
                pytest.fail("No JSON output found")

    def test_start_with_model(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """start --model sets DUO_COPILOT_MODEL env var."""
        with (
            patch("duo.cli.lifecycle_cmd._create_worktree") as mock_wt,
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

    def test_start_from_thinking(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """start --from-thinking reads plan.md from thinking session."""
        fake_thinking = tmp_path / "thinking"
        tdir = fake_thinking / "my-app"
        tdir.mkdir(parents=True)
        (tdir / "plan.md").write_text(
            "# Plan: my-app\n\nBuild the app.", encoding="utf-8"
        )

        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        with (
            patch("duo.cli.lifecycle_cmd._create_worktree") as mock_wt,
            patch("duo.commander.start_session"),
            patch("duo.scheduler.enqueue_or_start", return_value="started"),
        ):
            mock_wt.return_value = (str(tmp_path / "wt" / "my-app"), "abc123")
            result = runner.invoke(
                main,
                ["start", "my-app", "--from-thinking", "--repo", str(tmp_path)],
            )
            assert result.exit_code == 0
            assert "Plan: loaded from thinking session" in result.output

        task = load_task("my-app")
        assert task is not None
        assert "Plan: my-app" in task.description

    def test_start_from_thinking_no_plan(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """start --from-thinking fails if plan.md doesn't exist."""
        fake_thinking = tmp_path / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        with patch("duo.thinking.thinking_dir", return_value=fake_thinking / "no-plan"):
            result = runner.invoke(
                main,
                ["start", "no-plan", "--from-thinking", "--repo", str(tmp_path)],
            )
        assert result.exit_code != 0
        assert "No plan.md" in result.output

    def test_start_from_thinking_empty_plan(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """start --from-thinking fails if plan.md is empty."""
        fake_thinking = tmp_path / "thinking"
        tdir = fake_thinking / "empty-plan"
        tdir.mkdir(parents=True)
        (tdir / "plan.md").write_text("", encoding="utf-8")

        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        result = runner.invoke(
            main,
            ["start", "empty-plan", "--from-thinking", "--repo", str(tmp_path)],
        )
        assert result.exit_code != 0
        assert "empty" in result.output.lower()

    def test_start_queue_json_output(self, runner: CliRunner, tmp_path: Path):
        """start --queue --json-output returns structured JSON."""
        with (
            patch("duo.cli.lifecycle_cmd._create_worktree") as mock_wt,
            patch("duo.commander.start_session"),
        ):
            mock_wt.return_value = (str(tmp_path / "wt" / "q-json"), "abc123")
            result = runner.invoke(
                main,
                [
                    "start",
                    "q-json",
                    "--queue",
                    "--json-output",
                    "--repo",
                    str(tmp_path),
                ],
            )
            assert result.exit_code == 0
            data = json.loads(result.output.strip().split("\n")[-1])
            assert data["created"] is True
            assert data["task"] == "q-json"
            assert data["status"] == "queued"
            assert "worktree" in data

    def test_start_json_output(self, runner: CliRunner, tmp_path: Path):
        """start --json-output returns structured JSON without human-readable preamble."""
        with (
            patch("duo.cli.lifecycle_cmd._create_worktree") as mock_wt,
            patch("duo.commander.start_session"),
        ):
            mock_wt.return_value = (str(tmp_path / "wt" / "s-json"), "abc123")
            result = runner.invoke(
                main,
                ["start", "s-json", "--json-output", "--repo", str(tmp_path)],
            )
            assert result.exit_code == 0
            # Output should be pure JSON — no human text before it
            data = json.loads(result.output.strip())
            assert data["created"] is True
            assert data["task"] == "s-json"
            assert data["status"] == "deferred"
            assert "pane_label" in data
            # Verify no human-readable preamble leaked
            assert "Created task:" not in result.output

    def test_start_auto_queued_json_output(self, runner: CliRunner, tmp_path: Path):
        """start --json-output when auto-queued (slots full) returns queued status."""
        with (
            patch("duo.cli.lifecycle_cmd._create_worktree") as mock_wt,
            patch("duo.scheduler.enqueue_or_start", return_value="queued"),
            patch(
                "duo.scheduler.queue_status",
                return_value={
                    "active_count": 3,
                    "queued_count": 1,
                    "max_parallel": 3,
                    "active_tasks": ["a", "b", "c"],
                    "queued_tasks": ["aq-json"],
                },
            ),
        ):
            mock_wt.return_value = (str(tmp_path / "wt" / "aq-json"), "abc123")
            result = runner.invoke(
                main,
                ["start", "aq-json", "--json-output", "--repo", str(tmp_path)],
            )
            assert result.exit_code == 0
            data = json.loads(result.output.strip().split("\n")[-1])
            assert data["created"] is True
            assert data["task"] == "aq-json"
            assert data["status"] == "queued"
            assert "queue_position" in data

    def test_start_reuse_pane(self, runner: CliRunner, tmp_path: Path):
        """start --reuse-pane passes pane ID to start_session."""
        with (
            patch("duo.cli.lifecycle_cmd._create_worktree") as mock_wt,
            patch("duo.commander.start_session") as mock_start,
            patch("duo.scheduler.enqueue_or_start", return_value="start"),
        ):
            mock_wt.return_value = (str(tmp_path / "wt" / "rp-task"), "abc123")
            result = runner.invoke(
                main,
                [
                    "start",
                    "rp-task",
                    "--reuse-pane",
                    "%55",
                    "--repo",
                    str(tmp_path),
                ],
            )
            assert result.exit_code == 0
            mock_start.assert_called_once()
            _, kwargs = mock_start.call_args
            assert kwargs["reuse_pane"] == "%55"

    def test_start_quiet(self, runner: CliRunner, tmp_path: Path):
        """start -q prints only the task name."""
        with (
            patch("duo.cli.lifecycle_cmd._create_worktree") as mock_wt,
            patch("duo.commander.start_session"),
            patch("duo.scheduler.enqueue_or_start", return_value="start"),
        ):
            mock_wt.return_value = (str(tmp_path / "wt" / "q-task"), "abc123")
            result = runner.invoke(
                main,
                ["start", "q-task", "--repo", str(tmp_path), "-q"],
            )
            assert result.exit_code == 0
            assert result.output.strip() == "q-task"

    def test_start_quiet_queued(self, runner: CliRunner, tmp_path: Path):
        """start -q --queue prints only the task name."""
        with patch("duo.cli.lifecycle_cmd._create_worktree") as mock_wt:
            mock_wt.return_value = (str(tmp_path / "wt" / "qq-task"), "abc123")
            result = runner.invoke(
                main,
                ["start", "qq-task", "--repo", str(tmp_path), "--queue", "-q"],
            )
            assert result.exit_code == 0
            assert result.output.strip() == "qq-task"

    def test_start_quiet_queue_transition_fail(self, runner: CliRunner, tmp_path: Path):
        """start -q --queue prints name even when transition fails."""
        with (
            patch("duo.cli.lifecycle_cmd._create_worktree") as mock_wt,
            patch("duo.protocol.transition", return_value=False),
        ):
            mock_wt.return_value = (str(tmp_path / "wt" / "qqf-task"), "abc123")
            result = runner.invoke(
                main,
                ["start", "qqf-task", "--repo", str(tmp_path), "--queue", "-q"],
            )
            assert result.exit_code == 0
            assert result.output.strip() == "qqf-task"

    def test_start_quiet_auto_queued(self, runner: CliRunner, tmp_path: Path):
        """start -q prints name when auto-queued by scheduler."""
        with (
            patch("duo.cli.lifecycle_cmd._create_worktree") as mock_wt,
            patch("duo.scheduler.enqueue_or_start", return_value="queued"),
            patch(
                "duo.scheduler.queue_status",
                return_value={"queued_count": 1, "active_count": 2, "max_parallel": 2},
            ),
        ):
            mock_wt.return_value = (str(tmp_path / "wt" / "aq-task"), "abc123")
            result = runner.invoke(
                main,
                ["start", "aq-task", "--repo", str(tmp_path), "-q"],
            )
            assert result.exit_code == 0
            assert result.output.strip() == "aq-task"


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
            patch("duo.config.get_config", return_value=str(tmp_path / "wt")),
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
            patch("duo.config.get_config", return_value=str(tmp_path / "wt")),
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
            patch("duo.config.get_config", return_value=str(tmp_path / "wt")),
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

    def test_queue_transition_failure_warns(self, tmp_path: Path):
        """_create_single_task(queue_only=True) warns when transition fails."""
        from duo.cli import _create_single_task

        defn = {"name": "cq-trans-fail", "description": "Fail trans"}
        with (
            patch("duo.cli.subprocess.run") as mock_run,
            patch("duo.config.get_config", return_value=str(tmp_path / "wt")),
            patch("duo.protocol.transition", return_value=False),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            result = _create_single_task(defn, str(tmp_path), queue_only=True)
            assert result == "cq-trans-fail"


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
        changed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="src/a.py\nsrc/b.py\n", stderr=""
        )
        untracked = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="new.txt\n", stderr=""
        )
        diff = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="diff --git a/src/a.py\n+hello\n", stderr=""
        )

        def fake_run_git(args, cwd, *, check=True):
            if args[:2] == ["diff", "--name-only"]:
                return changed
            if args[0] == "ls-files":
                return untracked
            return diff

        with (
            patch("duo.cli.inspect_cmd._run_git", side_effect=fake_run_git),
            patch("os.path.isdir", return_value=True),
        ):
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
        changed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="x.py\n", stderr=""
        )
        untracked = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        diff = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="diff content", stderr=""
        )

        def fake_run_git(args, cwd, *, check=True):
            if args[:2] == ["diff", "--name-only"]:
                return changed
            if args[0] == "ls-files":
                return untracked
            return diff

        with (
            patch("duo.cli.inspect_cmd._run_git", side_effect=fake_run_git),
            patch("os.path.isdir", return_value=True),
        ):
            result = runner.invoke(
                main, ["inspect", "incl-json", "--json-output", "--include-files"]
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["changed_files"] == ["x.py"]
        assert data["untracked_files"] == []
        assert "diff content" in data["diff_preview"]

    def test_inspect_json_include_files_truncates_diff(self, runner: CliRunner):
        """inspect --json-output --include-files truncates diff > 500 chars."""
        _make_task("incl-trunc")
        big_diff = "x" * 600
        changed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="a.py\n", stderr=""
        )
        untracked = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        diff = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=big_diff, stderr=""
        )

        def fake_run_git(args, cwd, *, check=True):
            if args[:2] == ["diff", "--name-only"]:
                return changed
            if args[0] == "ls-files":
                return untracked
            return diff

        with (
            patch("duo.cli.inspect_cmd._run_git", side_effect=fake_run_git),
            patch("os.path.isdir", return_value=True),
        ):
            result = runner.invoke(
                main, ["inspect", "incl-trunc", "--json-output", "--include-files"]
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["diff_preview"].endswith("... (truncated)")
        assert len(data["diff_preview"].split("\n... (truncated)")[0]) == 500

    def test_inspect_json_include_files_no_worktree(self, runner: CliRunner):
        """inspect --json-output --include-files sets files_error when worktree missing."""
        _make_task("incl-nodir-json")
        result = runner.invoke(
            main, ["inspect", "incl-nodir-json", "--json-output", "--include-files"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "files_error" in data
        assert "Worktree not found" in data["files_error"]

    def test_inspect_text_include_files_truncates_diff(self, runner: CliRunner):
        """inspect --include-files (text) truncates diff > 500 chars."""
        _make_task("incl-trunc-text")
        big_diff = "y" * 600
        changed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="b.py\n", stderr=""
        )
        untracked = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        diff = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=big_diff, stderr=""
        )

        def fake_run_git(args, cwd, *, check=True):
            if args[:2] == ["diff", "--name-only"]:
                return changed
            if args[0] == "ls-files":
                return untracked
            return diff

        with (
            patch("duo.cli.inspect_cmd._run_git", side_effect=fake_run_git),
            patch("os.path.isdir", return_value=True),
        ):
            result = runner.invoke(
                main, ["inspect", "incl-trunc-text", "--include-files"]
            )
        assert result.exit_code == 0
        assert "... (truncated)" in result.output


# ---------------------------------------------------------------------------
# inspect — edge cases
# ---------------------------------------------------------------------------


class TestInspectEdgeCases:
    def test_inspect_task_no_steps(self, runner: CliRunner):
        """inspect a task whose subtask list is empty — no Current Step section."""
        task = _make_task("no-steps")
        # Clear subtasks after creation to simulate an edge case
        task.subtasks = []
        save_task(task)
        # current_step=1 but len(subtasks)==0, so the guard should skip
        assert task.current_step == 1
        result = runner.invoke(main, ["inspect", "no-steps"])
        assert result.exit_code == 0
        assert "Task: no-steps" in result.output
        assert "Current Step" not in result.output

    def test_inspect_json_output_structure(self, runner: CliRunner):
        """Validate all expected top-level keys in JSON output."""
        _make_task("json-struct")
        result = runner.invoke(main, ["inspect", "json-struct", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        expected_keys = {
            "id",
            "description",
            "status",
            "step",
            "total_steps",
            "attempt",
            "incarnation_id",
            "worktree",
            "branch",
            "base_commit",
            "created_at",
            "session_started_at",
            "age",
            "subtasks",
        }
        assert expected_keys.issubset(data.keys())
        assert isinstance(data["subtasks"], list)
        assert isinstance(data["step"], int)
        assert isinstance(data["attempt"], int)

    def test_inspect_with_heartbeat_missing_fields(self, runner: CliRunner):
        """Heartbeat JSON with missing fields still displays with defaults."""
        from duo.protocol import write_json

        task = _make_task("hb-missing")
        # Write heartbeat with only partial fields
        write_json(task.heartbeat_path, {"ts": "2025-01-01T10:00:00"})

        result = runner.invoke(main, ["inspect", "hb-missing"])
        assert result.exit_code == 0
        assert "Heartbeat:" in result.output
        assert "2025-01-01T10:00:00" in result.output

    def test_inspect_step_with_ack_no_result(self, runner: CliRunner):
        """Step has ack but no result — only Ack section shown."""
        from duo.protocol import write_json

        task = _make_task("ack-only")
        task.step_dir(1).mkdir(parents=True, exist_ok=True)
        write_json(
            task.ack_path(1, 1),
            {
                "step": 1,
                "attempt": 1,
                "incarnation": "inc1",
                "prompt_hash": "abc",
                "acked_at": "2025-01-01T12:00:00",
            },
        )

        result = runner.invoke(main, ["inspect", "ack-only"])
        assert result.exit_code == 0
        assert "Ack:" in result.output
        assert "abc" in result.output
        assert "Result:" not in result.output

    def test_inspect_step_with_result_no_ack(self, runner: CliRunner):
        """Step has result but no ack (unusual state) — only Result section shown."""
        from duo.protocol import write_json

        task = _make_task("res-only")
        task.step_dir(1).mkdir(parents=True, exist_ok=True)
        write_json(
            task.result_path(1, 1),
            {
                "step": 1,
                "attempt": 1,
                "incarnation": "inc2",
                "status": "done",
                "files_changed": ["x.py"],
                "summary": "Finished",
                "reason": "",
            },
        )

        result = runner.invoke(main, ["inspect", "res-only"])
        assert result.exit_code == 0
        assert "Ack:" not in result.output
        assert "Result:" in result.output
        assert "Finished" in result.output

    def test_inspect_multiple_attempts(self, runner: CliRunner):
        """Task on attempt 2 — inspect reads ack/result for that attempt."""
        from duo.protocol import write_json

        task = _make_task("multi-att")
        task.current_attempt = 2
        save_task(task)
        task.step_dir(1).mkdir(parents=True, exist_ok=True)
        # Write ack for attempt 1 (should NOT show) and attempt 2 (should show)
        write_json(
            task.ack_path(1, 1),
            {
                "step": 1,
                "attempt": 1,
                "incarnation": "old",
                "prompt_hash": "old-hash",
                "acked_at": "2025-01-01T11:00:00",
            },
        )
        write_json(
            task.ack_path(1, 2),
            {
                "step": 1,
                "attempt": 2,
                "incarnation": "new",
                "prompt_hash": "new-hash",
                "acked_at": "2025-01-01T12:00:00",
            },
        )

        result = runner.invoke(main, ["inspect", "multi-att"])
        assert result.exit_code == 0
        assert "new-hash" in result.output
        assert "old-hash" not in result.output


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


class TestFmtAge:
    """Tests for the _fmt_age elapsed-time formatting helper."""

    def test_seconds(self):
        from datetime import datetime, timedelta

        from duo.cli import _fmt_age

        ts = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
        assert _fmt_age(ts) == "30s"

    def test_future_timestamp(self):
        from datetime import datetime, timedelta

        from duo.cli import _fmt_age

        ts = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        assert _fmt_age(ts) == "0s"

    def test_naive_timestamp(self):
        from datetime import datetime, timedelta

        from duo.cli import _fmt_age

        naive = datetime.now(UTC) - timedelta(minutes=10)
        ts = naive.strftime("%Y-%m-%dT%H:%M:%S")
        assert _fmt_age(ts) == "10m"

    def test_minutes(self):
        from datetime import datetime, timedelta

        from duo.cli import _fmt_age

        ts = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        assert _fmt_age(ts) == "5m"

    def test_hours(self):
        from datetime import datetime, timedelta

        from duo.cli import _fmt_age

        ts = (datetime.now(UTC) - timedelta(hours=2, minutes=30)).isoformat()
        assert _fmt_age(ts) == "2h 30m"

    def test_days(self):
        from datetime import datetime, timedelta

        from duo.cli import _fmt_age

        ts = (datetime.now(UTC) - timedelta(days=3, hours=5)).isoformat()
        assert _fmt_age(ts) == "3d 5h"

    def test_invalid_input(self):
        from duo.cli import _fmt_age

        assert _fmt_age("not-a-timestamp") == "?"

    def test_none_input(self):
        from duo.cli import _fmt_age

        assert _fmt_age(None) == "?"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# batch: invalid task name in batch file
# ---------------------------------------------------------------------------


class TestBatchInvalidName:
    """Batch rejects task definitions with invalid names."""

    def test_batch_invalid_name_traversal(self, runner: CliRunner, tmp_path: Path):
        """Batch file with path-traversal task name is rejected."""
        batch_file = tmp_path / "bad-names.json"
        batch_file.write_text(
            json.dumps({"tasks": [{"name": "../evil", "description": "bad"}]})
        )
        result = runner.invoke(
            main, ["batch", str(batch_file), "--repo", str(tmp_path)]
        )
        assert "invalid task name" in result.output.lower() or result.exit_code != 0


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


# TestBatchCorruptedJson — deleted: duplicate of TestBatchEdgeCases.test_batch_invalid_json_file


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


# TestBatchFileNotFound — deleted: duplicate of TestBatchValidation.test_batch_nonexistent_file


class TestRunGitNotInstalled:
    def test_run_git_not_installed(self, runner: CliRunner, tmp_path: Path):
        """When git is not found, a friendly error is shown."""
        (tmp_path / ".git").mkdir()
        with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
            result = runner.invoke(main, ["start", "sometask", "--repo", str(tmp_path)])
        assert result.exit_code != 0
        out = result.output + (result.stderr or "")
        assert "git is not installed" in out

    def test_run_git_timeout(self, runner: CliRunner, tmp_path: Path):
        """When git times out, a timeout error is shown."""
        (tmp_path / ".git").mkdir()
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

    def test_retry_blocked(self, runner: CliRunner):
        """retry a BLOCKED task transitions to SESSION_STARTING."""
        task = _make_task("retry-blocked")
        task.status = TaskStatus.BLOCKED
        save_task(task)
        result = runner.invoke(main, ["retry", "retry-blocked"])
        assert result.exit_code == 0
        assert "retry" in result.output.lower()
        reloaded = load_task("retry-blocked")
        assert reloaded.status == TaskStatus.SESSION_STARTING

    def test_retry_escalated(self, runner: CliRunner):
        """retry an ESCALATED task transitions to PROMPT_SENT."""
        task = _make_task("retry-esc")
        task.status = TaskStatus.ESCALATED
        save_task(task)
        result = runner.invoke(main, ["retry", "retry-esc"])
        assert result.exit_code == 0
        assert "retry" in result.output.lower()
        reloaded = load_task("retry-esc")
        assert reloaded.status == TaskStatus.PROMPT_SENT

    def test_retry_not_found(self, runner: CliRunner):
        """retry a nonexistent task shows not-found error."""
        result = runner.invoke(main, ["retry", "nonexistent"])
        assert result.exit_code != 0
        out = result.output + (result.stderr or "")
        assert "not found" in out.lower()

    def test_retry_json_output(self, runner: CliRunner):
        """retry --json-output returns structured JSON."""
        task = _make_task("retry-json")
        task.status = TaskStatus.FAILED
        save_task(task)

        result = runner.invoke(main, ["retry", "retry-json", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["retried"] is True
        assert data["previous_status"] == "failed"
        assert data["new_status"] == "session_starting"
        assert "step" in data

    def test_retry_escalated_json_output(self, runner: CliRunner):
        """retry --json-output on escalated task shows prompt_sent target."""
        task = _make_task("retry-esc-json")
        task.status = TaskStatus.ESCALATED
        save_task(task)

        result = runner.invoke(main, ["retry", "retry-esc-json", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["retried"] is True
        assert data["previous_status"] == "escalated"
        assert data["new_status"] == "prompt_sent"

    def test_retry_invalid_name(self, runner: CliRunner):
        """retry with a path-traversal name is rejected."""
        result = runner.invoke(main, ["retry", "../bad"])
        assert result.exit_code != 0

    def test_retry_illegal_transition_fails(self, runner: CliRunner):
        """retry where transition unexpectedly fails — exit code != 0."""
        task = _make_task("retry-trans-fail")
        task.status = TaskStatus.FAILED
        save_task(task)
        with patch("duo.protocol.transition", return_value=False):
            result = runner.invoke(main, ["retry", "retry-trans-fail"])
        assert result.exit_code != 0

    def test_retry_illegal_transition_json(self, runner: CliRunner):
        """retry --json-output where transition fails returns retried=False."""
        task = _make_task("retry-tj-fail")
        task.status = TaskStatus.BLOCKED
        save_task(task)
        with patch("duo.protocol.transition", return_value=False):
            result = runner.invoke(main, ["retry", "retry-tj-fail", "--json-output"])
        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["retried"] is False
        assert "error" in data

    def test_retry_quiet(self, runner: CliRunner):
        """retry -q prints only the new status value."""
        task = _make_task("retry-q")
        task.status = TaskStatus.FAILED
        save_task(task)
        result = runner.invoke(main, ["retry", "retry-q", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "session_starting"


class TestNotFoundParametrized:
    """All task-based commands return 'not found' for nonexistent tasks."""

    def test_all_commands_task_not_found(self, runner: CliRunner):
        for args in [
            ["status", "nonexistent"],
            ["logs", "nonexistent"],
            ["inspect", "nonexistent"],
            ["audit", "nonexistent"],
            ["kill", "nonexistent"],
            ["stop", "nonexistent"],
            ["merge", "nonexistent"],
            ["diff", "nonexistent"],
            ["retry", "nonexistent"],
            ["resume", "nonexistent"],
            ["send", "nonexistent", "hello"],
        ]:
            result = runner.invoke(main, args)
            assert result.exit_code != 0, f"{args} should fail"
            assert "not found" in result.output.lower(), f"{args} missing 'not found'"

    def test_not_found_suggests_similar(self, runner: CliRunner):
        """When task not found, suggest similar task names."""
        _make_task("my-feature")
        result = runner.invoke(main, ["status", "feature"])
        assert result.exit_code != 0
        assert "Did you mean" in result.output
        assert "my-feature" in result.output

    def test_not_found_no_suggestions_when_no_tasks(self, runner: CliRunner):
        """When no tasks exist, show generic message."""
        result = runner.invoke(main, ["status", "anything"])
        assert result.exit_code != 0
        assert "duo list" in result.output

    def test_not_found_no_similar_tasks(self, runner: CliRunner):
        """When tasks exist but none match, show generic message."""
        _make_task("alpha")
        result = runner.invoke(main, ["status", "zzz-unrelated"])
        assert result.exit_code != 0
        assert "duo list" in result.output
        assert "Did you mean" not in result.output


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

        monkeypatch.setattr("duo.cli.doctor.shutil.which", fake_which)
        monkeypatch.setattr("duo.cli.doctor.os.access", lambda p, m: True)
        usage = MagicMock(free=5 * 1024 * 1024 * 1024)
        monkeypatch.setattr("duo.cli.doctor.shutil.disk_usage", lambda p: usage)
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="tmux 3.4\n"),
        )
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])
        result = runner.invoke(main, ["doctor"])
        assert "task_timeout" in result.output

    def test_doctor_invalid_task_timeout(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """doctor reports invalid task_timeout value."""
        monkeypatch.setattr("duo.cli.doctor.shutil.which", lambda _: "/usr/bin/fake")
        monkeypatch.setattr("duo.cli.doctor.os.access", lambda p, m: True)
        usage = MagicMock(free=5 * 1024 * 1024 * 1024)
        monkeypatch.setattr("duo.cli.doctor.shutil.disk_usage", lambda p: usage)
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="tmux 3.4\n"),
        )
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])
        monkeypatch.setattr(
            "duo.cli.doctor.get_config", lambda k: -1 if k == "task_timeout" else 0
        )
        result = runner.invoke(main, ["doctor"])
        assert "task_timeout" in result.output
        assert "invalid" in result.output.lower()


class TestDoctorStaleLocks:
    """Tests for _doctor_check_stale_locks()."""

    def test_no_stale_locks(self):
        """No lock files → pass."""
        from duo.cli.doctor import _doctor_check_stale_locks

        result = _doctor_check_stale_locks()
        assert result.status == "pass"

    def test_stale_locks_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Lock files present → warn."""
        from duo.cli.doctor import _doctor_check_stale_locks

        monkeypatch.setattr("duo.cli.doctor.TASKS_DIR", tmp_path)
        (tmp_path / ".my-task.lock").touch()
        (tmp_path / ".other.lock").touch()

        result = _doctor_check_stale_locks()
        assert result.status == "warn"
        assert "2 found" in result.message


class TestDoctorOrphanWorktrees:
    """Tests for _doctor_check_orphan_worktrees()."""

    def test_no_orphans(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """No orphan worktrees → pass."""
        from duo.cli.doctor import _doctor_check_orphan_worktrees

        monkeypatch.setattr("duo.cli.doctor.TASKS_DIR", tmp_path)
        (tmp_path / "my-task").mkdir()
        porcelain = "worktree /repo\n\nworktree /repo/duo-my-task\nbranch refs/heads/duo/my-task\n"
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout=porcelain),
        )
        result = _doctor_check_orphan_worktrees()
        assert result.status == "pass"

    def test_orphan_found(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """Worktree exists but task dir does not → warn."""
        from duo.cli.doctor import _doctor_check_orphan_worktrees

        monkeypatch.setattr("duo.cli.doctor.TASKS_DIR", tmp_path)
        porcelain = "worktree /repo\n\nworktree /repo/duo-ghost-task\nbranch refs/heads/duo/ghost-task\n"
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout=porcelain),
        )
        result = _doctor_check_orphan_worktrees()
        assert result.status == "warn"
        assert "duo-ghost-task" in result.message

    def test_git_unavailable(self, monkeypatch: pytest.MonkeyPatch):
        """Git failure → skip gracefully."""
        from duo.cli.doctor import _doctor_check_orphan_worktrees

        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("no git")),
        )
        result = _doctor_check_orphan_worktrees()
        assert result.status == "pass"
        assert "skipped" in result.message

    def test_not_git_repo(self, monkeypatch: pytest.MonkeyPatch):
        """git worktree list fails (not a repo) → skip."""
        from duo.cli.doctor import _doctor_check_orphan_worktrees

        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=128, stdout=""),
        )
        result = _doctor_check_orphan_worktrees()
        assert result.status == "pass"
        assert "skipped" in result.message


class TestDoctorAutoFix:
    """Tests for _doctor_auto_fix() and doctor --fix."""

    def test_fix_removes_stale_locks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """--fix removes stale .lock files (only if older than 1 hour)."""
        import os

        from duo.cli.doctor import _doctor_auto_fix

        monkeypatch.setattr("duo.cli.doctor.TASKS_DIR", tmp_path)
        lock_file = tmp_path / ".my-task.lock"
        lock_file.touch()
        os.utime(lock_file, (0, 0))
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=128, stdout=""),
        )
        fixed = _doctor_auto_fix()
        assert any("stale lock" in f for f in fixed)
        assert not lock_file.exists()

    def test_fix_lock_oserror_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Lock files that raise OSError on stat are silently skipped."""
        from duo.cli.doctor import _doctor_auto_fix

        monkeypatch.setattr("duo.cli.doctor.TASKS_DIR", tmp_path)
        lock_file = tmp_path / ".bad.lock"
        lock_file.touch()
        # Remove the file so stat fails, but glob still finds it via race
        lock_file.unlink()
        # Re-create as a broken symlink so glob finds it but stat fails
        lock_file.symlink_to(tmp_path / "nonexistent-target")
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=128, stdout=""),
        )
        fixed = _doctor_auto_fix()
        assert not any("stale lock" in f for f in fixed)

    def test_fix_fresh_lock_not_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Lock files less than 1 hour old are NOT removed."""
        from duo.cli.doctor import _doctor_auto_fix

        monkeypatch.setattr("duo.cli.doctor.TASKS_DIR", tmp_path)
        lock_file = tmp_path / ".fresh.lock"
        lock_file.touch()  # mtime = now (fresh)
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=128, stdout=""),
        )
        fixed = _doctor_auto_fix()
        assert not any("stale lock" in f for f in fixed)
        assert lock_file.exists()

    def test_fix_purges_quarantined(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """--fix purges quarantined tasks."""
        from duo.cli.doctor import _doctor_auto_fix

        monkeypatch.setattr("duo.cli.doctor.TASKS_DIR", tmp_path)
        qdir = tmp_path / "bad-task"
        qdir.mkdir()
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [qdir])
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=128, stdout=""),
        )
        fixed = _doctor_auto_fix()
        assert any("quarantined" in f for f in fixed)

    def test_fix_nothing_to_fix(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """--fix with nothing broken returns empty list."""
        from duo.cli.doctor import _doctor_auto_fix

        monkeypatch.setattr("duo.cli.doctor.TASKS_DIR", tmp_path)
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=128, stdout=""),
        )
        fixed = _doctor_auto_fix()
        assert fixed == []

    def test_fix_orphan_worktree(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """--fix removes orphan duo-* worktrees."""
        from duo.cli.doctor import _doctor_auto_fix

        monkeypatch.setattr("duo.cli.doctor.TASKS_DIR", tmp_path)
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])
        porcelain = "worktree /repo\n\nworktree /repo/duo-orphan\nbranch refs/heads/duo/orphan\n"
        calls: list[list[str]] = []

        def fake_run(*a: object, **kw: object) -> MagicMock:
            cmd = a[0] if a else kw.get("args", [])
            calls.append(cmd)
            if cmd and "list" in cmd:
                return MagicMock(returncode=0, stdout=porcelain)
            return MagicMock(returncode=0, stdout="")

        monkeypatch.setattr("duo.cli.doctor.subprocess.run", fake_run)
        fixed = _doctor_auto_fix()
        assert any("orphan worktree" in f for f in fixed)

    def test_fix_git_error_skips_orphan_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """--fix handles git errors gracefully during orphan scan."""
        from duo.cli.doctor import _doctor_auto_fix

        monkeypatch.setattr("duo.cli.doctor.TASKS_DIR", tmp_path)
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])

        def raise_os_error(*a: object, **kw: object) -> None:
            raise OSError("no git")

        monkeypatch.setattr("duo.cli.doctor.subprocess.run", raise_os_error)
        fixed = _doctor_auto_fix()
        assert not any("orphan" in f for f in fixed)

    def test_doctor_fix_flag_output(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """doctor --fix shows fixed items in output."""
        monkeypatch.setattr("duo.cli.doctor.TASKS_DIR", tmp_path)
        lock_file = tmp_path / ".stale.lock"
        lock_file.touch()
        import os

        os.utime(lock_file, (0, 0))
        monkeypatch.setattr("duo.cli.doctor.shutil.which", lambda _: "/usr/bin/fake")
        monkeypatch.setattr("duo.cli.doctor.os.access", lambda p, m: True)
        usage = MagicMock(free=5 * 1024 * 1024 * 1024)
        monkeypatch.setattr("duo.cli.doctor.shutil.disk_usage", lambda p: usage)
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="tmux 3.4\n"),
        )
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])
        result = runner.invoke(main, ["doctor", "--fix"])
        assert "Auto-fixed" in result.output
        assert "stale lock" in result.output

    def test_doctor_fix_json(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """doctor --fix --json-output includes fixed list."""
        monkeypatch.setattr("duo.cli.doctor.TASKS_DIR", tmp_path)
        lock_file = tmp_path / ".old.lock"
        lock_file.touch()
        import os

        os.utime(lock_file, (0, 0))
        monkeypatch.setattr("duo.cli.doctor.shutil.which", lambda _: "/usr/bin/fake")
        monkeypatch.setattr("duo.cli.doctor.os.access", lambda p, m: True)
        usage = MagicMock(free=5 * 1024 * 1024 * 1024)
        monkeypatch.setattr("duo.cli.doctor.shutil.disk_usage", lambda p: usage)
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="tmux 3.4\n"),
        )
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])
        result = runner.invoke(main, ["doctor", "--fix", "--json-output"])
        data = json.loads(result.output)
        assert "fixed" in data
        assert len(data["fixed"]) >= 1


# ── Copilot health check (doctor) ────────────────────────────────────


class TestGetPidFdCount:
    """Tests for _get_pid_fd_count()."""

    def test_counts_lines(self, monkeypatch: pytest.MonkeyPatch):
        """Should return line count minus header."""
        header = "COMMAND PID FD TYPE"
        lines = "\n".join([header] + [f"line{i}" for i in range(10)])
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout=lines),
        )
        assert _get_pid_fd_count(1234) == 10

    def test_lsof_fails(self, monkeypatch: pytest.MonkeyPatch):
        """lsof returns non-zero → -1."""
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=1, stdout=""),
        )
        assert _get_pid_fd_count(1234) == -1

    def test_lsof_timeout(self, monkeypatch: pytest.MonkeyPatch):
        """Timeout → -1."""
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: (_ for _ in ()).throw(
                subprocess.TimeoutExpired("lsof", 10)
            ),
        )
        assert _get_pid_fd_count(1234) == -1

    def test_lsof_oserror(self, monkeypatch: pytest.MonkeyPatch):
        """OSError → -1."""
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("no lsof")),
        )
        assert _get_pid_fd_count(1234) == -1

    def test_empty_output(self, monkeypatch: pytest.MonkeyPatch):
        """Empty output → 0."""
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout=""),
        )
        assert _get_pid_fd_count(1234) == 0


class TestGetPidKqueueCount:
    """Tests for _get_pid_kqueue_count()."""

    def test_counts_kqueue_lines(self, monkeypatch: pytest.MonkeyPatch):
        """Should count lines containing KQUEUE."""
        output = "HEADER\nnode 123 KQUEUE\nnode 124 FD\nnode 125 KQUEUE\n"
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout=output),
        )
        assert _get_pid_kqueue_count(1234) == 2

    def test_no_kqueues(self, monkeypatch: pytest.MonkeyPatch):
        """No KQUEUE lines → 0."""
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="HEADER\nfd\nfd\n"),
        )
        assert _get_pid_kqueue_count(1234) == 0

    def test_lsof_fails(self, monkeypatch: pytest.MonkeyPatch):
        """lsof non-zero → -1."""
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=1, stdout=""),
        )
        assert _get_pid_kqueue_count(1234) == -1

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch):
        """Timeout → -1."""
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: (_ for _ in ()).throw(
                subprocess.TimeoutExpired("lsof", 10)
            ),
        )
        assert _get_pid_kqueue_count(1234) == -1


class TestGetPidChildCount:
    """Tests for _get_pid_child_count()."""

    def test_counts_children(self, monkeypatch: pytest.MonkeyPatch):
        """Should count non-empty output lines."""
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="111\n222\n333\n"),
        )
        assert _get_pid_child_count(1234) == 3

    def test_no_children(self, monkeypatch: pytest.MonkeyPatch):
        """pgrep returns non-zero → 0."""
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=1, stdout=""),
        )
        assert _get_pid_child_count(1234) == 0

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch):
        """Timeout → -1."""
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: (_ for _ in ()).throw(
                subprocess.TimeoutExpired("pgrep", 5)
            ),
        )
        assert _get_pid_child_count(1234) == -1

    def test_oserror(self, monkeypatch: pytest.MonkeyPatch):
        """OSError → -1."""
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("no pgrep")),
        )
        assert _get_pid_child_count(1234) == -1

    def test_blank_lines_ignored(self, monkeypatch: pytest.MonkeyPatch):
        """Blank lines in pgrep output should be ignored."""
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="111\n\n222\n\n"),
        )
        assert _get_pid_child_count(1234) == 2


class TestDoctorCheckCopilotHealth:
    """Tests for _doctor_check_copilot_health()."""

    def test_no_active_tasks(self, monkeypatch: pytest.MonkeyPatch):
        """No active tasks → empty list."""
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [])
        assert _doctor_check_copilot_health() == []

    def test_list_tasks_exception(self, monkeypatch: pytest.MonkeyPatch):
        """list_tasks raises → empty list."""
        monkeypatch.setattr(
            "duo.cli.doctor.list_tasks",
            lambda: (_ for _ in ()).throw(OSError("fs error")),
        )
        assert _doctor_check_copilot_health() == []

    def test_terminal_tasks_skipped(self, monkeypatch: pytest.MonkeyPatch):
        """Completed/failed tasks should be skipped."""
        tasks = [
            MagicMock(status=TaskStatus.COMPLETED, pane_label="done-pane"),
            MagicMock(status=TaskStatus.FAILED, pane_label="fail-pane"),
            MagicMock(status=TaskStatus.ESCALATED, pane_label="esc-pane"),
        ]
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: tasks)
        assert _doctor_check_copilot_health() == []

    def test_pid_unavailable(self, monkeypatch: pytest.MonkeyPatch):
        """PID not found → warn."""
        task = MagicMock(status=TaskStatus.RUNNING, pane_label="my-pane")
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda label: None)
        results = _doctor_check_copilot_health()
        assert len(results) == 1
        assert results[0].status == "warn"
        assert "PID unavailable" in results[0].message

    def test_healthy_pane(self, monkeypatch: pytest.MonkeyPatch):
        """Low fd/kqueue/child counts → pass."""
        task = MagicMock(status=TaskStatus.RUNNING, pane_label="healthy-pane")
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda label: 9999)
        monkeypatch.setattr("duo.cli.doctor._get_pid_fd_count", lambda pid: 50)
        monkeypatch.setattr("duo.cli.doctor._get_pid_kqueue_count", lambda pid: 5)
        monkeypatch.setattr("duo.cli.doctor._get_pid_child_count", lambda pid: 2)
        results = _doctor_check_copilot_health()
        assert len(results) == 1
        assert results[0].status == "pass"
        assert "fds=50" in results[0].message

    def test_critical_on_very_high_fds(self, monkeypatch: pytest.MonkeyPatch):
        """fds >= 2000 → fail."""
        task = MagicMock(status=TaskStatus.RUNNING, pane_label="crit-pane")
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda label: 9999)
        monkeypatch.setattr("duo.cli.doctor._get_pid_fd_count", lambda pid: 3000)
        monkeypatch.setattr("duo.cli.doctor._get_pid_kqueue_count", lambda pid: 5)
        monkeypatch.setattr("duo.cli.doctor._get_pid_child_count", lambda pid: 2)
        results = _doctor_check_copilot_health()
        assert results[0].status == "fail"
        assert "restart" in results[0].fix.lower()

    def test_warn_on_high_kqueue(self, monkeypatch: pytest.MonkeyPatch):
        """kqueue >= 50 → warn."""
        task = MagicMock(status=TaskStatus.RUNNING, pane_label="kq-pane")
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda label: 9999)
        monkeypatch.setattr("duo.cli.doctor._get_pid_fd_count", lambda pid: 100)
        monkeypatch.setattr("duo.cli.doctor._get_pid_kqueue_count", lambda pid: 60)
        monkeypatch.setattr("duo.cli.doctor._get_pid_child_count", lambda pid: 2)
        results = _doctor_check_copilot_health()
        assert results[0].status == "warn"

    def test_warn_on_high_children(self, monkeypatch: pytest.MonkeyPatch):
        """children >= 10 → warn."""
        task = MagicMock(status=TaskStatus.RUNNING, pane_label="child-pane")
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda label: 9999)
        monkeypatch.setattr("duo.cli.doctor._get_pid_fd_count", lambda pid: 100)
        monkeypatch.setattr("duo.cli.doctor._get_pid_kqueue_count", lambda pid: 5)
        monkeypatch.setattr("duo.cli.doctor._get_pid_child_count", lambda pid: 15)
        results = _doctor_check_copilot_health()
        assert results[0].status == "warn"

    def test_fail_overrides_warn(self, monkeypatch: pytest.MonkeyPatch):
        """If fds critical AND kqueue high → status is fail (worst wins)."""
        task = MagicMock(status=TaskStatus.RUNNING, pane_label="both-pane")
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda label: 9999)
        monkeypatch.setattr("duo.cli.doctor._get_pid_fd_count", lambda pid: 2500)
        monkeypatch.setattr("duo.cli.doctor._get_pid_kqueue_count", lambda pid: 100)
        monkeypatch.setattr("duo.cli.doctor._get_pid_child_count", lambda pid: 20)
        results = _doctor_check_copilot_health()
        assert results[0].status == "fail"

    def test_negative_counts_ignored(self, monkeypatch: pytest.MonkeyPatch):
        """Negative counts (lsof unavailable) → pass with empty parts."""
        task = MagicMock(status=TaskStatus.RUNNING, pane_label="neg-pane")
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda label: 9999)
        monkeypatch.setattr("duo.cli.doctor._get_pid_fd_count", lambda pid: -1)
        monkeypatch.setattr("duo.cli.doctor._get_pid_kqueue_count", lambda pid: -1)
        monkeypatch.setattr("duo.cli.doctor._get_pid_child_count", lambda pid: -1)
        results = _doctor_check_copilot_health()
        assert results[0].status == "pass"
        assert "healthy" in results[0].message

    def test_multiple_panes(self, monkeypatch: pytest.MonkeyPatch):
        """Multiple active tasks → one result per pane."""
        tasks = [
            MagicMock(status=TaskStatus.RUNNING, pane_label="pane-a"),
            MagicMock(status=TaskStatus.ACKED, pane_label="pane-b"),
        ]
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: tasks)
        pids = {"pane-a": 111, "pane-b": 222}
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda label: pids.get(label))
        monkeypatch.setattr("duo.cli.doctor._get_pid_fd_count", lambda pid: 10)
        monkeypatch.setattr("duo.cli.doctor._get_pid_kqueue_count", lambda pid: 1)
        monkeypatch.setattr("duo.cli.doctor._get_pid_child_count", lambda pid: 0)
        results = _doctor_check_copilot_health()
        assert len(results) == 2
        names = {r.name for r in results}
        assert "pane:pane-a" in names
        assert "pane:pane-b" in names

    def test_empty_pane_label_skipped(self, monkeypatch: pytest.MonkeyPatch):
        """Task with empty pane_label → skipped."""
        task = MagicMock(status=TaskStatus.RUNNING, pane_label="")
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        assert _doctor_check_copilot_health() == []

    def test_doctor_integrates_health_checks(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """doctor command includes copilot health check results."""
        monkeypatch.setattr("duo.cli.doctor.shutil.which", lambda n: f"/usr/bin/{n}")
        monkeypatch.setattr("duo.cli.doctor.os.access", lambda p, m: True)
        usage = MagicMock(free=5 * 1024 * 1024 * 1024)
        monkeypatch.setattr("duo.cli.doctor.shutil.disk_usage", lambda p: usage)
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="tmux 3.4\n"),
        )
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])
        monkeypatch.setattr(duo.cli, "DUO_DIR", tmp_path)
        monkeypatch.setattr(duo.cli.doctor, "DUO_DIR", tmp_path)
        monkeypatch.setattr(duo.cli, "TASKS_DIR", tmp_path / "tasks")
        monkeypatch.setattr(duo.cli.doctor, "TASKS_DIR", tmp_path / "tasks")
        (tmp_path / "tasks").mkdir(exist_ok=True)

        task = MagicMock(status=TaskStatus.RUNNING, pane_label="test-pane")
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda label: 9999)
        monkeypatch.setattr("duo.cli.doctor._get_pid_fd_count", lambda pid: 600)
        monkeypatch.setattr("duo.cli.doctor._get_pid_kqueue_count", lambda pid: 10)
        monkeypatch.setattr("duo.cli.doctor._get_pid_child_count", lambda pid: 3)

        result = runner.invoke(main, ["doctor"])
        assert "pane:test-pane" in result.output
        assert "fds=600" in result.output


# ── Auto-restart signal ──────────────────────────────────────────────


class TestEmitRestartSignal:
    """Tests for _emit_restart_signal()."""

    def test_writes_signal_file(self, make_task):
        """Should create restart-recommended file in task dir."""
        task = make_task("restart-test")
        _emit_restart_signal(task.id)
        signal_path = task.dir / "restart-recommended"
        assert signal_path.exists()
        content = signal_path.read_text()
        assert "Restart recommended" in content

    def test_nonexistent_task_dir_ignored(self, tmp_path: Path):
        """Missing task dir → no crash (OSError caught)."""
        _emit_restart_signal("nonexistent-task-xyz")

    def test_critical_health_emits_signal(
        self, make_task, monkeypatch: pytest.MonkeyPatch
    ):
        """doctor health check with critical fds should emit restart signal."""
        task = make_task("signal-test")
        task.pane_label = "sig-pane"
        save_task(task)
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda label: 9999)
        monkeypatch.setattr("duo.cli.doctor._get_pid_fd_count", lambda pid: 3000)
        monkeypatch.setattr("duo.cli.doctor._get_pid_kqueue_count", lambda pid: 5)
        monkeypatch.setattr("duo.cli.doctor._get_pid_child_count", lambda pid: 2)
        results = _doctor_check_copilot_health()
        assert results[0].status == "fail"
        signal_path = task.dir / "restart-recommended"
        assert signal_path.exists()

    def test_warn_health_no_signal(self, make_task, monkeypatch: pytest.MonkeyPatch):
        """doctor health check with warn (not critical) should NOT emit signal."""
        task = make_task("no-signal-test")
        task.pane_label = "nosig-pane"
        save_task(task)
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda label: 9999)
        monkeypatch.setattr("duo.cli.doctor._get_pid_fd_count", lambda pid: 600)
        monkeypatch.setattr("duo.cli.doctor._get_pid_kqueue_count", lambda pid: 5)
        monkeypatch.setattr("duo.cli.doctor._get_pid_child_count", lambda pid: 2)
        results = _doctor_check_copilot_health()
        assert results[0].status == "warn"
        signal_path = task.dir / "restart-recommended"
        assert not signal_path.exists()


class TestDoctorCheckCapiError:
    """Tests for _doctor_check_capi_error()."""

    def test_no_tasks(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [])
        assert _doctor_check_capi_error() == []

    def test_list_tasks_error(self, monkeypatch: pytest.MonkeyPatch):
        def _raise():
            raise FileNotFoundError

        monkeypatch.setattr("duo.cli.doctor.list_tasks", _raise)
        assert _doctor_check_capi_error() == []

    def test_no_active_tasks(self, monkeypatch: pytest.MonkeyPatch):
        task = MagicMock(status=TaskStatus.COMPLETED)
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        assert _doctor_check_capi_error() == []

    def test_no_journal(self, monkeypatch: pytest.MonkeyPatch):
        from pathlib import Path

        task = MagicMock(
            status=TaskStatus.RUNNING,
            journal_path=Path("/nonexistent/journal.jsonl"),
        )
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        assert _doctor_check_capi_error() == []

    def test_no_capi_events(self, monkeypatch: pytest.MonkeyPatch):
        task = _make_task()
        task.status = TaskStatus.RUNNING
        save_task(task)
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        append_event(task, "api_error", {"terminal": "rate limit"})
        results = _doctor_check_capi_error()
        assert results == []

    def test_capi_error_detected(self, monkeypatch: pytest.MonkeyPatch):
        task = _make_task()
        task.status = TaskStatus.RUNNING
        save_task(task)
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        append_event(task, "capi_error", {"terminal": "CAPIError: 400"})
        results = _doctor_check_capi_error()
        assert len(results) == 1
        assert results[0].status == "fail"
        assert "CAPIError" in results[0].message
        assert "restart" in results[0].fix.lower()


class TestBatchBareArray:
    def test_batch_bare_array_auto_wrapped(self, runner: CliRunner, tmp_path: Path):
        """Batch file with bare JSON array (not wrapped in {tasks:...}) is auto-wrapped."""
        batch_file = tmp_path / "bare-array.json"
        batch_file.write_text(
            json.dumps(
                [
                    {"name": "task-a", "description": "first task"},
                    {"name": "task-b", "description": "second task"},
                ]
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
                    "active_tasks": ["task-a", "task-b"],
                    "queued_tasks": [],
                },
            ),
        ):
            mock_create.side_effect = ["task-a", "task-b"]
            result = runner.invoke(
                main, ["batch", str(batch_file), "--repo", str(tmp_path)]
            )
        # Should succeed, not error about missing 'tasks' key
        assert result.exit_code == 0
        assert "2 tasks created" in result.output


class TestBatchDuplicateNames:
    def test_batch_duplicate_names_rejected(self, runner: CliRunner, tmp_path: Path):
        """Batch file with duplicate task names is rejected."""
        batch_file = tmp_path / "dupes.json"
        batch_file.write_text(
            json.dumps(
                {
                    "tasks": [
                        {"name": "task-a", "description": "first"},
                        {"name": "task-b", "description": "second"},
                        {"name": "task-a", "description": "duplicate"},
                    ]
                }
            )
        )
        result = runner.invoke(
            main, ["batch", str(batch_file), "--repo", str(tmp_path)]
        )
        assert result.exit_code != 0
        assert "duplicate" in result.output.lower()
        assert "task-a" in result.output

    def test_batch_file_too_large_rejected(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Batch file exceeding 10 MB is rejected."""
        batch_file = tmp_path / "huge.json"
        batch_file.write_text("x" * (10_000_001))
        result = runner.invoke(
            main, ["batch", str(batch_file), "--repo", str(tmp_path)]
        )
        assert result.exit_code != 0
        assert "too large" in result.output.lower()


# ---------------------------------------------------------------------------
# Events command tests
# ---------------------------------------------------------------------------


class TestEventsCommand:
    """Tests for duo events subcommands."""

    def test_events_list_empty(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "duo.cli.events_cmd._WATCH_EVENTS_DIR", tmp_path / "no-events"
        )
        result = runner.invoke(main, ["events", "list"])
        assert result.exit_code == 0
        assert "No events" in result.output

    def test_events_list_with_files(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        from duo.protocol import write_json

        write_json(
            edir / "task1-2026-04-08.json",
            {"task_id": "task1", "detected_at": "2026-04-08T10:00:00Z"},
        )
        result = runner.invoke(main, ["events", "list"])
        assert result.exit_code == 0
        assert "task1" in result.output
        # Regression: event line should appear exactly once (not duplicated)
        lines = [ln for ln in result.output.strip().splitlines() if ln.strip()]
        assert len(lines) == 1

    def test_events_list_quiet(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        from duo.protocol import write_json

        write_json(
            edir / "e1.json",
            {"task_id": "t1", "detected_at": "2026-04-08T10:00:00Z"},
        )
        result = runner.invoke(main, ["events", "list", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "e1.json"

    def test_events_list_quiet_empty(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "duo.cli.events_cmd._WATCH_EVENTS_DIR", tmp_path / "no-events"
        )
        result = runner.invoke(main, ["events", "list", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_events_list_quiet_dir_no_files(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        result = runner.invoke(main, ["events", "list", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_events_list_count(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        from duo.protocol import write_json

        write_json(edir / "e1.json", {"task_id": "t1", "detected_at": "x"})
        write_json(edir / "e2.json", {"task_id": "t2", "detected_at": "x"})
        result = runner.invoke(main, ["events", "list", "-c"])
        assert result.exit_code == 0
        assert result.output.strip() == "2"

    def test_events_list_count_empty(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "duo.cli.events_cmd._WATCH_EVENTS_DIR", tmp_path / "no-events"
        )
        result = runner.invoke(main, ["events", "list", "-c"])
        assert result.exit_code == 0
        assert result.output.strip() == "0"

    def test_events_list_count_dir_no_files(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        result = runner.invoke(main, ["events", "list", "-c"])
        assert result.exit_code == 0
        assert result.output.strip() == "0"

    def test_events_show_latest(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        from duo.protocol import write_json

        write_json(
            edir / "a-ev.json", {"task_id": "t1", "detected_at": "2026-04-08T09:00:00Z"}
        )
        write_json(
            edir / "b-ev.json", {"task_id": "t2", "detected_at": "2026-04-08T10:00:00Z"}
        )
        result = runner.invoke(main, ["events", "show"])
        assert result.exit_code == 0
        assert "t2" in result.output

    def test_events_show_by_name(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        from duo.protocol import write_json

        write_json(
            edir / "my-event.json",
            {"task_id": "x", "detected_at": "2026-04-08T10:00:00Z"},
        )
        result = runner.invoke(main, ["events", "show", "my-event"])
        assert result.exit_code == 0
        assert '"task_id"' in result.output

    def test_events_show_exact_name_match(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Event file that matches the exact name (no .json fallback needed)."""
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        (edir / "exact-event").write_text(
            json.dumps({"task_id": "exact", "detected_at": "2026-04-08T10:00:00Z"})
        )
        result = runner.invoke(main, ["events", "show", "exact-event"])
        assert result.exit_code == 0
        assert "exact" in result.output

    def test_events_show_not_found(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        result = runner.invoke(main, ["events", "show", "nonexistent"])
        assert result.exit_code != 0

    def test_events_show_no_dir(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", tmp_path / "nope")
        result = runner.invoke(main, ["events", "show"])
        assert result.exit_code != 0

    def test_events_show_path_traversal(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Path traversal in event name is rejected."""
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        result = runner.invoke(main, ["events", "show", "../../../etc/passwd"])
        assert result.exit_code != 0

    def test_events_show_corrupt_file(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Corrupt event file shows error."""
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        (edir / "bad-event.json").write_text("{corrupt json!!!")
        result = runner.invoke(main, ["events", "show", "bad-event"])
        assert result.exit_code != 0
        assert "Invalid event file" in result.output or "corrupted" in result.output

    def test_events_clear_empty(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "duo.cli.events_cmd._WATCH_EVENTS_DIR", tmp_path / "no-events"
        )
        result = runner.invoke(main, ["events", "clear"])
        assert result.exit_code == 0
        assert "No events" in result.output

    def test_events_clear_force(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        from duo.protocol import write_json

        write_json(edir / "ev1.json", {"task_id": "t"})
        write_json(edir / "ev2.json", {"task_id": "t"})
        result = runner.invoke(main, ["events", "clear", "--force"])
        assert result.exit_code == 0
        assert "Cleared 2" in result.output
        assert not list(edir.glob("*.json"))

    def test_events_clear_confirm_abort(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        from duo.protocol import write_json

        write_json(edir / "ev.json", {"task_id": "t"})
        result = runner.invoke(main, ["events", "clear"], input="n\n")
        assert result.exit_code != 0  # aborted

    def test_events_tail_initial(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Tail shows initial events then gets interrupted."""
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        from duo.protocol import write_json

        write_json(
            edir / "ev.json",
            {"task_id": "tail-test", "detected_at": "2026-04-08T10:00:00Z"},
        )
        # Simulate KeyboardInterrupt on first sleep
        import time

        orig_sleep = time.sleep
        call_count = 0

        def fake_sleep(secs: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                raise KeyboardInterrupt
            orig_sleep(secs)

        monkeypatch.setattr("time.sleep", fake_sleep)
        result = runner.invoke(main, ["events", "tail"])
        assert "tail-test" in result.output
        assert "following" in result.output

    def test_events_list_dir_exists_no_files(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        result = runner.invoke(main, ["events", "list"])
        assert result.exit_code == 0
        assert "No events" in result.output

    def test_events_list_bad_json_skipped(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        (edir / "bad.json").write_text("{{{invalid")
        result = runner.invoke(main, ["events", "list"])
        assert result.exit_code == 0

    def test_events_show_latest_empty_dir(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        result = runner.invoke(main, ["events", "show"])
        assert result.exit_code != 0
        assert "No events" in result.output

    def test_events_show_invalid_json(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        (edir / "bad.json").write_text("{{{nope")
        result = runner.invoke(main, ["events", "show", "bad.json"])
        assert result.exit_code != 0
        assert "Invalid" in result.output

    def test_events_clear_dir_exists_no_files(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        result = runner.invoke(main, ["events", "clear"])
        assert result.exit_code == 0
        assert "No events" in result.output

    def test_events_tail_new_event_in_loop(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Tail picks up a new event added during the loop."""
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        from duo.protocol import write_json

        call_count = 0

        def fake_sleep(secs: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Simulate new event appearing during poll
                write_json(
                    edir / "new-ev.json",
                    {"task_id": "new-task", "detected_at": "2026-04-08T11:00:00Z"},
                )
            elif call_count >= 2:
                raise KeyboardInterrupt

        monkeypatch.setattr("time.sleep", fake_sleep)
        result = runner.invoke(main, ["events", "tail"])
        assert "new-task" in result.output

    def test_events_list_json_empty(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--json-output returns empty list when no events dir."""
        monkeypatch.setattr(
            "duo.cli.events_cmd._WATCH_EVENTS_DIR", tmp_path / "no-events"
        )
        result = runner.invoke(main, ["events", "list", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["events"] == []
        assert data["total"] == 0

    def test_events_list_json_with_files(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--json-output returns event data."""
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        from duo.protocol import write_json

        write_json(
            edir / "t1-2026.json",
            {"task_id": "t1", "detected_at": "2026-04-08T10:00:00Z"},
        )
        result = runner.invoke(main, ["events", "list", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["events"]) == 1
        assert data["events"][0]["task_id"] == "t1"
        assert data["total"] == 1

    def test_events_list_json_dir_no_files(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--json-output returns empty when dir exists but no .json files."""
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        result = runner.invoke(main, ["events", "list", "--json-output"])
        data = json.loads(result.output)
        assert data["events"] == []

    def test_events_clear_json_empty(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--json-output clear returns zero when no events."""
        monkeypatch.setattr(
            "duo.cli.events_cmd._WATCH_EVENTS_DIR", tmp_path / "no-events"
        )
        result = runner.invoke(main, ["events", "clear", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["cleared"] == 0

    def test_events_clear_json_with_files(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--json-output clear returns count of cleared files."""
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        from duo.protocol import write_json

        write_json(edir / "e1.json", {"task_id": "t1"})
        write_json(edir / "e2.json", {"task_id": "t2"})
        result = runner.invoke(main, ["events", "clear", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["cleared"] == 2

    def test_events_clear_json_dir_no_files(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--json-output clear when dir exists but empty."""
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        result = runner.invoke(main, ["events", "clear", "--json-output"])
        data = json.loads(result.output)
        assert data["cleared"] == 0

    def test_events_clear_quiet_no_dir(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "duo.cli.events_cmd._WATCH_EVENTS_DIR", tmp_path / "no-events"
        )
        result = runner.invoke(main, ["events", "clear", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "0"

    def test_events_clear_quiet_empty_dir(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        result = runner.invoke(main, ["events", "clear", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "0"

    def test_events_clear_quiet_with_files(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        from duo.protocol import write_json

        write_json(edir / "ev1.json", {"task_id": "t1"})
        write_json(edir / "ev2.json", {"task_id": "t2"})
        result = runner.invoke(main, ["events", "clear", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "2"
        assert not list(edir.glob("*.json"))


# ---------------------------------------------------------------------------
# CEO Workflow command tests
# ---------------------------------------------------------------------------


class TestCeoWait:
    """Tests for duo ceo-wait."""

    def test_task_not_found(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ceo-wait", "nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_pane_dead(self, runner: CliRunner, make_task) -> None:
        task = make_task("wait-dead")
        with patch("duo.transport.is_process_alive", return_value=False):
            result = runner.invoke(main, ["ceo-wait", task.id])
        assert result.exit_code != 0
        assert "not alive" in result.output

    def test_timeout(self, runner: CliRunner, make_task) -> None:
        task = make_task("wait-timeout")
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.wait_for_dialog", return_value=False),
        ):
            result = runner.invoke(main, ["ceo-wait", task.id, "--timeout", "10"])
        assert result.exit_code != 0
        assert "Timeout" in result.output

    def test_dialog_found(self, runner: CliRunner, make_task) -> None:
        task = make_task("wait-ok")
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.wait_for_dialog", return_value=True),
            patch("duo.transport.read_pane", return_value="dialog content here"),
            patch("duo.commander._write_watch_event") as mock_write,
        ):
            result = runner.invoke(main, ["ceo-wait", task.id])
        assert result.exit_code == 0
        assert "dialog content here" in result.output
        mock_write.assert_called_once()

    def test_custom_interval(self, runner: CliRunner, make_task) -> None:
        task = make_task("wait-interval")
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.wait_for_dialog", return_value=True) as mock_wait,
            patch("duo.transport.read_pane", return_value="content"),
            patch("duo.commander._write_watch_event"),
        ):
            runner.invoke(main, ["ceo-wait", task.id, "--interval", "2"])
        mock_wait.assert_called_once_with(task.pane_label, timeout=300, interval=2.0)

    def test_session_logging(
        self,
        runner: CliRunner,
        make_task,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import duo.ceo_log

        sessions_dir = tmp_path / "ceo-sessions"
        monkeypatch.setattr(duo.ceo_log, "CEO_SESSIONS_DIR", sessions_dir)
        sid = start_ceo_session()
        monkeypatch.setenv("DUO_CEO_SESSION", sid)
        task = make_task("wait-log")
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.wait_for_dialog", return_value=True),
            patch("duo.transport.read_pane", return_value="dialog!"),
            patch("duo.commander._write_watch_event"),
        ):
            result = runner.invoke(main, ["ceo-wait", task.id])
        assert result.exit_code == 0
        events = duo.ceo_log.replay_session(sid)
        assert any(e["event"] == "dialog_detected" for e in events)


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


class TestCeoSelect:
    """Tests for duo ceo-select."""

    def test_task_not_found(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ceo-select", "nope", "1"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_non_numeric_option_rejected(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ceo-select", "any-task", "abc"])
        assert result.exit_code != 0
        assert "must be a number" in result.output

    def test_not_in_dialog(self, runner: CliRunner, make_task) -> None:
        task = make_task("sel-nodlg")
        with (
            patch("duo.transport.is_in_dialog_stable", return_value=False),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.read_pane", return_value=""),
        ):
            result = runner.invoke(main, ["ceo-select", task.id, "1"])
        assert result.exit_code != 0
        assert "not in a stable dialog" in result.output

    def test_select_number(self, runner: CliRunner, make_task) -> None:
        task = make_task("sel-num")
        with (
            patch("duo.transport.is_in_dialog_stable", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.read_pane", return_value=""),
            patch("duo.transport.select_dialog_option") as mock_sel,
        ):
            result = runner.invoke(main, ["ceo-select", task.id, "2"])
        assert result.exit_code == 0
        assert "Selected option 2" in result.output
        mock_sel.assert_called_once_with(task.pane_label, "2")

    def test_select_other(self, runner: CliRunner, make_task) -> None:
        task = make_task("sel-other")
        with (
            patch("duo.transport.is_in_dialog_stable", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.read_pane", return_value=""),
            patch(
                "duo.transport.send_option_other_message", return_value=True
            ) as mock_send,
        ):
            result = runner.invoke(
                main, ["ceo-select", task.id, "--other", "my custom text"]
            )
        assert result.exit_code == 0
        assert "Other" in result.output
        mock_send.assert_called_once_with(task.pane_label, "my custom text")

    def test_ceo_select_other_dialog_persists(
        self, runner: CliRunner, make_task
    ) -> None:
        """--other shows warning when dialog persists after retries."""
        task = make_task("sel-other-fail")
        with (
            patch("duo.transport.is_in_dialog_stable", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.read_pane", return_value=""),
            patch("duo.transport.send_option_other_message", return_value=False),
        ):
            result = runner.invoke(
                main, ["ceo-select", task.id, "--other", "my custom text"]
            )
        assert result.exit_code == 0
        assert "may still be active" in result.output

    def test_bad_task_name(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ceo-select", "bad name!!", "1"])
        assert result.exit_code != 0

    def test_both_option_and_other_is_error(self, runner: CliRunner, make_task) -> None:
        task = make_task("sel-both")
        result = runner.invoke(main, ["ceo-select", task.id, "2", "--other", "text"])
        assert result.exit_code != 0
        assert "Cannot specify both" in result.output

    def test_neither_option_nor_other_is_error(
        self, runner: CliRunner, make_task
    ) -> None:
        task = make_task("sel-none")
        result = runner.invoke(main, ["ceo-select", task.id])
        assert result.exit_code != 0
        assert "Must specify" in result.output

    def test_refused_at_main_prompt(self, runner: CliRunner, make_task) -> None:
        """ceo-select REFUSES if pane is at main ❯ prompt."""
        task = make_task("sel-prompt")
        with (
            patch("duo.transport.is_at_main_prompt", return_value=True),
            patch("duo.transport.read_pane", return_value="❯ Type @"),
        ):
            result = runner.invoke(main, ["ceo-select", task.id, "1"])
        assert result.exit_code != 0
        assert "REFUSED" in result.output
        assert "Premium Request" in result.output

    def test_force_new_session_bypasses_assert(
        self, runner: CliRunner, make_task
    ) -> None:
        """--force-new-session bypasses the main-prompt check but logs."""
        task = make_task("sel-force")
        with (
            patch("duo.transport.is_in_dialog_stable", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=True),
            patch("duo.transport.read_pane", return_value="❯ Type @"),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.OPTION),
            patch("duo.transport.select_dialog_option"),
        ):
            result = runner.invoke(
                main,
                ["ceo-select", task.id, "1", "--force-new-session"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        # Check budget log was written
        from duo.protocol import DUO_DIR

        log_path = DUO_DIR / "pr-budget.log"
        assert log_path.exists()
        assert "--force-new-session" in log_path.read_text()

    def test_text_dialog_option_number_rejected(
        self, runner: CliRunner, make_task
    ) -> None:
        """Selecting a number in a TEXT dialog is rejected."""
        task = make_task("sel-text-num")
        with (
            patch("duo.transport.is_in_dialog_stable", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch(
                "duo.transport.read_pane", return_value="╭─ Q ─╮\n Type your answer\n╰─"
            ),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.TEXT),
        ):
            result = runner.invoke(main, ["ceo-select", task.id, "1"])
        assert result.exit_code != 0
        assert "text-input dialog" in result.output

    def test_text_dialog_other_works(self, runner: CliRunner, make_task) -> None:
        """--other in a TEXT dialog uses send_text_dialog_message."""
        task = make_task("sel-text-ok")
        with (
            patch("duo.transport.is_in_dialog_stable", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch(
                "duo.transport.read_pane", return_value="╭─ Q ─╮\n Type your answer\n╰─"
            ),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.TEXT),
            patch(
                "duo.transport.send_text_dialog_message", return_value=True
            ) as mock_send,
        ):
            result = runner.invoke(
                main, ["ceo-select", task.id, "--other", "my answer"]
            )
        assert result.exit_code == 0
        assert "Typed text" in result.output
        mock_send.assert_called_once_with(task.pane_label, "my answer")

    def test_text_dialog_other_retry_warning(
        self, runner: CliRunner, make_task
    ) -> None:
        """--other shows warning when dialog persists after retries."""
        task = make_task("sel-text-retry")
        with (
            patch("duo.transport.is_in_dialog_stable", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch(
                "duo.transport.read_pane", return_value="╭─ Q ─╮\n Type your answer\n╰─"
            ),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.TEXT),
            patch("duo.transport.send_text_dialog_message", return_value=False),
        ):
            result = runner.invoke(
                main, ["ceo-select", task.id, "--other", "my answer"]
            )
        assert result.exit_code == 0
        assert "may still be active" in result.output

    def test_bullet_dialog_select_option(self, runner: CliRunner, make_task) -> None:
        """Selecting a number in a BULLET dialog calls select_bullet_option."""
        task = make_task("sel-bullet")
        with (
            patch("duo.transport.is_in_dialog_stable", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.read_pane", return_value=""),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.BULLET),
            patch("duo.transport.select_bullet_option") as mock_sel,
        ):
            result = runner.invoke(main, ["ceo-select", task.id, "2"])
        assert result.exit_code == 0
        assert "Selected bullet option 2" in result.output
        mock_sel.assert_called_once_with(task.pane_label, 2)

    def test_bullet_dialog_other_text(self, runner: CliRunner, make_task) -> None:
        """--other in a BULLET dialog uses send_text_dialog_message."""
        task = make_task("sel-bullet-other")
        with (
            patch("duo.transport.is_in_dialog_stable", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.read_pane", return_value=""),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.BULLET),
            patch(
                "duo.transport.send_text_dialog_message", return_value=True
            ) as mock_send,
        ):
            result = runner.invoke(
                main, ["ceo-select", task.id, "--other", "custom answer"]
            )
        assert result.exit_code == 0
        assert "Typed text in bullet dialog" in result.output
        mock_send.assert_called_once_with(task.pane_label, "custom answer")

    """Tests for duo ceo-approve."""

    def test_approve_task_not_found(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ceo-approve", "nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_approve_success(self, runner: CliRunner, make_task) -> None:
        task = make_task("appr-ok")
        with (
            patch("duo.transport.is_permission_dialog", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.read_pane", return_value=""),
            patch("duo.transport.approve_permission") as mock_approve,
        ):
            result = runner.invoke(main, ["ceo-approve", task.id])
        assert result.exit_code == 0
        assert "Approved" in result.output
        mock_approve.assert_called_once_with(task.pane_label)

    def test_approve_not_permission_dialog(self, runner: CliRunner, make_task) -> None:
        task = make_task("appr-ask")
        with (
            patch("duo.transport.is_permission_dialog", return_value=False),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.read_pane", return_value=""),
        ):
            result = runner.invoke(main, ["ceo-approve", task.id])
        assert result.exit_code != 0
        assert "not showing a permission dialog" in result.output
        assert "ceo-select" in result.output

    def test_approve_runtime_error(self, runner: CliRunner, make_task) -> None:
        task = make_task("appr-fail")
        with (
            patch("duo.transport.is_permission_dialog", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.read_pane", return_value=""),
            patch(
                "duo.transport.approve_permission",
                side_effect=RuntimeError("SAFETY: not in dialog"),
            ),
        ):
            result = runner.invoke(main, ["ceo-approve", task.id])
        assert result.exit_code != 0

    def test_approve_oserror(self, runner: CliRunner, make_task) -> None:
        task = make_task("appr-os")
        with (
            patch("duo.transport.is_permission_dialog", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.read_pane", return_value=""),
            patch("duo.transport.approve_permission", side_effect=OSError("pane gone")),
        ):
            result = runner.invoke(main, ["ceo-approve", task.id])
        assert result.exit_code != 0

    def test_approve_bad_task_name(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ceo-approve", "inv@lid"])
        assert result.exit_code != 0

    def test_approve_refused_at_main_prompt(self, runner: CliRunner, make_task) -> None:
        """ceo-approve REFUSES if pane is at main ❯ prompt."""
        task = make_task("appr-prompt")
        with (
            patch("duo.transport.is_at_main_prompt", return_value=True),
            patch("duo.transport.read_pane", return_value="❯ Type @"),
        ):
            result = runner.invoke(main, ["ceo-approve", task.id])
        assert result.exit_code != 0
        assert "REFUSED" in result.output
        assert "Premium Request" in result.output

    def test_approve_force_new_session_bypasses_assert(
        self, runner: CliRunner, make_task
    ) -> None:
        """--force-new-session bypasses the main-prompt check but logs."""
        task = make_task("appr-force")
        with (
            patch("duo.transport.is_permission_dialog", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=True),
            patch("duo.transport.read_pane", return_value="❯ Type @"),
            patch("duo.transport.approve_permission"),
        ):
            result = runner.invoke(
                main, ["ceo-approve", task.id, "--force-new-session"]
            )
        assert result.exit_code == 0
        from duo.protocol import DUO_DIR

        log_path = DUO_DIR / "pr-budget.log"
        assert log_path.exists()
        assert "--force-new-session" in log_path.read_text()

    def test_approve_session_logging(
        self,
        runner: CliRunner,
        make_task,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import duo.ceo_log

        sessions_dir = tmp_path / "ceo-sessions"
        monkeypatch.setattr(duo.ceo_log, "CEO_SESSIONS_DIR", sessions_dir)
        sid = start_ceo_session()
        monkeypatch.setenv("DUO_CEO_SESSION", sid)
        task = make_task("appr-log")
        with (
            patch("duo.transport.is_permission_dialog", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.read_pane", return_value=""),
            patch("duo.transport.approve_permission"),
        ):
            result = runner.invoke(main, ["ceo-approve", task.id])
        assert result.exit_code == 0
        events = duo.ceo_log.replay_session(sid)
        assert any(
            e["event"] == "decision" and e["decision_type"] == "approve" for e in events
        )

    def test_select_session_logging(
        self,
        runner: CliRunner,
        make_task,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import duo.ceo_log

        sessions_dir = tmp_path / "ceo-sessions"
        monkeypatch.setattr(duo.ceo_log, "CEO_SESSIONS_DIR", sessions_dir)
        sid = start_ceo_session()
        monkeypatch.setenv("DUO_CEO_SESSION", sid)
        task = make_task("sel-log")
        with (
            patch("duo.transport.is_in_dialog_stable", return_value=True),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.OPTION),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.read_pane", return_value=""),
            patch("duo.transport.select_dialog_option"),
        ):
            result = runner.invoke(main, ["ceo-select", task.id, "2"])
        assert result.exit_code == 0
        events = duo.ceo_log.replay_session(sid)
        assert any(
            e["event"] == "decision" and e["decision_type"] == "select" for e in events
        )


class TestCeoStatus:
    """Tests for duo ceo-status."""

    def test_task_not_found(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ceo-status", "nope"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_dead_pane(self, runner: CliRunner, make_task) -> None:
        task = make_task("stat-dead")
        with patch("duo.transport.is_process_alive", return_value=False):
            result = runner.invoke(main, ["ceo-status", task.id])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {"task": task.id, "state": "dead"}

    def test_dialog_state(self, runner: CliRunner, make_task) -> None:
        task = make_task("stat-dlg")
        pane_content = "╭─ Question ─╮\n│ 1. Yes  \n│ 2. No   \n│ 3. Other\n╰─"
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value=pane_content),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.OPTION),
        ):
            result = runner.invoke(main, ["ceo-status", task.id])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["state"] == "dialog"
        assert data["options"] == 3

    def test_processing_state(self, runner: CliRunner, make_task) -> None:
        task = make_task("stat-proc")
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value="◉ Thinking..."),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.NONE),
        ):
            result = runner.invoke(main, ["ceo-status", task.id])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {"task": task.id, "state": "processing"}

    def test_idle_state(self, runner: CliRunner, make_task) -> None:
        task = make_task("stat-idle")
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value="❯ "),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.NONE),
        ):
            result = runner.invoke(main, ["ceo-status", task.id])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {"task": task.id, "state": "idle"}

    def test_bad_task_name(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ceo-status", "bad name"])
        assert result.exit_code != 0

    def test_assert_in_dialog_passes_when_dialog(
        self, runner: CliRunner, make_task
    ) -> None:
        """--assert-in-dialog exits 0 when pane IS in a dialog."""
        task = make_task("stat-aid-ok")
        pane_content = "╭─ Question ─╮\n│ 1. Yes  \n│ 2. No\n╰─"
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value=pane_content),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.OPTION),
        ):
            result = runner.invoke(main, ["ceo-status", task.id, "--assert-in-dialog"])
        assert result.exit_code == 0

    def test_assert_in_dialog_fails_when_idle(
        self, runner: CliRunner, make_task
    ) -> None:
        """--assert-in-dialog exits non-zero when pane is idle."""
        task = make_task("stat-aid-idle")
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value="❯ "),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.NONE),
        ):
            result = runner.invoke(main, ["ceo-status", task.id, "--assert-in-dialog"])
        # SystemExit(1) — Click wraps as exit_code=1
        assert result.exit_code == 1

    def test_assert_in_dialog_fails_when_processing(
        self, runner: CliRunner, make_task
    ) -> None:
        """--assert-in-dialog exits non-zero when pane is processing."""
        task = make_task("stat-aid-proc")
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value="◉ Thinking..."),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.NONE),
        ):
            result = runner.invoke(main, ["ceo-status", task.id, "--assert-in-dialog"])
        assert result.exit_code == 1

    def test_assert_in_dialog_fails_when_dead(
        self, runner: CliRunner, make_task
    ) -> None:
        """--assert-in-dialog exits non-zero when pane is dead."""
        task = make_task("stat-aid-dead")
        with patch("duo.transport.is_process_alive", return_value=False):
            result = runner.invoke(main, ["ceo-status", task.id, "--assert-in-dialog"])
        assert result.exit_code == 1

    def test_text_dialog_state(self, runner: CliRunner, make_task) -> None:
        """ceo-status reports text_dialog for text-input dialogs."""
        task = make_task("stat-text")
        pane_content = "╭─ Question ─╮\n Type your answer\n╰────────────╯"
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value=pane_content),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.TEXT),
        ):
            result = runner.invoke(main, ["ceo-status", task.id])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {"task": task.id, "state": "text_dialog"}

    def test_assert_in_dialog_passes_for_text_dialog(
        self, runner: CliRunner, make_task
    ) -> None:
        """--assert-in-dialog exits 0 for text_dialog (it IS a dialog)."""
        task = make_task("stat-aid-text")
        pane_content = "╭─ Q ─╮\n Type your answer\n╰─"
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value=pane_content),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.TEXT),
        ):
            result = runner.invoke(main, ["ceo-status", task.id, "--assert-in-dialog"])
        assert result.exit_code == 0

    def test_dialog_options_not_counted_outside_box(
        self, runner: CliRunner, make_task
    ) -> None:
        """Options in scrollback ABOVE the dialog box are not counted."""
        task = make_task("stat-box-above")
        # Scrollback has "1. foo", "2. bar" before the dialog box
        pane_content = (
            "Here are some steps:\n"
            "1. Install deps\n"
            "2. Run tests\n"
            "3. Deploy\n"
            "\n"
            "╭─ Permission ─╮\n"
            "│ ❯ 1. Yes\n"
            "│   2. No\n"
            "╰──────────────╯"
        )
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value=pane_content),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.OPTION),
        ):
            result = runner.invoke(main, ["ceo-status", task.id])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["options"] == 2  # only 2 inside box, not 5

    def test_dialog_options_not_counted_below_box(
        self, runner: CliRunner, make_task
    ) -> None:
        """Options BELOW the dialog box are not counted."""
        task = make_task("stat-box-below")
        pane_content = (
            "╭─ Run? ─╮\n"
            "│ ❯ 1. Yes\n"
            "│   2. No\n"
            "│   3. Other\n"
            "╰─────────╯\n"
            "4. Some other numbered text\n"
            "5. More numbered text\n"
        )
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value=pane_content),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.OPTION),
        ):
            result = runner.invoke(main, ["ceo-status", task.id])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["options"] == 3  # only 3 inside box

    def test_dialog_options_only_box(self, runner: CliRunner, make_task) -> None:
        """Pure dialog box with no surrounding noise."""
        task = make_task("stat-box-only")
        pane_content = (
            "╭─ Allow? ─╮\n"
            "│ ❯ 1. Allow once\n"
            "│   2. Allow for session\n"
            "│   3. Allow + add to allowed\n"
            "│   4. Deny\n"
            "│   5. Tell differently\n"
            "╰───────────╯"
        )
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value=pane_content),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.OPTION),
        ):
            result = runner.invoke(main, ["ceo-status", task.id])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["options"] == 5

    def test_bullet_dialog_state(self, runner: CliRunner, make_task) -> None:
        """ceo-status reports bullet_dialog for bullet-style dialogs."""
        task = make_task("stat-bullet")
        pane_content = (
            "╭─ Pick branch ─╮\n❯ main\n  develop\n  feature-x\n╰───────────────╯"
        )
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value=pane_content),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.BULLET),
            patch("duo.transport.strip_ansi", side_effect=lambda x: x),
        ):
            result = runner.invoke(main, ["ceo-status", task.id])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["state"] == "bullet_dialog"
        assert data["items"] == 3
        assert data["cursor"] == 1

    def test_assert_in_dialog_passes_for_bullet(
        self, runner: CliRunner, make_task
    ) -> None:
        """--assert-in-dialog exits 0 for bullet_dialog."""
        task = make_task("stat-aid-bullet")
        pane_content = "╭─ Q ─╮\n❯ A\n  B\n╰─────╯"
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value=pane_content),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.BULLET),
            patch("duo.transport.strip_ansi", side_effect=lambda x: x),
        ):
            result = runner.invoke(main, ["ceo-status", task.id, "--assert-in-dialog"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# duo think
# ---------------------------------------------------------------------------


class TestThinkInfo:
    """Tests for ``duo think <name>`` (info mode)."""

    def test_info_mode(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        tdir = fake_thinking / "my-app"
        tdir.mkdir(parents=True)
        with (
            patch("duo.thinking.ensure_pane", return_value="think-my-app"),
            patch("duo.thinking.thinking_dir", return_value=tdir),
        ):
            result = runner.invoke(main, ["think", "my-app"])
        assert result.exit_code == 0
        assert "Thinking session: my-app" in result.output
        assert "think-my-app" in result.output

    def test_no_name_error(self, runner: CliRunner, isolated_tasks: Path) -> None:
        result = runner.invoke(main, ["think"])
        assert result.exit_code != 0


class TestThinkAsk:
    """Tests for ``duo think <name> --ask``."""

    def test_ask_happy_path(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        with (
            patch("duo.thinking.ensure_pane", return_value="think-x"),
            patch(
                "duo.transport.read_pane",
                side_effect=["before", "after\nClaude says hello"],
            ),
            patch("duo.transport.type_text"),
            patch("duo.transport.send_keys"),
            patch("duo.thinking.wait_for_response_stable", return_value="idle"),
            patch("duo.thinking.extract_response", return_value="Claude says hello"),
            patch("duo.thinking.append_session_log"),
        ):
            result = runner.invoke(main, ["think", "x", "--ask", "hello"])
        assert result.exit_code == 0
        assert "Claude says hello" in result.output

    def test_ask_dialog_response(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        with (
            patch("duo.thinking.ensure_pane", return_value="think-x"),
            patch("duo.transport.read_pane", return_value="before"),
            patch("duo.transport.type_text"),
            patch("duo.transport.send_keys"),
            patch("duo.thinking.wait_for_response_stable", return_value="dialog"),
        ):
            result = runner.invoke(main, ["think", "x", "--ask", "q"])
        assert result.exit_code != 0
        assert "dialog" in result.output.lower() or "question" in result.output.lower()

    def test_ask_timeout(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        with (
            patch("duo.thinking.ensure_pane", return_value="think-x"),
            patch("duo.transport.read_pane", return_value="before"),
            patch("duo.transport.type_text"),
            patch("duo.transport.send_keys"),
            patch("duo.thinking.wait_for_response_stable", return_value="timeout"),
        ):
            result = runner.invoke(main, ["think", "x", "--ask", "q"])
        assert result.exit_code != 0
        assert (
            "not responding" in result.output.lower()
            or "timeout" in result.output.lower()
        )


class TestThinkFinalize:
    """Tests for ``duo think <name> --finalize``."""

    def test_finalize_success(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        tdir = fake_thinking / "my-app"
        tdir.mkdir(parents=True)
        plan_path = tdir / "plan.md"

        # Pre-write plan.md so the poll finds it immediately
        plan_path.write_text("# Plan: my-app\n## Goal\nBuild it.", encoding="utf-8")

        counter = {"n": 0}

        def fake_time() -> float:
            counter["n"] += 1
            return counter["n"]

        with (
            patch("duo.thinking.ensure_pane", return_value="think-my-app"),
            patch("duo.thinking.thinking_dir", return_value=tdir),
            patch("duo.thinking.wait_for_response_stable", return_value="idle"),
            patch("duo.transport.type_text"),
            patch("duo.transport.send_keys"),
            patch("time.sleep"),
            patch("time.time", side_effect=fake_time),
        ):
            result = runner.invoke(main, ["think", "my-app", "--finalize"])
        assert result.exit_code == 0
        assert "Plan written" in result.output

    def test_finalize_pane_in_dialog(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        with (
            patch("duo.thinking.ensure_pane", return_value="think-x"),
            patch("duo.thinking.thinking_dir", return_value=fake_thinking / "x"),
            patch("duo.thinking.wait_for_response_stable", return_value="dialog"),
        ):
            result = runner.invoke(main, ["think", "x", "--finalize"])
        assert result.exit_code != 0
        assert "dialog" in result.output.lower()

    def test_finalize_pane_unresponsive(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        with (
            patch("duo.thinking.ensure_pane", return_value="think-x"),
            patch("duo.thinking.thinking_dir", return_value=fake_thinking / "x"),
            patch("duo.thinking.wait_for_response_stable", return_value="timeout"),
        ):
            result = runner.invoke(main, ["think", "x", "--finalize"])
        assert result.exit_code != 0
        assert "unresponsive" in result.output.lower() or "Pane" in result.output

    def test_finalize_timeout_no_plan(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Finalize times out when Claude doesn't produce plan.md."""
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        tdir = fake_thinking / "my-app"
        tdir.mkdir(parents=True)
        # No plan.md written — deadline exceeded immediately

        counter = {"n": 0}

        def fake_time() -> float:
            counter["n"] += 1
            return counter["n"] * 100  # jumps past deadline on first loop check

        with (
            patch("duo.thinking.ensure_pane", return_value="think-my-app"),
            patch("duo.thinking.thinking_dir", return_value=tdir),
            patch("duo.thinking.wait_for_response_stable", return_value="idle"),
            patch("duo.transport.type_text"),
            patch("duo.transport.send_keys"),
            patch("time.sleep"),
            patch("time.time", side_effect=fake_time),
        ):
            result = runner.invoke(main, ["think", "my-app", "--finalize"])
        assert result.exit_code != 0
        assert "plan.md" in result.output


class TestThinkClose:
    """Tests for ``duo think <name> --close``."""

    def test_close_existing(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        tdir = fake_thinking / "my-app"
        tdir.mkdir(parents=True)
        (tdir / "CLAUDE.md").write_text("test", encoding="utf-8")
        with (
            patch("duo.thinking.close_pane", return_value=True),
            patch("duo.thinking.thinking_dir", return_value=tdir),
        ):
            result = runner.invoke(main, ["think", "my-app", "--close"])
        assert result.exit_code == 0
        assert "Closed" in result.output

    def test_close_no_active_pane(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Close when session dir exists but pane is not active."""
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        tdir = fake_thinking / "my-app"
        tdir.mkdir(parents=True)
        (tdir / "CLAUDE.md").write_text("test", encoding="utf-8")
        with (
            patch("duo.thinking.close_pane", return_value=False),
            patch("duo.thinking.thinking_dir", return_value=tdir),
        ):
            result = runner.invoke(main, ["think", "my-app", "--close"])
        assert result.exit_code == 0
        assert "No active pane" in result.output

    def test_close_no_session(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        with patch("duo.thinking.thinking_dir", return_value=fake_thinking / "nope"):
            result = runner.invoke(main, ["think", "nope", "--close"])
        assert result.exit_code != 0


class TestThinkDelete:
    """Tests for ``duo think <name> --delete``."""

    def test_delete_confirmed(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        tdir = fake_thinking / "my-app"
        tdir.mkdir(parents=True)
        (tdir / "CLAUDE.md").write_text("test", encoding="utf-8")
        with (
            patch("duo.thinking.close_pane", return_value=True),
            patch("duo.thinking.thinking_dir", return_value=tdir),
        ):
            result = runner.invoke(main, ["think", "my-app", "--delete"], input="y\n")
        assert result.exit_code == 0
        assert "Deleted" in result.output
        assert not tdir.exists()

    def test_delete_aborted(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        tdir = fake_thinking / "my-app"
        tdir.mkdir(parents=True)
        (tdir / "CLAUDE.md").write_text("test", encoding="utf-8")
        with patch("duo.thinking.thinking_dir", return_value=tdir):
            result = runner.invoke(main, ["think", "my-app", "--delete"], input="n\n")
        assert result.exit_code == 0
        assert "Aborted" in result.output
        assert tdir.exists()

    def test_delete_no_session(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        with patch("duo.thinking.thinking_dir", return_value=fake_thinking / "nope"):
            result = runner.invoke(main, ["think", "nope", "--delete"], input="y\n")
        assert result.exit_code != 0


class TestThinkList:
    """Tests for ``duo think list``."""

    def test_empty_list(self, runner: CliRunner, isolated_tasks: Path) -> None:
        with patch("duo.thinking.list_sessions", return_value=[]):
            result = runner.invoke(main, ["think", "list"])
        assert result.exit_code == 0
        assert "No thinking sessions" in result.output

    def test_with_sessions(self, runner: CliRunner, isolated_tasks: Path) -> None:
        sessions = [
            {
                "name": "alpha",
                "pane": "alive",
                "status": "active",
                "files": "CLAUDE.md",
            },
            {
                "name": "beta",
                "pane": "none",
                "status": "finalized",
                "files": "CLAUDE.md plan.md",
            },
        ]
        with patch("duo.thinking.list_sessions", return_value=sessions):
            result = runner.invoke(main, ["think", "list"])
        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "beta" in result.output
        assert "finalized" in result.output


# ── Bench command tests ───────────────────────────────────────────────


class TestMultiProjectIsolation:
    """Tests documenting task-name collision behavior across projects."""

    def test_start_same_name_different_repo_blocked(self, runner: CliRunner):
        """Starting a task with the same name from a different repo is blocked.

        The error message must mention the existing task's worktree so the
        user can tell which project owns it.
        """
        # Create task "fix" owned by repo-a
        create_task(
            task_id="fix",
            description="Fix for repo-a",
            worktree="/projects/repo-a",
            branch="duo/fix",
            base_commit="aaa111",
            subtasks=[
                Subtask(
                    step_id=1,
                    description="fix stuff",
                    target_files=[],
                    writable_paths=["*"],
                )
            ],
        )
        # Attempt to start "fix" from repo-b — should fail with worktree info
        with patch("duo.cli.lifecycle_cmd._create_worktree") as mock_wt:
            mock_wt.return_value = ("/projects/repo-b/worktrees/fix", "bbb222")
            result = runner.invoke(
                main,
                ["start", "fix", "--repo", "/projects/repo-b", "--desc", "repo-b fix"],
            )
        assert result.exit_code != 0
        assert "already exists" in result.output
        assert "/projects/repo-a" in result.output
        assert "different task name" in result.output

    def test_task_worktree_stored_correctly(self):
        """Each task stores the correct worktree path."""
        task_a = create_task(
            task_id="task-a",
            description="Task in repo-a",
            worktree="/repos/alpha",
            branch="duo/task-a",
            base_commit="aaa",
            subtasks=[
                Subtask(
                    step_id=1,
                    description="s",
                    target_files=[],
                    writable_paths=["*"],
                )
            ],
        )
        task_b = create_task(
            task_id="task-b",
            description="Task in repo-b",
            worktree="/repos/beta",
            branch="duo/task-b",
            base_commit="bbb",
            subtasks=[
                Subtask(
                    step_id=1,
                    description="s",
                    target_files=[],
                    writable_paths=["*"],
                )
            ],
        )
        assert task_a.worktree == "/repos/alpha"
        assert task_b.worktree == "/repos/beta"
        loaded_a = load_task("task-a")
        loaded_b = load_task("task-b")
        assert loaded_a is not None and loaded_a.worktree == "/repos/alpha"
        assert loaded_b is not None and loaded_b.worktree == "/repos/beta"

    def test_list_shows_worktree_in_json(self, runner: CliRunner):
        """duo list --json-output includes worktree for multi-project visibility."""
        create_task(
            task_id="proj-x",
            description="X",
            worktree="/projects/x",
            branch="duo/proj-x",
            base_commit="xxx",
            subtasks=[
                Subtask(
                    step_id=1,
                    description="s",
                    target_files=[],
                    writable_paths=["*"],
                )
            ],
        )
        create_task(
            task_id="proj-y",
            description="Y",
            worktree="/projects/y",
            branch="duo/proj-y",
            base_commit="yyy",
            subtasks=[
                Subtask(
                    step_id=1,
                    description="s",
                    target_files=[],
                    writable_paths=["*"],
                )
            ],
        )
        result = runner.invoke(main, ["list", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        worktrees = {d["id"]: d["worktree"] for d in data}
        assert worktrees["proj-x"] == "/projects/x"
        assert worktrees["proj-y"] == "/projects/y"

    def test_task_names_can_coexist_with_prefix(self):
        """Prefixed names (e.g. 'repoA-fix', 'repoB-fix') coexist."""
        t1 = create_task(
            task_id="repoA-fix",
            description="Fix for A",
            worktree="/repos/a",
            branch="duo/repoA-fix",
            base_commit="aaa",
            subtasks=[
                Subtask(
                    step_id=1,
                    description="s",
                    target_files=[],
                    writable_paths=["*"],
                )
            ],
        )
        t2 = create_task(
            task_id="repoB-fix",
            description="Fix for B",
            worktree="/repos/b",
            branch="duo/repoB-fix",
            base_commit="bbb",
            subtasks=[
                Subtask(
                    step_id=1,
                    description="s",
                    target_files=[],
                    writable_paths=["*"],
                )
            ],
        )
        assert t1.id != t2.id
        assert load_task("repoA-fix") is not None
        assert load_task("repoB-fix") is not None

    def test_kill_does_not_affect_other_tasks(self, runner: CliRunner, tmp_path: Path):
        """Killing one task leaves other similarly-named tasks intact."""
        _make_task("alpha-fix")
        task_beta = _make_task("beta-fix")
        wt_dir = tmp_path / "alpha_wt"
        wt_dir.mkdir()
        alpha = load_task("alpha-fix")
        assert alpha is not None
        alpha.worktree = str(wt_dir)
        save_task(alpha)

        with patch("duo.cli.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="worktree /main\n  branch refs/heads/main\n\n",
                stderr="",
            )
            result = runner.invoke(main, ["kill", "alpha-fix"])
            assert result.exit_code == 0

        # beta-fix must still exist and be unchanged
        beta_loaded = load_task("beta-fix")
        assert beta_loaded is not None
        assert beta_loaded.id == task_beta.id
        assert beta_loaded.status == TaskStatus.CREATED


# ---------------------------------------------------------------------------
# CEO focus commands
# ---------------------------------------------------------------------------


class TestCliBranchGapsBatch1:
    """Close easy cli.py branch gaps: merge JSON, stop JSON, inspect display."""

    # -- merge --json-output with fetch failure (807→810) --
    def test_merge_json_fetch_fails(self, runner: CliRunner, tmp_path: Path):
        """merge --json-output suppresses fetch warning (branch 807→810)."""
        task = _make_task("merge-jf")
        task.status = TaskStatus.COMPLETED
        wt_dir = tmp_path / "wt_merge_jf"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        task.branch = "duo/merge-jf"
        save_task(task)

        worktree_base = str(tmp_path / "wt_base")

        def mock_run(args, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            if args[:3] == ["git", "fetch", "origin"]:
                m.returncode = 1
                m.stderr = "network error"
                return m
            if args[:3] == ["git", "worktree", "list"]:
                m.stdout = (
                    f"worktree /main/repo\n\nworktree {worktree_base}/merge-jf\n\n"
                )
            return m

        with (
            patch("duo.cli.subprocess.run", side_effect=mock_run),
            patch("duo.cli._helpers.get_config", return_value=worktree_base),
        ):
            result = runner.invoke(main, ["merge", "merge-jf", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["merged"] is True
        # No text warning in JSON mode
        assert "Warning" not in result.output

    # -- merge --json-output with rebase abort failure (814→819) --
    def test_merge_json_rebase_abort_fails(self, runner: CliRunner, tmp_path: Path):
        """merge --json-output suppresses abort warning (branch 814→819)."""
        task = _make_task("merge-ja")
        task.status = TaskStatus.COMPLETED
        wt_dir = tmp_path / "wt_merge_ja"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        task.branch = "duo/merge-ja"
        save_task(task)

        def mock_run(args, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            if args[:3] == ["git", "fetch", "origin"]:
                return m
            if args[:2] == ["git", "rebase"] and "--abort" not in args:
                m.returncode = 1
                m.stderr = "CONFLICT in file.py"
                return m
            if args == ["git", "rebase", "--abort"]:
                m.returncode = 1
                m.stderr = "abort failed"
                return m
            return m

        with patch("duo.cli.subprocess.run", side_effect=mock_run):
            result = runner.invoke(main, ["merge", "merge-ja", "--json-output"])
        assert result.exit_code != 0
        # Should NOT have plain-text warning about abort
        assert "could not abort rebase" not in result.output

    # -- stop --json-output kill_pane fails (923→926) --
    def test_stop_json_kill_pane_fails(self, runner: CliRunner):
        """stop --json-output suppresses pane kill warning (branch 923→926)."""
        task = _make_task("stop-jpf")
        task.status = TaskStatus.RUNNING
        task.pane_label = "test-jpf"
        save_task(task)

        with (
            patch("duo.transport.kill_pane", return_value=False),
            patch("duo.transport.cleanup_pane_state"),
        ):
            result = runner.invoke(main, ["stop", "stop-jpf", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["stopped"] is True
        # No text warning in JSON mode
        assert "failed to kill pane" not in result.output

    # -- inspect result with empty files_changed (1612→1615) --
    def test_inspect_result_empty_files_changed(self, runner: CliRunner):
        """inspect shows result without files_changed line when empty (1612→1615)."""
        from duo.protocol import write_json

        task = _make_task("res-nofiles")
        task.step_dir(1).mkdir(parents=True, exist_ok=True)
        write_json(
            task.result_path(1, 1),
            {
                "step": 1,
                "attempt": 1,
                "incarnation": "inc1",
                "status": "done",
                "files_changed": [],
                "summary": "Nothing changed",
                "reason": "",
            },
        )

        result = runner.invoke(main, ["inspect", "res-nofiles"])
        assert result.exit_code == 0
        assert "Result:" in result.output
        assert "Nothing changed" in result.output
        assert "Files changed:" not in result.output

    # -- inspect with empty journal (1739→1746) --
    def test_inspect_no_events(self, runner: CliRunner):
        """inspect with empty journal skips Recent Events section (1739→1746)."""
        task = _make_task("no-events")
        # Ensure journal file does not exist
        if task.journal_path.exists():
            task.journal_path.unlink()
        result = runner.invoke(main, ["inspect", "no-events"])
        assert result.exit_code == 0
        assert "PR Consumed:     0" in result.output
        assert "Recent Events" not in result.output

    # -- inspect --include-files: empty changed/untracked/diff (1750,1754,1758) --
    def test_inspect_include_files_all_empty(self, runner: CliRunner):
        """inspect --include-files with no changes (1750→1754, 1758→exit)."""
        _make_task("incl-empty")
        empty = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        def fake_run_git(args, cwd, *, check=True):
            return empty

        with (
            patch("duo.cli.inspect_cmd._run_git", side_effect=fake_run_git),
            patch("os.path.isdir", return_value=True),
        ):
            result = runner.invoke(main, ["inspect", "incl-empty", "--include-files"])
        assert result.exit_code == 0
        assert "Changed files" not in result.output
        assert "Untracked files" not in result.output
        assert "Diff preview" not in result.output


class TestCliBranchGapsBatch2:
    """Close more cli.py branch gaps: kill worktree, audit, recover JSON."""

    # -- kill: worktree list returns only base-path lines (978→986, 979→978) --
    def test_kill_no_main_worktree_found(self, runner: CliRunner, tmp_path: Path):
        """kill when all worktree lines contain base_path (978→986, 979→978)."""
        task = _make_task("kill-nomw")
        task.worktree = str(tmp_path / "gone")  # doesn't exist → also covers 990→1001
        save_task(task)

        base = "/my/worktrees"

        def mock_run(args, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            if args[:3] == ["git", "worktree", "list"]:
                # Only lines containing the base path → no main_worktree found
                m.stdout = (
                    f"worktree {base}/kill-nomw\n  branch refs/heads/duo/kill-nomw\n\n"
                )
            return m

        with (
            patch("duo.transport.kill_pane", return_value=True),
            patch("duo.transport.cleanup_pane_state"),
            patch("duo.cli.subprocess.run", side_effect=mock_run),
            patch("duo.config.get_config", return_value=base),
        ):
            result = runner.invoke(main, ["kill", "kill-nomw"])
        assert result.exit_code == 0
        assert "Killed" in result.output

    # -- kill: worktree doesn't exist, skip removal (990→1001) --
    def test_kill_worktree_gone(self, runner: CliRunner, tmp_path: Path):
        """kill skips worktree removal when path doesn't exist (990→1001)."""
        task = _make_task("kill-wgone")
        task.worktree = str(tmp_path / "vanished")
        save_task(task)

        def mock_run(args, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            if args[:3] == ["git", "worktree", "list"]:
                m.stdout = "worktree /main\n  branch refs/heads/main\n\n"
            return m

        with (
            patch("duo.transport.kill_pane", return_value=True),
            patch("duo.transport.cleanup_pane_state"),
            patch("duo.cli.subprocess.run", side_effect=mock_run),
        ):
            result = runner.invoke(main, ["kill", "kill-wgone"])
        assert result.exit_code == 0
        assert "Killed" in result.output

    # -- audit: task with no pr_consumed events (1306→exit) --
    def test_audit_no_pr_events(self, runner: CliRunner):
        """audit shows task with 0 PR consumed, no table (1306→exit)."""
        from duo.protocol import append_event

        task = _make_task("audit-nopr")
        # Write a non-pr event
        append_event(task, "task_started", {})

        result = runner.invoke(main, ["audit", "audit-nopr"])
        assert result.exit_code == 0
        assert "PR consumed: 0" in result.output
        assert "TIME" not in result.output  # no table header

    # -- resume --json-output: is_process_alive throws (2575→2581) --
    def test_resume_json_pane_check_error(self, runner: CliRunner):
        """resume --json-output suppresses pane check warning (2575→2581)."""
        task = _make_task("res-jpce")
        task.status = TaskStatus.RUNNING
        save_task(task)

        with (
            patch("duo.transport.is_process_alive", side_effect=OSError("no tmux")),
            patch("duo.commander.normalize_for_restart", return_value=True),
            patch("duo.commander.start_session"),
            patch("duo.commander.build_task_prompt", return_value="prompt"),
            patch("duo.commander.send_task_prompt"),
        ):
            result = runner.invoke(main, ["resume", "res-jpce", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["resumed"]) == 1
        assert "could not check pane" not in result.output

    # -- resume --json-output: pane alive + restart success (2594→2596) --
    def test_resume_json_restart_success(self, runner: CliRunner):
        """resume --json-output suppresses restart echo (2594→2596)."""
        task = _make_task("res-jrs")
        task.status = TaskStatus.RUNNING
        save_task(task)

        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.kill_pane", return_value=True),
            patch("duo.transport.cleanup_pane_state"),
            patch("duo.commander.restart_session"),
            patch("duo.commander.build_task_prompt", return_value="prompt"),
            patch("duo.commander.send_task_prompt"),
        ):
            result = runner.invoke(main, ["resume", "res-jrs", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["resumed"][0]["resumed"] is True
        assert data["resumed"][0]["method"] == "restart"
        assert "Resumed task" not in result.output

    # -- resume --json-output: replay prompt fails (2633→2570 loop) --
    def test_resume_json_replay_prompt_fails(self, runner: CliRunner):
        """resume --json-output suppresses prompt replay warning (2633→2570)."""
        task = _make_task("res-jrpf")
        task.status = TaskStatus.RUNNING
        save_task(task)

        with (
            patch("duo.transport.is_process_alive", return_value=False),
            patch("duo.commander.normalize_for_restart", return_value=True),
            patch("duo.commander.start_session"),
            patch("duo.commander.build_task_prompt", return_value="prompt"),
            patch("duo.commander.send_task_prompt", side_effect=OSError("tmux dead")),
        ):
            result = runner.invoke(main, ["resume", "res-jrpf", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["resumed"][0]["resumed"] is True
        assert "could not replay prompt" not in result.output


class TestCliBranchGapsBatch3:
    """Close ceo-now, init, cleanup display gaps."""

    def test_init_config_already_exists(self, runner: CliRunner, tmp_path: Path):
        """init skips config.json creation when it already exists (1832→1837)."""
        from duo.cli import DUO_DIR

        # Create config.json before init
        config_path = DUO_DIR / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("{}", encoding="utf-8")

        result = runner.invoke(main, ["init"])
        assert result.exit_code == 0
        # config.json should NOT be in the "Created:" list since it already existed
        # (The init command may still succeed but just not mention creating config)

    # -- cleanup/doctor: TASKS_DIR doesn't exist → skip lock cleanup (2423→2429) --
    def test_doctor_auto_fix_tasks_dir_absent(self, monkeypatch):
        """_doctor_auto_fix skips lock scan when TASKS_DIR gone (2423→2429)."""
        import duo.cli.doctor as doctor_mod

        monkeypatch.setattr(doctor_mod, "TASKS_DIR", Path("/nonexistent/tasks"))
        # Also mock subprocess to avoid git calls
        with patch("duo.cli.doctor.subprocess.run", side_effect=OSError("no git")):
            fixed = doctor_mod._doctor_auto_fix()
        # Should succeed without error, just skip lock cleanup
        assert isinstance(fixed, list)

    # -- cleanup: orphan worktree removal succeeds (2463→2452) --
    def test_cleanup_orphan_worktree_removed(self, runner: CliRunner, tmp_path: Path):
        """cleanup successfully removes orphan worktree (2463→2452)."""
        task = _make_task("cleanup-orphan")
        task.status = TaskStatus.FAILED
        save_task(task)

        def mock_run(args, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            if args[:3] == ["git", "worktree", "list"]:
                # List a worktree that matches TASKS_DIR naming but task is FAILED
                m.stdout = "worktree /tmp/wt/cleanup-orphan\n  branch refs/heads/duo/cleanup-orphan\n\n"
            if args[:3] == ["git", "worktree", "remove"]:
                m.returncode = 0
            return m

        with (
            patch("duo.cli.subprocess.run", side_effect=mock_run),
            patch("duo.config.get_config", return_value="/tmp/wt"),
        ):
            result = runner.invoke(main, ["cleanup", "--json-output"])
        assert result.exit_code == 0


class TestCliBranchGapsBatch5:
    """Close more cli.py branch gaps: dialog handling, ceo-dispatch, init."""

    def test_init_creates_config(self, runner: CliRunner, monkeypatch, tmp_path: Path):
        """init creates config.json when absent (1832→1837 True branch)."""
        import duo.cli as cli_mod
        import duo.config as config_mod
        import duo.protocol as proto_mod

        duo_dir = tmp_path / "dot-duo"
        monkeypatch.setattr(cli_mod, "DUO_DIR", duo_dir)
        monkeypatch.setattr(cli_mod, "TASKS_DIR", duo_dir / "tasks")
        monkeypatch.setattr(proto_mod, "DUO_DIR", duo_dir)
        monkeypatch.setattr(config_mod, "CONFIG_PATH", duo_dir / "config.json")

        # Create a fake git repo
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        result = runner.invoke(main, ["init", "--repo", str(repo)])
        assert result.exit_code == 0
        assert (duo_dir / "config.json").exists()

    # -- doctor orphan worktree removal fails (2463→2452 False branch) --
    def test_doctor_auto_fix_orphan_removal_fails(self, monkeypatch):
        """_doctor_auto_fix: orphan worktree removal fails (2463→2452 False)."""
        import duo.cli.doctor as doctor_mod

        calls = []

        def mock_run(args, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            calls.append(args)
            if args[:3] == ["git", "worktree", "list"]:
                m.stdout = (
                    "worktree /tmp/wt/duo-orphan\n  branch refs/heads/duo/orphan\n\n"
                )
            elif args[:3] == ["git", "worktree", "remove"]:
                m.returncode = 1
                m.stderr = "in use"
            return m

        with patch("duo.cli.doctor.subprocess.run", side_effect=mock_run):
            fixed = doctor_mod._doctor_auto_fix()
        assert "removed orphan worktree" not in " ".join(fixed)


class TestCliBranchGapsBatch6:
    """Batch 6: close more branch gaps in cli.py."""

    # -- 1832→1837: init when config.json already exists (False branch) --
    def test_init_config_already_exists(
        self, runner: CliRunner, monkeypatch, tmp_path: Path
    ):
        """init skips creating config.json when it already exists."""
        import duo.cli as cli_mod
        import duo.config as config_mod
        import duo.protocol as proto_mod

        duo_dir = tmp_path / "dot-duo"
        duo_dir.mkdir()
        config_path = duo_dir / "config.json"
        config_path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(cli_mod, "DUO_DIR", duo_dir)
        monkeypatch.setattr(cli_mod, "TASKS_DIR", duo_dir / "tasks")
        monkeypatch.setattr(proto_mod, "DUO_DIR", duo_dir)
        monkeypatch.setattr(config_mod, "CONFIG_PATH", config_path)

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        result = runner.invoke(main, ["init", "--repo", str(repo)])
        assert result.exit_code == 0
        # config.json should NOT appear in the created list
        assert "config.json" not in result.output

    # -- 349→351: open() raises OSError → lock_fd is still None --
    def test_start_lock_open_fails(
        self, runner: CliRunner, monkeypatch, tmp_path: Path
    ):
        """start command lock acquisition when open() itself fails."""
        import duo.cli as cli_mod

        monkeypatch.setattr(cli_mod, "TASKS_DIR", tmp_path)

        original_open = builtins.open

        def mock_open(path, *a, **kw):
            if str(path).endswith(".lock"):
                raise OSError("permission denied")
            return original_open(path, *a, **kw)

        with patch("builtins.open", side_effect=mock_open):
            result = runner.invoke(main, ["start", "locktest", "--desc", "test"])
        assert result.exit_code != 0
        assert "being created by another process" in result.output

    # -- 2959→2962 + 2970→2966: events tail with empty JSON (False branches) --
    def test_events_tail_empty_json(self, monkeypatch, tmp_path: Path):
        """events tail with event files containing invalid/empty JSON."""
        import duo.cli.events_cmd as events_mod

        monkeypatch.setattr(events_mod, "_WATCH_EVENTS_DIR", tmp_path)

        # Create an event file with empty/invalid content
        (tmp_path / "evt-001.json").write_text("", encoding="utf-8")

        runner = CliRunner()
        # events tail blocks forever, so we raise KeyboardInterrupt after first loop
        call_count = [0]
        original_sleep = time.sleep

        def mock_sleep(s):
            call_count[0] += 1
            if call_count[0] >= 1:
                raise KeyboardInterrupt
            original_sleep(0)

        with patch("time.sleep", side_effect=mock_sleep):
            result = runner.invoke(main, ["events", "tail", "-n", "5"])
        # The empty JSON files should be skipped (data is falsy)
        assert "Stopped" in result.output


class TestCliBranchGapsBatch7:
    """Batch 7: close the final 5 branch gaps in cli.py."""

    # -- 96→87: formatter section with no valid commands (empty rows) --
    def test_help_formatter_empty_section(self, runner: CliRunner, monkeypatch):
        """Help formatter skips sections where all commands are None."""
        import duo.cli._helpers as helpers_mod

        original = helpers_mod._COMMAND_SECTIONS
        patched = dict(original)
        patched["Phantom"] = ["nonexistent-cmd-xyz"]
        monkeypatch.setattr(helpers_mod, "_COMMAND_SECTIONS", patched)

        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        # "Phantom" section should not appear in output
        assert "Phantom" not in result.output

    # -- 2970→2966: watch loop new file with empty JSON (False branch) --
    def test_events_tail_loop_empty_json(self, monkeypatch, tmp_path: Path):
        """events tail while-loop skips new files with empty JSON."""
        import duo.cli.events_cmd as events_mod

        monkeypatch.setattr(events_mod, "_WATCH_EVENTS_DIR", tmp_path)

        call_count = [0]

        def mock_sleep(s):
            call_count[0] += 1
            if call_count[0] == 1:
                # Create a new file during the loop with empty content
                (tmp_path / "evt-new.json").write_text("{}", encoding="utf-8")
            elif call_count[0] >= 2:
                raise KeyboardInterrupt

        runner = CliRunner()
        with patch("time.sleep", side_effect=mock_sleep):
            result = runner.invoke(main, ["events", "tail", "-n", "0"])
        assert "Stopped" in result.output

    def test_think_finalize_plan_not_exist_yet(self, monkeypatch, tmp_path: Path):
        """_think_finalize waits when plan.md doesn't exist yet."""
        import duo.cli.think_cmd as think_mod

        # time.time: first call sets deadline, then loop enters, then exceeds
        time_values = iter([100.0, 100.0, 100.0, 200.0, 300.0])

        with (
            patch("duo.thinking.ensure_pane", return_value="think-pane"),
            patch("duo.thinking.thinking_dir", return_value=tmp_path),
            patch("duo.thinking.wait_for_response_stable", return_value="idle"),
            patch("duo.transport.type_text"),
            patch("duo.transport.send_keys"),
            patch("time.time", side_effect=lambda: next(time_values)),
            patch("time.sleep"),
        ):
            with pytest.raises(DuoUserError, match="didn't produce plan.md"):
                think_mod._think_finalize("test-think")


# ---------------------------------------------------------------------------
# duo go
# ---------------------------------------------------------------------------


class TestDuoGo:
    """Tests for the duo go one-command setup."""

    def test_no_tmux_error(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        """duo go outside tmux gives clear error."""
        monkeypatch.delenv("TMUX", raising=False)
        result = runner.invoke(main, ["go", "--repo", str(tmp_path)])
        assert result.exit_code != 0
        assert "tmux" in result.output.lower()

    def test_auto_git_init(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        """duo go in a non-git dir auto-initializes git."""
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")

        with (
            patch("subprocess.run") as mock_run,
            patch("duo.commander.write_project_claude_md"),
            patch("duo.protocol.load_go_session", return_value=None),
            patch("duo.protocol.save_go_session"),
            patch("duo.transport.name_pane"),
            patch("duo.transport.send_shell_command"),
            patch("duo.transport.wait_for_idle", return_value=True),
            patch("duo.transport.read_pane", return_value="❯"),
            patch("duo.transport.is_at_main_prompt", return_value=True),
            patch("duo.transport.split_window_horizontal", return_value="%42"),
            patch("duo.config.get_config", return_value=False),
            patch("os.execvp"),
            patch("os.chdir"),
            patch("time.sleep"),
        ):
            from unittest.mock import MagicMock

            git_init = MagicMock(returncode=0, stdout="", stderr="")
            mock_run.return_value = git_init

            result = runner.invoke(main, ["go", "--repo", str(tmp_path)])
            assert result.exit_code == 0
            assert "git init" in result.output.lower()

    def test_happy_path(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        """duo go in a proper git+tmux environment succeeds."""
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".duo").mkdir()

        with (
            patch("duo.commander.write_project_claude_md"),
            patch("duo.protocol.load_go_session", return_value=None),
            patch("duo.protocol.save_go_session") as mock_save,
            patch("duo.transport.name_pane"),
            patch("duo.transport.send_shell_command"),
            patch("duo.transport.wait_for_idle", return_value=True),
            patch("duo.transport.read_pane", return_value="❯"),
            patch("duo.transport.is_at_main_prompt", return_value=True),
            patch("duo.transport.split_window_horizontal", return_value="%42"),
            patch("duo.config.get_config", return_value=False),
            patch("os.execvp") as mock_exec,
            patch("os.chdir"),
            patch("time.sleep"),
        ):
            result = runner.invoke(main, ["go", "--repo", str(tmp_path)])
            assert result.exit_code == 0
            assert "launching" in result.output.lower()
            mock_exec.assert_called_once()
            mock_save.assert_called_once()

    def test_resume_existing_pane(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        """duo go reuses existing standby pane from go-session."""
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".duo").mkdir()

        existing_session = {
            "pane_label": "duo-copilot-standby",
            "repo_root": str(tmp_path),
            "copilot_pane": "%77",
            "tmux_env": "/tmp/tmux-1000/default,12345,0",
            "started_at": "2024-01-01T00:00:00Z",
        }

        with (
            patch("duo.commander.write_project_claude_md"),
            patch("duo.protocol.load_go_session", return_value=existing_session),
            patch("duo.protocol.save_go_session"),
            patch("duo.transport.is_pane_alive", return_value=True),
            patch("duo.config.get_config", return_value=False),
            patch("os.execvp"),
            patch("os.chdir"),
        ):
            result = runner.invoke(main, ["go", "--repo", str(tmp_path)])
            assert result.exit_code == 0
            assert "reusing" in result.output.lower()

    def test_resume_dead_pane_creates_new(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """duo go creates new pane when saved pane is dead."""
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".duo").mkdir()

        existing_session = {
            "pane_label": "duo-copilot-standby",
            "repo_root": str(tmp_path),
            "copilot_pane": "%dead",
            "tmux_env": "/tmp/tmux-1000/default,12345,0",
            "started_at": "2024-01-01T00:00:00Z",
        }

        with (
            patch("duo.commander.write_project_claude_md"),
            patch("duo.protocol.load_go_session", return_value=existing_session),
            patch("duo.protocol.save_go_session"),
            patch("duo.transport.is_pane_alive", return_value=False),
            patch("duo.transport.name_pane"),
            patch("duo.transport.send_shell_command"),
            patch("duo.transport.wait_for_idle", return_value=True),
            patch("duo.transport.read_pane", return_value="❯"),
            patch("duo.transport.is_at_main_prompt", return_value=True),
            patch("duo.transport.split_window_horizontal", return_value="%99"),
            patch("duo.config.get_config", return_value=False),
            patch("os.execvp"),
            patch("os.chdir"),
            patch("time.sleep"),
        ):
            result = runner.invoke(main, ["go", "--repo", str(tmp_path)])
            assert result.exit_code == 0
            assert "reusing" not in result.output.lower()

    def test_split_window_failure(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        """duo go handles split-window failure."""
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".duo").mkdir()

        with (
            patch("duo.commander.write_project_claude_md"),
            patch("duo.protocol.load_go_session", return_value=None),
            patch("duo.config.get_config", return_value=False),
            patch(
                "duo.transport.split_window_horizontal",
                side_effect=RuntimeError("tmux split-window failed: no space"),
            ),
        ):
            result = runner.invoke(main, ["go", "--repo", str(tmp_path)])
            assert result.exit_code != 0

    def test_bypass_permissions_adds_flags(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """duo go passes --dangerously-skip-permissions when bypass enabled."""
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".duo").mkdir()

        def config_side_effect(key):
            if key == "bypass_permissions":
                return True
            if key == "copilot_model":
                return "claude-sonnet-4-5"
            return key == "auto_allow_all"

        with (
            patch("duo.commander.write_project_claude_md"),
            patch("duo.protocol.load_go_session", return_value=None),
            patch("duo.protocol.save_go_session"),
            patch("duo.transport.name_pane"),
            patch("duo.transport.send_shell_command"),
            patch("duo.transport.wait_for_idle", return_value=True),
            patch("duo.transport.read_pane", return_value="❯"),
            patch("duo.transport.is_at_main_prompt", return_value=True),
            patch("duo.transport.split_window_horizontal", return_value="%42"),
            patch("duo.config.get_config", side_effect=config_side_effect),
            patch("os.execvp") as mock_exec,
            patch("os.chdir"),
            patch("time.sleep"),
        ):
            result = runner.invoke(main, ["go", "--repo", str(tmp_path)])
            assert result.exit_code == 0
            mock_exec.assert_called_once_with(
                "claude", ["claude", "--dangerously-skip-permissions"]
            )

    def test_copilot_not_at_prompt_continues(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """duo go continues even if Copilot isn't at prompt."""
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".duo").mkdir()

        with (
            patch("duo.commander.write_project_claude_md"),
            patch("duo.protocol.load_go_session", return_value=None),
            patch("duo.protocol.save_go_session"),
            patch("duo.transport.name_pane"),
            patch("duo.transport.send_shell_command"),
            patch("duo.transport.wait_for_idle", return_value=True),
            patch("duo.transport.read_pane", return_value="loading..."),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.split_window_horizontal", return_value="%42"),
            patch("duo.config.get_config", return_value=False),
            patch("os.execvp"),
            patch("os.chdir"),
            patch("time.sleep"),
        ):
            result = runner.invoke(main, ["go", "--repo", str(tmp_path)])
            assert result.exit_code == 0
            assert "not at prompt" in result.output.lower()

    def test_copilot_slow_startup_continues(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """duo go continues even if wait_for_idle times out."""
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".duo").mkdir()

        with (
            patch("duo.commander.write_project_claude_md"),
            patch("duo.protocol.load_go_session", return_value=None),
            patch("duo.protocol.save_go_session"),
            patch("duo.transport.name_pane"),
            patch("duo.transport.send_shell_command"),
            patch("duo.transport.wait_for_idle", return_value=False),
            patch("duo.transport.split_window_horizontal", return_value="%42"),
            patch("duo.config.get_config", return_value=False),
            patch("os.execvp"),
            patch("os.chdir"),
            patch("time.sleep"),
        ):
            result = runner.invoke(main, ["go", "--repo", str(tmp_path)])
            assert result.exit_code == 0
            assert "slow" in result.output.lower() or "loading" in result.output.lower()

    def test_git_init_failure(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        """duo go fails gracefully when git init fails."""
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")

        with patch("subprocess.run") as mock_run:
            from unittest.mock import MagicMock

            fail = MagicMock(returncode=1, stdout="", stderr="permission denied")
            mock_run.return_value = fail

            result = runner.invoke(main, ["go", "--repo", str(tmp_path)])
            assert result.exit_code != 0
            assert "git init failed" in result.output.lower()

    def test_pane_check_timeout(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        """duo go handles timeout when checking existing pane."""
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".duo").mkdir()

        existing_session = {
            "pane_label": "duo-copilot-standby",
            "repo_root": str(tmp_path),
            "copilot_pane": "%timeout",
            "started_at": "2024-01-01T00:00:00Z",
        }

        with (
            patch("duo.commander.write_project_claude_md"),
            patch("duo.protocol.load_go_session", return_value=existing_session),
            patch("duo.protocol.save_go_session"),
            patch("duo.transport.is_pane_alive", return_value=False),
            patch("duo.transport.name_pane"),
            patch("duo.transport.send_shell_command"),
            patch("duo.transport.wait_for_idle", return_value=True),
            patch("duo.transport.read_pane", return_value="❯"),
            patch("duo.transport.is_at_main_prompt", return_value=True),
            patch("duo.transport.split_window_horizontal", return_value="%new"),
            patch("duo.config.get_config", return_value=False),
            patch("os.execvp"),
            patch("os.chdir"),
            patch("time.sleep"),
        ):
            result = runner.invoke(main, ["go", "--repo", str(tmp_path)])
            assert result.exit_code == 0
            assert "reusing" not in result.output.lower()

    def test_split_window_timeout(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        """duo go handles split-window timeout."""
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".duo").mkdir()

        with (
            patch(
                "duo.transport.split_window_horizontal",
                side_effect=RuntimeError("tmux split-window timed out"),
            ),
            patch("duo.commander.write_project_claude_md"),
            patch("duo.protocol.load_go_session", return_value=None),
        ):
            result = runner.invoke(main, ["go", "--repo", str(tmp_path)])
            assert result.exit_code != 0
            assert "timed out" in result.output.lower()

    def test_name_pane_failure_cleanup(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """duo go cleans up orphaned pane when name_pane fails."""
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".duo").mkdir()

        with (
            patch("duo.transport.split_window_horizontal", return_value="%orphan"),
            patch("duo.transport.kill_pane", return_value=True) as mock_kill,
            patch("duo.commander.write_project_claude_md"),
            patch("duo.protocol.load_go_session", return_value=None),
            patch("duo.transport.name_pane", side_effect=RuntimeError("name failed")),
            patch("duo.config.get_config", return_value=False),
        ):
            result = runner.invoke(main, ["go", "--repo", str(tmp_path)])
            assert result.exit_code != 0
            assert "failed to name" in result.output.lower()
            mock_kill.assert_called_once_with("%orphan")

    def test_copilot_start_send_failure_continues(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """duo go continues when send_shell_command fails during Copilot start."""
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".duo").mkdir()

        with (
            patch("duo.commander.write_project_claude_md"),
            patch("duo.protocol.load_go_session", return_value=None),
            patch("duo.protocol.save_go_session"),
            patch("duo.transport.name_pane"),
            patch(
                "duo.transport.send_shell_command",
                side_effect=RuntimeError("pane not found"),
            ),
            patch("duo.transport.wait_for_idle", return_value=True),
            patch("duo.transport.read_pane", return_value="❯"),
            patch("duo.transport.is_at_main_prompt", return_value=True),
            patch("duo.transport.split_window_horizontal", return_value="%42"),
            patch("duo.config.get_config", return_value=False),
            patch("os.execvp"),
            patch("os.chdir"),
            patch("time.sleep"),
        ):
            result = runner.invoke(main, ["go", "--repo", str(tmp_path)])
            assert result.exit_code == 0
            assert "Failed to start Copilot" in result.output


class TestMainModule:
    """Tests for ``python -m duo`` entry point."""

    def test_main_module_calls_cli_main(self) -> None:
        with patch("duo.cli.main") as mock_main:
            import runpy

            runpy.run_module("duo", run_name="__main__", alter_sys=False)
            mock_main.assert_called_once()


class TestCliEdgeCases:
    """Edge case value coverage for CLI commands."""

    def test_load_task_or_fail_suggests_similar(self, runner: CliRunner):
        """When task not found, similar task names are suggested."""
        _make_task("my-auth-task")
        result = runner.invoke(main, ["status", "auth"])
        assert result.exit_code != 0
        assert "my-auth-task" in result.output

    def test_load_task_or_fail_no_similar(self, runner: CliRunner):
        """When no similar tasks exist, generic fix message shown."""
        result = runner.invoke(main, ["status", "zzz-nonexistent"])
        assert result.exit_code != 0
        assert "duo list" in result.output

    def test_batch_empty_dict(self, runner: CliRunner, tmp_path: Path):
        """batch with {} (no 'tasks' key) shows error."""
        f = tmp_path / "empty.json"
        f.write_text("{}")
        result = runner.invoke(main, ["batch", str(f)])
        assert result.exit_code != 0
        assert "tasks" in result.output.lower()

    def test_batch_tasks_is_string(self, runner: CliRunner, tmp_path: Path):
        """batch with tasks: 'not a list' shows error."""
        f = tmp_path / "bad.json"
        f.write_text(json.dumps({"tasks": "not a list"}))
        result = runner.invoke(main, ["batch", str(f)])
        assert result.exit_code != 0
        assert "must be a list" in result.output

    def test_batch_tasks_contains_non_dict(self, runner: CliRunner, tmp_path: Path):
        """batch with tasks containing non-dict items shows error."""
        f = tmp_path / "bad.json"
        f.write_text(json.dumps({"tasks": ["string-item", 42]}))
        result = runner.invoke(main, ["batch", str(f)])
        assert result.exit_code != 0
        assert "must be a JSON object" in result.output

    def test_parse_age_zero_rejected(self, runner: CliRunner):
        """cleanup with --age 0d is rejected."""
        result = runner.invoke(main, ["cleanup", "--age", "0d", "--force"])
        assert result.exit_code != 0
        assert "must be > 0" in result.output

    def test_diff_no_changes_message(self, runner: CliRunner, tmp_path: Path):
        """diff with no changes shows appropriate message."""
        task = _make_task("diff-empty")
        task.worktree = str(tmp_path)
        save_task(task)
        with patch(
            "duo.cli._run_git",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            ),
        ):
            result = runner.invoke(main, ["diff", "diff-empty"])
        assert result.exit_code == 0
        assert "No changes" in result.output


# --- Architectural guard tests ---


class TestArchitecturalGuards:
    """Prevent regression toward God File and other anti-patterns."""

    def test_cli_py_line_count(self):
        """cli package must not grow back into a God File."""
        import pathlib

        cli_path = pathlib.Path(__file__).parent.parent / "src" / "duo" / "cli"
        total = 0
        for py in cli_path.glob("**/*.py"):
            total += py.read_text().count("\n")
        assert total < 6000, f"cli/ package is {total} lines — split before it grows"

    def test_transport_py_line_count(self):
        """transport.py must stay under control."""
        import pathlib

        path = pathlib.Path(__file__).parent.parent / "src" / "duo" / "transport.py"
        lines = path.read_text().count("\n")
        assert lines < 2500, f"transport.py is {lines} lines — split if growing"

    def test_no_github_workflows(self):
        """Never create .github/workflows/ — causes email floods."""
        import pathlib

        workflows = pathlib.Path(__file__).parent.parent / ".github" / "workflows"
        assert not workflows.exists(), "Do NOT create .github/workflows/"

    def test_command_count_bounded(self):
        """Top-level command count should not silently grow."""
        from duo.cli import main

        top_level = len(main.commands)
        assert top_level <= 40, f"Too many top-level commands: {top_level}"
