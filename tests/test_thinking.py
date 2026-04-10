"""Tests for duo.thinking — pre-start brainstorming module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from duo.thinking import (
    _ensure_thinking_dir,
    _pane_alive,
    _pane_exists,
    _pane_label,
    _spawn_claude_pane,
    append_session_log,
    close_pane,
    ensure_pane,
    extract_response,
    list_sessions,
    thinking_dir,
    wait_for_response_stable,
    write_plan_template,
    write_thinking_claude_md,
)
from duo.transport import DialogKind

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_thinking_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect THINKING_DIR to a temp dir for test isolation."""
    fake_dir = tmp_path / "thinking"
    monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_dir)
    return fake_dir


# ---------------------------------------------------------------------------
# thinking_dir / _pane_label
# ---------------------------------------------------------------------------


class TestThinkingDir:
    def test_returns_path(self, tmp_path: Path) -> None:
        d = thinking_dir("my-app")
        assert d.name == "my-app"
        # Parent is the monkeypatched THINKING_DIR
        assert d.parent.name == "thinking"

    def test_pane_label(self) -> None:
        assert _pane_label("rate-limiter") == "think-rate-limiter"

    def test_ensure_thinking_dir_creates_nested(self) -> None:
        d = _ensure_thinking_dir("deep-session")
        assert d.is_dir()
        assert d.name == "deep-session"

    def test_ensure_thinking_dir_idempotent(self) -> None:
        d1 = _ensure_thinking_dir("idem")
        d2 = _ensure_thinking_dir("idem")
        assert d1 == d2
        assert d2.is_dir()


# ---------------------------------------------------------------------------
# write_thinking_claude_md
# ---------------------------------------------------------------------------


class TestWriteThinkingClaudeMd:
    def test_creates_dir_and_file(self) -> None:
        path = write_thinking_claude_md("my-app")
        assert path.exists()
        content = path.read_text()
        assert "Thinking Partner" in content
        assert "my-app" in content
        assert "Do NOT write code" in content

    def test_overwrites_existing(self) -> None:
        write_thinking_claude_md("x")
        write_thinking_claude_md("x")  # no error


# ---------------------------------------------------------------------------
# write_plan_template
# ---------------------------------------------------------------------------


class TestWritePlanTemplate:
    def test_creates_template(self) -> None:
        path = write_plan_template("my-app")
        assert path.exists()
        content = path.read_text()
        assert "# Plan: my-app" in content
        assert "## Goal" in content
        assert "Acceptance Criteria" in content


# ---------------------------------------------------------------------------
# _pane_exists / _pane_alive
# ---------------------------------------------------------------------------


class TestPaneChecks:
    def test_pane_exists_true(self) -> None:
        with patch("duo.transport.resolve_label", return_value="%42"):
            assert _pane_exists("think-x") is True

    def test_pane_exists_false_runtime(self) -> None:
        with patch("duo.transport.resolve_label", side_effect=RuntimeError("nope")):
            assert _pane_exists("think-x") is False

    def test_pane_exists_false_value(self) -> None:
        with patch("duo.transport.resolve_label", side_effect=ValueError("bad")):
            assert _pane_exists("think-x") is False

    def test_pane_alive_true(self) -> None:
        with patch("duo.transport.is_process_alive", return_value=True):
            assert _pane_alive("think-x") is True

    def test_pane_alive_false(self) -> None:
        with patch("duo.transport.is_process_alive", return_value=False):
            assert _pane_alive("think-x") is False


# ---------------------------------------------------------------------------
# _spawn_claude_pane
# ---------------------------------------------------------------------------


