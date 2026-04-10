"""Tests for duo.transport — tmux-bridge CLI wrapper."""

from __future__ import annotations

import logging
import subprocess
import threading
from unittest.mock import MagicMock, call, patch

import pytest

import duo.transport
from duo.transport import (
    MINIMUM_PANE_COLS,
    MINIMUM_PANE_ROWS,
    DialogKind,
    PaneInfo,
    TmuxServerDownError,
    _find_bridge,
    _retry,
    _tmux_send_hex,
    approve_permission,
    bridge,
    cancel_current,
    count_bullet_items,
    diagnose_pane,
    doctor,
    ensure_minimum_pane_size,
    get_dialog_kind,
    get_pane_id,
    get_pane_pid,
    get_pane_size,
    is_at_main_prompt,
    is_in_dialog,
    is_in_dialog_stable,
    is_pane_process_alive,
    is_permission_dialog,
    is_process_alive,
    list_panes,
    name_pane,
    read_pane,
    resolve_label,
    safe_enter,
    select_bullet_option,
    select_dialog_option,
    select_other_option,
    send_bootstrap,
    send_eof,
    send_keys,
    send_keys_verified,
    send_message,
    send_option_other_message,
    send_prompt,
    send_shell_command,
    send_text_dialog_message,
    type_text,
    wait_for_dialog,
    wait_for_idle,
)

BRIDGE = "/usr/local/bin/tmux-bridge"


@pytest.fixture(autouse=True)
def _mock_bridge_path(monkeypatch):
    """Pin _BRIDGE so tests never try to locate the real binary."""
    monkeypatch.setattr(duo.transport, "_BRIDGE", BRIDGE)


def _ok(stdout: str = "") -> MagicMock:
    return MagicMock(returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str = "boom") -> MagicMock:
    return MagicMock(returncode=1, stdout="", stderr=stderr)


# ── _find_bridge ──────────────────────────────────────────────────────


class TestFindBridge:
    @patch("shutil.which", return_value="/usr/bin/tmux-bridge")
    def test_found_via_which(self, mock_which):
        assert _find_bridge() == "/usr/bin/tmux-bridge"

    @patch("os.path.isfile", return_value=True)
    @patch("shutil.which", return_value=None)
    def test_found_via_fallback(self, _which, _isfile):
        result = _find_bridge()
        assert "tmux-bridge" in result

    @patch("os.path.isfile", return_value=False)
    @patch("shutil.which", return_value=None)
    def test_not_found_raises(self, _which, _isfile):
        with pytest.raises(FileNotFoundError, match="tmux-bridge not found"):
            _find_bridge()


# ── bridge ────────────────────────────────────────────────────────────


