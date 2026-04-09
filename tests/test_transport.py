"""Tests for duo.transport — tmux-bridge CLI wrapper."""

from __future__ import annotations

import logging
import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

import duo.transport
from duo.transport import (
    DialogKind,
    PaneInfo,
    _find_bridge,
    _is_at_main_prompt,
    _retry,
    approve_permission,
    bridge,
    cancel_current,
    diagnose_pane,
    doctor,
    get_dialog_kind,
    get_pane_id,
    is_in_dialog,
    is_in_dialog_stable,
    is_permission_dialog,
    is_process_alive,
    list_panes,
    name_pane,
    read_pane,
    resolve_label,
    safe_enter,
    select_dialog_option,
    select_other_option,
    send_bootstrap,
    send_eof,
    send_keys,
    send_message,
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
    def test_single_key(self, mock_run):
        mock_run.return_value = _ok()
        send_keys("editor", "Enter")
        mock_run.assert_called_once_with(
            [BRIDGE, "keys", "editor", "Enter"],
            capture_output=True,
            text=True,
            timeout=30,
        )

    @patch("subprocess.run")
    def test_multiple_keys(self, mock_run):
        mock_run.return_value = _ok()
        send_keys("editor", "C-c", "Enter")
        mock_run.assert_called_once_with(
            [BRIDGE, "keys", "editor", "C-c", "Enter"],
            capture_output=True,
            text=True,
            timeout=30,
        )


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
        expected = [
            call([BRIDGE, "read", "agent", "5"], capture_output=True, text=True, timeout=30),
            call([BRIDGE, "type", "agent", "cd /tmp"], capture_output=True, text=True, timeout=30),
            call([BRIDGE, "read", "agent", "5"], capture_output=True, text=True, timeout=30),
            call([BRIDGE, "keys", "agent", "Enter"], capture_output=True, text=True, timeout=30),
        ]
        assert mock_run.call_args_list == expected

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
        expected = [
            call([BRIDGE, "read", "agent", "5"], capture_output=True, text=True, timeout=30),
            call([BRIDGE, "message", "agent", "hello"], capture_output=True, text=True, timeout=30),
            call([BRIDGE, "read", "agent", "5"], capture_output=True, text=True, timeout=30),
            call([BRIDGE, "keys", "agent", "Enter"], capture_output=True, text=True, timeout=30),
        ]
        assert mock_run.call_args_list == expected


class TestCancelCurrent:
    @patch("subprocess.run")
    def test_sends_ctrl_c(self, mock_run):
        mock_run.return_value = _ok()
        cancel_current("agent")
        calls = mock_run.call_args_list
        assert len(calls) == 2
        assert calls[1] == call(
            [BRIDGE, "keys", "agent", "C-c"],
            capture_output=True,
            text=True,
            timeout=30,
        )


class TestSendEof:
    @patch("subprocess.run")
    def test_sends_ctrl_d(self, mock_run):
        mock_run.return_value = _ok()
        send_eof("agent")
        calls = mock_run.call_args_list
        assert len(calls) == 2
        assert calls[1] == call(
            [BRIDGE, "keys", "agent", "C-d"],
            capture_output=True,
            text=True,
            timeout=30,
        )


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


# ── _is_at_main_prompt ───────────────────────────────────────────────


class TestIsAtMainPrompt:
    def test_skips_remaining_reqs_line(self):
        """Lines with 'Remaining reqs' are skipped to find prompt (line 231)."""
        content = "Remaining reqs: 42\n❯ Type @ to mention files"
        assert _is_at_main_prompt(content) is True

    def test_skips_shift_tab_line(self):
        """Lines with 'shift+tab' are skipped to find prompt (line 231)."""
        content = "Press shift+tab for options\n❯"
        assert _is_at_main_prompt(content) is True

    def test_remaining_reqs_without_prompt(self):
        """'Remaining reqs' line alone, no prompt below → False."""
        content = "some output\nRemaining reqs: 10"
        assert _is_at_main_prompt(content) is False


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
        # Return same content twice = idle
        mock_run.return_value = MagicMock(
            returncode=0, stdout="stable output", stderr=""
        )
        assert wait_for_idle("test-pane", timeout=5.0, poll_interval=0.01) is True

    @patch("subprocess.run")
    @patch("duo.transport._time")
    def test_timeout(self, mock_time, mock_run):
        mock_time.sleep = MagicMock()
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
        mock_stable.return_value = True
        assert wait_for_dialog("test-pane", timeout=10, interval=1) is True
        assert mock_stable.call_count == 1

    @patch("duo.transport._time")
    @patch("duo.transport.is_in_dialog_stable")
    def test_wait_for_dialog_timeout(self, mock_stable, mock_time):
        """Always returns False → returns False after timeout."""
        mock_time.sleep = MagicMock()
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


# ── select_dialog_option ─────────────────────────────────────────────


class TestSelectDialogOption:
    @patch("subprocess.run")
    @patch("duo.transport._time")
    @patch("duo.transport.is_in_dialog")
    @patch("duo.transport.is_in_dialog_stable")
    def test_select_dialog_option_success(self, mock_stable, mock_dialog, mock_time, mock_run):
        """Mock is_in_dialog_stable True, verify type_text and send_keys called, PR recorded."""
        mock_time.sleep = MagicMock()
        mock_stable.return_value = True
        mock_dialog.return_value = True  # Dialog still present after type_text → Enter sent
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
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["tmux-bridge", "read"], timeout=30)
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


