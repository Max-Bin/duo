"""CLI integration tests for duo.cli using Click's CliRunner."""

from __future__ import annotations

import builtins
import json
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
from duo.cli import (
    _fmt_ts,
    _parse_age,
    _safe_join,
    _validate_task_name,
    main,
)
from duo.errors import DuoUserError
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
