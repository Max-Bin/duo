"""Tests for duo.transport — tmux-bridge CLI wrapper."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

import duo.transport
from duo.transport import (
    PaneInfo,
    _find_bridge,
    bridge,
    cancel_current,
    diagnose_pane,
    doctor,
    get_pane_id,
    is_process_alive,
    list_panes,
    name_pane,
    read_pane,
    resolve_label,
    send_eof,
    send_keys,
    send_message,
    send_prompt,
    type_text,
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
            [BRIDGE, "echo"], capture_output=True, text=True,
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
            [BRIDGE, "read", "editor", "100"], capture_output=True, text=True,
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
            capture_output=True, text=True,
        )


class TestSendKeys:
    @patch("subprocess.run")
    def test_single_key(self, mock_run):
        mock_run.return_value = _ok()
        send_keys("editor", "Enter")
        mock_run.assert_called_once_with(
            [BRIDGE, "keys", "editor", "Enter"],
            capture_output=True, text=True,
        )

    @patch("subprocess.run")
    def test_multiple_keys(self, mock_run):
        mock_run.return_value = _ok()
        send_keys("editor", "C-c", "Enter")
        mock_run.assert_called_once_with(
            [BRIDGE, "keys", "editor", "C-c", "Enter"],
            capture_output=True, text=True,
        )


class TestNamePane:
    @patch("subprocess.run")
    def test_calls_bridge(self, mock_run):
        mock_run.return_value = _ok()
        name_pane("%5", "editor")
        mock_run.assert_called_once_with(
            [BRIDGE, "name", "%5", "editor"],
            capture_output=True, text=True,
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
        assert panes[0] == PaneInfo("%1", "main:0", "80x24", "zsh", "shell", "/home/user")
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
    @patch("subprocess.run")
    def test_call_sequence(self, mock_run):
        mock_run.return_value = _ok()
        send_prompt("agent", "do something")
        expected = [
            call([BRIDGE, "read", "agent", "5"], capture_output=True, text=True),
            call([BRIDGE, "type", "agent", "do something"], capture_output=True, text=True),
            call([BRIDGE, "read", "agent", "5"], capture_output=True, text=True),
            call([BRIDGE, "keys", "agent", "Enter"], capture_output=True, text=True),
        ]
        assert mock_run.call_args_list == expected


class TestSendMessage:
    @patch("subprocess.run")
    def test_call_sequence(self, mock_run):
        mock_run.return_value = _ok()
        send_message("agent", "hello")
        expected = [
            call([BRIDGE, "read", "agent", "5"], capture_output=True, text=True),
            call([BRIDGE, "message", "agent", "hello"], capture_output=True, text=True),
            call([BRIDGE, "read", "agent", "5"], capture_output=True, text=True),
            call([BRIDGE, "keys", "agent", "Enter"], capture_output=True, text=True),
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
            [BRIDGE, "keys", "agent", "C-c"], capture_output=True, text=True,
        )


class TestSendEof:
    @patch("subprocess.run")
    def test_sends_ctrl_d(self, mock_run):
        mock_run.return_value = _ok()
        send_eof("agent")
        calls = mock_run.call_args_list
        assert len(calls) == 2
        assert calls[1] == call(
            [BRIDGE, "keys", "agent", "C-d"], capture_output=True, text=True,
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
            [BRIDGE, "read", "agent", "200"], capture_output=True, text=True,
        )


class TestIsProcessAlive:
    @patch("subprocess.run")
    def test_shell_process_returns_false(self, mock_run):
        mock_run.return_value = _ok(
            "HEADER\n%1 main:0 80x24 zsh agent /home\n"
        )
        assert is_process_alive("agent") is False

    @patch("subprocess.run")
    def test_dash_shell_returns_false(self, mock_run):
        mock_run.return_value = _ok(
            "HEADER\n%1 main:0 80x24 -zsh agent /home\n"
        )
        assert is_process_alive("agent") is False

    @patch("subprocess.run")
    def test_non_shell_returns_true(self, mock_run):
        mock_run.return_value = _ok(
            "HEADER\n%1 main:0 80x24 copilot agent /home\n"
        )
        assert is_process_alive("agent") is True

    @patch("subprocess.run")
    def test_unknown_label_returns_false(self, mock_run):
        mock_run.return_value = _ok(
            "HEADER\n%1 main:0 80x24 copilot other /home\n"
        )
        assert is_process_alive("missing") is False
