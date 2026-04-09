"""Tests for duo.verifier — quality-gate checks."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from duo.protocol import (
    StepResult,
    Subtask,
    create_task,
)
from duo.verifier import (
    Correction,
    Pass,
    _check_acceptance,
    _check_secret_leak,
    _check_security_scope,
    _check_task_scope,
    _check_untracked,
    git_diff,
    git_diff_names,
    git_untracked,
    run_in_worktree,
    verify_step,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_tasks_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect TASKS_DIR to a temporary directory for every test."""
    monkeypatch.setattr("duo.protocol.TASKS_DIR", tmp_path / "tasks")


def _make_subtask(
    step_id: int = 1,
    *,
    writable_paths: list[str] | None = None,
    target_files: list[str] | None = None,
    acceptance: str = "",
) -> Subtask:
    return Subtask(
        step_id=step_id,
        description=f"step-{step_id}",
        target_files=target_files or ["main.py"],
        writable_paths=writable_paths or ["src/*"],
        acceptance=acceptance,
    )


def _make_task(subtasks: list[Subtask] | None = None, **kw):
    defaults = dict(
        task_id="test-task",
        description="test",
        worktree="/fake/worktree",
        branch="main",
        base_commit="abc123",
        subtasks=subtasks or [_make_subtask()],
    )
    defaults.update(kw)
    return create_task(**defaults)


def _mock_proc(stdout: str = "", returncode: int = 0):
    proc = MagicMock()
    proc.stdout = stdout
    proc.returncode = returncode
    return proc


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


class TestGitDiffNames:
    @patch("duo.verifier.subprocess.run")
    def test_returns_set_of_paths(self, mock_run):
        mock_run.return_value = _mock_proc("src/a.py\nsrc/b.py\n")
        result = git_diff_names("/w")
        assert result == {"src/a.py", "src/b.py"}

    @patch("duo.verifier.subprocess.run")
    def test_empty_diff(self, mock_run):
        mock_run.return_value = _mock_proc("")
        assert git_diff_names("/w") == set()

    @patch("duo.verifier.subprocess.run")
    def test_strips_blank_lines(self, mock_run):
        mock_run.return_value = _mock_proc("a.py\n\nb.py\n\n")
        assert git_diff_names("/w") == {"a.py", "b.py"}

    @patch("duo.verifier.subprocess.run")
    def test_raises_on_git_failure(self, mock_run):
        proc = _mock_proc("")
        proc.returncode = 128
        proc.stderr = "fatal: not a git repository"
        mock_run.return_value = proc
        with pytest.raises(RuntimeError, match="git diff --name-only failed"):
            git_diff_names("/w")


class TestGitDiff:
    @patch("duo.verifier.subprocess.run")
    def test_returns_raw_stdout(self, mock_run):
        mock_run.return_value = _mock_proc("diff --git a/f b/f\n+hello\n")
        assert git_diff("/w") == "diff --git a/f b/f\n+hello\n"

    @patch("duo.verifier.subprocess.run")
    def test_raises_on_failure(self, mock_run):
        """git_diff raises RuntimeError when git diff fails (line 62)."""
        proc = _mock_proc("")
        proc.returncode = 1
        proc.stderr = "fatal: bad revision"
        mock_run.return_value = proc
        with pytest.raises(RuntimeError, match="git diff failed"):
            git_diff("/w")


class TestGitUntracked:
    @patch("duo.verifier.subprocess.run")
    def test_returns_list(self, mock_run):
        mock_run.return_value = _mock_proc("new.txt\nother.py\n")
        assert git_untracked("/w") == ["new.txt", "other.py"]

    @patch("duo.verifier.subprocess.run")
    def test_empty(self, mock_run):
        mock_run.return_value = _mock_proc("")
        assert git_untracked("/w") == []

    @patch("duo.verifier.subprocess.run")
    def test_raises_on_failure(self, mock_run):
        """git_untracked raises RuntimeError when git ls-files fails (line 75)."""
        proc = _mock_proc("")
        proc.returncode = 1
        proc.stderr = "fatal: not a git repository"
        mock_run.return_value = proc
        with pytest.raises(RuntimeError, match="git ls-files failed"):
            git_untracked("/w")


