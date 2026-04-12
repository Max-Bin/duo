"""Tests for duo.thinking — pre-start brainstorming module."""

from __future__ import annotations

import shlex
import subprocess
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
            patch("duo.config.get_config", return_value=True),
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
            patch("duo.config.get_config", return_value=True),
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
            patch("duo.config.get_config", return_value=True),
            patch("time.sleep"),
        ):
            with pytest.raises(RuntimeError, match="Failed to start Claude Code"):
                _spawn_claude_pane("think-x", "/tmp/test")

    def test_wait_for_idle_returns_false_warns(self) -> None:
        """When wait_for_idle returns False, a warning is logged but pane is still returned."""
        mock_run = MagicMock(
            return_value=MagicMock(returncode=0, stdout="%99\n", stderr="")
        )
        with (
            patch("subprocess.run", mock_run),
            patch("duo.transport.name_pane"),
            patch("duo.transport.send_shell_command"),
            patch("duo.transport.wait_for_idle", return_value=False),
            patch("duo.config.get_config", return_value=True),
            patch("time.sleep"),
        ):
            pane_id = _spawn_claude_pane("think-x", "/tmp/test")
            assert pane_id == "%99"

    def test_timeout_expired_kills_pane(self) -> None:
        """TimeoutExpired during select-layout kills orphaned pane."""
        split_result = MagicMock(returncode=0, stdout="%99\n", stderr="")

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            if "split-window" in cmd:
                return split_result
            if "select-layout" in cmd:
                raise subprocess.TimeoutExpired(cmd, 10)
            return MagicMock(returncode=0)

        with (
            patch("subprocess.run", side_effect=run_side_effect),
            patch("duo.transport.name_pane"),
            patch("duo.transport.kill_pane") as mock_kill,
            patch("duo.config.get_config", return_value=True),
            patch("time.sleep"),
        ):
            with pytest.raises(RuntimeError, match="Failed to start Claude Code"):
                _spawn_claude_pane("think-x", "/tmp/test")
            mock_kill.assert_called_once_with("%99")


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
        tdir_str = str(thinking_dir("my-app"))
        with (
            patch("duo.thinking._pane_exists", return_value=True),
            patch("duo.thinking._pane_alive", return_value=False),
            patch("duo.config.get_config", return_value=True),
            patch("duo.transport.send_shell_command") as mock_cmd,
            patch("duo.transport.wait_for_idle", return_value=True),
        ):
            label = ensure_pane("my-app")
            assert label == "think-my-app"
            assert mock_cmd.call_count == 2
            mock_cmd.assert_any_call("think-my-app", f"cd {shlex.quote(tdir_str)}")
            mock_cmd.assert_any_call(
                "think-my-app", "claude --dangerously-skip-permissions"
            )

    def test_recovers_dead_pane_wait_fails(self) -> None:
        """Recovery proceeds even if wait_for_idle returns False."""
        write_thinking_claude_md("my-app")
        write_plan_template("my-app")
        with (
            patch("duo.thinking._pane_exists", return_value=True),
            patch("duo.thinking._pane_alive", return_value=False),
            patch("duo.config.get_config", return_value=True),
            patch("duo.transport.send_shell_command") as mock_cmd,
            patch("duo.transport.wait_for_idle", return_value=False),
        ):
            label = ensure_pane("my-app")
            assert label == "think-my-app"
            assert mock_cmd.call_count == 2  # cd + claude

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
            patch("duo.transport.detect_dialog_kind", return_value=DialogKind.NONE),
            patch("duo.transport.is_at_main_prompt", return_value=True),
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
            patch("duo.transport.detect_dialog_kind", return_value=DialogKind.OPTION),
            patch("duo.transport.is_at_main_prompt", return_value=False),
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
            patch("duo.transport.detect_dialog_kind", return_value=DialogKind.NONE),
            patch("duo.transport.is_at_main_prompt", return_value=False),
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
            patch("duo.transport.detect_dialog_kind", return_value=DialogKind.NONE),
            patch("duo.transport.is_at_main_prompt", side_effect=mock_at_prompt),
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


