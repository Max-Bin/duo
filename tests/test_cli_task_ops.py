"""CLI tests for task ops commands."""

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
            "duo.cli.task_ops_cmd.subprocess.run",
            MagicMock(return_value=MagicMock(returncode=0)),
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
        monkeypatch.setattr(
            "duo.cli.task_ops_cmd.subprocess.run", MagicMock(returncode=0)
        )
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