# ---------------------------------------------------------------------------
# run_in_worktree
# ---------------------------------------------------------------------------


class TestRunInWorktree:
    def test_simple_command(self, tmp_path):
        """A basic command like 'echo hello' should succeed."""
        assert run_in_worktree(str(tmp_path), "echo hello") == 0

    def test_shell_metacharacters_not_interpreted(self, tmp_path):
        """Shell metacharacters must NOT be interpreted (no injection)."""
        marker = tmp_path / "injected.txt"
        # With shell=True this would execute the second command and create the file.
        # With shell=False via shlex.split, the semicollon and rest are passed
        # as literal arguments to echo, so the file must NOT be created.
        run_in_worktree(str(tmp_path), f"echo hello; touch {marker}")
        assert not marker.exists(), "Shell injection was executed!"

    def test_malformed_command_returns_127(self, tmp_path):
        """Unbalanced quotes in command return 127 instead of crashing."""
        assert run_in_worktree(str(tmp_path), 'echo "unterminated') == 127

    def test_missing_executable_returns_127(self, tmp_path):
        """Non-existent executable returns 127."""
        assert run_in_worktree(str(tmp_path), "nonexistent_binary_xyz") == 127

    def test_timeout_returns_124(self, tmp_path):
        """Command timeout returns exit code 124."""
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="sleep", timeout=300),
        ):
            assert run_in_worktree(str(tmp_path), "sleep 999") == 124


# ---------------------------------------------------------------------------
# _check_security_scope
# ---------------------------------------------------------------------------


class TestCheckSecurityScope:
    def test_files_within_writable_paths(self):
        task = _make_task()
        result = _check_security_scope(task, {"src/main.py"}, ["src/*"])
        assert result is None

    def test_file_outside_writable_paths(self):
        task = _make_task()
        result = _check_security_scope(task, {"etc/config"}, ["src/*"])
        assert isinstance(result, Correction)
        assert "etc/config" in result.reason

    def test_wildcard_star_matches_everything(self):
        task = _make_task()
        result = _check_security_scope(task, {"any/deep/path.py"}, ["*"])
        assert result is None

    def test_glob_pattern_matches(self):
        task = _make_task()
        result = _check_security_scope(task, {"src/main.py"}, ["src/*.py"])
        assert result is None

    def test_glob_pattern_rejects_mismatch(self):
        task = _make_task()
        result = _check_security_scope(task, {"src/main.js"}, ["src/*.py"])
        assert isinstance(result, Correction)

    def test_multiple_writable_paths(self):
        task = _make_task()
        paths = ["src/*", "tests/*"]
        assert _check_security_scope(task, {"src/a.py"}, paths) is None
        assert _check_security_scope(task, {"tests/b.py"}, paths) is None
        result = _check_security_scope(task, {"docs/c.md"}, paths)
        assert isinstance(result, Correction)

    def test_empty_changed_set(self):
        task = _make_task()
        assert _check_security_scope(task, set(), ["src/*"]) is None

    def test_first_violation_short_circuits(self):
        task = _make_task()
        result = _check_security_scope(
            task,
            {"bad1.txt", "bad2.txt"},
            ["src/*"],
        )
        assert isinstance(result, Correction)
        # Only the first (sorted) offender is reported
        assert "bad1.txt" in result.reason

    def test_empty_writable_paths_rejects_all(self):
        """With no writable paths, every changed file is rejected."""
        task = _make_task()
        result = _check_security_scope(task, {"src/main.py"}, [])
        assert isinstance(result, Correction)
        assert "src/main.py" in result.reason

    def test_empty_writable_paths_empty_changed(self):
        """Empty writable_paths + no changes = pass."""
        task = _make_task()
        result = _check_security_scope(task, set(), [])
        assert result is None


# ---------------------------------------------------------------------------
# _check_task_scope
# ---------------------------------------------------------------------------


class TestCheckTaskScope:
    def test_returns_none(self):
        """_check_task_scope never returns a Correction — soft warning only."""
        task = _make_task()
        result = _check_task_scope(task, {"outside.py"}, ["main.py"])
        assert result is None

    def test_no_warning_when_in_scope(self):
        task = _make_task()
        result = _check_task_scope(task, {"main.py"}, ["main.py"])
        assert result is None