# ── _is_at_main_prompt edge cases ────────────────────────────────────


class TestIsAtMainPromptEdgeCases:
    def test_empty_content(self):
        """Empty string is not at prompt."""
        assert _is_at_main_prompt("") is False

    def test_only_separators(self):
        """Content with only separator lines is not at prompt."""
        assert _is_at_main_prompt("─────\n─────") is False

    def test_chevron_without_menu_text(self):
        """❯ with non-menu text is not at prompt."""
        assert _is_at_main_prompt("❯ some random text here") is False

    def test_chevron_after_separators(self):
        """❯ prompt preceded by separator lines IS at prompt."""
        content = "─────\nRemaining reqs: 10\n❯ Type @ to mention files"
        assert _is_at_main_prompt(content) is True

    def test_spinner_suppresses_prompt(self) -> None:
        """Spinner marker means Copilot is processing — not idle."""
        content = "◉ Processing...\n❯ Type @ to mention files"
        assert _is_at_main_prompt(content) is False

    def test_spinner_variants(self) -> None:
        """All spinner markers suppress the prompt."""
        for marker in ("◉ ", "◎ ", "○ "):
            content = f"{marker}Thinking\n❯ Type @ to mention files"
            assert _is_at_main_prompt(content) is False, marker

    def test_shift_tab_skipped(self) -> None:
        """shift+tab line is skipped when scanning for prompt."""
        content = "shift+tab to switch\n❯ Type @ to mention files"
        assert _is_at_main_prompt(content) is True

    def test_remaining_reqs_skipped(self) -> None:
        """Remaining reqs line is skipped when scanning for prompt."""
        content = "Remaining reqs: 5\n❯"
        assert _is_at_main_prompt(content) is True

    def test_non_prompt_text_breaks_scan(self) -> None:
        """Non-separator, non-prompt text between bottom and ❯ stops scan."""
        # ❯ is NOT the last stripped line — 'Some output' appears below
        content = "❯ Type @ to mention files\nSome output"
        assert _is_at_main_prompt(content) is False

    def test_mention_files_prompt(self) -> None:
        """❯ with 'mention files' text is at prompt."""
        assert _is_at_main_prompt("❯ mention files to include") is True


# ── is_permission_dialog ──────────────────────────────────────────────


class TestIsPermissionDialog:
    @patch("duo.transport.read_pane")
    def test_detects_run_permission(self, mock_read):
        """Detects 'Do you want to run' as permission dialog."""
        mock_read.return_value = "╭──\n  Do you want to run this command?\n  1. Yes\n╰──"
        assert is_permission_dialog("test") is True

    @patch("duo.transport.read_pane")
    def test_detects_allow_directory(self, mock_read):
        """Detects 'Allow directory' as permission dialog."""
        mock_read.return_value = "Allow directory access\n  1. Allow\n  2. Deny"
        assert is_permission_dialog("test") is True

    @patch("duo.transport.read_pane")
    def test_not_permission_for_regular_dialog(self, mock_read):
        """Regular dialog without permission keywords returns False."""
        mock_read.return_value = "What would you like to do?\n  1. Option A\n  2. Option B"
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
    def test_no_options_falls_back_to_1(self, mock_read, mock_select):
        """Empty pane with no numbered options falls back to 1."""
        mock_read.return_value = "╭──\nsome random text\n╰──"
        approve_permission("test")
        mock_select.assert_called_once_with("test", "1")

    @patch("duo.transport.select_dialog_option")
    @patch("duo.transport.read_pane")
    def test_add_to_allowed_list(self, mock_read, mock_select):
        """Picks 'Add to allowed list' option."""
        mock_read.return_value = (
            "╭──\n"
            "  1. Yes\n"
            "  2. Add to allowed list\n"
            "  3. No\n"
            "╰──"
        )
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
        mock_read.return_value = (
            "╭──\n"
            "Choose:\n"
            "  1. Yes\n"
            "  2. Add something else\n"
            "╰──"
        )
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


# ---------------------------------------------------------------------------
# select_other_option
# ---------------------------------------------------------------------------