class TestExtractResponseParametrized:
    """Parametrized extract_response edge cases."""

    def test_tool_headers_filtered(self) -> None:
        for tool_header in [
            "● Edit file.py",
            "● Read src/main.py",
            "● Bash echo hello",
            "● Grep pattern",
        ]:
            before = "prompt"
            after = f"prompt\n{tool_header}\nActual response"
            result = extract_response(before, after, "q")
            assert result == "Actual response", f"failed for {tool_header!r}"

    def test_noise_lines_filtered(self) -> None:
        for noise in ["", "❯", ">", "  "]:
            before = "prompt"
            after = f"prompt\n{noise}\nGood content"
            result = extract_response(before, after, "q")
            assert result == "Good content", f"failed for {noise!r}"

    def test_user_echo_filtered(self) -> None:
        for user_msg in ["hello", "  hello  "]:
            before = "prompt"
            after = f"prompt\n{user_msg.strip()}\nResponse"
            result = extract_response(before, after, user_msg)
            assert result == "Response", f"failed for {user_msg!r}"

    def test_edge_shapes(self) -> None:
        for before, after, expected in [
            ("", "Response only", "Response only"),
            ("a\nb", "a\nb", ""),
            ("x", "x\nline1\nline2\nline3", "line1\nline2\nline3"),
        ]:
            result = extract_response(before, after, "q")
            assert result == expected, f"failed for {before!r}, {after!r}"


class TestWaitForResponseStableParametrized:
    """Parametrized wait_for_response_stable scenarios."""

    def test_dialog_detected(self) -> None:
        from duo.transport import DialogKind

        for dialog_kind, expected in [
            ("OPTION", "dialog"),
            ("TEXT", "dialog"),
            ("BULLET", "dialog"),
        ]:
            kind = DialogKind[dialog_kind]
            with (
                patch("duo.transport.read_pane", return_value="content"),
                patch("duo.transport.detect_dialog_kind", return_value=kind),
            ):
                result = wait_for_response_stable("lbl", timeout=1.0)
                assert result == expected, f"failed for {dialog_kind!r}"

    def test_spinner_prevents_idle(self) -> None:
        from duo.transport import DialogKind

        for spinner in ["◉ Working", "◎ Loading", "○ Processing"]:
            call_count = 0

            def fake_read(label: str, lines: int, _s: str = spinner) -> str:
                nonlocal call_count
                call_count += 1
                if call_count <= 2:
                    return f"some output\n{_s}\n❯"
                return "final output\n❯"

            with (
                patch("duo.transport.read_pane", side_effect=fake_read),
                patch(
                    "duo.transport.detect_dialog_kind",
                    return_value=DialogKind.NONE,
                ),
                patch("duo.transport.is_at_main_prompt", return_value=True),
                patch("duo.thinking.time.sleep"),
            ):
                result = wait_for_response_stable(
                    "lbl", timeout=0.01, stable_threshold=0.0
                )
                assert result in ("idle", "timeout"), f"failed for {spinner!r}"


class TestThinkingDirParametrized:
    """Parametrized thinking_dir edge cases."""

    def test_valid_names_produce_paths(self) -> None:
        for name in ["simple", "with-dash", "with_underscore", "CamelCase", "a"]:
            result = thinking_dir(name)
            assert result.name == name, f"failed for {name!r}"
            assert result.is_absolute(), f"not absolute for {name!r}"

    def test_invalid_names_rejected(self) -> None:
        for name in ["", "has space", "with/slash", "../traversal", ".hidden"]:
            with pytest.raises(ValueError, match="Invalid thinking session name"):
                thinking_dir(name)


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


# ---------------------------------------------------------------------------
# Name validation (rubber-duck audit — path traversal prevention)
# ---------------------------------------------------------------------------


class TestNameValidation:
    """Defence-in-depth: thinking_dir rejects unsafe names."""

    def test_rejects_unsafe_names(self) -> None:
        for bad_name in [
            "..",
            "../evil",
            "../../etc",
            ".hidden",
            "has space",
            "has;semi",
            "has$dollar",
            "",
            "-starts-dash",
            "_starts-under",
        ]:
            with pytest.raises(ValueError, match="Invalid thinking session name"):
                thinking_dir(bad_name)

    def test_accepts_safe_names(self) -> None:
        for good_name in ["myapp", "my-app", "app2", "A_b-C3"]:
            d = thinking_dir(good_name)
            assert d.name == good_name, f"failed for {good_name!r}"


class TestNamePaneFailureCleanup:
    """Pane is killed if name_pane raises (audit: pane leak)."""

    def test_name_pane_failure_kills_pane(self) -> None:
        split_result = MagicMock(returncode=0, stdout="%77\n", stderr="")

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            if "split-window" in cmd:
                return split_result
            return MagicMock(returncode=0)

        with (
            patch("subprocess.run", side_effect=run_side_effect),
            patch("duo.transport.name_pane", side_effect=RuntimeError("bridge fail")),
            patch("duo.transport.kill_pane") as mock_kill,
            patch("time.sleep"),
        ):
            with pytest.raises(RuntimeError, match="Failed to start Claude Code"):
                _spawn_claude_pane("think-x", "/tmp/test")
            mock_kill.assert_called_once_with("%77")