class TestBridge:
    @patch("subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = _ok("hello")
        assert bridge(["echo"]) == "hello"
        mock_run.assert_called_once_with(
            [BRIDGE, "echo"],
            capture_output=True,
            text=True,
            timeout=30,
        )

    @patch("subprocess.run")
    def test_failure_check_true(self, mock_run):
        mock_run.return_value = _fail("oops")
        with pytest.raises(RuntimeError, match="oops"):
            bridge(["bad"])

    @patch("subprocess.run")
    def test_failure_check_false(self, mock_run):
        mock_run.return_value = _fail()
        result = bridge(["bad"], check=False)
        assert result == ""

    @patch("subprocess.run")
    def test_read_only_commands_are_retried(self, mock_run):
        """Read-only bridge commands use retry wrapper."""
        mock_run.side_effect = [RuntimeError("transient"), _ok("ok")]
        # 'read' is in _READ_ONLY_BRIDGE_CMDS, should be retried
        result = bridge(["read", "pane", "50"])
        assert result == "ok"
        assert mock_run.call_count == 2

    @patch("subprocess.run")
    def test_mutating_commands_not_retried(self, mock_run):
        """Mutating bridge commands (type, keys) execute only once."""
        mock_run.side_effect = RuntimeError("type failed")
        with pytest.raises(RuntimeError, match="type failed"):
            bridge(["type", "pane", "hello"])
        assert mock_run.call_count == 1


# ── Atomic operations ─────────────────────────────────────────────────


class TestReadPane:
    @patch("subprocess.run")
    def test_returns_stdout(self, mock_run):
        mock_run.return_value = _ok("pane content")
        assert read_pane("editor", 100) == "pane content"
        mock_run.assert_called_once_with(
            [BRIDGE, "read", "editor", "100"],
            capture_output=True,
            text=True,
            timeout=30,
        )

    @patch("subprocess.run")
    def test_default_lines(self, mock_run):
        mock_run.return_value = _ok("")
        read_pane("editor")
        assert mock_run.call_args[0][0] == [BRIDGE, "read", "editor", "50"]


class TestTypeText:
    @patch("subprocess.run")
    def test_calls_bridge(self, mock_run):
        mock_run.return_value = _ok()
        type_text("editor", "hello world")
        mock_run.assert_called_once_with(
            [BRIDGE, "type", "editor", "hello world"],
            capture_output=True,
            text=True,
            timeout=30,
        )


class TestSendKeys:
    @patch("subprocess.run")
    def test_single_hex_key(self, mock_run):
        """Known keys (Enter, arrows) are sent as raw hex via tmux send-keys -H."""
        mock_run.return_value = _ok()
        send_keys("editor", "Enter")
        # resolve_label + select-pane + tmux send-keys -H 0d
        assert mock_run.call_count == 3
        resolve_call = mock_run.call_args_list[0]
        select_call = mock_run.call_args_list[1]
        hex_call = mock_run.call_args_list[2]
        assert resolve_call.args[0] == [BRIDGE, "resolve", "editor"]
        assert select_call.args[0][:3] == ["tmux", "select-pane", "-t"]
        assert hex_call.args[0][:4] == ["tmux", "send-keys", "-t", ""]
        assert hex_call.args[0][-2:] == ["-H", "0d"]

    @patch("subprocess.run")
    def test_multiple_hex_keys(self, mock_run):
        mock_run.return_value = _ok()
        send_keys("editor", "C-c", "Enter")
        # resolve_label + select-pane + hex send twice
        assert mock_run.call_count == 4
        calls = mock_run.call_args_list
        assert calls[0].args[0] == [BRIDGE, "resolve", "editor"]
        assert calls[1].args[0][:3] == ["tmux", "select-pane", "-t"]
        assert "-H" in calls[2].args[0] and "03" in calls[2].args[0]
        assert "-H" in calls[3].args[0] and "0d" in calls[3].args[0]

    @patch("subprocess.run")
    def test_unknown_key_falls_back_to_bridge(self, mock_run):
        """Keys not in _KEY_TO_HEX table fall through to tmux-bridge keys."""
        mock_run.return_value = _ok()
        send_keys("editor", "F1")
        # resolve_label + select-pane + bridge keys
        assert mock_run.call_count == 3
        assert mock_run.call_args_list[2].args[0] == [BRIDGE, "keys", "editor", "F1"]

    @patch("subprocess.run")
    def test_select_pane_failure_does_not_block(self, mock_run):
        """select-pane failure is silently ignored; keys still sent."""
        call_count = {"n": 0}

        def side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise OSError("tmux gone")
            return _ok()

        mock_run.side_effect = side_effect
        send_keys("editor", "Enter")
        # resolve_label + select-pane(fails) + hex send
        assert call_count["n"] == 3


class TestTmuxSendHexErrors:
    """_tmux_send_hex normalizes subprocess errors like bridge()."""

    @patch("subprocess.run")
    def test_timeout_raises_runtime_error(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["tmux"], timeout=10)
        with pytest.raises(RuntimeError, match="timed out"):
            _tmux_send_hex("%1", "0d")

    @patch("subprocess.run")
    def test_file_not_found_raises_tmux_down(self, mock_run):
        mock_run.side_effect = FileNotFoundError("tmux not found")
        with pytest.raises(TmuxServerDownError, match="tmux binary not found"):
            _tmux_send_hex("%1", "0d")

    @patch("subprocess.run")
    def test_server_down_raises_tmux_down(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "tmux", stderr="error connecting to /tmp/tmux-501/default (no such file)"
        )
        with pytest.raises(TmuxServerDownError, match="tmux server is down"):
            _tmux_send_hex("%1", "0d")

    @patch("subprocess.run")
    def test_generic_failure_raises_runtime_error(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "tmux", stderr="pane %99 not found"
        )
        with pytest.raises(RuntimeError, match="pane %99 not found"):
            _tmux_send_hex("%1", "0d")


class TestNamePane:
    @patch("subprocess.run")
    def test_calls_bridge(self, mock_run):
        mock_run.return_value = _ok()
        name_pane("%5", "editor")
        mock_run.assert_called_once_with(
            [BRIDGE, "name", "%5", "editor"],
            capture_output=True,
            text=True,
            timeout=30,
        )


class TestResolveLabel:
    @patch("subprocess.run")
    def test_strips_output(self, mock_run):
        mock_run.return_value = _ok("%5\n")
        assert resolve_label("editor") == "%5"


class TestGetPaneId:
    @patch("subprocess.run")
    def test_strips_output(self, mock_run):
        mock_run.return_value = _ok("%3\n")
        assert get_pane_id() == "%3"


class TestGetTmuxSessionTarget:
    """Tests for get_tmux_session_target()."""

    def test_reads_tmux_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """$TMUX set → extracts session ID."""
        monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,12345,0")
        from duo.transport import get_tmux_session_target

        assert get_tmux_session_target() == "$0"

    def test_tmux_env_session_id_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Different session ID."""
        monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,99999,3")
        from duo.transport import get_tmux_session_target

        assert get_tmux_session_target() == "$3"

    def test_tmux_env_socket_with_commas(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Socket path containing commas — rsplit handles it."""
        monkeypatch.setenv("TMUX", "/tmp/path,with,commas,12345,2")
        from duo.transport import get_tmux_session_target

        assert get_tmux_session_target() == "$2"

    def test_malformed_tmux_env_falls_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Malformed $TMUX with non-digit session → falls through to list-sessions."""
        monkeypatch.setenv("TMUX", "badvalue")
        mock_result = MagicMock(returncode=0, stdout="$5\n")
        with patch("subprocess.run", return_value=mock_result):
            from duo.transport import get_tmux_session_target

            assert get_tmux_session_target() == "$5"

    def test_no_tmux_env_single_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """$TMUX not set, exactly 1 session → uses it."""
        monkeypatch.delenv("TMUX", raising=False)
        mock_result = MagicMock(returncode=0, stdout="$0\n")
        with patch("subprocess.run", return_value=mock_result):
            from duo.transport import get_tmux_session_target

            assert get_tmux_session_target() == "$0"

    def test_no_tmux_env_no_sessions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """$TMUX not set, 0 sessions → RuntimeError."""
        monkeypatch.delenv("TMUX", raising=False)
        mock_result = MagicMock(returncode=0, stdout="\n")
        with patch("subprocess.run", return_value=mock_result):
            from duo.transport import get_tmux_session_target

            with pytest.raises(RuntimeError, match="No tmux sessions"):
                get_tmux_session_target()

    def test_no_tmux_env_multiple_sessions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """$TMUX not set, 2+ sessions → RuntimeError."""
        monkeypatch.delenv("TMUX", raising=False)
        mock_result = MagicMock(returncode=0, stdout="$0\n$1\n")
        with patch("subprocess.run", return_value=mock_result):
            from duo.transport import get_tmux_session_target

            with pytest.raises(RuntimeError, match="Multiple tmux sessions"):
                get_tmux_session_target()

    def test_tmux_not_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """tmux binary not found → RuntimeError."""
        monkeypatch.delenv("TMUX", raising=False)
        with patch("subprocess.run", side_effect=FileNotFoundError("tmux")):
            from duo.transport import get_tmux_session_target

            with pytest.raises(RuntimeError, match="tmux is not running"):
                get_tmux_session_target()

    def test_list_sessions_nonzero_returncode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """list-sessions returns non-zero → RuntimeError."""
        monkeypatch.delenv("TMUX", raising=False)
        mock_result = MagicMock(returncode=1, stdout="", stderr="error")
        with patch("subprocess.run", return_value=mock_result):
            from duo.transport import get_tmux_session_target

            with pytest.raises(RuntimeError, match="Cannot determine tmux session"):
                get_tmux_session_target()

    def test_tmux_timeout_expired(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """subprocess.TimeoutExpired in fallback → RuntimeError."""
        monkeypatch.delenv("TMUX", raising=False)
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="tmux", timeout=5),
        ):
            from duo.transport import get_tmux_session_target

            with pytest.raises(RuntimeError, match="tmux is not running"):
                get_tmux_session_target()

    def test_tmux_env_only_commas(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """$TMUX set to just commas → falls through to list-sessions."""
        monkeypatch.setenv("TMUX", ",,,")
        mock_result = MagicMock(returncode=0, stdout="$0\n")
        with patch("subprocess.run", return_value=mock_result):
            from duo.transport import get_tmux_session_target

            assert get_tmux_session_target() == "$0"

    def test_tmux_env_whitespace_session_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """$TMUX with whitespace around session_id → strip handles it."""
        monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,123, 0 ")
        from duo.transport import get_tmux_session_target

        assert get_tmux_session_target() == "$0"


# ── list_panes ────────────────────────────────────────────────────────

LIST_OUTPUT = """\
TARGET  SESSION:WIN  SIZE    PROCESS  LABEL    CWD
%1      main:0       80x24   zsh      shell    /home/user
%2      main:0       80x24   copilot  agent    /home/user/project
%3      main:1       120x40  python   runner   /home/user/scripts
"""


class TestListPanes:
    @patch("subprocess.run")
    def test_parses_output(self, mock_run):
        mock_run.return_value = _ok(LIST_OUTPUT)
        panes = list_panes()
        assert len(panes) == 3
        assert panes[0] == PaneInfo(
            "%1", "main:0", "80x24", "zsh", "shell", "/home/user"
        )
        assert panes[1].label == "agent"
        assert panes[2].process == "python"

    @patch("subprocess.run")
    def test_empty_output(self, mock_run):
        mock_run.return_value = _ok("")
        assert list_panes() == []

    @patch("subprocess.run")
    def test_header_only(self, mock_run):
        mock_run.return_value = _ok("TARGET  SESSION:WIN  SIZE  PROCESS  LABEL  CWD\n")
        assert list_panes() == []

    @patch("subprocess.run")
    def test_skips_short_lines(self, mock_run):
        output = "HEADER\n%1 main:0 80x24\n%2 main:0 80x24 copilot agent /home\n"
        mock_run.return_value = _ok(output)
        panes = list_panes()
        assert len(panes) == 1
        assert panes[0].label == "agent"


# ── Composite operations ──────────────────────────────────────────────


class TestSendPrompt:
    def test_send_prompt_banned(self):
        """send_prompt() is BANNED and always raises."""
        with pytest.raises(RuntimeError, match="BANNED"):
            send_prompt("agent", "do something")


class TestSendShellCommand:
    @patch("subprocess.run")
    def test_call_sequence(self, mock_run):
        mock_run.return_value = _ok()
        send_shell_command("agent", "cd /tmp")
        # Sequence: read → type → read → resolve_label → select-pane → tmux send-keys -H 0d
        calls = mock_run.call_args_list
        assert calls[0].args[0] == [BRIDGE, "read", "agent", "5"]
        assert calls[1].args[0] == [BRIDGE, "type", "agent", "cd /tmp"]
        assert calls[2].args[0] == [BRIDGE, "read", "agent", "5"]
        assert calls[3].args[0] == [BRIDGE, "resolve", "agent"]
        assert calls[4].args[0][:3] == ["tmux", "select-pane", "-t"]
        # Enter is sent as raw hex 0d
        assert calls[5].args[0][:3] == ["tmux", "send-keys", "-t"]
        assert "-H" in calls[5].args[0] and "0d" in calls[5].args[0]

    @patch("subprocess.run")
    def test_rejects_at_main_prompt(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="❯ Type @ to mention files", stderr=""
        )
        with pytest.raises(RuntimeError, match="BLOCKED"):
            send_shell_command("agent", "cd /tmp")


class TestSafeEnter:
    @patch("subprocess.run")
    def test_allows_non_prompt(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="normal output", stderr=""
        )
        safe_enter("agent")  # should not raise

    @patch("subprocess.run")
    def test_blocks_main_prompt(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="❯ Type @ to mention files", stderr=""
        )
        with pytest.raises(RuntimeError, match="BLOCKED"):
            safe_enter("agent")


class TestSendBootstrap:
    @patch("subprocess.run")
    def test_first_call_works(self, mock_run):
        mock_run.return_value = _ok()
        duo.transport._BOOTSTRAP_DONE.discard("agent")
        duo.transport._PR_LOG.clear()
        send_bootstrap("agent", "bootstrap prompt")
        assert "agent" in duo.transport._BOOTSTRAP_DONE
        # PR audit recorded
        assert len(duo.transport._PR_LOG) >= 1
        assert duo.transport._PR_LOG[-1]["action"] == "bootstrap"

    @patch("subprocess.run")
    def test_second_call_raises(self, mock_run):
        mock_run.return_value = _ok()
        duo.transport._BOOTSTRAP_DONE.add("agent2")
        with pytest.raises(RuntimeError, match="PERMANENT LOCK"):
            send_bootstrap("agent2", "prompt")
        duo.transport._BOOTSTRAP_DONE.discard("agent2")


class TestSendMessage:
    @patch("subprocess.run")
    def test_call_sequence(self, mock_run):
        mock_run.return_value = _ok()
        send_message("agent", "hello")
        calls = mock_run.call_args_list
        assert calls[0].args[0] == [BRIDGE, "read", "agent", "5"]
        assert calls[1].args[0] == [BRIDGE, "message", "agent", "hello"]
        assert calls[2].args[0] == [BRIDGE, "read", "agent", "5"]
        assert calls[3].args[0] == [BRIDGE, "resolve", "agent"]
        assert calls[4].args[0][:3] == ["tmux", "select-pane", "-t"]
        assert calls[5].args[0][:3] == ["tmux", "send-keys", "-t"]
        assert "-H" in calls[5].args[0] and "0d" in calls[5].args[0]


class TestCancelCurrent:
    @patch("subprocess.run")
    def test_sends_ctrl_c(self, mock_run):
        mock_run.return_value = _ok()
        cancel_current("agent")
        calls = mock_run.call_args_list
        # read + resolve + select-pane + tmux send-keys -H 03
        assert len(calls) == 4
        assert calls[2].args[0][:3] == ["tmux", "select-pane", "-t"]
        assert calls[3].args[0][:3] == ["tmux", "send-keys", "-t"]
        assert "-H" in calls[3].args[0] and "03" in calls[3].args[0]


class TestSendEof:
    @patch("subprocess.run")
    def test_sends_ctrl_d(self, mock_run):
        mock_run.return_value = _ok()
        send_eof("agent")
        calls = mock_run.call_args_list
        assert len(calls) == 4
        assert calls[2].args[0][:3] == ["tmux", "select-pane", "-t"]
        assert calls[3].args[0][:3] == ["tmux", "send-keys", "-t"]
        assert "-H" in calls[3].args[0] and "04" in calls[3].args[0]


# ── Diagnostics ───────────────────────────────────────────────────────


class TestDoctor:
    @patch("subprocess.run")
    def test_returns_output(self, mock_run):
        mock_run.return_value = _ok("all good")
        assert doctor() == "all good"

    @patch("subprocess.run")
    def test_does_not_raise_on_failure(self, mock_run):
        mock_run.return_value = _fail("issue found")
        result = doctor()
        assert result == ""


class TestDiagnosePane:
    @patch("subprocess.run")
    def test_reads_200_lines(self, mock_run):
        mock_run.return_value = _ok("diag output")
        assert diagnose_pane("agent") == "diag output"
        mock_run.assert_called_once_with(
            [BRIDGE, "read", "agent", "200"],
            capture_output=True,
            text=True,
            timeout=30,
        )


class TestIsProcessAlive:
    @patch("subprocess.run")
    def test_shell_process_returns_false(self, mock_run):
        mock_run.return_value = _ok("HEADER\n%1 main:0 80x24 zsh agent /home\n")
        assert is_process_alive("agent") is False

    @patch("subprocess.run")
    def test_dash_shell_returns_false(self, mock_run):
        mock_run.return_value = _ok("HEADER\n%1 main:0 80x24 -zsh agent /home\n")
        assert is_process_alive("agent") is False

    @patch("subprocess.run")
    def test_non_shell_returns_true(self, mock_run):
        mock_run.return_value = _ok("HEADER\n%1 main:0 80x24 copilot agent /home\n")
        assert is_process_alive("agent") is True

    @patch("subprocess.run")
    def test_unknown_label_returns_false(self, mock_run):
        mock_run.return_value = _ok("HEADER\n%1 main:0 80x24 copilot other /home\n")
        assert is_process_alive("missing") is False


# ── Retry decorator ──────────────────────────────────────────────────


class TestRetry:
    def test_succeeds_first_try(self):
        call_count = 0

        @_retry(max_attempts=3, delay=0.01)
        def fn():
            nonlocal call_count
            call_count += 1
            return "ok"

        assert fn() == "ok"
        assert call_count == 1

    def test_succeeds_after_retries(self):
        call_count = 0

        @_retry(max_attempts=3, delay=0.01)
        def fn():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("transient")
            return "ok"

        assert fn() == "ok"
        assert call_count == 3

    def test_exhausts_retries(self):
        @_retry(max_attempts=2, delay=0.01)
        def fn():
            raise RuntimeError("permanent")

        with pytest.raises(RuntimeError, match="permanent"):
            fn()

    def test_non_runtime_error_not_retried(self):
        call_count = 0

        @_retry(max_attempts=3, delay=0.01)
        def fn():
            nonlocal call_count
            call_count += 1
            raise ValueError("not retried")

        with pytest.raises(ValueError):
            fn()
        assert call_count == 1

    def test_retry_succeeds_on_second_attempt(self):
        """Function fails once then succeeds on second call."""
        call_count = 0

        @_retry(max_attempts=3, delay=0.01)
        def fn():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient")
            return "recovered"

        assert fn() == "recovered"
        assert call_count == 2

    def test_retry_exhausts_all_attempts(self):
        """Function fails max_attempts times, raises the last error."""
        call_count = 0

        @_retry(max_attempts=4, delay=0.01)
        def fn():
            nonlocal call_count
            call_count += 1
            raise RuntimeError(f"fail-{call_count}")

        with pytest.raises(RuntimeError, match="fail-4"):
            fn()
        assert call_count == 4

    @patch("duo.transport._time")
    def test_retry_backoff_timing(self, mock_time):
        """Verify exponential backoff delays between retries (with jitter)."""
        mock_time.sleep = MagicMock()

        @_retry(max_attempts=4, delay=1.0, backoff=2.0)
        def fn():
            raise RuntimeError("fail")

        with pytest.raises(RuntimeError):
            fn()

        assert mock_time.sleep.call_count == 3
        delays = [c[0][0] for c in mock_time.sleep.call_args_list]
        # Jitter adds ±20%, so check within tolerance
        expected = [1.0, 2.0, 4.0]
        for actual, base in zip(delays, expected, strict=True):
            assert base * 0.75 <= actual <= base * 1.25, (
                f"delay {actual} not within ±25% of {base}"
            )

    def test_oserror_retried(self):
        """OSError is retried alongside RuntimeError."""
        call_count = 0

        @_retry(max_attempts=3, delay=0.01)
        def fn():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise OSError("transient OS error")
            return "recovered"

        assert fn() == "recovered"
        assert call_count == 3

    def test_oserror_exhausts_retries(self):
        """OSError exhausts all retry attempts."""

        @_retry(max_attempts=2, delay=0.01)
        def fn():
            raise OSError("permanent OS error")

        with pytest.raises(OSError, match="permanent OS error"):
            fn()


# ── PR audit ──────────────────────────────────────────────────────────


class TestPRAudit:
    def test_get_pr_log(self):
        from duo.transport import _record_pr, get_pr_log

        initial = len(get_pr_log())
        _record_pr("test-pane", "test_action", "ctx")
        log = get_pr_log()
        assert len(log) == initial + 1
        assert log[-1]["action"] == "test_action"
        assert log[-1]["label"] == "test-pane"

    def test_pr_callback(self):
        from duo.transport import _record_pr, set_pr_callback

        calls: list[tuple[str, str, str]] = []
        set_pr_callback(lambda l, a, c: calls.append((l, a, c)))
        _record_pr("pane", "act", "ctx")
        assert len(calls) == 1
        assert calls[0] == ("pane", "act", "ctx")
        set_pr_callback(None)  # cleanup

    def test_pr_callback_exception_is_swallowed(self):
        """Callback exceptions are caught and logged, not propagated."""
        from duo.transport import _record_pr, set_pr_callback

        def bad_callback(l, a, c):
            raise ValueError("callback boom")

        set_pr_callback(bad_callback)
        try:
            # Must not raise
            _record_pr("pane", "act", "ctx")
        finally:
            set_pr_callback(None)

    def test_pr_log_capped_at_10000(self):
        """_PR_LOG doesn't grow beyond 10000 entries."""
        import duo.transport

        original = list(duo.transport._PR_LOG)
        try:
            duo.transport._PR_LOG.clear()
            # Fill to 10001
            for i in range(10001):
                duo.transport._PR_LOG.append({"i": str(i)})
            from duo.transport import _record_pr

            _record_pr("cap-test", "overflow", "")
            assert len(duo.transport._PR_LOG) == 10000
        finally:
            duo.transport._PR_LOG.clear()
            duo.transport._PR_LOG.extend(original)


# ── is_at_main_prompt ───────────────────────────────────────────────


class TestIsAtMainPrompt:
    def test_skips_remaining_reqs_line(self):
        """Lines with 'Remaining reqs' are skipped to find prompt (line 231)."""
        content = "Remaining reqs: 42\n❯ Type @ to mention files"
        assert is_at_main_prompt(content) is True

    def test_skips_shift_tab_line(self):
        """Lines with 'shift+tab' are skipped to find prompt (line 231)."""
        content = "Press shift+tab for options\n❯"
        assert is_at_main_prompt(content) is True

    def test_remaining_reqs_without_prompt(self):
        """'Remaining reqs' line alone, no prompt below → False."""
        content = "some output\nRemaining reqs: 10"
        assert is_at_main_prompt(content) is False


# ── is_in_dialog ─────────────────────────────────────────────────────


class TestIsInDialog:
    @patch("subprocess.run")
    def test_detects_box_dialog(self, mock_run):
        content = "╭─ Question ─╮\n1. Yes\n2. No\n╰────────────╯"
        mock_run.return_value = MagicMock(returncode=0, stdout=content, stderr="")
        assert is_in_dialog("test-pane") is True

    @patch("subprocess.run")
    def test_no_dialog(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="$ normal prompt", stderr=""
        )
        assert is_in_dialog("test-pane") is False

    @patch("subprocess.run")
    def test_main_prompt_rejected(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="❯ Type @ to mention files", stderr=""
        )
        assert is_in_dialog("test-pane") is False

    @patch("subprocess.run")
    def test_box_without_options_rejected(self, mock_run):
        """Box borders without numbered options → not a dialog."""
        content = "╭─ Info ─╮\nSome text here\n╰────────╯"
        mock_run.return_value = MagicMock(returncode=0, stdout=content, stderr="")
        assert is_in_dialog("test-pane") is False

    @patch("subprocess.run")
    def test_options_without_box_rejected(self, mock_run):
        """Numbered options without box borders → not a dialog."""
        content = "1. Yes\n2. No\n3. Maybe"
        mock_run.return_value = MagicMock(returncode=0, stdout=content, stderr="")
        assert is_in_dialog("test-pane") is False

    @patch("subprocess.run")
    def test_text_dialog_detected(self, mock_run):
        """Box with 'Type your answer' but no options → TEXT dialog → is_in_dialog True."""
        content = "╭─ Question ─╮\n Type your answer\n╰────────────╯"
        mock_run.return_value = MagicMock(returncode=0, stdout=content, stderr="")
        assert is_in_dialog("test-pane") is True


# ── DialogKind / get_dialog_kind ─────────────────────────────────────


class TestDialogKind:
    @patch("subprocess.run")
    def test_option_dialog(self, mock_run):
        content = "╭─ Question ─╮\n1. Yes\n2. No\n╰────────────╯"
        mock_run.return_value = MagicMock(returncode=0, stdout=content, stderr="")
        assert get_dialog_kind("test-pane") == DialogKind.OPTION

    @patch("subprocess.run")
    def test_text_dialog(self, mock_run):
        content = "╭─ Question ─╮\n Type your answer\n╰────────────╯"
        mock_run.return_value = MagicMock(returncode=0, stdout=content, stderr="")
        assert get_dialog_kind("test-pane") == DialogKind.TEXT

    @patch("subprocess.run")
    def test_text_dialog_enter_to_submit(self, mock_run):
        """'Enter to submit' also triggers TEXT kind."""
        content = "╭─ Question ─╮\nPlease type\nEnter to submit\n╰─"
        mock_run.return_value = MagicMock(returncode=0, stdout=content, stderr="")
        assert get_dialog_kind("test-pane") == DialogKind.TEXT

    @patch("subprocess.run")
    def test_main_prompt_none(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="❯ Type @ to mention files", stderr=""
        )
        assert get_dialog_kind("test-pane") == DialogKind.NONE

    @patch("subprocess.run")
    def test_no_box_none(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="normal output", stderr=""
        )
        assert get_dialog_kind("test-pane") == DialogKind.NONE

    @patch("subprocess.run")
    def test_box_without_options_or_text_none(self, mock_run):
        """Box with no options and no text indicator → NONE."""
        content = "╭─ Info ─╮\nSome info\n╰────────╯"
        mock_run.return_value = MagicMock(returncode=0, stdout=content, stderr="")
        assert get_dialog_kind("test-pane") == DialogKind.NONE


# ── wait_for_idle ────────────────────────────────────────────────────


class TestWaitForIdle:
    @patch("subprocess.run")
    @patch("duo.transport._time")
    def test_detects_idle(self, mock_time, mock_run):
        mock_time.sleep = MagicMock()
        mock_time.monotonic = MagicMock(side_effect=[0.0, 0.1, 0.2])
        # Return same content twice = idle
        mock_run.return_value = MagicMock(
            returncode=0, stdout="stable output", stderr=""
        )
        assert wait_for_idle("test-pane", timeout=5.0, poll_interval=0.01) is True

    @patch("subprocess.run")
    @patch("duo.transport._time")
    def test_timeout(self, mock_time, mock_run):
        mock_time.sleep = MagicMock()
        # monotonic: first call sets deadline, then exceed it after a few iterations
        mock_time.monotonic = MagicMock(side_effect=[0.0, 0.01, 0.02, 0.06])
        # Return different content each time
        call_count = [0]

        def changing_output(*args, **kwargs):
            call_count[0] += 1
            return MagicMock(returncode=0, stdout=f"output {call_count[0]}", stderr="")

        mock_run.side_effect = changing_output
        assert wait_for_idle("test-pane", timeout=0.05, poll_interval=0.01) is False


# ── is_in_dialog_stable ──────────────────────────────────────────────


class TestIsInDialogStable:
    @patch("duo.transport._time")
    @patch("duo.transport.is_in_dialog")
    def test_is_in_dialog_stable_both_true(self, mock_dialog, mock_time):
        """Both reads return True with 1s gap → returns True."""
        mock_time.sleep = MagicMock()
        mock_dialog.return_value = True
        assert is_in_dialog_stable("test-pane") is True
        assert mock_dialog.call_count == 2
        mock_time.sleep.assert_called_once_with(1.0)

    @patch("duo.transport._time")
    @patch("duo.transport.is_in_dialog")
    def test_is_in_dialog_stable_first_true_second_false(self, mock_dialog, mock_time):
        """First True, second False → returns False."""
        mock_time.sleep = MagicMock()
        mock_dialog.side_effect = [True, False]
        assert is_in_dialog_stable("test-pane") is False

    @patch("duo.transport._time")
    @patch("duo.transport.is_in_dialog")
    def test_first_check_false_returns_immediately(self, mock_dialog, mock_time):
        """First is_in_dialog False → immediate False, no sleep (line 253)."""
        mock_time.sleep = MagicMock()
        mock_dialog.return_value = False
        assert is_in_dialog_stable("test-pane") is False
        mock_dialog.assert_called_once()
        mock_time.sleep.assert_not_called()


# ── wait_for_dialog ──────────────────────────────────────────────────


class TestWaitForDialog:
    @patch("duo.transport._time")
    @patch("duo.transport.is_in_dialog_stable")
    def test_wait_for_dialog_immediate(self, mock_stable, mock_time):
        """is_in_dialog_stable returns True on first call."""
        mock_time.sleep = MagicMock()
        mock_time.monotonic = MagicMock(side_effect=[0.0, 0.1])
        mock_stable.return_value = True
        assert wait_for_dialog("test-pane", timeout=10, interval=1) is True
        assert mock_stable.call_count == 1

    @patch("duo.transport._time")
    @patch("duo.transport.is_in_dialog_stable")
    def test_wait_for_dialog_timeout(self, mock_stable, mock_time):
        """Always returns False → returns False after timeout."""
        mock_time.sleep = MagicMock()
        # monotonic: first sets deadline, then exceeds it
        mock_time.monotonic = MagicMock(side_effect=[0.0, 0.005, 0.02])
        mock_stable.return_value = False
        assert wait_for_dialog("test-pane", timeout=0.01, interval=0.01) is False

    def test_wait_for_dialog_negative_interval_raises(self):
        """interval <= 0 raises ValueError."""
        with pytest.raises(ValueError, match="interval must be positive"):
            wait_for_dialog("x", timeout=10, interval=0)

    def test_wait_for_dialog_negative_timeout_raises(self):
        """timeout <= 0 raises ValueError."""
        with pytest.raises(ValueError, match="timeout must be positive"):
            wait_for_dialog("x", timeout=0, interval=1)

    @patch("duo.transport._time")
    @patch("duo.transport.is_in_dialog_stable")
    def test_wait_for_dialog_stop_event_aborts(self, mock_stable, mock_time):
        """stop_event already set → returns False immediately."""
        import threading

        mock_time.monotonic = MagicMock(side_effect=[0.0, 0.1])
        mock_stable.return_value = False
        stop = threading.Event()
        stop.set()
        assert wait_for_dialog("x", timeout=10, interval=1, stop_event=stop) is False
        mock_stable.assert_not_called()

    @patch("duo.transport._time")
    @patch("duo.transport.is_in_dialog_stable")
    def test_wait_for_dialog_stop_event_uses_event_wait(self, mock_stable, mock_time):
        """With stop_event, uses event.wait() instead of time.sleep()."""
        import threading

        mock_time.sleep = MagicMock()
        # first call: deadline set; second: loop check; third: exceeds deadline
        mock_time.monotonic = MagicMock(side_effect=[0.0, 0.5, 20.0])
        mock_stable.return_value = False
        stop = threading.Event()
        result = wait_for_dialog("x", timeout=10, interval=2, stop_event=stop)
        assert result is False
        mock_time.sleep.assert_not_called()

    @patch("duo.transport._time")
    @patch("duo.transport.is_in_dialog_stable")
    def test_wait_for_dialog_no_stop_event_uses_sleep(self, mock_stable, mock_time):
        """Without stop_event, still uses time.sleep()."""
        mock_time.sleep = MagicMock()
        mock_time.monotonic = MagicMock(side_effect=[0.0, 0.5, 20.0])
        mock_stable.return_value = False
        result = wait_for_dialog("x", timeout=10, interval=2)
        assert result is False
        mock_time.sleep.assert_called_once_with(2)


# ── select_dialog_option ─────────────────────────────────────────────


class TestSelectDialogOption:
    @patch("subprocess.run")
    @patch("duo.transport._time")
    @patch("duo.transport.is_in_dialog")
    @patch("duo.transport.is_in_dialog_stable")
    def test_select_dialog_option_success(
        self, mock_stable, mock_dialog, mock_time, mock_run
    ):
        """Mock is_in_dialog_stable True, verify type_text and send_keys called, PR recorded."""
        mock_time.sleep = MagicMock()
        mock_stable.return_value = True
        mock_dialog.return_value = (
            True  # Dialog still present after type_text → Enter sent
        )
        # safe_enter reads pane to check prompt — return non-prompt content
        mock_run.return_value = MagicMock(returncode=0, stdout="some output", stderr="")

        from duo.transport import get_pr_log

        initial_count = len(get_pr_log())
        select_dialog_option("test-pane", "1")

        # Verify subprocess calls were made (type_text + send_keys for Enter)
        assert mock_run.call_count >= 2
        # Verify PR was recorded
        log = get_pr_log()
        assert len(log) == initial_count + 1
        assert log[-1]["action"] == "dialog_option"
        assert log[-1]["label"] == "test-pane"

    @patch("duo.transport.is_in_dialog_stable")
    def test_select_dialog_not_stable_raises(self, mock_stable):
        """select_dialog_option refuses when not in stable dialog (line 301)."""
        mock_stable.return_value = False
        with pytest.raises(RuntimeError, match="SAFETY"):
            select_dialog_option("test-pane", "1")

    @patch("subprocess.run")
    @patch("duo.transport._time")
    @patch("duo.transport.is_in_dialog")
    @patch("duo.transport.is_in_dialog_stable")
    def test_select_dialog_skips_enter_when_dismissed(
        self, mock_stable, mock_dialog, mock_time, mock_run
    ):
        """When type_text dismisses the dialog, safe_enter is skipped."""
        mock_time.sleep = MagicMock()
        mock_stable.return_value = True
        # After type_text, dialog is gone
        mock_dialog.return_value = False
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        from duo.transport import get_pr_log

        initial_count = len(get_pr_log())
        select_dialog_option("test-pane", "2")

        # PR should still be recorded
        log = get_pr_log()
        assert len(log) == initial_count + 1
        # No "Enter" send_keys call — only type_text call
        key_args = [str(c) for c in mock_run.call_args_list]
        assert not any("Enter" in a for a in key_args)


# ---------------------------------------------------------------------------
# Security: pane label sanitisation
# ---------------------------------------------------------------------------


class TestLabelValidation:
    """_validate_label and resolve_label reject unsafe labels."""

    def test_resolve_label_safe(self):
        """Valid label passes validation and reaches tmux-bridge."""
        with patch("subprocess.run", return_value=_ok("%42\n")):
            result = resolve_label("task-fix.auth_01")
        assert result == "%42"

    def test_resolve_label_unsafe_rejected(self):
        """Label with shell metacharacters is rejected before reaching tmux."""
        for bad in ["lab;rm -rf /", "pane$(whoami)", "a b", "foo&bar", "x|y"]:
            with pytest.raises(ValueError, match="Unsafe pane label"):
                resolve_label(bad)


# ── Bridge timeout ───────────────────────────────────────────────────


class TestBridgeTimeout:
    @patch("subprocess.run")
    def test_timeout_raises_runtime_error(self, mock_run):
        """bridge() converts subprocess.TimeoutExpired into RuntimeError."""
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["tmux-bridge", "read"], timeout=30
        )
        with pytest.raises(RuntimeError, match="timed out after 30s"):
            bridge(["read"])