# ---------------------------------------------------------------------------
# _check_secret_leak
# ---------------------------------------------------------------------------


class TestCheckSecretLeak:
    def test_no_added_lines(self):
        task = _make_task()
        diff = "--- a/f\n+++ b/f\n context line\n-removed line\n"
        assert _check_secret_leak(task, diff, ["API_KEY="]) is None

    def test_secret_in_added_line(self):
        task = _make_task()
        diff = "+API_KEY=abc123\n"
        result = _check_secret_leak(task, diff, ["API_KEY="])
        assert isinstance(result, Correction)
        assert "API_KEY=" in result.reason

    def test_secret_in_removed_line_is_ok(self):
        task = _make_task()
        diff = "-API_KEY=old_value\n"
        assert _check_secret_leak(task, diff, ["API_KEY="]) is None

    def test_secret_in_context_line_is_ok(self):
        task = _make_task()
        diff = " API_KEY=existing\n"
        assert _check_secret_leak(task, diff, ["API_KEY="]) is None

    def test_header_line_not_checked(self):
        task = _make_task()
        diff = "+++ b/API_KEY=value\n"
        assert _check_secret_leak(task, diff, ["API_KEY="]) is None

    def test_case_insensitive(self):
        task = _make_task()
        diff = "+api_key=secret\n"
        result = _check_secret_leak(task, diff, ["API_KEY="])
        assert isinstance(result, Correction)

    def test_multiple_patterns(self):
        task = _make_task()
        diff = "+password=hunter2\n"
        result = _check_secret_leak(task, diff, ["API_KEY=", "password="])
        assert isinstance(result, Correction)
        assert "password=" in result.reason

    def test_no_patterns(self):
        task = _make_task()
        diff = "+API_KEY=abc\n"
        assert _check_secret_leak(task, diff, []) is None

    def test_empty_diff(self):
        task = _make_task()
        assert _check_secret_leak(task, "", ["API_KEY="]) is None

    @pytest.mark.parametrize(
        "pattern,sample",
        [
            ("github_pat_", "+GITHUB_TOKEN=github_pat_abc123xyz"),
            ("ghp_", "+TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"),
            ("gho_", "+OAUTH=gho_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"),
            ("ghs_", "+SERVER_TOKEN=ghs_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"),
            ("ghr_", "+REFRESH=ghr_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"),
            ("sk-proj-", "+OPENAI_KEY=sk-proj-xxxxxxxxxxxxxxxxxxxxxxxxxxxx"),
            ("sk-ant-", "+ANTHROPIC_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxxxxxxxxxx"),
            ("AKIA", "+AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"),
            ("ASIA", "+AWS_ACCESS_KEY_ID=ASIAIOSFODNN7EXAMPLE"),
            ("sk_live_", "+STRIPE_KEY=sk_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"),
            ("sk_test_", "+STRIPE_TEST=sk_test_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"),
            ("xoxb-", "+SLACK_BOT=xoxb-xxxxxxxxxxxx-xxxxxxxxxxxx"),
            ("xoxp-", "+SLACK_USER=xoxp-xxxxxxxxxxxx-xxxxxxxxxxxx"),
            (
                "-----BEGIN RSA PRIVATE KEY",
                "+-----BEGIN RSA PRIVATE KEY-----",
            ),
            (
                "-----BEGIN EC PRIVATE KEY",
                "+-----BEGIN EC PRIVATE KEY-----",
            ),
            (
                "-----BEGIN OPENSSH PRIVATE KEY",
                "+-----BEGIN OPENSSH PRIVATE KEY-----",
            ),
        ],
    )
    def test_modern_token_patterns(self, pattern, sample):
        task = _make_task()
        diff = sample + "\n"
        result = _check_secret_leak(task, diff, [pattern])
        assert isinstance(result, Correction)
        assert pattern in result.reason


# ---------------------------------------------------------------------------
# _check_untracked
# ---------------------------------------------------------------------------


class TestCheckUntracked:
    @patch("duo.verifier.git_untracked", return_value=[])
    def test_no_untracked_files(self, _mock):
        task = _make_task()
        assert _check_untracked(task, "/w") is None

    @patch("duo.verifier.git_untracked", return_value=["stray.txt"])
    def test_untracked_files_exist(self, _mock):
        task = _make_task()
        result = _check_untracked(task, "/w")
        assert isinstance(result, Correction)
        assert "stray.txt" in result.reason