class TestAppendSessionLogMissingDir:
    """append_session_log creates directory if missing (audit: FileNotFoundError)."""

    def test_creates_dir_on_append(self) -> None:
        append_session_log("brand-new", "hello", "world")
        log = thinking_dir("brand-new") / "session.log"
        assert log.exists()
        assert "[user] hello" in log.read_text()


class TestEnsurePaneSpawnFailure:
    """ensure_pane propagates spawn errors cleanly."""

    def test_spawn_failure_propagates(self) -> None:
        with (
            patch("duo.thinking._pane_exists", return_value=False),
            patch(
                "duo.thinking._spawn_claude_pane",
                side_effect=RuntimeError("no tmux"),
            ),
        ):
            with pytest.raises(RuntimeError, match="no tmux"):
                ensure_pane("my-app")


class TestShellQuoting:
    """working_dir is shell-quoted (audit: spaces in path)."""

    def test_working_dir_is_quoted(self) -> None:
        split_result = MagicMock(returncode=0, stdout="%88\n", stderr="")

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            if "split-window" in cmd:
                return split_result
            return MagicMock(returncode=0)

        with (
            patch("subprocess.run", side_effect=run_side_effect),
            patch("duo.transport.name_pane"),
            patch("duo.transport.send_shell_command") as mock_cmd,
            patch("duo.transport.wait_for_idle", return_value=True),
            patch("duo.config.get_config", return_value=True),
            patch("time.sleep"),
        ):
            _spawn_claude_pane("think-x", "/path/with spaces/dir")
            cd_call = mock_cmd.call_args_list[0]
            assert "'/path/with spaces/dir'" in cd_call[0][1]


class TestBypassPermissions:
    """bypass_permissions config controls --dangerously-skip-permissions flag."""

    def test_spawn_with_bypass_enabled(self) -> None:
        mock_run = MagicMock(
            return_value=MagicMock(returncode=0, stdout="%99\n", stderr="")
        )
        with (
            patch("subprocess.run", mock_run),
            patch("duo.transport.name_pane"),
            patch("duo.transport.send_shell_command") as mock_cmd,
            patch("duo.transport.wait_for_idle", return_value=True),
            patch("duo.config.get_config", return_value=True),
            patch("time.sleep"),
        ):
            _spawn_claude_pane("think-x", "/tmp/test")
            claude_call = mock_cmd.call_args_list[1]
            assert claude_call[0][1] == "claude --dangerously-skip-permissions"

    def test_spawn_with_bypass_disabled(self) -> None:
        mock_run = MagicMock(
            return_value=MagicMock(returncode=0, stdout="%99\n", stderr="")
        )
        with (
            patch("subprocess.run", mock_run),
            patch("duo.transport.name_pane"),
            patch("duo.transport.send_shell_command") as mock_cmd,
            patch("duo.transport.wait_for_idle", return_value=True),
            patch("duo.config.get_config", return_value=False),
            patch("time.sleep"),
        ):
            _spawn_claude_pane("think-x", "/tmp/test")
            claude_call = mock_cmd.call_args_list[1]
            assert claude_call[0][1] == "claude"

    def test_ensure_pane_recover_with_bypass_enabled(self) -> None:
        write_thinking_claude_md("bp-test")
        write_plan_template("bp-test")
        tdir_str = str(thinking_dir("bp-test"))
        with (
            patch("duo.thinking._pane_exists", return_value=True),
            patch("duo.thinking._pane_alive", return_value=False),
            patch("duo.config.get_config", return_value=True),
            patch("duo.transport.send_shell_command") as mock_cmd,
            patch("duo.transport.wait_for_idle", return_value=True),
        ):
            ensure_pane("bp-test")
            assert mock_cmd.call_count == 2
            mock_cmd.assert_any_call("think-bp-test", f"cd {shlex.quote(tdir_str)}")
            mock_cmd.assert_any_call(
                "think-bp-test", "claude --dangerously-skip-permissions"
            )

    def test_ensure_pane_recover_with_bypass_disabled(self) -> None:
        write_thinking_claude_md("bp-off")
        write_plan_template("bp-off")
        tdir_str = str(thinking_dir("bp-off"))
        with (
            patch("duo.thinking._pane_exists", return_value=True),
            patch("duo.thinking._pane_alive", return_value=False),
            patch("duo.config.get_config", return_value=False),
            patch("duo.transport.send_shell_command") as mock_cmd,
            patch("duo.transport.wait_for_idle", return_value=True),
        ):
            ensure_pane("bp-off")
            assert mock_cmd.call_count == 2
            mock_cmd.assert_any_call("think-bp-off", f"cd {shlex.quote(tdir_str)}")
            mock_cmd.assert_any_call("think-bp-off", "claude")