# ── list_panes skipped-line logging ──────────────────────────────────


class TestListPanesLogging:
    @patch("subprocess.run")
    def test_malformed_line_logs_debug(self, mock_run, caplog):
        """Malformed tmux output triggers debug log for skipped lines."""
        output = "HEADER\n%1 main:0\n%2 main:0 80x24 copilot agent /home\n"
        mock_run.return_value = _ok(output)
        with caplog.at_level(logging.DEBUG, logger="duo.transport"):
            panes = list_panes()
        assert len(panes) == 1
        assert any("Skipping unparseable tmux line" in m for m in caplog.messages)


# ── is_at_main_prompt edge cases ────────────────────────────────────


class TestIsAtMainPromptEdgeCases:
    def test_empty_content(self):
        """Empty string is not at prompt."""
        assert is_at_main_prompt("") is False

    def test_only_separators(self):
        """Content with only separator lines is not at prompt."""
        assert is_at_main_prompt("─────\n─────") is False

    def test_chevron_without_menu_text(self):
        """❯ with non-menu text is not at prompt."""
        assert is_at_main_prompt("❯ some random text here") is False

    def test_chevron_after_separators(self):
        """❯ prompt preceded by separator lines IS at prompt."""
        content = "─────\nRemaining reqs: 10\n❯ Type @ to mention files"
        assert is_at_main_prompt(content) is True

    def test_spinner_suppresses_prompt(self) -> None:
        """Spinner marker means Copilot is processing — not idle."""
        content = "◉ Processing...\n❯ Type @ to mention files"
        assert is_at_main_prompt(content) is False

    def test_spinner_variants(self) -> None:
        """All spinner markers suppress the prompt."""
        for marker in ("◉ ", "◎ ", "○ "):
            content = f"{marker}Thinking\n❯ Type @ to mention files"
            assert is_at_main_prompt(content) is False, marker

    def test_shift_tab_skipped(self) -> None:
        """shift+tab line is skipped when scanning for prompt."""
        content = "shift+tab to switch\n❯ Type @ to mention files"
        assert is_at_main_prompt(content) is True

    def test_remaining_reqs_skipped(self) -> None:
        """Remaining reqs line is skipped when scanning for prompt."""
        content = "Remaining reqs: 5\n❯"
        assert is_at_main_prompt(content) is True

    def test_non_prompt_text_breaks_scan(self) -> None:
        """Non-separator, non-prompt text between bottom and ❯ stops scan."""
        # ❯ is NOT the last stripped line — 'Some output' appears below
        content = "❯ Type @ to mention files\nSome output"
        assert is_at_main_prompt(content) is False

    def test_mention_files_prompt(self) -> None:
        """❯ with 'mention files' text is at prompt."""
        assert is_at_main_prompt("❯ mention files to include") is True


# ── is_permission_dialog ──────────────────────────────────────────────


class TestIsPermissionDialog:
    @patch("duo.transport.read_pane")
    def test_detects_run_permission(self, mock_read):
        """Detects 'Do you want to run' as permission dialog."""
        mock_read.return_value = (
            "╭──\n  Do you want to run this command?\n  1. Yes\n╰──"
        )
        assert is_permission_dialog("test") is True

    @patch("duo.transport.read_pane")
    def test_detects_allow_directory(self, mock_read):
        """Detects 'Allow directory' as permission dialog."""
        mock_read.return_value = "Allow directory access\n  1. Allow\n  2. Deny"
        assert is_permission_dialog("test") is True

    @patch("duo.transport.read_pane")
    def test_not_permission_for_regular_dialog(self, mock_read):
        """Regular dialog without permission keywords returns False."""
        mock_read.return_value = (
            "What would you like to do?\n  1. Option A\n  2. Option B"
        )
        assert is_permission_dialog("test") is False

    @patch("duo.transport.read_pane")
    def test_empty_content_not_permission(self, mock_read):
        """Empty pane content returns False."""
        mock_read.return_value = ""
        assert is_permission_dialog("test") is False


# ── approve_permission ────────────────────────────────────────────────


class TestApprovePermission:
    @pytest.fixture(autouse=True)
    def _stable_dialog(self):
        """Assume stable dialog for all tests; override individually to test rejection."""
        with patch("duo.transport.is_in_dialog_stable", return_value=True):
            yield

    @pytest.fixture(autouse=True)
    def _no_sleep(self):
        """Eliminate real sleeps for test speed."""
        with patch("duo.transport._time.sleep"):
            yield

    def test_rejects_unstable_dialog(self):
        """Refuses to approve when dialog is not stable."""
        with patch("duo.transport.is_in_dialog_stable", return_value=False):
            with pytest.raises(RuntimeError, match="SAFETY"):
                approve_permission("test")

    @patch("duo.transport.select_dialog_option")
    @patch("duo.transport.read_pane")
    def test_picks_approve_for_session(self, mock_read, mock_select):
        """Prefers 'approve for session' over plain Yes."""
        mock_read.return_value = (
            "╭──\n"
            "  1. Yes\n"
            "  ❯ 2. Yes, approve for session\n"
            "  3. No, tell me differently\n"
            "╰──"
        )
        approve_permission("test")
        mock_select.assert_called_once_with("test", "2")

    @patch("duo.transport.select_dialog_option")
    @patch("duo.transport.read_pane")
    def test_picks_yes_when_no_approve(self, mock_read, mock_select):
        """Falls back to 'Yes' when no approve option."""
        mock_read.return_value = "╭──\n  1. Yes\n  2. No\n╰──"
        approve_permission("test")
        mock_select.assert_called_once_with("test", "1")

    @patch("duo.transport.select_dialog_option")
    @patch("duo.transport.read_pane")
    def test_skips_no_options(self, mock_read, mock_select):
        """Skips options starting with 'No'."""
        mock_read.return_value = "╭──\n  1. No\n  2. Yes\n╰──"
        approve_permission("test")
        mock_select.assert_called_once_with("test", "2")

    @patch("duo.transport.select_dialog_option")
    @patch("duo.transport.read_pane")
    def test_fallback_to_option_1(self, mock_read, mock_select):
        """Falls back to option 1 when no clear yes/approve."""
        mock_read.return_value = "╭──\n  1. Continue\n  2. Cancel\n╰──"
        approve_permission("test")
        mock_select.assert_called_once_with("test", "1")

    @patch("duo.transport.select_dialog_option")
    @patch("duo.transport.read_pane")
    def test_no_options_raises(self, mock_read, mock_select):
        """Empty pane with no numbered options raises RuntimeError."""
        mock_read.return_value = "╭──\nsome random text\n╰──"
        with pytest.raises(RuntimeError, match="no recognizable"):
            approve_permission("test")
        mock_select.assert_not_called()

    @patch("duo.transport.select_dialog_option")
    @patch("duo.transport.read_pane")
    def test_add_to_allowed_list(self, mock_read, mock_select):
        """Picks 'Add to allowed list' option."""
        mock_read.return_value = "╭──\n  1. Yes\n  2. Add to allowed list\n  3. No\n╰──"
        approve_permission("test")
        mock_select.assert_called_once_with("test", "2")

    @patch("duo.transport.select_dialog_option")
    @patch("duo.transport.read_pane")
    def test_skips_tell_differently(self, mock_read, mock_select):
        """Skips options containing 'no' with 'tell differently' or 'esc'."""
        mock_read.return_value = (
            "╭──\n"
            "Do you want to proceed?\n"
            "  1. I'd say no, tell me differently\n"
            "  2. Yes\n"
            "╰──"
        )
        approve_permission("test")
        mock_select.assert_called_once_with("test", "2")

    @patch("duo.transport.select_dialog_option")
    @patch("duo.transport.read_pane")
    def test_add_without_allowed_not_preferred(self, mock_read, mock_select):
        """'add' without 'allowed' should not be preferred over plain 'Yes'."""
        mock_read.return_value = "╭──\nChoose:\n  1. Yes\n  2. Add something else\n╰──"
        approve_permission("test")
        mock_select.assert_called_once_with("test", "1")

    @patch("duo.transport.select_dialog_option")
    @patch("duo.transport.read_pane")
    def test_ignores_options_outside_box(self, mock_read, mock_select):
        """Options in scrollback before the dialog box are ignored."""
        mock_read.return_value = (
            "Steps:\n"
            "  1. Install\n"
            "  2. Build\n"
            "  3. Deploy\n"
            "╭──\n"
            "  1. Yes, approve\n"
            "  2. No\n"
            "╰──"
        )
        approve_permission("test")
        # Should pick "Yes, approve" (option 1 inside box), not be confused by scrollback
        mock_select.assert_called_once_with("test", "1")

    @patch("duo.transport.select_dialog_option")
    @patch("duo.transport.read_pane")
    def test_no_bottom_border_parses_all_options(self, mock_read, mock_select):
        """1182->1196: dialog without ╰─ — for loop exhausts all lines."""
        mock_read.return_value = "╭──\n  1. Yes\n  2. No\n"
        approve_permission("test")
        mock_select.assert_called_once_with("test", "1")