class TestSpawnClaudePane:
    def test_success(self) -> None:
        mock_run = MagicMock(
            return_value=MagicMock(returncode=0, stdout="%99\n", stderr="")
        )
        with (
            patch("subprocess.run", mock_run),
            patch("duo.transport.name_pane") as mock_name,
            patch("duo.transport.send_shell_command") as mock_cmd,
            patch("duo.transport.wait_for_idle", return_value=True),
            patch("time.sleep"),
        ):
            pane_id = _spawn_claude_pane("think-x", "/tmp/test")
            assert pane_id == "%99"
            mock_name.assert_called_once_with("%99", "think-x")
            assert mock_cmd.call_count == 2  # cd + claude

    def test_tmux_failure(self) -> None:
        mock_run = MagicMock(
            return_value=MagicMock(returncode=1, stdout="", stderr="no tmux")
        )
        with patch("subprocess.run", mock_run):
            with pytest.raises(RuntimeError, match="tmux running"):
                _spawn_claude_pane("think-x", "/tmp/test")

    def test_claude_start_failure(self) -> None:
        split_result = MagicMock(returncode=0, stdout="%99\n", stderr="")
        layout_result = MagicMock(returncode=0)

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            if "split-window" in cmd:
                return split_result
            if "select-layout" in cmd:
                return layout_result
            return MagicMock(returncode=0)

        with (
            patch("subprocess.run", side_effect=run_side_effect),
            patch("duo.transport.name_pane"),
            patch("duo.transport.send_shell_command", side_effect=RuntimeError("fail")),
            patch("duo.transport.kill_pane") as mock_kill,
            patch("time.sleep"),
        ):
            with pytest.raises(RuntimeError, match="Failed to start Claude Code"):
                _spawn_claude_pane("think-x", "/tmp/test")
            mock_kill.assert_called_once_with("%99")

    def test_claude_start_failure_cleanup_also_fails(self) -> None:
        """When send_shell_command fails AND kill_pane returns False, still propagate."""
        split_result = MagicMock(returncode=0, stdout="%99\n", stderr="")
        layout_result = MagicMock(returncode=0)

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            if "split-window" in cmd:
                return split_result
            if "select-layout" in cmd:
                return layout_result
            return MagicMock(returncode=0)

        with (
            patch("subprocess.run", side_effect=run_side_effect),
            patch("duo.transport.name_pane"),
            patch("duo.transport.send_shell_command", side_effect=RuntimeError("fail")),
            patch("duo.transport.kill_pane", return_value=False),
            patch("time.sleep"),
        ):
            with pytest.raises(RuntimeError, match="Failed to start Claude Code"):
                _spawn_claude_pane("think-x", "/tmp/test")


# ---------------------------------------------------------------------------
# ensure_pane
# ---------------------------------------------------------------------------


class TestEnsurePane:
    def test_creates_new_pane(self) -> None:
        with (
            patch("duo.thinking._pane_exists", return_value=False),
            patch("duo.thinking._spawn_claude_pane", return_value="%42") as mock_spawn,
        ):
            label = ensure_pane("my-app")
            assert label == "think-my-app"
            mock_spawn.assert_called_once()

    def test_reuses_alive_pane(self) -> None:
        # Pre-create files
        write_thinking_claude_md("my-app")
        write_plan_template("my-app")
        with (
            patch("duo.thinking._pane_exists", return_value=True),
            patch("duo.thinking._pane_alive", return_value=True),
        ):
            label = ensure_pane("my-app")
            assert label == "think-my-app"

    def test_recovers_dead_pane(self) -> None:
        write_thinking_claude_md("my-app")
        write_plan_template("my-app")
        with (
            patch("duo.thinking._pane_exists", return_value=True),
            patch("duo.thinking._pane_alive", return_value=False),
            patch("duo.transport.send_shell_command") as mock_cmd,
            patch("duo.transport.wait_for_idle", return_value=True),
        ):
            label = ensure_pane("my-app")
            assert label == "think-my-app"
            mock_cmd.assert_called_once_with("think-my-app", "claude")

    def test_writes_scaffold_files(self) -> None:
        with (
            patch("duo.thinking._pane_exists", return_value=False),
            patch("duo.thinking._spawn_claude_pane", return_value="%1"),
        ):
            ensure_pane("new-idea")
        tdir = thinking_dir("new-idea")
        assert (tdir / "CLAUDE.md").exists()
        assert (tdir / "plan-template.md").exists()