class TestSelectOtherOption:
    """Tests for select_other_option — navigate to 'Other', type, submit."""

    @patch("duo.transport._record_pr")
    @patch("duo.transport.safe_enter")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.type_text")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.is_in_dialog", return_value=True)
    def test_basic_navigation(
        self, mock_dialog, mock_keys, mock_type, mock_read, mock_enter, mock_pr
    ):
        """Navigates to last option, types text, and submits."""
        mock_read.return_value = (
            "╭─ Choose an action: ─╮\n"
            "  ❯ 1. Run command\n"
            "  2. Edit file\n"
            "  3. Other\n"
            "╰─\n"
        )
        select_other_option("test", "custom action")
        # Should navigate down 2 times (from pos 1 to pos 3)
        assert mock_keys.call_count == 2
        mock_type.assert_called_once_with("test", "custom action")
        mock_enter.assert_called_once_with("test")
        mock_pr.assert_called_once()

    @patch("duo.transport.read_pane")
    @patch("duo.transport._is_at_main_prompt", return_value=True)
    def test_blocked_at_prompt(self, mock_prompt, mock_read):
        """Refuses when at main ❯ prompt."""
        mock_read.return_value = "❯ "
        with pytest.raises(RuntimeError, match="BLOCKED"):
            select_other_option("test", "text")

    @patch("duo.transport.read_pane")
    @patch("duo.transport.is_in_dialog", return_value=False)
    def test_not_in_dialog(self, mock_dialog, mock_read):
        """Refuses when not in a dialog."""
        mock_read.return_value = "some output\n"
        with pytest.raises(RuntimeError, match="SAFETY"):
            select_other_option("test", "text")

    @patch("duo.transport.read_pane")
    @patch("duo.transport.is_in_dialog", return_value=True)
    def test_too_few_options(self, mock_dialog, mock_read):
        """Refuses when dialog has fewer than 2 options."""
        mock_read.return_value = (
            "╭─ Choose: ─╮\n"
            "  ❯ 1. Only option\n"
            "╰─\n"
        )
        with pytest.raises(RuntimeError, match="need ≥2"):
            select_other_option("test", "text")

    @patch("duo.transport._record_pr")
    @patch("duo.transport.safe_enter")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.type_text")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.is_in_dialog", return_value=True)
    def test_cursor_already_at_last(
        self, mock_dialog, mock_keys, mock_type, mock_read, mock_enter, mock_pr
    ):
        """No navigation needed when cursor is already at last option."""
        mock_read.return_value = (
            "╭─ Choose: ─╮\n"
            "  1. Run\n"
            "  ❯ 2. Other\n"
            "╰─\n"
        )
        select_other_option("test", "custom")
        # Should not navigate at all (already at position 2 of 2)
        mock_keys.assert_not_called()
        mock_type.assert_called_once_with("test", "custom")

    @patch("duo.transport._record_pr")
    @patch("duo.transport.safe_enter")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.type_text")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.is_in_dialog", return_value=True)
    def test_ignores_options_above_box(
        self, mock_dialog, mock_keys, mock_type, mock_read, mock_enter, mock_pr
    ):
        """Numbered lines in scrollback above the dialog box are ignored."""
        mock_read.return_value = (
            "Steps:\n"
            "  1. Install\n"
            "  2. Build\n"
            "╭─ Action ─╮\n"
            "  ❯ 1. Run\n"
            "  2. Other\n"
            "╰─\n"
        )
        select_other_option("test", "my text")
        # Only 2 options inside box; cursor at 1, navigate to 2
        assert mock_keys.call_count == 1


# ---------------------------------------------------------------------------
# send_text_dialog_message
# ---------------------------------------------------------------------------


class TestSendTextDialogMessage:
    """Tests for send_text_dialog_message — reliable Enter for text dialogs."""

    @patch("duo.transport._detect_dialog_kind", return_value=DialogKind.NONE)
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

    @patch("duo.transport._detect_dialog_kind")
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

    @patch("duo.transport._detect_dialog_kind", return_value=DialogKind.TEXT)
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

    @patch("duo.transport._detect_dialog_kind", return_value=DialogKind.NONE)
    @patch("duo.transport.send_keys")
    @patch("duo.transport.resolve_label", return_value="%42")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.type_text")
    def test_sigwinch_on_invisible_text(self, mock_type, mock_read, mock_resolve, mock_keys, mock_detect):
        """When typed text is not visible, sends SIGWINCH to refresh."""
        call_count = {"n": 0}

        def fake_read(label, lines):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return "no text here"
            return "my answer is visible"

        mock_read.side_effect = fake_read
        pid_run = MagicMock(returncode=0, stdout="12345\n")
        with patch("subprocess.run", return_value=pid_run), \
             patch("os.kill") as mock_kill:
            result = send_text_dialog_message("test", "my answer")
        assert result is True
        # os.kill should have been called with SIGWINCH
        assert mock_kill.call_count >= 1

    @patch("duo.transport._detect_dialog_kind", return_value=DialogKind.NONE)
    @patch("duo.transport.send_keys")
    @patch("duo.transport.resolve_label", side_effect=RuntimeError("no pane"))
    @patch("duo.transport.read_pane")
    @patch("duo.transport.type_text")
    def test_sigwinch_failure_ignored(self, mock_type, mock_read, mock_resolve, mock_keys, mock_detect):
        """SIGWINCH failure is silently ignored."""
        # read_pane calls: verify text (x3 retries) + after Enter (x1) = 4+
        mock_read.side_effect = ["no text", "no text", "my answer", "dismissed"]
        result = send_text_dialog_message("test", "my answer")
        assert result is True


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