# ---------------------------------------------------------------------------
# _check_acceptance
# ---------------------------------------------------------------------------


class TestCheckAcceptance:
    def test_empty_acceptance_skips(self):
        task = _make_task()
        assert _check_acceptance(task, "/w", "") is None

    @patch("duo.verifier.run_in_worktree", return_value=0)
    def test_command_passes(self, _mock):
        task = _make_task()
        assert _check_acceptance(task, "/w", "make test") is None

    @patch("duo.verifier.run_in_worktree", return_value=1)
    def test_command_fails(self, _mock):
        task = _make_task()
        result = _check_acceptance(task, "/w", "make test")
        assert isinstance(result, Correction)
        assert "exit 1" in result.reason

    @patch("duo.verifier.run_in_worktree", return_value=2)
    def test_nonzero_exit_code(self, _mock):
        task = _make_task()
        result = _check_acceptance(task, "/w", "lint")
        assert isinstance(result, Correction)
        assert "exit 2" in result.reason


# ---------------------------------------------------------------------------
# verify_step
# ---------------------------------------------------------------------------


class TestVerifyStep:
    def _result(self, **kw) -> StepResult:
        defaults = dict(
            step=1,
            attempt=1,
            incarnation="abc",
            status="done",
            files_changed=[],
            summary="ok",
        )
        defaults.update(kw)
        return StepResult(**defaults)

    @patch("duo.verifier.git_untracked", return_value=[])
    @patch("duo.verifier.git_diff", return_value="")
    @patch("duo.verifier.git_diff_names", return_value=set())
    def test_all_checks_pass(self, _names, _diff, _untracked):
        task = _make_task()
        result = verify_step(task, self._result())
        assert isinstance(result, Pass)

    @patch("duo.verifier.git_untracked", return_value=[])
    @patch("duo.verifier.git_diff", return_value="")
    @patch("duo.verifier.git_diff_names", return_value={"etc/passwd"})
    def test_security_violation_short_circuits(self, _names, _diff, _untracked):
        task = _make_task()
        result = verify_step(task, self._result())
        assert isinstance(result, Correction)
        assert "Security violation" in result.reason

    def test_missing_subtask(self):
        task = _make_task()
        task.current_step = 99  # no subtask for step 99
        result = verify_step(task, self._result(step=99))
        assert isinstance(result, Correction)
        assert "No subtask" in result.reason

    @patch("duo.verifier.git_untracked", return_value=[])
    @patch("duo.verifier.git_diff", return_value="+token=secret\n")
    @patch("duo.verifier.git_diff_names", return_value={"src/a.py"})
    def test_secret_leak_rejects(self, _names, _diff, _untracked):
        task = _make_task()
        result = verify_step(task, self._result())
        assert isinstance(result, Correction)
        assert "Secret leak" in result.reason

    @patch("duo.verifier.git_untracked", return_value=["junk.tmp"])
    @patch("duo.verifier.git_diff", return_value="")
    @patch("duo.verifier.git_diff_names", return_value=set())
    def test_untracked_files_rejects(self, _names, _diff, _untracked):
        task = _make_task()
        result = verify_step(task, self._result())
        assert isinstance(result, Correction)
        assert "Untracked" in result.reason

    @patch("duo.verifier.run_in_worktree", return_value=1)
    @patch("duo.verifier.git_untracked", return_value=[])
    @patch("duo.verifier.git_diff", return_value="")
    @patch("duo.verifier.git_diff_names", return_value=set())
    def test_acceptance_failure_rejects(self, _names, _diff, _untracked, _run):
        subtask = _make_subtask(acceptance="pytest")
        task = _make_task(subtasks=[subtask])
        result = verify_step(task, self._result())
        assert isinstance(result, Correction)
        assert "Acceptance" in result.reason

    @patch(
        "duo.verifier.git_diff_names",
        side_effect=RuntimeError("git diff --name-only failed"),
    )
    def test_git_failure_returns_correction(self, _names):
        task = _make_task()
        result = verify_step(task, self._result())
        assert isinstance(result, Correction)
        assert "Git operation failed" in result.reason
