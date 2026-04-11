"""Edge case tests for duo.transport — large options, ANSI, server down, long text."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import duo.transport
from duo.transport import (
    DialogKind,
    read_pane,
    resolve_label,
    select_dialog_option,
    send_text_dialog_message,
)

BRIDGE = "/usr/local/bin/tmux-bridge"


@pytest.fixture(autouse=True)
def _mock_bridge_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin _BRIDGE so tests never try to locate the real binary."""
    monkeypatch.setattr(duo.transport, "_BRIDGE", BRIDGE)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTransportEdgeCasesNew:
    """Edge case tests for transport module."""

    @pytest.fixture(autouse=True)
    def _no_sleep(self):
        """Eliminate real sleeps for test speed."""
        with patch("duo.transport._time.sleep"):
            yield

    @patch("subprocess.run")
    @patch("duo.transport._time")
    @patch("duo.transport.is_in_dialog")
    @patch("duo.transport.is_in_dialog_stable")
    def test_select_dialog_option_large_number(
        self,
        mock_stable: MagicMock,
        mock_dialog: MagicMock,
        mock_time: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        """select_dialog_option handles large option numbers like 99."""
        mock_stable.return_value = True
        mock_dialog.return_value = True
        mock_time.sleep = MagicMock()
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="some output",
            stderr="",
        )

        from duo.transport import get_pr_log

        initial_count = len(get_pr_log())
        select_dialog_option("test-pane", "99")
        log = get_pr_log()
        assert len(log) == initial_count + 1
        assert log[-1]["context"] == "99"

    def test_detect_dialog_kind_with_ansi_256_color(self) -> None:
        """detect_dialog_kind strips ANSI 256-color codes inside dialog box."""
        from duo.transport import detect_dialog_kind

        content = (
            "\x1b[38;5;82m\u256d\u2500 Choose \u2500\u256e\x1b[0m\n"
            "\x1b[38;5;214m\u276f 1.\x1b[0m Accept\n"
            "\x1b[38;5;214m  2.\x1b[0m Reject\n"
            "\x1b[38;5;82m\u2570\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u256f\x1b[0m"
        )
        assert detect_dialog_kind(content) == DialogKind.OPTION

    def test_strip_ansi_truecolor_sequences(self) -> None:
        """strip_ansi removes truecolor RGB escape sequences."""
        from duo.transport import strip_ansi

        text = "\x1b[38;2;255;0;0mRed\x1b[0m \x1b[38;2;0;255;0mGreen\x1b[0m"
        assert strip_ansi(text) == "Red Green"

    def test_strip_ansi_cursor_movement(self) -> None:
        """strip_ansi removes cursor movement sequences."""
        from duo.transport import strip_ansi

        text = "\x1b[2Ahello\x1b[5Cworld"
        assert strip_ansi(text) == "helloworld"

    def test_strip_ansi_consecutive_codes(self) -> None:
        """strip_ansi handles many consecutive ANSI codes with no text."""
        from duo.transport import strip_ansi

        text = "\x1b[1m\x1b[31m\x1b[4m\x1b[48;5;16mX\x1b[0m"
        assert strip_ansi(text) == "X"

    def test_read_pane_tmux_server_down(self) -> None:
        """read_pane raises TmuxServerDownError when tmux server is down."""
        from duo.transport import TmuxServerDownError

        result = MagicMock(returncode=1, stdout="", stderr="no server running")
        with (
            patch("subprocess.run", return_value=result),
            patch("duo.transport._bridge_bin", return_value="tmux-bridge"),
        ):
            with pytest.raises(TmuxServerDownError):
                read_pane("nonexistent-pane")

    @patch("duo.transport.detect_dialog_kind", return_value=DialogKind.NONE)
    @patch("duo.transport.send_keys")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.type_text")
    def test_send_text_dialog_empty_string(
        self,
        mock_type: MagicMock,
        mock_read: MagicMock,
        mock_keys: MagicMock,
        mock_detect: MagicMock,
    ) -> None:
        """send_text_dialog_message handles empty string input."""
        mock_read.return_value = ""
        result = send_text_dialog_message("test", "")
        assert result is True
        mock_type.assert_called_once_with("test", "")

    @patch("duo.transport.detect_dialog_kind", return_value=DialogKind.NONE)
    @patch("duo.transport.send_keys")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.type_text")
    def test_send_text_dialog_very_long_text(
        self,
        mock_type: MagicMock,
        mock_read: MagicMock,
        mock_keys: MagicMock,
        mock_detect: MagicMock,
    ) -> None:
        """send_text_dialog_message handles very long text (>1000 chars)."""
        long_text = "x" * 1500
        mock_read.return_value = long_text
        result = send_text_dialog_message("test", long_text)
        assert result is True
        mock_type.assert_called_once_with("test", long_text)