class TestAnsiInDialogDetection:
    """Ensure dialog detection works with ANSI-colored pane output."""

    def test_main_prompt_with_ansi_colored_prompt(self) -> None:
        from duo.transport import _is_at_main_prompt
        content = "\x1b[32m❯\x1b[0m \x1b[90mType @ to mention files\x1b[0m"
        assert _is_at_main_prompt(content) is True

    def test_main_prompt_with_ansi_spinner_detected(self) -> None:
        from duo.transport import _is_at_main_prompt
        content = "\x1b[33m◉ \x1b[0mProcessing...\n❯"
        assert _is_at_main_prompt(content) is False

    def test_main_prompt_with_ansi_box_chars(self) -> None:
        from duo.transport import _is_at_main_prompt
        content = "\x1b[1m╭─\x1b[0m question\n1. Yes\n\x1b[1m╰─\x1b[0m\n❯"
        assert _is_at_main_prompt(content) is False

    def test_detect_option_dialog_with_ansi(self) -> None:
        from duo.transport import DialogKind, _detect_dialog_kind
        content = (
            "\x1b[1m╭─ Choose ─╮\x1b[0m\n"
            "\x1b[32m❯ 1.\x1b[0m Accept\n"
            "  2. Reject\n"
            "\x1b[1m╰─────────╯\x1b[0m"
        )
        assert _detect_dialog_kind(content) == DialogKind.OPTION

    def test_detect_text_dialog_with_ansi(self) -> None:
        from duo.transport import DialogKind, _detect_dialog_kind
        content = (
            "\x1b[1m╭─ Input ─╮\x1b[0m\n"
            "\x1b[90mType your answer\x1b[0m\n"
            "\x1b[1m╰─────────╯\x1b[0m"
        )
        assert _detect_dialog_kind(content) == DialogKind.TEXT

    def test_detect_no_dialog_with_ansi_noise(self) -> None:
        from duo.transport import DialogKind, _detect_dialog_kind
        content = "\x1b[32m❯\x1b[0m \x1b[90mType @ to mention files\x1b[0m"
        assert _detect_dialog_kind(content) == DialogKind.NONE

    def test_read_pane_strips_ansi(self) -> None:
        """read_pane() should return ANSI-free text."""
        with patch("duo.transport.bridge", return_value="\x1b[31mhello\x1b[0m"):
            result = read_pane("test", 10)
        assert result == "hello"
        assert "\x1b" not in result


class TestTmuxServerDownError:
    """Tests for TmuxServerDownError detection."""

    def test_bridge_detects_server_down(self) -> None:
        """Bridge raises TmuxServerDownError on 'no server running'."""
        from duo.transport import TmuxServerDownError
        result = MagicMock()
        result.returncode = 1
        result.stderr = "error: no server running on /tmp/tmux-1000/default"
        result.stdout = ""
        with patch("subprocess.run", return_value=result), \
             patch("duo.transport._bridge_bin", return_value="tmux-bridge"):
            with pytest.raises(TmuxServerDownError, match="tmux server is down"):
                bridge(["read", "test", "10"])

    def test_bridge_detects_lost_server(self) -> None:
        """Bridge raises TmuxServerDownError on 'lost server'."""
        from duo.transport import TmuxServerDownError
        result = MagicMock()
        result.returncode = 1
        result.stderr = "lost server"
        result.stdout = ""
        with patch("subprocess.run", return_value=result), \
             patch("duo.transport._bridge_bin", return_value="tmux-bridge"):
            with pytest.raises(TmuxServerDownError, match="tmux server is down"):
                bridge(["read", "test", "10"])

    def test_bridge_normal_error_not_server_down(self) -> None:
        """Normal errors don't raise TmuxServerDownError."""
        result = MagicMock()
        result.returncode = 1
        result.stderr = "error: no pane found with label 'missing'"
        result.stdout = ""
        with patch("subprocess.run", return_value=result), \
             patch("duo.transport._bridge_bin", return_value="tmux-bridge"):
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