# ---------------------------------------------------------------------------
# select_other_option
# ---------------------------------------------------------------------------


class TestSendOptionOtherMessage:
    """Tests for send_option_other_message — reliable Enter for Other option."""

    @pytest.fixture(autouse=True)
    def _no_sleep(self):
        """Eliminate real sleeps for test speed."""
        with patch("duo.transport._time.sleep"):
            yield

    @patch("duo.transport._record_pr")
    @patch("duo.transport.detect_dialog_kind", return_value=DialogKind.NONE)
    @patch("duo.transport.read_pane")
    @patch("duo.transport.type_text")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.is_in_dialog", return_value=True)
    def test_send_option_other_message_success(
        self, mock_dialog, mock_keys, mock_type, mock_read, mock_detect, mock_pr
    ):
        """Navigate+type+enter, dialog dismissed on first try."""
        mock_read.return_value = (
            "╭─ Choose an action: ─╮\n"
            "  ❯ 1. Run command\n"
            "  2. Edit file\n"
            "  3. Other\n"
            "╰─\n"
        )
        result = send_option_other_message("test", "custom action")
        assert result is True
        # 2 Down keys + 1 Enter (initial)
        down_calls = [c for c in mock_keys.call_args_list if c == call("test", "Down")]
        enter_calls = [
            c for c in mock_keys.call_args_list if c == call("test", "Enter")
        ]
        assert len(down_calls) == 2
        assert len(enter_calls) == 1
        mock_type.assert_called_once_with("test", "custom action")
        mock_pr.assert_called_once()

    @patch("duo.transport._record_pr")
    @patch("duo.transport.detect_dialog_kind")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.type_text")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.is_in_dialog", return_value=True)
    def test_send_option_other_message_retry_enter(
        self, mock_dialog, mock_keys, mock_type, mock_read, mock_detect, mock_pr
    ):
        """First Enter doesn't dismiss, second does → returns True."""
        mock_read.return_value = "╭─ Choose: ─╮\n  ❯ 1. Run\n  2. Other\n╰─\n"
        mock_detect.side_effect = [DialogKind.OPTION, DialogKind.NONE]
        result = send_option_other_message("test", "custom")
        assert result is True
        enter_calls = [
            c for c in mock_keys.call_args_list if c == call("test", "Enter")
        ]
        assert len(enter_calls) == 2

    @patch("duo.transport.detect_dialog_kind", return_value=DialogKind.OPTION)
    @patch("duo.transport.read_pane")
    @patch("duo.transport.type_text")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.is_in_dialog", return_value=True)
    def test_send_option_other_message_all_retries_fail(
        self, mock_dialog, mock_keys, mock_type, mock_read, mock_detect
    ):
        """Dialog never dismisses → returns False."""
        mock_read.return_value = "╭─ Choose: ─╮\n  ❯ 1. Run\n  2. Other\n╰─\n"
        result = send_option_other_message("test", "stuck")
        assert result is False
        enter_calls = [
            c for c in mock_keys.call_args_list if c == call("test", "Enter")
        ]
        # 1 initial + 2 retries = 3
        assert len(enter_calls) == 3

    @patch("duo.transport.read_pane")
    @patch("duo.transport.is_at_main_prompt", return_value=True)
    def test_send_option_other_message_at_prompt_blocked(self, mock_prompt, mock_read):
        """Raises RuntimeError when at main ❯ prompt."""
        mock_read.return_value = "❯ "
        with pytest.raises(RuntimeError, match="BLOCKED"):
            send_option_other_message("test", "text")

    @patch("duo.transport.read_pane")
    @patch("duo.transport.is_in_dialog", return_value=False)
    def test_send_option_other_message_not_in_dialog(self, mock_dialog, mock_read):
        """Raises RuntimeError when not in a dialog."""
        mock_read.return_value = "some output\n"
        with pytest.raises(RuntimeError, match="SAFETY"):
            send_option_other_message("test", "text")

    @patch("duo.transport.read_pane")
    @patch("duo.transport.is_in_dialog", return_value=True)
    def test_send_option_other_message_too_few_options(self, mock_dialog, mock_read):
        """Raises RuntimeError when dialog has fewer than 2 options."""
        mock_read.return_value = "╭─ Choose: ─╮\n  ❯ 1. Only option\n╰─\n"
        with pytest.raises(RuntimeError, match="need ≥2"):
            send_option_other_message("test", "text")

    @patch("duo.transport._record_pr")
    @patch("duo.transport.detect_dialog_kind")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.type_text")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.is_in_dialog", return_value=True)
    def test_lines_before_box_ignored(
        self, mock_dialog, mock_keys, mock_type, mock_read, mock_detect, mock_pr
    ):
        """Lines before the dialog box (╭─) are skipped."""
        mock_read.return_value = (
            "Some preamble\nMore text\n╭─ Action ─╮\n  ❯ 1. Run\n  2. Other\n╰─\n"
        )
        mock_detect.return_value = DialogKind.NONE
        result = send_option_other_message("test", "my text")
        assert result is True
        down_calls = [c for c in mock_keys.call_args_list if c == call("test", "Down")]
        assert len(down_calls) == 1

    @patch("duo.transport._record_pr")
    @patch("duo.transport.detect_dialog_kind")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.type_text")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.is_in_dialog", return_value=True)
    def test_final_check_succeeds(
        self, mock_dialog, mock_keys, mock_type, mock_read, mock_detect, mock_pr
    ):
        """Dialog persists through retries but dismissed on final check."""
        mock_read.return_value = "╭─ Choose: ─╮\n  ❯ 1. Run\n  2. Other\n╰─\n"
        # 2 retries fail, then final check succeeds
        mock_detect.side_effect = [
            DialogKind.OPTION,
            DialogKind.OPTION,
            DialogKind.NONE,
        ]
        result = send_option_other_message("test", "text")
        assert result is True
        enter_calls = [
            c for c in mock_keys.call_args_list if c == call("test", "Enter")
        ]
        assert len(enter_calls) == 3
        mock_pr.assert_called_once()

    @patch("duo.transport._record_pr")
    @patch("duo.transport.detect_dialog_kind", return_value=DialogKind.NONE)
    @patch("duo.transport.read_pane")
    @patch("duo.transport.type_text")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.is_in_dialog", return_value=True)
    def test_no_bottom_border_parses_all_lines(
        self, mock_dialog, mock_keys, mock_type, mock_read, mock_detect, mock_pr
    ):
        """1332->1347: dialog without ╰─ — for loop exhausts all lines."""
        mock_read.return_value = "╭─ Choose: ─╮\n  ❯ 1. Run\n  2. Other\n"
        result = send_option_other_message("test", "my text")
        assert result is True

    @patch("duo.transport._record_pr")
    @patch("duo.transport.detect_dialog_kind", return_value=DialogKind.NONE)
    @patch("duo.transport.read_pane")
    @patch("duo.transport.type_text")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.is_in_dialog", return_value=True)
    def test_non_option_lines_inside_box_skipped(
        self, mock_dialog, mock_keys, mock_type, mock_read, mock_detect, mock_pr
    ):
        """1341->1332: lines inside box that don't match option regex are skipped."""
        mock_read.return_value = (
            "╭─ Action ─╮\nSome description text\n  ❯ 1. Run\n  2. Other\n╰─\n"
        )
        result = send_option_other_message("test", "hello")
        assert result is True
        down_calls = [c for c in mock_keys.call_args_list if c == call("test", "Down")]
        assert len(down_calls) == 1  # navigate from 1 to 2


class TestSelectOtherOption:
    """Tests for select_other_option — delegates to send_option_other_message."""

    @patch("duo.transport.send_option_other_message", return_value=True)
    def test_delegates_success(self, mock_send):
        """Delegates to send_option_other_message and returns on success."""
        select_other_option("test", "custom action")
        mock_send.assert_called_once_with("test", "custom action")

    @patch("duo.transport.send_option_other_message", return_value=False)
    def test_delegates_failure_logs_warning(self, mock_send, caplog):
        """Logs warning when send_option_other_message returns False."""
        with caplog.at_level(logging.WARNING):
            select_other_option("test", "stuck text")
        mock_send.assert_called_once_with("test", "stuck text")
        assert "dialog may still be active" in caplog.text

    @patch(
        "duo.transport.send_option_other_message",
        side_effect=RuntimeError("BLOCKED"),
    )
    def test_propagates_errors(self, mock_send):
        """RuntimeError from send_option_other_message propagates."""
        with pytest.raises(RuntimeError, match="BLOCKED"):
            select_other_option("test", "text")


# ---------------------------------------------------------------------------
# send_text_dialog_message
# ---------------------------------------------------------------------------


class TestSendTextDialogMessage:
    """Tests for send_text_dialog_message — reliable Enter for text dialogs."""

    @pytest.fixture(autouse=True)
    def _no_sleep(self):
        """Eliminate real sleeps for test speed."""
        with patch("duo.transport._time.sleep"):
            yield

    @patch("duo.transport.detect_dialog_kind", return_value=DialogKind.NONE)
    @patch("duo.transport.send_keys")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.type_text")
    def test_success_first_try(self, mock_type, mock_read, mock_keys, mock_detect):
        """Text visible, Enter dismisses dialog on first try."""
        mock_read.return_value = "input: my answer"
        result = send_text_dialog_message("test", "my answer")
        assert result is True
        mock_type.assert_called_once_with("test", "my answer")
        # Enter sent once
        mock_keys.assert_called_once_with("test", "Enter")

    @patch("duo.transport.detect_dialog_kind")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.type_text")
    def test_retry_enter_once(self, mock_type, mock_read, mock_keys, mock_detect):
        """Dialog persists after first Enter, dismissed after retry."""
        mock_read.return_value = "input: my answer"
        # First check: still in dialog. Second check: dismissed.
        mock_detect.side_effect = [DialogKind.TEXT, DialogKind.NONE]
        result = send_text_dialog_message("test", "my answer")
        assert result is True
        # Enter sent twice (initial + 1 retry)
        assert mock_keys.call_count == 2

    @patch("duo.transport.detect_dialog_kind", return_value=DialogKind.TEXT)
    @patch("duo.transport.send_keys")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.type_text")
    def test_exhausts_retries(self, mock_type, mock_read, mock_keys, mock_detect):
        """All retries exhausted — dialog still showing."""
        mock_read.return_value = "input: stuck answer"
        result = send_text_dialog_message("test", "stuck answer")
        assert result is False
        # Enter sent 3 times: initial + 2 retries
        assert mock_keys.call_count == 3

    @patch("duo.transport.detect_dialog_kind", return_value=DialogKind.NONE)
    @patch("duo.transport.send_keys")
    @patch("duo.transport.resolve_label", return_value="%42")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.type_text")
    def test_sigwinch_on_invisible_text(
        self, mock_type, mock_read, mock_resolve, mock_keys, mock_detect
    ):
        """When typed text is not visible, sends SIGWINCH to refresh."""
        call_count = {"n": 0}

        def fake_read(label, lines):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return "no text here"
            return "my answer is visible"

        mock_read.side_effect = fake_read
        pid_run = MagicMock(returncode=0, stdout="12345\n")
        with (
            patch("subprocess.run", return_value=pid_run),
            patch("os.kill") as mock_kill,
        ):
            result = send_text_dialog_message("test", "my answer")
        assert result is True
        # os.kill should have been called with SIGWINCH
        assert mock_kill.call_count >= 1

    @patch("duo.transport.detect_dialog_kind", return_value=DialogKind.NONE)
    @patch("duo.transport.send_keys")
    @patch("duo.transport.resolve_label", side_effect=RuntimeError("no pane"))
    @patch("duo.transport.read_pane")
    @patch("duo.transport.type_text")
    def test_sigwinch_failure_ignored(
        self, mock_type, mock_read, mock_resolve, mock_keys, mock_detect
    ):
        """SIGWINCH failure is silently ignored."""
        # read_pane calls: verify text (x3 retries) + after Enter (x1) = 4+
        mock_read.side_effect = ["no text", "no text", "my answer", "dismissed"]
        result = send_text_dialog_message("test", "my answer")
        assert result is True

    @patch("duo.transport.detect_dialog_kind", return_value=DialogKind.NONE)
    @patch("duo.transport.send_keys")
    @patch("duo.transport.resolve_label", return_value="%42")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.type_text")
    def test_text_never_visible_aborts_enter(
        self, mock_type, mock_read, mock_resolve, mock_keys, mock_detect
    ):
        """Text never appears — Enter NOT sent, returns False."""
        mock_read.return_value = "nothing relevant here"
        pid_run = MagicMock(returncode=0, stdout="12345\n")
        with (
            patch("subprocess.run", return_value=pid_run),
            patch("os.kill"),
        ):
            result = send_text_dialog_message("test", "my answer")
        # Enter NOT sent because text was never confirmed visible
        mock_keys.assert_not_called()
        assert result is False

    @patch("duo.transport.detect_dialog_kind", return_value=DialogKind.NONE)
    @patch("duo.transport.send_keys")
    @patch("duo.transport.resolve_label", return_value="%42")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.type_text")
    def test_sigwinch_skipped_on_nonzero_returncode(
        self, mock_type, mock_read, mock_resolve, mock_keys, mock_detect
    ):
        """1422->1426: subprocess returns non-zero, SIGWINCH not sent."""
        call_count = {"n": 0}

        def fake_read(label, lines):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return "no text"
            return "my answer visible"

        mock_read.side_effect = fake_read
        pid_run = MagicMock(returncode=1, stdout="")
        with (
            patch("subprocess.run", return_value=pid_run),
            patch("os.kill") as mock_kill,
        ):
            result = send_text_dialog_message("test", "my answer")
        assert result is True
        mock_kill.assert_not_called()


# === Edge-case tests: ANSI stripping, dialog detection robustness ===


class TestStripAnsi:
    """Tests for strip_ansi utility."""

    def test_no_ansi(self) -> None:
        from duo.transport import strip_ansi

        assert strip_ansi("hello world") == "hello world"

    def test_strips_color_codes(self) -> None:
        from duo.transport import strip_ansi

        assert strip_ansi("\x1b[31mred\x1b[0m") == "red"

    def test_strips_bold_and_reset(self) -> None:
        from duo.transport import strip_ansi

        assert strip_ansi("\x1b[1m╭─ title ─╮\x1b[0m") == "╭─ title ─╮"

    def test_strips_multi_param_sequences(self) -> None:
        from duo.transport import strip_ansi

        assert strip_ansi("\x1b[38;5;196mhello\x1b[0m") == "hello"

    def test_preserves_unicode(self) -> None:
        from duo.transport import strip_ansi

        assert strip_ansi("❯ Type @") == "❯ Type @"

    def test_empty_string(self) -> None:
        from duo.transport import strip_ansi

        assert strip_ansi("") == ""

    def test_strip_ansi_empty_string(self) -> None:
        from duo.transport import strip_ansi

        assert strip_ansi("") == ""

    def test_strip_ansi_no_ansi(self) -> None:
        from duo.transport import strip_ansi

        assert strip_ansi("plain text unchanged") == "plain text unchanged"

    def test_strip_ansi_only_ansi(self) -> None:
        from duo.transport import strip_ansi

        assert strip_ansi("\x1b[31m\x1b[1m\x1b[0m") == ""

    def test_strip_ansi_nested_sequences(self) -> None:
        from duo.transport import strip_ansi

        assert (
            strip_ansi("\x1b[1m\x1b[31mhello\x1b[0m \x1b[32mworld\x1b[0m")
            == "hello world"
        )

    def test_strip_ansi_partial_sequence(self) -> None:
        from duo.transport import strip_ansi

        assert strip_ansi("text\x1b[") == "text\x1b["