# ---------------------------------------------------------------------------
# wait_for_response_stable
# ---------------------------------------------------------------------------


class TestWaitForResponseStable:
    def test_idle_detected(self) -> None:
        """Two reads at main prompt with same hash → idle."""
        content = "❯ Type @\nshift+tab"
        with (
            patch("duo.transport.read_pane", return_value=content),
            patch("duo.transport._detect_dialog_kind", return_value=DialogKind.NONE),
            patch("duo.transport._is_at_main_prompt", return_value=True),
            patch("time.sleep"),
            patch("time.time", side_effect=[0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.5]),
        ):
            result = wait_for_response_stable(
                "think-x", timeout=10, stable_threshold=2.0
            )
            assert result == "idle"

    def test_dialog_detected(self) -> None:
        with (
            patch("duo.transport.read_pane", return_value="╭─ some dialog"),
            patch("duo.transport._detect_dialog_kind", return_value=DialogKind.OPTION),
            patch("duo.transport._is_at_main_prompt", return_value=False),
            patch("time.sleep"),
            patch("time.time", side_effect=[0, 0.5]),
        ):
            result = wait_for_response_stable("think-x", timeout=10)
            assert result == "dialog"

    def test_timeout(self) -> None:
        """Spinner active → never idle → timeout."""
        content = "◉ Processing..."
        call_count = 0

        def mock_time() -> float:
            nonlocal call_count
            call_count += 1
            return call_count * 5.0

        with (
            patch("duo.transport.read_pane", return_value=content),
            patch("duo.transport._detect_dialog_kind", return_value=DialogKind.NONE),
            patch("duo.transport._is_at_main_prompt", return_value=False),
            patch("time.sleep"),
            patch("time.time", side_effect=mock_time),
        ):
            result = wait_for_response_stable("think-x", timeout=2)
            assert result == "timeout"

    def test_spinner_resets_stability(self) -> None:
        """Spinner during processing resets the stable counter."""
        call_idx = 0
        contents = [
            "❯ Type @\nshift+tab",
            "◉ Processing...",
            "❯ Type @\nshift+tab",
        ]

        def mock_read_pane(label: str, lines: int) -> str:
            nonlocal call_idx
            idx = min(call_idx, len(contents) - 1)
            call_idx += 1
            return contents[idx]

        def mock_at_prompt(content: str) -> bool:
            return "❯" in content and "◉" not in content

        with (
            patch("duo.transport.read_pane", side_effect=mock_read_pane),
            patch("duo.transport._detect_dialog_kind", return_value=DialogKind.NONE),
            patch("duo.transport._is_at_main_prompt", side_effect=mock_at_prompt),
            patch("time.sleep"),
            patch("time.time", side_effect=[0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.5, 5.0, 8.0]),
        ):
            result = wait_for_response_stable(
                "think-x", timeout=10, stable_threshold=2.0
            )
            assert result == "idle"


# ---------------------------------------------------------------------------
# extract_response
# ---------------------------------------------------------------------------


class TestExtractResponse:
    def test_basic_delta(self) -> None:
        before = "line1\nline2\nline3"
        after = "line1\nline2\nline3\nHello from Claude!"
        result = extract_response(before, after, "my question")
        assert result == "Hello from Claude!"

    def test_filters_user_echo(self) -> None:
        before = "line1"
        after = "line1\nmy question\nThe answer is 42."
        result = extract_response(before, after, "my question")
        assert result == "The answer is 42."

    def test_filters_prompt_chars(self) -> None:
        before = "line1"
        after = "line1\n❯\nSome response\n>"
        result = extract_response(before, after, "q")
        assert result == "Some response"

    def test_filters_tool_headers(self) -> None:
        before = "line1"
        after = "line1\n● Read file.py\nThe file contains classes.\n● Grep results"
        result = extract_response(before, after, "q")
        assert result == "The file contains classes."

    def test_empty_delta(self) -> None:
        before = "same\nlines"
        after = "same\nlines"
        result = extract_response(before, after, "q")
        assert result == ""

    def test_diverging_lines_break(self) -> None:
        """When before/after lines differ mid-stream, break early."""
        before = "line1\noriginal\nline3"
        after = "line1\nchanged\nResponse text"
        result = extract_response(before, after, "q")
        assert "changed" in result or "Response text" in result

    def test_trims_blank_lines(self) -> None:
        before = "a"
        after = "a\n\n  \nreal content\n  \n"
        result = extract_response(before, after, "q")
        assert result == "real content"


