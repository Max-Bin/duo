"""CLI tests for lifecycle commands."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

import duo.cli
import duo.protocol
from duo.cli import (
    _create_worktree,
    main,
)
from duo.errors import DuoUserError
from duo.protocol import (
    Subtask,
    TaskStatus,
    create_task,
    load_task,
    save_task,
)


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

    def test_start_race_recheck_after_lock(
        self, runner: CliRunner, tmp_path: Path, make_task
    ):
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
            return make_task(name)  # re-check finds task

        with patch("duo.cli.lifecycle_cmd.load_task", side_effect=load_side_effect):
            result = runner.invoke(
                main, ["start", "race-task", "--repo", str(repo), "--desc", "t"]
            )
            assert result.exit_code != 0
            assert "already exists" in result.output


# ---------------------------------------------------------------------------
# kill command (error case)
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

    def test_kill_does_not_affect_other_tasks(
        self, runner: CliRunner, tmp_path: Path, make_task
    ):
        """Killing one task leaves other similarly-named tasks intact."""
        make_task("alpha-fix")
        task_beta = make_task("beta-fix")
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