class TestAnsiInDialogDetection:
    """Ensure dialog detection works with ANSI-colored pane output."""

    def test_main_prompt_with_ansi_colored_prompt(self) -> None:
        from duo.transport import is_at_main_prompt

        content = "\x1b[32m❯\x1b[0m \x1b[90mType @ to mention files\x1b[0m"
        assert is_at_main_prompt(content) is True

    def test_main_prompt_with_ansi_spinner_detected(self) -> None:
        from duo.transport import is_at_main_prompt

        content = "\x1b[33m◉ \x1b[0mProcessing...\n❯"
        assert is_at_main_prompt(content) is False

    def test_main_prompt_with_ansi_box_chars(self) -> None:
        from duo.transport import is_at_main_prompt

        content = "\x1b[1m╭─\x1b[0m question\n1. Yes\n\x1b[1m╰─\x1b[0m\n❯"
        assert is_at_main_prompt(content) is False

    def test_detect_option_dialog_with_ansi(self) -> None:
        from duo.transport import DialogKind, detect_dialog_kind

        content = (
            "\x1b[1m╭─ Choose ─╮\x1b[0m\n"
            "\x1b[32m❯ 1.\x1b[0m Accept\n"
            "  2. Reject\n"
            "\x1b[1m╰─────────╯\x1b[0m"
        )
        assert detect_dialog_kind(content) == DialogKind.OPTION

    def test_detect_text_dialog_with_ansi(self) -> None:
        from duo.transport import DialogKind, detect_dialog_kind

        content = (
            "\x1b[1m╭─ Input ─╮\x1b[0m\n"
            "\x1b[90mType your answer\x1b[0m\n"
            "\x1b[1m╰─────────╯\x1b[0m"
        )
        assert detect_dialog_kind(content) == DialogKind.TEXT

    def test_detect_no_dialog_with_ansi_noise(self) -> None:
        from duo.transport import DialogKind, detect_dialog_kind

        content = "\x1b[32m❯\x1b[0m \x1b[90mType @ to mention files\x1b[0m"
        assert detect_dialog_kind(content) == DialogKind.NONE

    def test_read_pane_strips_ansi(self) -> None:
        """read_pane() should return ANSI-free text."""
        with patch("duo.transport.bridge", return_value="\x1b[31mhello\x1b[0m"):
            result = read_pane("test", 10)
        assert result == "hello"
        assert "\x1b" not in result


class TestDialogBoundaryDetection:
    """Tests that detect_dialog_kind only considers content within box boundaries."""

    def test_numbered_list_outside_box_not_dialog(self) -> None:
        """Numbered list in scrollback above a spinner → NONE, not OPTION."""
        from duo.transport import DialogKind, detect_dialog_kind

        content = (
            "Here is my plan:\n"
            "1. Fix this\n"
            "2. Fix that\n"
            "3. Deploy changes\n"
            "\n"
            "◉ Working on step 1..."
        )
        assert detect_dialog_kind(content) == DialogKind.NONE

    def test_numbered_list_inside_box_is_dialog(self) -> None:
        """Numbered list inside ╭─…╰─ box → OPTION."""
        from duo.transport import DialogKind, detect_dialog_kind

        content = (
            "╭─ Choose an action ─╮\n1. Fix this\n2. Fix that\n╰────────────────────╯"
        )
        assert detect_dialog_kind(content) == DialogKind.OPTION

    def test_mixed_content_only_box_counted(self) -> None:
        """Numbered list outside box + text input inside box → TEXT (not OPTION)."""
        from duo.transport import DialogKind, detect_dialog_kind

        content = (
            "Here is my plan:\n"
            "1. Add tests\n"
            "2. Fix bug\n"
            "3. Deploy\n"
            "\n"
            "╭─ Provide details ─╮\n"
            "Type your answer below\n"
            "╰────────────────────╯"
        )
        assert detect_dialog_kind(content) == DialogKind.TEXT

    def test_box_with_options_after_scrollback_plan(self) -> None:
        """Copilot printed a plan, then shows a Yes/No dialog → OPTION with 2 opts."""
        from duo.transport import (
            DialogKind,
            detect_dialog_kind,
            extract_last_box_lines,
        )

        content = (
            "I'll implement the following:\n"
            "1. Add tests\n"
            "2. Fix bug\n"
            "3. Deploy\n"
            "4. Celebrate\n"
            "5. Write docs\n"
            "\n"
            "╭─ Proceed? ─╮\n"
            "❯ 1. Yes\n"
            "  2. No\n"
            "╰─────────────╯"
        )
        assert detect_dialog_kind(content) == DialogKind.OPTION
        box_lines = extract_last_box_lines(content)
        assert box_lines is not None
        # Only 2 options inside the box, not the 5 from the plan
        opt_count = sum(
            1
            for l in box_lines
            if any(l.strip().startswith(f"{n}.") or f"❯ {n}." in l for n in range(1, 7))
        )
        assert opt_count == 2

    def test_unclosed_box_returns_none(self) -> None:
        """Only ╭─ with no ╰─ → NONE (dialog still rendering)."""
        from duo.transport import DialogKind, detect_dialog_kind

        content = "╭─ Loading ─╮\nPlease wait...\n1. Option A"
        assert detect_dialog_kind(content) == DialogKind.NONE

    def test_empty_box_returns_none(self) -> None:
        """╭─╰─ with nothing between → NONE."""
        from duo.transport import DialogKind, detect_dialog_kind

        content = "╭─ Empty ─╮\n╰──────────╯"
        assert detect_dialog_kind(content) == DialogKind.NONE

    def test_multiple_boxes_uses_last(self) -> None:
        """Two dialog boxes in content → uses the last one."""
        from duo.transport import DialogKind, detect_dialog_kind

        content = (
            "╭─ Old dialog ─╮\n"
            "1. Old option A\n"
            "2. Old option B\n"
            "╰───────────────╯\n"
            "\n"
            "╭─ New dialog ─╮\n"
            "Type your answer\n"
            "╰───────────────╯"
        )
        # Last box has text input, not options → TEXT
        assert detect_dialog_kind(content) == DialogKind.TEXT

    def test_tall_dialog_partial_box_option(self) -> None:
        """Tall dialog where ╭─ scrolled off: only ╰─ visible → detect OPTION."""
        from duo.transport import DialogKind, detect_dialog_kind

        body = "\n".join([f"  line {i}" for i in range(40)])
        content = body + "\n  1. Yes\n  2. No\n╰────────────────────╯"
        assert detect_dialog_kind(content) == DialogKind.OPTION

    def test_tall_dialog_partial_box_text(self) -> None:
        """Tall dialog where ╭─ scrolled off: only ╰─ visible → detect TEXT."""
        from duo.transport import DialogKind, detect_dialog_kind

        body = "\n".join([f"  line {i}" for i in range(40)])
        content = body + "\nType your answer\n╰────────────────────╯"
        assert detect_dialog_kind(content) == DialogKind.TEXT

    def test_tall_dialog_full_box_still_works(self) -> None:
        """Normal dialog with both ╭─ and ╰─ visible still works."""
        from duo.transport import DialogKind, detect_dialog_kind

        content = "╭─ Choose ─╮\n1. Yes\n2. No\n╰────────────╯"
        assert detect_dialog_kind(content) == DialogKind.OPTION

    def test_partial_box_no_false_positive(self) -> None:
        """╰─ without dialog indicators → NONE."""
        from duo.transport import DialogKind, detect_dialog_kind

        content = "some old output\n╰────╯\n\n❯ mention files"
        assert detect_dialog_kind(content) == DialogKind.NONE

    def test_extract_partial_box_lines(self) -> None:
        """extract_last_box_lines returns lines above ╰─ when ╭─ is missing."""
        from duo.transport import extract_last_box_lines

        content = "  1. Option A\n  2. Option B\n╰────────╯"
        result = extract_last_box_lines(content)
        assert result is not None
        assert len(result) == 2


class TestDialogFalsePositiveReduction:
    """Regression tests for dialog detection false positives."""

    def test_narration_with_box_chars_not_dialog(self) -> None:
        """Copilot narrating about dialogs (with ╭─╰─ in text) → NONE."""
        from duo.transport import DialogKind, detect_dialog_kind

        content = (
            "I found the dialog detection code. Here's how it works:\n"
            "╭─ Example dialog ─╮\n"
            "Some example content\n"
            "╰───────────────────╯\n"
            "\n"
            "The function `detect_dialog_kind` checks for numbered options\n"
            "inside the box. Let me now implement the fix:\n"
            "\n"
            "First, I'll update the detection logic...\n"
            "Then I'll add tests for edge cases...\n"
            "Finally, I'll run the full test suite...\n"
            "This should resolve the false positive issue.\n"
            "Let me start with the transport.py changes.\n"
            "Opening the file now...\n"
        )
        assert detect_dialog_kind(content) == DialogKind.NONE

    def test_stats_table_with_box_chars_not_dialog(self) -> None:
        """Stats table using box-drawing chars → NONE."""
        from duo.transport import DialogKind, detect_dialog_kind

        content = (
            "╭──────────────────────────╮\n"
            "│  Tests: 1677 passed      │\n"
            "│  Coverage: 100%          │\n"
            "│  Duration: 68s           │\n"
            "╰──────────────────────────╯\n"
            "\n"
            "All checks passed. Committing changes...\n"
            "git add -A && git commit -m 'test: add coverage'\n"
            "Pushing to origin...\n"
            "Done! Moving to next task.\n"
            "Reading the file structure...\n"
            "Analyzing patterns...\n"
        )
        assert detect_dialog_kind(content) == DialogKind.NONE

    def test_real_option_dialog_at_bottom(self) -> None:
        """Real option dialog at pane bottom (no trailing content) → OPTION."""
        from duo.transport import DialogKind, detect_dialog_kind

        content = (
            "I've made the changes. Here are the results:\n"
            "- test_foo: PASSED\n"
            "- test_bar: PASSED\n"
            "\n"
            "╭─ Proceed? ─╮\n"
            "❯ 1. Yes\n"
            "  2. No\n"
            "╰─────────────╯"
        )
        assert detect_dialog_kind(content) == DialogKind.OPTION

    def test_real_bullet_dialog_at_bottom(self) -> None:
        """Bullet dialog at pane bottom → BULLET."""
        from duo.transport import DialogKind, detect_dialog_kind

        content = (
            "Which branch should I use?\n"
            "╭─ Select branch ─╮\n"
            "❯ main\n"
            "  develop\n"
            "  feature-x\n"
            "  ↑↓ select · Enter accept\n"
            "╰──────────────────╯"
        )
        assert detect_dialog_kind(content) == DialogKind.BULLET

    def test_real_text_dialog_at_bottom(self) -> None:
        """Text dialog at pane bottom → TEXT."""
        from duo.transport import DialogKind, detect_dialog_kind

        content = (
            "Please provide the API key:\n╭─ Input ─╮\nType your answer\n╰──────────╯"
        )
        assert detect_dialog_kind(content) == DialogKind.TEXT

    def test_dialog_with_few_trailing_lines_still_detected(self) -> None:
        """Dialog with ≤5 trailing empty/prompt lines → still detected."""
        from duo.transport import DialogKind, detect_dialog_kind

        content = "╭─ Run? ─╮\n❯ 1. Yes\n  2. No\n╰─────────╯\n\n\n\n"
        assert detect_dialog_kind(content) == DialogKind.OPTION

    def test_permission_dialog_with_long_command(self) -> None:
        """Permission dialog wrapping a long shell command → OPTION."""
        from duo.transport import DialogKind, detect_dialog_kind

        content = (
            "╭─ Allow this? ─╮\n"
            "❯ 1. Yes\n"
            "  2. No\n"
            '  python -c "print(hello)"  \n'
            "╰────────────────╯"
        )
        assert detect_dialog_kind(content) == DialogKind.OPTION

    def test_old_dialog_in_scrollback_with_new_output(self) -> None:
        """Old dialog in scrollback + lots of new output → NONE."""
        from duo.transport import DialogKind, detect_dialog_kind

        dialog_part = (
            "╭─ Old question ─╮\n❯ 1. Approve\n  2. Deny\n╰─────────────────╯\n"
        )
        new_output = "\n".join([f"Processing step {i}..." for i in range(20)])
        content = dialog_part + "\n" + new_output
        assert detect_dialog_kind(content) == DialogKind.NONE


class TestTmuxServerDownError:
    """Tests for TmuxServerDownError detection."""

    def test_bridge_detects_server_down(self) -> None:
        """Bridge raises TmuxServerDownError on 'no server running'."""
        from duo.transport import TmuxServerDownError

        result = MagicMock()
        result.returncode = 1
        result.stderr = "error: no server running on /tmp/tmux-1000/default"
        result.stdout = ""
        with (
            patch("subprocess.run", return_value=result),
            patch("duo.transport._bridge_bin", return_value="tmux-bridge"),
        ):
            with pytest.raises(TmuxServerDownError, match="tmux server is down"):
                bridge(["read", "test", "10"])

    def test_bridge_detects_lost_server(self) -> None:
        """Bridge raises TmuxServerDownError on 'lost server'."""
        from duo.transport import TmuxServerDownError

        result = MagicMock()
        result.returncode = 1
        result.stderr = "lost server"
        result.stdout = ""
        with (
            patch("subprocess.run", return_value=result),
            patch("duo.transport._bridge_bin", return_value="tmux-bridge"),
        ):
            with pytest.raises(TmuxServerDownError, match="tmux server is down"):
                bridge(["read", "test", "10"])

    def test_bridge_normal_error_not_server_down(self) -> None:
        """Normal errors don't raise TmuxServerDownError."""
        result = MagicMock()
        result.returncode = 1
        result.stderr = "error: no pane found with label 'missing'"
        result.stdout = ""
        with (
            patch("subprocess.run", return_value=result),
            patch("duo.transport._bridge_bin", return_value="tmux-bridge"),
        ):
            with pytest.raises(RuntimeError, match="tmux-bridge read failed"):
                bridge(["read", "missing", "10"])


class TestIsTmuxServerAlive:
    """Tests for is_tmux_server_alive."""

    def test_alive_when_sessions_exist(self) -> None:
        from duo.transport import is_tmux_server_alive

        result = MagicMock()
        result.returncode = 0
        with patch("subprocess.run", return_value=result):
            assert is_tmux_server_alive() is True

    def test_dead_when_no_server(self) -> None:
        from duo.transport import is_tmux_server_alive

        result = MagicMock()
        result.returncode = 1
        with patch("subprocess.run", return_value=result):
            assert is_tmux_server_alive() is False

    def test_dead_when_tmux_missing(self) -> None:
        from duo.transport import is_tmux_server_alive

        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert is_tmux_server_alive() is False

    def test_dead_when_timeout(self) -> None:
        from duo.transport import is_tmux_server_alive

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("tmux", 5)):
            assert is_tmux_server_alive() is False