# ---------------------------------------------------------------------------
# append_session_log
# ---------------------------------------------------------------------------


class TestAppendSessionLog:
    def test_creates_log(self) -> None:
        write_thinking_claude_md("logtest")  # ensure dir exists
        append_session_log("logtest", "hello", "world")
        log = thinking_dir("logtest") / "session.log"
        assert log.exists()
        content = log.read_text()
        assert "[user] hello" in content
        assert "[response]\nworld" in content

    def test_appends_to_existing(self) -> None:
        write_thinking_claude_md("logtest2")
        append_session_log("logtest2", "q1", "a1")
        append_session_log("logtest2", "q2", "a2")
        log = thinking_dir("logtest2") / "session.log"
        content = log.read_text()
        assert content.count("--- ask at") == 2


# ---------------------------------------------------------------------------
# list_sessions
# ---------------------------------------------------------------------------


class TestListSessions:
    def test_empty(self) -> None:
        assert list_sessions() == []

    def test_with_sessions(self) -> None:
        write_thinking_claude_md("alpha")
        write_thinking_claude_md("beta")
        tdir_b = thinking_dir("beta")
        (tdir_b / "plan.md").write_text("# Plan", encoding="utf-8")

        with (
            patch("duo.thinking._pane_exists", return_value=False),
        ):
            sessions = list_sessions()
            assert len(sessions) == 2
            names = [s["name"] for s in sessions]
            assert "alpha" in names
            assert "beta" in names
            beta = next(s for s in sessions if s["name"] == "beta")
            assert beta["status"] == "finalized"
            assert beta["pane"] == "none"

    def test_alive_pane(self) -> None:
        write_thinking_claude_md("live")
        with (
            patch("duo.thinking._pane_exists", return_value=True),
            patch("duo.thinking._pane_alive", return_value=True),
        ):
            sessions = list_sessions()
            assert sessions[0]["pane"] == "alive"

    def test_dead_pane(self) -> None:
        write_thinking_claude_md("dead")
        with (
            patch("duo.thinking._pane_exists", return_value=True),
            patch("duo.thinking._pane_alive", return_value=False),
        ):
            sessions = list_sessions()
            assert sessions[0]["pane"] == "dead"

    def test_skips_non_directory_entries(self) -> None:
        """Files (not dirs) in THINKING_DIR are ignored."""
        import duo.thinking as _th

        write_thinking_claude_md("real-session")
        # Create a stray file in the (monkeypatched) thinking dir
        (_th.THINKING_DIR / "stray-file.txt").write_text("noise", encoding="utf-8")
        with patch("duo.thinking._pane_exists", return_value=False):
            sessions = list_sessions()
            names = [s["name"] for s in sessions]
            assert "real-session" in names
            assert "stray-file.txt" not in names


# ---------------------------------------------------------------------------
# close_pane
# ---------------------------------------------------------------------------


class TestClosePane:
    def test_close_success(self) -> None:
        with (
            patch("duo.transport.resolve_label", return_value="%42"),
            patch("duo.transport.kill_pane", return_value=True),
        ):
            assert close_pane("my-app") is True

    def test_close_no_pane(self) -> None:
        with patch("duo.transport.resolve_label", side_effect=RuntimeError("nope")):
            assert close_pane("my-app") is False

    def test_close_kill_fails(self) -> None:
        with (
            patch("duo.transport.resolve_label", return_value="%42"),
            patch("duo.transport.kill_pane", return_value=False),
        ):
            assert close_pane("my-app") is False