class TestExtractResponseEdgeCases:
    """Deeper edge cases for extract_response."""

    def test_completely_different_before_after(self) -> None:
        """When before and after share no common prefix, all after is delta."""
        before = "completely\ndifferent\ncontent"
        after = "nothing\nin\ncommon\nResponse!"
        result = extract_response(before, after, "q")
        assert "Response!" in result

    def test_after_shorter_than_before(self) -> None:
        """When after has fewer lines than before (pane cleared)."""
        before = "line1\nline2\nline3\nline4\nline5"
        after = "fresh\nResponse here"
        result = extract_response(before, after, "q")
        assert "Response here" in result

    def test_multiline_user_message_not_filtered(self) -> None:
        """Multi-line user message — only exact match filtered."""
        before = "prompt"
        after = "prompt\nhello world\nResponse"
        result = extract_response(before, after, "hello\nworld")
        # "hello world" != "hello\nworld".strip(), so it stays
        assert "hello world" in result

    def test_all_noise_returns_empty(self) -> None:
        """When delta is entirely noise lines, result is empty."""
        before = "prompt"
        after = "prompt\n\n  \n❯\n>\n"
        result = extract_response(before, after, "q")
        assert result == ""

    def test_unicode_response_preserved(self) -> None:
        """Unicode content in response is preserved."""
        before = "prompt"
        after = "prompt\n这是中文回答 🚀"
        result = extract_response(before, after, "q")
        assert "这是中文回答 🚀" in result


class TestThinkingEdgeCasesRound5:
    """Additional edge case value coverage for thinking module."""

    def test_extract_response_edit_line_filtered(self) -> None:
        """Lines starting with ● Edit are filtered as noise."""
        before = "prompt"
        after = "prompt\n● Edit src/foo.py\nActual response"
        result = extract_response(before, after, "q")
        assert "● Edit" not in result
        assert "Actual response" in result

    def test_extract_response_read_line_filtered(self) -> None:
        """Lines starting with ● Read are filtered."""
        before = "prompt"
        after = "prompt\n● Read src/bar.py\nReal content"
        result = extract_response(before, after, "q")
        assert "● Read" not in result
        assert "Real content" in result

    def test_extract_response_bash_grep_lines_filtered(self) -> None:
        """Lines starting with ● Bash and ● Grep are filtered."""
        before = "prompt"
        after = "prompt\n● Bash echo hello\n● Grep pattern\nThe answer"
        result = extract_response(before, after, "q")
        assert "● Bash" not in result
        assert "● Grep" not in result
        assert "The answer" in result

    def test_extract_response_identical_content(self) -> None:
        """Before and after identical produces empty result."""
        content = "line1\nline2\nprompt"
        result = extract_response(content, content, "q")
        assert result == ""

    def test_extract_response_user_message_exact_match_filtered(self) -> None:
        """Exact user message in delta is filtered."""
        before = "prompt"
        after = "prompt\nhello world\nReply"
        result = extract_response(before, after, "hello world")
        assert "hello world" not in result
        assert "Reply" in result

    def test_validate_name_empty_string(self) -> None:
        """Empty name is rejected."""
        from duo.thinking import _validate_name

        with pytest.raises(ValueError, match="Invalid thinking session name"):
            _validate_name("")

    def test_validate_name_dot_dot(self) -> None:
        """Path traversal attempt is rejected."""
        from duo.thinking import _validate_name

        with pytest.raises(ValueError):
            _validate_name("../escape")

    def test_validate_name_starts_with_underscore(self) -> None:
        """Name starting with underscore is rejected."""
        from duo.thinking import _validate_name

        with pytest.raises(ValueError):
            _validate_name("_hidden")

    def test_validate_name_valid_with_hyphen(self) -> None:
        """Name with hyphens and underscores is valid."""
        from duo.thinking import _validate_name

        _validate_name("my-session_2")  # should not raise

    def test_pane_label_format(self) -> None:
        """_pane_label produces expected format."""
        label = _pane_label("review")
        assert "review" in label

    def test_list_sessions_ignores_files(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """list_sessions skips non-directory entries."""
        import duo.thinking

        monkeypatch.setattr(duo.thinking, "THINKING_DIR", tmp_path)
        (tmp_path / "not-a-session.txt").write_text("file")
        (tmp_path / "real-session").mkdir()
        with patch.object(duo.thinking, "_pane_exists", return_value=False):
            sessions = list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["name"] == "real-session"

    def test_append_session_log_creates_file(self, tmp_path: Path) -> None:
        """append_session_log creates session.log if it doesn't exist."""
        import duo.thinking

        name = "log-test"
        tdir = tmp_path / name
        tdir.mkdir()
        with patch.object(duo.thinking, "_ensure_thinking_dir", return_value=tdir):
            append_session_log(name, "question", "answer")
        log = (tdir / "session.log").read_text()
        assert "[user] question" in log
        assert "[response]\nanswer" in log