class TestSendKeysVerified:
    """Tests for send_keys_verified()."""

    @pytest.fixture(autouse=True)
    def _alive(self, monkeypatch):
        monkeypatch.setattr("duo.transport.is_pane_process_alive", lambda _: True)

    @patch("duo.transport._time")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.read_pane")
    def test_key_consumed_first_try(self, mock_read, mock_send, mock_time):
        """Returns True when pane content changes on first attempt."""
        mock_read.side_effect = ["before content", "after content"]
        mock_time.sleep = MagicMock()
        from duo.transport import send_keys_verified

        result = send_keys_verified("test-pane", "Enter", settle=0.1, retries=1)
        assert result is True
        mock_send.assert_called_once_with("test-pane", "Enter")

    @patch("duo.transport._time")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.read_pane")
    def test_key_not_consumed(self, mock_read, mock_send, mock_time):
        """Returns False when pane content never changes after all retries."""
        mock_read.return_value = "same content"
        mock_time.sleep = MagicMock()
        from duo.transport import send_keys_verified

        result = send_keys_verified("test-pane", "Enter", settle=0.1, retries=2)
        assert result is False
        assert mock_send.call_count == 2

    @patch("duo.transport._time")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.read_pane")
    def test_key_consumed_second_try(self, mock_read, mock_send, mock_time):
        """Returns True when pane content changes on second attempt."""
        mock_read.side_effect = ["before", "before", "changed"]
        mock_time.sleep = MagicMock()
        from duo.transport import send_keys_verified

        result = send_keys_verified("test-pane", "x", settle=0.1, retries=3)
        assert result is True
        assert mock_send.call_count == 2


class TestIsLikelyStuck:
    """Tests for is_likely_stuck()."""

    @patch("duo.transport._time")
    @patch("duo.transport.read_pane")
    def test_stuck_when_content_unchanged(self, mock_read, mock_time):
        """Returns True when content is identical across two reads."""
        mock_read.return_value = "frozen content"
        mock_time.sleep = MagicMock()
        from duo.transport import is_likely_stuck

        result = is_likely_stuck("test-pane", poll_ms=100)
        assert result is True
        assert mock_read.call_count == 2

    @patch("duo.transport._time")
    @patch("duo.transport.read_pane")
    def test_not_stuck_when_content_changes(self, mock_read, mock_time):
        """Returns False when content changes between reads."""
        mock_read.side_effect = ["first state", "second state"]
        mock_time.sleep = MagicMock()
        from duo.transport import is_likely_stuck

        result = is_likely_stuck("test-pane", poll_ms=100)
        assert result is False


class TestGetPaneSize:
    """Tests for get_pane_size()."""

    @patch("duo.transport.resolve_label", return_value="%1")
    @patch("subprocess.run")
    def test_returns_dimensions(self, mock_run, mock_resolve):
        mock_run.return_value = MagicMock(returncode=0, stdout="200 50\n", stderr="")
        cols, rows = get_pane_size("my-pane")
        assert cols == 200
        assert rows == 50
        mock_resolve.assert_called_once_with("my-pane")

    @patch("duo.transport.resolve_label", return_value="%1")
    @patch("subprocess.run")
    def test_tmux_failure_raises(self, mock_run, mock_resolve):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="no pane found"
        )
        with pytest.raises(RuntimeError, match="Cannot query pane size"):
            get_pane_size("bad-pane")

    @patch("duo.transport.resolve_label", return_value="%1")
    @patch("subprocess.run")
    def test_unexpected_output_raises(self, mock_run, mock_resolve):
        mock_run.return_value = MagicMock(returncode=0, stdout="only-one\n", stderr="")
        with pytest.raises(RuntimeError, match="Unexpected pane size"):
            get_pane_size("weird-pane")


class TestEnsureMinimumPaneSize:
    """Tests for ensure_minimum_pane_size()."""

    @pytest.fixture(autouse=True)
    def _large_client(self, monkeypatch):
        monkeypatch.setattr("duo.transport._get_client_size", lambda: (300, 100))

    @patch("duo.transport.resolve_label", return_value="%1")
    @patch("subprocess.run")
    @patch("duo.transport.get_pane_size")
    def test_no_resize_needed(self, mock_size, mock_run, mock_resolve):
        mock_size.return_value = (200, 50)
        resized = ensure_minimum_pane_size("my-pane")
        assert resized is False
        mock_run.assert_not_called()

    @patch("duo.transport.resolve_label", return_value="%1")
    @patch("subprocess.run")
    @patch("duo.transport.get_pane_size")
    def test_width_too_small(self, mock_size, mock_run, mock_resolve):
        mock_size.return_value = (50, 50)
        mock_run.return_value = MagicMock(returncode=0)
        resized = ensure_minimum_pane_size("my-pane")
        assert resized is True
        # Should have called resize-pane for width
        calls = mock_run.call_args_list
        assert any("-x" in str(c) for c in calls)

    @patch("duo.transport.resolve_label", return_value="%1")
    @patch("subprocess.run")
    @patch("duo.transport.get_pane_size")
    def test_height_too_small(self, mock_size, mock_run, mock_resolve):
        mock_size.return_value = (200, 15)
        mock_run.return_value = MagicMock(returncode=0)
        resized = ensure_minimum_pane_size("my-pane")
        assert resized is True
        calls = mock_run.call_args_list
        assert any("-y" in str(c) for c in calls)

    @patch("duo.transport.resolve_label", return_value="%1")
    @patch("subprocess.run")
    @patch("duo.transport.get_pane_size")
    def test_both_too_small(self, mock_size, mock_run, mock_resolve):
        mock_size.return_value = (50, 15)
        mock_run.return_value = MagicMock(returncode=0)
        resized = ensure_minimum_pane_size("my-pane", min_cols=100, min_rows=40)
        assert resized is True
        assert mock_run.call_count == 2

    @patch("duo.transport.resolve_label", return_value="%1")
    @patch("subprocess.run")
    @patch("duo.transport.get_pane_size")
    def test_custom_minimums(self, mock_size, mock_run, mock_resolve):
        mock_size.return_value = (80, 30)
        resized = ensure_minimum_pane_size("my-pane", min_cols=80, min_rows=30)
        assert resized is False


class TestSendKeysVerifiedAutoResize:
    """Tests for send_keys_verified auto-resize fallback."""

    @pytest.fixture(autouse=True)
    def _alive(self, monkeypatch):
        monkeypatch.setattr("duo.transport.is_pane_process_alive", lambda _: True)

    @patch("duo.transport._time")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.ensure_minimum_pane_size")
    def test_auto_resize_succeeds(self, mock_ensure, mock_read, mock_send, mock_time):
        """Auto-resize rescues a stuck key send."""
        mock_read.side_effect = ["before", "before", "before", "changed"]
        mock_time.sleep = MagicMock()
        mock_ensure.return_value = True

        result = send_keys_verified("test-pane", "Enter", settle=0.1, retries=2)
        assert result is True
        mock_ensure.assert_called_once_with("test-pane")

    @patch("duo.transport._time")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.ensure_minimum_pane_size")
    def test_auto_resize_not_needed(self, mock_ensure, mock_read, mock_send, mock_time):
        """Auto-resize skipped when pane already meets minimums."""
        mock_read.side_effect = ["before", "before", "before"]
        mock_time.sleep = MagicMock()
        mock_ensure.return_value = False  # no resize needed

        result = send_keys_verified("test-pane", "Enter", settle=0.1, retries=1)
        assert result is False

    @patch("duo.transport._time")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.ensure_minimum_pane_size")
    def test_auto_resize_disabled(self, mock_ensure, mock_read, mock_send, mock_time):
        """No auto-resize when auto_resize=False."""
        mock_read.return_value = "same"
        mock_time.sleep = MagicMock()

        result = send_keys_verified(
            "test-pane", "Enter", settle=0.1, retries=1, auto_resize=False
        )
        assert result is False
        mock_ensure.assert_not_called()

    @patch("duo.transport._time")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.read_pane")
    @patch(
        "duo.transport.ensure_minimum_pane_size", side_effect=RuntimeError("no pane")
    )
    def test_auto_resize_error_graceful(
        self, mock_ensure, mock_read, mock_send, mock_time
    ):
        """Auto-resize errors are caught gracefully."""
        mock_read.return_value = "same"
        mock_time.sleep = MagicMock()

        result = send_keys_verified("test-pane", "Enter", settle=0.1, retries=1)
        assert result is False


class TestMinimumPaneConstants:
    """Test pane size constants are reasonable."""

    def test_minimum_cols(self):
        assert MINIMUM_PANE_COLS >= 80
        assert MINIMUM_PANE_COLS <= 200

    def test_minimum_rows(self):
        assert MINIMUM_PANE_ROWS >= 20
        assert MINIMUM_PANE_ROWS <= 60


class TestGetPanePid:
    """Tests for get_pane_pid."""

    def test_returns_pid(self, monkeypatch):
        monkeypatch.setattr("duo.transport.resolve_label", lambda _: "%42")
        monkeypatch.setattr(
            "duo.transport.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="12345\n"),
        )
        assert get_pane_pid("test") == 12345

    def test_returns_none_on_failure(self, monkeypatch):
        monkeypatch.setattr("duo.transport.resolve_label", lambda _: "%42")
        monkeypatch.setattr(
            "duo.transport.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=1, stdout=""),
        )
        assert get_pane_pid("test") is None

    def test_returns_none_on_resolve_error(self, monkeypatch):
        monkeypatch.setattr(
            "duo.transport.resolve_label",
            lambda _: (_ for _ in ()).throw(RuntimeError("no pane")),
        )
        assert get_pane_pid("test") is None

    def test_returns_none_on_empty_stdout(self, monkeypatch):
        monkeypatch.setattr("duo.transport.resolve_label", lambda _: "%42")
        monkeypatch.setattr(
            "duo.transport.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout=""),
        )
        assert get_pane_pid("test") is None

    def test_returns_none_on_timeout(self, monkeypatch):
        monkeypatch.setattr("duo.transport.resolve_label", lambda _: "%42")

        def _raise(*a, **kw):
            raise subprocess.TimeoutExpired("tmux", 5)

        monkeypatch.setattr("duo.transport.subprocess.run", _raise)
        assert get_pane_pid("test") is None


class TestIsPaneProcessAlive:
    """Tests for is_pane_process_alive."""

    def test_alive_running(self, monkeypatch):
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda _: 12345)
        monkeypatch.setattr(
            "duo.transport.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="S\n"),
        )
        assert is_pane_process_alive("test") is True

    def test_dead_no_pid(self, monkeypatch):
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda _: None)
        assert is_pane_process_alive("test") is False

    def test_stopped_process(self, monkeypatch):
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda _: 12345)
        monkeypatch.setattr(
            "duo.transport.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="T\n"),
        )
        assert is_pane_process_alive("test") is False

    def test_stopped_process_with_plus(self, monkeypatch):
        """macOS ps may report 'T+' for stopped foreground processes."""
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda _: 12345)
        monkeypatch.setattr(
            "duo.transport.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="T+\n"),
        )
        assert is_pane_process_alive("test") is False

    def test_ps_failure(self, monkeypatch):
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda _: 12345)
        monkeypatch.setattr(
            "duo.transport.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=1, stdout=""),
        )
        assert is_pane_process_alive("test") is False

    def test_ps_timeout(self, monkeypatch):
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda _: 12345)

        def _raise(*a, **kw):
            raise subprocess.TimeoutExpired("ps", 5)

        monkeypatch.setattr("duo.transport.subprocess.run", _raise)
        assert is_pane_process_alive("test") is False

    def test_ps_oserror(self, monkeypatch):
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda _: 12345)

        def _raise(*a, **kw):
            raise OSError("no ps")

        monkeypatch.setattr("duo.transport.subprocess.run", _raise)
        assert is_pane_process_alive("test") is False


class TestSendKeysVerifiedProcessCheck:
    """send_keys_verified raises RuntimeError for dead/stopped processes."""

    def test_dead_process_raises(self, monkeypatch):
        monkeypatch.setattr("duo.transport.is_pane_process_alive", lambda _: False)
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda _: None)
        with pytest.raises(RuntimeError, match="process is dead"):
            send_keys_verified("test", "Enter")

    def test_stopped_process_raises(self, monkeypatch):
        monkeypatch.setattr("duo.transport.is_pane_process_alive", lambda _: False)
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda _: 12345)
        with pytest.raises(RuntimeError, match="stopped"):
            send_keys_verified("test", "Enter")


class TestNormalizePaneContent:
    """Tests for _normalize_pane_content."""

    def test_strips_trailing_whitespace(self):
        from duo.transport import _normalize_pane_content

        result = _normalize_pane_content("hello   \nworld  \n")
        assert result == "hello\nworld"

    def test_collapses_trailing_blank_lines(self):
        from duo.transport import _normalize_pane_content

        result = _normalize_pane_content("hello\nworld\n\n\n\n")
        assert result == "hello\nworld"

    def test_preserves_internal_blank_lines(self):
        from duo.transport import _normalize_pane_content

        result = _normalize_pane_content("hello\n\nworld\n")
        assert result == "hello\n\nworld"

    def test_empty_input(self):
        from duo.transport import _normalize_pane_content

        assert _normalize_pane_content("") == ""
        assert _normalize_pane_content("\n\n\n") == ""

    def test_cjk_content_preserved(self):
        from duo.transport import _normalize_pane_content

        result = _normalize_pane_content("你好世界   \n测试\n")
        assert result == "你好世界\n测试"


class TestGetClientSize:
    """Tests for _get_client_size."""

    def test_returns_dimensions(self, monkeypatch):
        from duo.transport import _get_client_size

        monkeypatch.setattr(
            "duo.transport.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="200 50\n"),
        )
        assert _get_client_size() == (200, 50)

    def test_returns_fallback_on_failure(self, monkeypatch):
        from duo.transport import _get_client_size

        monkeypatch.setattr(
            "duo.transport.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=1, stdout=""),
        )
        assert _get_client_size() == (200, 50)

    def test_returns_fallback_on_timeout(self, monkeypatch):
        from duo.transport import _get_client_size

        def _raise(*a, **kw):
            raise subprocess.TimeoutExpired("tmux", 5)

        monkeypatch.setattr("duo.transport.subprocess.run", _raise)
        assert _get_client_size() == (200, 50)

    def test_returns_fallback_on_oserror(self, monkeypatch):
        from duo.transport import _get_client_size

        def _raise(*a, **kw):
            raise OSError("no tmux")

        monkeypatch.setattr("duo.transport.subprocess.run", _raise)
        assert _get_client_size() == (200, 50)

    def test_returns_fallback_on_bad_output(self, monkeypatch):
        from duo.transport import _get_client_size

        monkeypatch.setattr(
            "duo.transport.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="invalid\n"),
        )
        assert _get_client_size() == (200, 50)


class TestEnsureMinimumPaneSizeClientCap:
    """Tests for client-dimension capping in ensure_minimum_pane_size."""

    @pytest.fixture(autouse=True)
    def _mock_deps(self, monkeypatch):
        self._resized = []
        monkeypatch.setattr("duo.transport.resolve_label", lambda _: "%1")

        def mock_run(cmd, **kw):
            if "resize-pane" in cmd:
                self._resized.append(cmd)
            return MagicMock(returncode=0, stdout="")

        monkeypatch.setattr("duo.transport.subprocess.run", mock_run)

    def test_caps_to_client_when_small(self, monkeypatch):
        monkeypatch.setattr("duo.transport.get_pane_size", lambda _: (60, 20))
        monkeypatch.setattr("duo.transport._get_client_size", lambda: (80, 22))

        result = ensure_minimum_pane_size("test")
        assert result is True
        # Should cap cols to 79 (80-1), rows to 21 (22-1)
        col_resize = [c for c in self._resized if "-x" in c]
        row_resize = [c for c in self._resized if "-y" in c]
        assert col_resize
        assert "79" in col_resize[0]
        assert row_resize
        assert "21" in row_resize[0]

    def test_no_resize_when_already_meets_capped_minimum(self, monkeypatch):
        monkeypatch.setattr("duo.transport.get_pane_size", lambda _: (79, 21))
        monkeypatch.setattr("duo.transport._get_client_size", lambda: (80, 22))

        result = ensure_minimum_pane_size("test")
        assert result is False