# ---------------------------------------------------------------------------
# Parametrized transport tests — Round HO
# ---------------------------------------------------------------------------


class TestStripAnsiParametrized:
    """Test strip_ansi with representative ANSI variant families."""

    def test_strip_ansi_cases(self) -> None:
        from duo.transport import strip_ansi

        cases = [
            ("hello world", "hello world"),
            ("", ""),
            ("\x1b[31mred\x1b[0m", "red"),
            ("\x1b[1mbold\x1b[0m", "bold"),
            ("\x1b[38;5;196mhi\x1b[0m", "hi"),
            ("\x1b[38;2;255;0;0mR\x1b[0m", "R"),
            ("\x1b[1m\x1b[31m\x1b[4mX\x1b[0m", "X"),
            ("\x1b[31m\x1b[1m\x1b[0m", ""),
            ("\x1b[1m\x1b[31mhello\x1b[0m \x1b[32mworld\x1b[0m", "hello world"),
            ("text\x1b[", "text\x1b["),
            ("❯ Type @", "❯ Type @"),
            ("\x1b[2Ahello\x1b[5Cworld", "helloworld"),
        ]
        for raw, expected in cases:
            assert strip_ansi(raw) == expected, f"strip_ansi({raw!r})"


class TestDetectCopilotApiErrorParametrized:
    """Tests for detect_copilot_api_error()."""

    def test_detect_cases(self) -> None:
        from duo.transport import detect_copilot_api_error

        positive = [
            "✗ Execution failed: CAPIError: 400",
            "rate limit exceeded, try again",
            "Rate Limit hit",
            "RATE LIMIT exceeded",
            "Some output\nCAPIError: 429 context limit\nmore text",
            "Some output with rate limit in the middle of a line",
        ]
        negative = [
            "error: something went wrong",
            "all good, working fine",
            "",
            "normal copilot output line\n❯ Type @ to mention files",
        ]
        for content in positive:
            assert detect_copilot_api_error(content) is True, (
                f"should detect: {content[:40]}"
            )
        for content in negative:
            assert detect_copilot_api_error(content) is False, (
                f"false positive: {content[:40]}"
            )


class TestIsCAPIContextErrorParametrized:
    """Tests for is_capi_context_error()."""

    def test_detect_cases(self) -> None:
        from duo.transport import is_capi_context_error

        assert is_capi_context_error("CAPIError: 400 Bad Request") is True
        assert is_capi_context_error("CAPIError: context window exceeded") is True
        assert is_capi_context_error("capierror: something") is False
        assert is_capi_context_error("rate limit exceeded") is False
        assert is_capi_context_error("") is False


class TestLabelValidationParametrized:
    """Tests for _validate_label (via resolve_label)."""

    def test_unsafe_labels_rejected(self) -> None:
        unsafe = [
            "lab;rm -rf /",
            "pane$(whoami)",
            "a b",
            "foo&bar",
            "x|y",
            "a`id`b",
            "a\nb",
            "a>b",
        ]
        for label in unsafe:
            with pytest.raises(ValueError, match="Unsafe pane label"):
                resolve_label(label)

    def test_safe_labels_accepted(self) -> None:
        from unittest.mock import MagicMock, patch

        safe = ["task-fix.auth_01", "duo-copilot-standby", "simple", "UPPER_CASE.v2"]
        for label in safe:
            with patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout="%1\n", stderr=""),
            ):
                result = resolve_label(label)
                assert result == "%1"


class TestDialogBoundaryParametrized:
    """Tests for detect_dialog_kind with various content shapes."""

    def test_kind_detection_cases(self) -> None:
        from duo.transport import detect_dialog_kind

        cases = [
            ("normal output text\n❯ Type @ to mention files", DialogKind.NONE),
            ("╭─ Choose ─╮\n❯ 1. Yes\n  2. No\n╰───────────╯", DialogKind.OPTION),
            (
                "╭─ Provide details ─╮\nType your answer\n╰────────────────────╯",
                DialogKind.TEXT,
            ),
            ("1. Fix\n2. Deploy\n3. Test\n\n◉ Working on step 1...", DialogKind.NONE),
            ("", DialogKind.NONE),
        ]
        for content, expected_kind in cases:
            assert detect_dialog_kind(content) == expected_kind, (
                f"content={content[:30]!r}"
            )
