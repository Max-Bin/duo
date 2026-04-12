"""CLI tests for inspect commands."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import duo.cli
import duo.protocol
from duo.cli import (
    main,
)
from duo.protocol import (
    Subtask,
    create_task,
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