class TestPreemptiveDialogResize:
    """Tests for _preemptive_dialog_resize."""

    def test_no_resize_when_pane_big_enough(self, monkeypatch):
        from duo.transport import _preemptive_dialog_resize

        monkeypatch.setattr("duo.transport.get_pane_size", lambda _: (100, 40))
        resized = []
        monkeypatch.setattr(
            "duo.transport.ensure_minimum_pane_size",
            lambda *a, **kw: resized.append(kw) or False,
        )
        content = "some text\n╭─ dialog ─╮\n  option 1\n  option 2\n╰─────────╯\n"
        _preemptive_dialog_resize("test", content)
        assert not resized

    def test_resize_when_dialog_taller_than_pane(self, monkeypatch):
        from duo.transport import _preemptive_dialog_resize

        monkeypatch.setattr("duo.transport.get_pane_size", lambda _: (100, 10))
        resize_args = []
        monkeypatch.setattr(
            "duo.transport.ensure_minimum_pane_size",
            lambda label, **kw: resize_args.append(kw) or True,
        )
        # 8-line dialog box
        dialog_lines = "\n".join(
            ["╭─ dialog ─╮"] + [f"  option {i}" for i in range(6)] + ["╰─────────╯"]
        )
        content = f"prompt\n{dialog_lines}\n"
        _preemptive_dialog_resize("test", content)
        assert resize_args
        assert resize_args[0]["min_rows"] == 14  # 8 + 6 margin

    def test_no_resize_when_no_dialog_box(self, monkeypatch):
        from duo.transport import _preemptive_dialog_resize

        monkeypatch.setattr("duo.transport.get_pane_size", lambda _: (100, 10))
        _preemptive_dialog_resize("test", "just plain text")
        # No error — gracefully does nothing

    def test_handles_pane_size_error(self, monkeypatch):
        from duo.transport import _preemptive_dialog_resize

        def _raise(_label):
            raise RuntimeError("no pane")

        monkeypatch.setattr("duo.transport.get_pane_size", _raise)
        content = "╭─ dialog ─╮\n  option 1\n╰─────────╯\n"
        _preemptive_dialog_resize("test", content)  # No error

    def test_handles_resize_error(self, monkeypatch):
        from duo.transport import _preemptive_dialog_resize

        monkeypatch.setattr("duo.transport.get_pane_size", lambda _: (100, 10))

        def _raise(*a, **kw):
            raise subprocess.SubprocessError("resize failed")

        monkeypatch.setattr("duo.transport.ensure_minimum_pane_size", _raise)
        content = "╭─ dialog ─╮\n  opt 1\n  opt 2\n  opt 3\n  opt 4\n  opt 5\n  opt 6\n╰─────────╯\n"
        _preemptive_dialog_resize("test", content)  # No error raised


# ── Bullet dialog detection ─────────────────────────────────────────


class TestBulletDialogDetection:
    """Tests for DialogKind.BULLET detection in detect_dialog_kind."""

    @patch("subprocess.run")
    def test_bullet_footer_markers(self, mock_run):
        """Footer with ↑↓ select / Enter accept → BULLET."""
        content = (
            "╭─ Question ─╮\n"
            "❯ Option A\n"
            "  Option B\n"
            "  ↑↓ select · Enter accept · ctrl+d decline\n"
            "╰────────────╯"
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=content, stderr="")
        assert get_dialog_kind("test-pane") == DialogKind.BULLET

    @patch("subprocess.run")
    def test_bullet_cursor_no_numbers(self, mock_run):
        """❯ prefix without numbered options → BULLET."""
        content = (
            "╭─ Question ─╮\n"
            "❯ Create a new branch\n"
            "  Use existing branch\n"
            "╰────────────╯"
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=content, stderr="")
        assert get_dialog_kind("test-pane") == DialogKind.BULLET

    @patch("subprocess.run")
    def test_numbered_options_not_bullet(self, mock_run):
        """Numbered options → OPTION, not BULLET, even with ❯."""
        content = "╭─ Question ─╮\n❯ 1. Yes\n  2. No\n╰────────────╯"
        mock_run.return_value = MagicMock(returncode=0, stdout=content, stderr="")
        assert get_dialog_kind("test-pane") == DialogKind.OPTION

    @patch("subprocess.run")
    def test_bullet_with_pipe_prefix(self, mock_run):
        """❯ prefix with │ pipe chars → BULLET."""
        content = (
            "╭─ Pick one ─╮\n"
            "│ ❯ Alpha    │\n"
            "│   Beta     │\n"
            "│   Gamma    │\n"
            "╰────────────╯"
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=content, stderr="")
        assert get_dialog_kind("test-pane") == DialogKind.BULLET

    @patch("subprocess.run")
    def test_enter_accept_only_footer(self, mock_run):
        """Only 'Enter accept' in footer → BULLET."""
        content = "╭─ Question ─╮\n  Item 1\n  Item 2\n  Enter accept\n╰────────────╯"
        mock_run.return_value = MagicMock(returncode=0, stdout=content, stderr="")
        assert get_dialog_kind("test-pane") == DialogKind.BULLET

    @patch("subprocess.run")
    def test_ctrl_d_decline_footer(self, mock_run):
        """'ctrl+d decline' in footer → BULLET."""
        content = (
            "╭─ Question ─╮\n  Item X\n  ctrl+d decline · Esc cancel\n╰────────────╯"
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=content, stderr="")
        assert get_dialog_kind("test-pane") == DialogKind.BULLET

    @patch("subprocess.run")
    def test_bullet_enum_value(self, mock_run):
        assert DialogKind.BULLET.value == "bullet"


# ── count_bullet_items ─────────────────────────────────────────────


class TestCountBulletItems:
    """Tests for count_bullet_items parsing logic."""

    def test_basic_three_items(self):
        content = "╭─ Question ─╮\n❯ Alpha\n  Beta\n  Gamma\n╰────────────╯"
        total, cursor = count_bullet_items(content)
        assert total == 3
        assert cursor == 1

    def test_cursor_on_second_item(self):
        content = "╭─ Question ─╮\n  Alpha\n❯ Beta\n  Gamma\n╰────────────╯"
        total, cursor = count_bullet_items(content)
        assert total == 3
        assert cursor == 2

    def test_cursor_on_last_item(self):
        content = "╭─ Question ─╮\n  Alpha\n  Beta\n❯ Gamma\n╰────────────╯"
        total, cursor = count_bullet_items(content)
        assert total == 3
        assert cursor == 3

    def test_no_box(self):
        total, cursor = count_bullet_items("just some text")
        assert total == 0
        assert cursor == 0

    def test_empty_box(self):
        content = "╭─ Title ─╮\n╰────────╯"
        total, cursor = count_bullet_items(content)
        assert total == 0
        assert cursor == 0

    def test_ignores_footer_lines(self):
        content = (
            "╭─ Pick ─╮\n"
            "❯ One\n"
            "  Two\n"
            "  ↑↓ select · Enter accept · ctrl+d decline\n"
            "╰────────╯"
        )
        total, cursor = count_bullet_items(content)
        assert total == 2
        assert cursor == 1

    def test_single_item(self):
        content = "╭─ Q ─╮\n❯ Only option\n╰─────╯"
        total, cursor = count_bullet_items(content)
        assert total == 1
        assert cursor == 1

    def test_pipe_borders(self):
        content = "╭─ Dialog ─╮\n│ ❯ First  │\n│   Second │\n│   Third  │\n╰──────────╯"
        total, cursor = count_bullet_items(content)
        assert total == 3
        assert cursor == 1

    def test_blank_lines_between_items(self):
        """Blank lines inside the box are ignored."""
        content = "╭─ Q ─╮\n❯ A\n\n  B\n\n  C\n╰─────╯"
        total, cursor = count_bullet_items(content)
        assert total == 3
        assert cursor == 1


# ── select_bullet_option ────────────────────────────────────────────


class TestSelectBulletOption:
    """Tests for select_bullet_option navigation."""

    @patch("duo.transport._record_pr")
    @patch("duo.transport.safe_enter")
    @patch("duo.transport.send_keys")
    @patch("duo.transport._time")
    @patch("duo.transport.is_in_dialog")
    @patch("duo.transport.strip_ansi", side_effect=lambda x: x)
    @patch("duo.transport.read_pane")
    @patch("duo.transport.is_in_dialog_stable", return_value=True)
    @patch("duo.transport.pane_lock")
    def test_navigate_down(
        self,
        mock_lock,
        mock_stable,
        mock_read,
        mock_strip,
        mock_dialog,
        mock_time,
        mock_keys,
        mock_enter,
        mock_record,
    ):
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)
        mock_dialog.return_value = True
        mock_time.sleep = MagicMock()
        mock_read.return_value = "╭─ Q ─╮\n❯ First\n  Second\n  Third\n╰─────╯"
        select_bullet_option("test-pane", 3)
        # cursor at 1, target 3 → 2 Down presses
        down_calls = [c for c in mock_keys.call_args_list if c[0][1] == "Down"]
        assert len(down_calls) == 2
        mock_enter.assert_called_once()

    @patch("duo.transport._record_pr")
    @patch("duo.transport.safe_enter")
    @patch("duo.transport.send_keys")
    @patch("duo.transport._time")
    @patch("duo.transport.is_in_dialog")
    @patch("duo.transport.strip_ansi", side_effect=lambda x: x)
    @patch("duo.transport.read_pane")
    @patch("duo.transport.is_in_dialog_stable", return_value=True)
    @patch("duo.transport.pane_lock")
    def test_navigate_up(
        self,
        mock_lock,
        mock_stable,
        mock_read,
        mock_strip,
        mock_dialog,
        mock_time,
        mock_keys,
        mock_enter,
        mock_record,
    ):
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)
        mock_dialog.return_value = True
        mock_time.sleep = MagicMock()
        mock_read.return_value = "╭─ Q ─╮\n  First\n  Second\n❯ Third\n╰─────╯"
        select_bullet_option("test-pane", 1)
        # cursor at 3, target 1 → 2 Up presses
        up_calls = [c for c in mock_keys.call_args_list if c[0][1] == "Up"]
        assert len(up_calls) == 2
        mock_enter.assert_called_once()

    @patch("duo.transport._record_pr")
    @patch("duo.transport.safe_enter")
    @patch("duo.transport.send_keys")
    @patch("duo.transport._time")
    @patch("duo.transport.is_in_dialog")
    @patch("duo.transport.strip_ansi", side_effect=lambda x: x)
    @patch("duo.transport.read_pane")
    @patch("duo.transport.is_in_dialog_stable", return_value=True)
    @patch("duo.transport.pane_lock")
    def test_no_movement_same_position(
        self,
        mock_lock,
        mock_stable,
        mock_read,
        mock_strip,
        mock_dialog,
        mock_time,
        mock_keys,
        mock_enter,
        mock_record,
    ):
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)
        mock_dialog.return_value = True
        mock_time.sleep = MagicMock()
        mock_read.return_value = "╭─ Q ─╮\n  First\n❯ Second\n  Third\n╰─────╯"
        select_bullet_option("test-pane", 2)
        # cursor already at 2 → no arrow keys
        assert mock_keys.call_count == 0
        mock_enter.assert_called_once()

    @patch("duo.transport.is_in_dialog_stable", return_value=False)
    @patch("duo.transport.pane_lock")
    def test_refuses_when_not_in_dialog(self, mock_lock, mock_stable):
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises(RuntimeError, match="not in stable dialog"):
            select_bullet_option("test-pane", 1)

    @patch("duo.transport.strip_ansi", side_effect=lambda x: x)
    @patch("duo.transport.read_pane")
    @patch("duo.transport.is_in_dialog_stable", return_value=True)
    @patch("duo.transport.pane_lock")
    def test_refuses_no_items(self, mock_lock, mock_stable, mock_read, mock_strip):
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)
        mock_read.return_value = "just some text without a dialog box"
        with pytest.raises(RuntimeError, match="no bullet items"):
            select_bullet_option("test-pane", 1)

    @patch("duo.transport.strip_ansi", side_effect=lambda x: x)
    @patch("duo.transport.read_pane")
    @patch("duo.transport.is_in_dialog_stable", return_value=True)
    @patch("duo.transport.pane_lock")
    def test_refuses_out_of_range(self, mock_lock, mock_stable, mock_read, mock_strip):
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)
        mock_read.return_value = "╭─ Q ─╮\n❯ One\n  Two\n╰─────╯"
        with pytest.raises(RuntimeError, match="out of range"):
            select_bullet_option("test-pane", 5)

    @patch("duo.transport.strip_ansi", side_effect=lambda x: x)
    @patch("duo.transport.read_pane")
    @patch("duo.transport.is_in_dialog_stable", return_value=True)
    @patch("duo.transport.pane_lock")
    def test_refuses_position_zero(self, mock_lock, mock_stable, mock_read, mock_strip):
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)
        mock_read.return_value = "╭─ Q ─╮\n❯ One\n  Two\n╰─────╯"
        with pytest.raises(RuntimeError, match="out of range"):
            select_bullet_option("test-pane", 0)

    @patch("duo.transport._record_pr")
    @patch("duo.transport.is_in_dialog")
    @patch("duo.transport.send_keys")
    @patch("duo.transport._time")
    @patch("duo.transport.strip_ansi", side_effect=lambda x: x)
    @patch("duo.transport.read_pane")
    @patch("duo.transport.is_in_dialog_stable", return_value=True)
    @patch("duo.transport.pane_lock")
    def test_skips_enter_when_dialog_dismissed(
        self,
        mock_lock,
        mock_stable,
        mock_read,
        mock_strip,
        mock_time,
        mock_keys,
        mock_dialog,
        mock_record,
    ):
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)
        mock_dialog.return_value = False  # Dialog gone after navigation
        mock_time.sleep = MagicMock()
        mock_read.return_value = "╭─ Q ─╮\n❯ One\n╰─────╯"
        select_bullet_option("test-pane", 1)
        # No enter sent because dialog is gone
        # safe_enter is NOT called (is_in_dialog returned False)


# ── Round BH: Rubber-duck audit regression tests ─────────────────────


class TestRetrySkipsTmuxServerDown:
    """_retry must NOT retry TmuxServerDownError (dead server won't recover)."""

    def test_tmux_down_not_retried(self):
        from duo.transport import TmuxServerDownError

        call_count = [0]

        @_retry(max_attempts=3, delay=0.01)
        def always_down():
            call_count[0] += 1
            raise TmuxServerDownError("server is down")

        with pytest.raises(TmuxServerDownError):
            always_down()
        assert call_count[0] == 1  # no retries

    def test_runtime_error_still_retried(self):
        call_count = [0]

        @_retry(max_attempts=3, delay=0.01)
        def transient_fail():
            call_count[0] += 1
            if call_count[0] < 3:
                raise RuntimeError("transient")
            return "ok"

        assert transient_fail() == "ok"
        assert call_count[0] == 3


class TestFlockOwnersThreadSafe:
    """_FLOCK_OWNERS accesses must be guarded by _THREAD_LOCKS_GUARD."""

    @patch("subprocess.run")
    def test_flock_owners_guarded(self, mock_run):
        """Verify _FLOCK_OWNERS uses _THREAD_LOCKS_GUARD (not bare dict access)."""
        import inspect

        from duo.transport import pane_lock

        source = inspect.getsource(pane_lock)
        # The implementation must use _THREAD_LOCKS_GUARD around _FLOCK_OWNERS
        assert "_THREAD_LOCKS_GUARD" in source
        assert "_FLOCK_OWNERS" in source


class TestSetPrCallbackThreadSafe:
    """set_pr_callback must use _LOCK."""

    def test_set_pr_callback_uses_lock(self):
        import inspect

        from duo.transport import set_pr_callback

        source = inspect.getsource(set_pr_callback)
        assert "_LOCK" in source


class TestSendBootstrapRollback:
    """send_bootstrap must rollback _BOOTSTRAP_DONE on I/O failure."""

    @pytest.fixture(autouse=True)
    def _no_sleep(self):
        """Eliminate real sleeps for test speed."""
        with patch("duo.transport._time.sleep"):
            yield

    @patch("subprocess.run")
    def test_rollback_on_io_failure(self, mock_run):
        label = "rollback-test"
        duo.transport._BOOTSTRAP_DONE.discard(label)

        # First read_pane succeeds, type_text fails
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 1:
                return _ok("content")
            raise RuntimeError("bridge timeout")

        mock_run.side_effect = side_effect

        with pytest.raises(RuntimeError, match="bridge timeout"):
            send_bootstrap(label, "test prompt")

        # Label must NOT be in _BOOTSTRAP_DONE after failure
        assert label not in duo.transport._BOOTSTRAP_DONE

    @patch("subprocess.run")
    def test_no_rollback_on_success(self, mock_run):
        label = "no-rollback-test"
        duo.transport._BOOTSTRAP_DONE.discard(label)
        duo.transport._PR_LOG.clear()
        mock_run.return_value = _ok()
        send_bootstrap(label, "test prompt")
        assert label in duo.transport._BOOTSTRAP_DONE
        duo.transport._BOOTSTRAP_DONE.discard(label)

    @patch("subprocess.run")
    def test_no_rollback_after_type_text_succeeds(self, mock_run):
        """Once type_text succeeds, lock is committed even if send_keys fails."""
        label = "post-type-test"
        duo.transport._BOOTSTRAP_DONE.discard(label)
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            # call 1: read_pane OK, call 2: type_text OK, call 3: read_pane #2 fails
            if call_count[0] <= 2:
                return _ok("content")
            raise RuntimeError("pane died after type")

        mock_run.side_effect = side_effect

        with pytest.raises(RuntimeError, match="pane died after type"):
            send_bootstrap(label, "typed prompt")

        # Lock must remain — text was already sent to the pane
        assert label in duo.transport._BOOTSTRAP_DONE
        duo.transport._BOOTSTRAP_DONE.discard(label)


class TestSafeEnterToctuDetection:
    """safe_enter must detect TOCTOU races via post-send pane read."""

    @patch("duo.transport._record_pr")
    @patch("duo.transport._time")
    @patch("subprocess.run")
    def test_toctou_detected_and_logged(self, mock_run, mock_time, mock_record, caplog):
        mock_time.sleep = MagicMock()
        # First read: non-prompt (passes check), second read: at prompt (TOCTOU)
        mock_run.side_effect = [
            _ok("normal output"),  # pre-check read
            _ok("%42"),  # resolve_label
            MagicMock(returncode=0),  # select-pane
            MagicMock(returncode=0),  # send-keys -H Enter
            _ok("❯ Type @ to mention files"),  # post-send read (TOCTOU!)
        ]
        with caplog.at_level(logging.CRITICAL, logger="duo.transport"):
            safe_enter("test-pane")
        assert "TOCTOU" in caplog.text
        mock_record.assert_called_once_with(
            "test-pane", "toctou_enter", "safe_enter race detected"
        )

    @patch("duo.transport._time")
    @patch("subprocess.run")
    def test_no_toctou_when_normal(self, mock_run, mock_time):
        mock_time.sleep = MagicMock()
        # Both reads: non-prompt content
        mock_run.return_value = _ok("normal dialog content")
        safe_enter("test-pane")  # should not log anything


class TestApprovePermissionNoFallback:
    """approve_permission must raise when no recognizable yes option."""

    @pytest.fixture(autouse=True)
    def _no_sleep(self):
        """Eliminate real sleeps for test speed."""
        with patch("duo.transport._time.sleep"):
            yield

    @patch("duo.transport.select_dialog_option")
    @patch("duo.transport.read_pane")
    def test_raises_on_unrecognized_options(self, mock_read, mock_select):
        mock_read.return_value = "╭──\n  1. Foo\n  2. Bar\n╰──"
        with pytest.raises(RuntimeError, match="no recognizable"):
            approve_permission("test")
        mock_select.assert_not_called()

    @patch("duo.transport.select_dialog_option")
    @patch("duo.transport.read_pane")
    def test_recognizes_continue(self, mock_read, mock_select):
        mock_read.return_value = "╭──\n  1. Continue\n  2. Cancel\n╰──"
        approve_permission("test")
        mock_select.assert_called_once_with("test", "1")

    @patch("duo.transport.select_dialog_option")
    @patch("duo.transport.read_pane")
    def test_recognizes_ok(self, mock_read, mock_select):
        mock_read.return_value = "╭──\n  1. Ok\n  2. No\n╰──"
        approve_permission("test")
        mock_select.assert_called_once_with("test", "1")

    @patch("duo.transport.select_dialog_option")
    @patch("duo.transport.read_pane")
    def test_recognizes_proceed(self, mock_read, mock_select):
        mock_read.return_value = "╭──\n  1. No\n  2. Proceed\n╰──"
        approve_permission("test")
        mock_select.assert_called_once_with("test", "2")


class TestCleanupPaneState:
    """cleanup_pane_state must clear all per-label module state."""

    def test_clears_bootstrap_and_thread_locks(self):
        from duo.transport import cleanup_pane_state

        label = "cleanup-test"
        duo.transport._BOOTSTRAP_DONE.add(label)
        with duo.transport._THREAD_LOCKS_GUARD:
            duo.transport._THREAD_LOCKS[label] = MagicMock()

        cleanup_pane_state(label)

        assert label not in duo.transport._BOOTSTRAP_DONE
        assert label not in duo.transport._THREAD_LOCKS

    def test_idempotent_on_missing_label(self):
        from duo.transport import cleanup_pane_state

        # Should not raise even if label was never used
        cleanup_pane_state("never-existed-label")


class TestEvictIdleLocks:
    """_evict_idle_locks removes entries not in _FLOCK_OWNERS."""

    def test_evicts_idle_locks(self):
        """Idle locks (no flock owner) are evicted."""
        import duo.transport

        saved = duo.transport._MAX_CACHED_LOCKS
        try:
            duo.transport._MAX_CACHED_LOCKS = 2

            with duo.transport._THREAD_LOCKS_GUARD:
                duo.transport._THREAD_LOCKS.clear()
                duo.transport._FLOCK_OWNERS.clear()
                duo.transport._THREAD_LOCKS["a"] = threading.RLock()
                duo.transport._THREAD_LOCKS["b"] = threading.RLock()

            # Getting a third lock triggers eviction
            lock_c = duo.transport._get_thread_lock("c")
            assert lock_c is not None

            with duo.transport._THREAD_LOCKS_GUARD:
                # a and b should be evicted, c remains
                assert "c" in duo.transport._THREAD_LOCKS
                assert "a" not in duo.transport._THREAD_LOCKS
                assert "b" not in duo.transport._THREAD_LOCKS
        finally:
            duo.transport._MAX_CACHED_LOCKS = saved
            with duo.transport._THREAD_LOCKS_GUARD:
                duo.transport._THREAD_LOCKS.pop("c", None)

    def test_preserves_active_flock_owners(self):
        """Locks with active flock owners are NOT evicted."""
        import duo.transport

        saved = duo.transport._MAX_CACHED_LOCKS
        try:
            duo.transport._MAX_CACHED_LOCKS = 2

            with duo.transport._THREAD_LOCKS_GUARD:
                duo.transport._THREAD_LOCKS.clear()
                duo.transport._FLOCK_OWNERS.clear()
                duo.transport._THREAD_LOCKS["active"] = threading.RLock()
                duo.transport._THREAD_LOCKS["idle"] = threading.RLock()
                duo.transport._FLOCK_OWNERS["active"] = threading.get_ident()

            lock_new = duo.transport._get_thread_lock("new-label")
            assert lock_new is not None

            with duo.transport._THREAD_LOCKS_GUARD:
                assert "active" in duo.transport._THREAD_LOCKS
                assert "idle" not in duo.transport._THREAD_LOCKS
                assert "new-label" in duo.transport._THREAD_LOCKS
        finally:
            duo.transport._MAX_CACHED_LOCKS = saved
            with duo.transport._THREAD_LOCKS_GUARD:
                duo.transport._THREAD_LOCKS.pop("active", None)
                duo.transport._THREAD_LOCKS.pop("new-label", None)
                duo.transport._FLOCK_OWNERS.pop("active", None)

    def test_no_eviction_below_threshold(self):
        """No eviction when below _MAX_CACHED_LOCKS."""
        import duo.transport

        saved = duo.transport._MAX_CACHED_LOCKS
        try:
            duo.transport._MAX_CACHED_LOCKS = 100

            with duo.transport._THREAD_LOCKS_GUARD:
                duo.transport._THREAD_LOCKS.clear()
                duo.transport._THREAD_LOCKS["keep-me"] = threading.RLock()

            lock = duo.transport._get_thread_lock("also-keep")

            with duo.transport._THREAD_LOCKS_GUARD:
                assert "keep-me" in duo.transport._THREAD_LOCKS
                assert "also-keep" in duo.transport._THREAD_LOCKS
                assert lock is duo.transport._THREAD_LOCKS["also-keep"]
        finally:
            duo.transport._MAX_CACHED_LOCKS = saved
            with duo.transport._THREAD_LOCKS_GUARD:
                duo.transport._THREAD_LOCKS.pop("keep-me", None)
                duo.transport._THREAD_LOCKS.pop("also-keep", None)


class TestWaitForIdleMonotonic:
    """wait_for_idle must use time.monotonic() for accurate timeout."""

    @patch("subprocess.run")
    @patch("duo.transport._time")
    def test_uses_monotonic_not_accumulation(self, mock_time, mock_run):
        mock_time.sleep = MagicMock()
        # Simulate: monotonic returns increasing values, 3rd call past deadline
        mock_time.monotonic = MagicMock(side_effect=[0.0, 0.5, 1.5, 3.0])
        call_count = [0]

        def changing(*args, **kwargs):
            call_count[0] += 1
            return MagicMock(returncode=0, stdout=f"content-{call_count[0]}", stderr="")

        mock_run.side_effect = changing
        result = wait_for_idle("test", timeout=2.0, poll_interval=0.5)
        assert result is False
        # monotonic was called (not elapsed accumulation)
        assert mock_time.monotonic.call_count >= 3


class TestWaitForDialogMonotonic:
    """wait_for_dialog must use time.monotonic() for accurate timeout."""

    @patch("duo.transport._time")
    @patch("duo.transport.is_in_dialog_stable")
    def test_uses_monotonic(self, mock_stable, mock_time):
        mock_time.sleep = MagicMock()
        mock_time.monotonic = MagicMock(side_effect=[0.0, 5.0, 11.0])
        mock_stable.return_value = False
        result = wait_for_dialog("test", timeout=10, interval=5)
        assert result is False
        assert mock_time.monotonic.call_count >= 2


# ---------------------------------------------------------------------------
# kill_pane
# ---------------------------------------------------------------------------


class TestKillPane:
    """Tests for kill_pane()."""

    def test_kill_success(self):
        from duo.transport import kill_pane

        with patch("duo.transport.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert kill_pane("my-label") is True
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert args == ["tmux", "kill-pane", "-t", "my-label"]

    def test_kill_pane_id_with_percent(self):
        from duo.transport import kill_pane

        with patch("duo.transport.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert kill_pane("%42") is True
            args = mock_run.call_args[0][0]
            assert args == ["tmux", "kill-pane", "-t", "%42"]

    def test_kill_os_error(self):
        from duo.transport import kill_pane

        with patch("duo.transport.subprocess.run", side_effect=OSError("fail")):
            assert kill_pane("my-label") is False

    def test_kill_timeout(self):
        from duo.transport import kill_pane

        with patch(
            "duo.transport.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="tmux", timeout=10),
        ):
            assert kill_pane("my-label") is False

    def test_kill_unsafe_target_rejected(self):
        from duo.transport import kill_pane

        with pytest.raises(ValueError, match="Unsafe pane target"):
            kill_pane("bad;rm -rf /")

    def test_kill_nonzero_returncode_still_returns_true(self):
        """Even if kill-pane returns non-zero, the function returns True and logs."""
        from duo.transport import kill_pane

        with patch("duo.transport.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="no pane found")
            assert kill_pane("gone-pane") is True

    def test_kill_nonzero_returncode_bytes_stderr(self):
        """Debug logging handles bytes stderr from subprocess."""
        from duo.transport import kill_pane

        with patch("duo.transport.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr=b"no pane found")
            assert kill_pane("gone-pane") is True

    def test_kill_nonzero_unexpected_error_returns_false(self):
        """Unexpected tmux errors return False so callers know kill failed."""
        from duo.transport import kill_pane

        with patch("duo.transport.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stderr="server exited unexpectedly"
            )
            assert kill_pane("some-pane") is False


class TestDetectCopilotApiError:
    """Tests for detect_copilot_api_error() and is_capi_context_error()."""

    def test_detects_capi_error(self):
        from duo.transport import detect_copilot_api_error

        assert detect_copilot_api_error("✗ Execution failed: CAPIError: 400") is True

    def test_detects_rate_limit(self):
        from duo.transport import detect_copilot_api_error

        assert detect_copilot_api_error("rate limit exceeded, try again") is True

    def test_rate_limit_case_insensitive(self):
        from duo.transport import detect_copilot_api_error

        assert detect_copilot_api_error("Rate Limit hit") is True

    def test_no_match_on_generic_error(self):
        from duo.transport import detect_copilot_api_error

        assert detect_copilot_api_error("error: something went wrong") is False

    def test_no_match_on_clean_content(self):
        from duo.transport import detect_copilot_api_error

        assert detect_copilot_api_error("all good, working fine") is False

    def test_empty_content(self):
        from duo.transport import detect_copilot_api_error

        assert detect_copilot_api_error("") is False

    def test_is_capi_context_error_positive(self):
        from duo.transport import is_capi_context_error

        assert is_capi_context_error("CAPIError: 400 Bad Request") is True

    def test_is_capi_context_error_case_sensitive(self):
        from duo.transport import is_capi_context_error

        assert is_capi_context_error("capierror: something") is False

    def test_is_capi_context_error_negative(self):
        from duo.transport import is_capi_context_error

        assert is_capi_context_error("rate limit exceeded") is False


class TestIsPaneAlive:
    """Tests for is_pane_alive()."""

    def test_alive_returns_true(self):
        from duo.transport import is_pane_alive

        with patch("duo.transport.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert is_pane_alive("%42") is True
            args = mock_run.call_args[0][0]
            assert args == ["tmux", "display-message", "-t", "%42", "-p", ""]

    def test_dead_returns_false(self):
        from duo.transport import is_pane_alive

        with patch("duo.transport.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            assert is_pane_alive("%99") is False

    def test_os_error_returns_false(self):
        from duo.transport import is_pane_alive

        with patch("duo.transport.subprocess.run", side_effect=OSError("fail")):
            assert is_pane_alive("my-pane") is False

    def test_timeout_returns_false(self):
        from duo.transport import is_pane_alive

        with patch(
            "duo.transport.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="tmux", timeout=5),
        ):
            assert is_pane_alive("my-pane") is False

    def test_unsafe_target_rejected(self):
        from duo.transport import is_pane_alive

        with pytest.raises(ValueError, match="Unsafe pane target"):
            is_pane_alive("bad;rm -rf /")

    def test_label_accepted(self):
        from duo.transport import is_pane_alive

        with patch("duo.transport.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert is_pane_alive("duo-copilot-standby") is True


class TestSplitWindowHorizontal:
    """Tests for split_window_horizontal()."""

    def test_success_returns_pane_id(self):
        from duo.transport import split_window_horizontal

        with patch("duo.transport.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="%42\n", stderr="")
            result = split_window_horizontal()
            assert result == "%42"
            args = mock_run.call_args[0][0]
            assert args == [
                "tmux",
                "split-window",
                "-h",
                "-P",
                "-F",
                "#{pane_id}",
            ]

    def test_failure_raises_runtime_error(self):
        from duo.transport import split_window_horizontal

        with patch("duo.transport.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="no room for split")
            with pytest.raises(RuntimeError, match="split-window failed"):
                split_window_horizontal()

    def test_timeout_raises_runtime_error(self):
        from duo.transport import split_window_horizontal

        with patch(
            "duo.transport.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="tmux", timeout=10),
        ):
            with pytest.raises(RuntimeError, match="timed out"):
                split_window_horizontal()
