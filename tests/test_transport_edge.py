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
    """Parametrized strip_ansi covering all ANSI variant families."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("hello world", "hello world", id="plain-text"),
            pytest.param("", "", id="empty-string"),
            pytest.param("\x1b[31mred\x1b[0m", "red", id="basic-color"),
            pytest.param("\x1b[1mbold\x1b[0m", "bold", id="bold"),
            pytest.param("\x1b[38;5;196mhi\x1b[0m", "hi", id="256-color"),
            pytest.param("\x1b[38;2;255;0;0mR\x1b[0m", "R", id="truecolor"),
            pytest.param("\x1b[1m\x1b[31m\x1b[4mX\x1b[0m", "X", id="stacked-codes"),
            pytest.param("\x1b[31m\x1b[1m\x1b[0m", "", id="only-ansi"),
            pytest.param(
                "\x1b[1m\x1b[31mhello\x1b[0m \x1b[32mworld\x1b[0m",
                "hello world",
                id="nested",
            ),
            pytest.param("text\x1b[", "text\x1b[", id="partial-sequence"),
            pytest.param("❯ Type @", "❯ Type @", id="unicode-preserved"),
            pytest.param(
                "\x1b[2Ahello\x1b[5Cworld",
                "helloworld",
                id="cursor-movement",
            ),
        ],
    )
    def test_strip_ansi(self, raw: str, expected: str) -> None:
        from duo.transport import strip_ansi

        assert strip_ansi(raw) == expected


class TestDetectCopilotApiErrorParametrized:
    """Parametrized tests for detect_copilot_api_error()."""

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            pytest.param(
                "✗ Execution failed: CAPIError: 400",
                True,
                id="capi-error-400",
            ),
            pytest.param(
                "rate limit exceeded, try again",
                True,
                id="rate-limit-lower",
            ),
            pytest.param(
                "Rate Limit hit",
                True,
                id="rate-limit-mixed-case",
            ),
            pytest.param(
                "RATE LIMIT exceeded",
                True,
                id="rate-limit-upper",
            ),
            pytest.param(
                "Some output\nCAPIError: 429 context limit\nmore text",
                True,
                id="capi-in-multiline",
            ),
            pytest.param(
                "Some output with rate limit in the middle of a line",
                True,
                id="rate-limit-substring",
            ),
            pytest.param(
                "error: something went wrong",
                False,
                id="generic-error",
            ),
            pytest.param(
                "all good, working fine",
                False,
                id="clean-content",
            ),
            pytest.param("", False, id="empty-string"),
            pytest.param(
                "normal copilot output line\n❯ Type @ to mention files",
                False,
                id="normal-prompt",
            ),
        ],
    )
    def test_detect(self, content: str, expected: bool) -> None:
        from duo.transport import detect_copilot_api_error

        assert detect_copilot_api_error(content) is expected


class TestIsCAPIContextErrorParametrized:
    """Parametrized tests for is_capi_context_error()."""

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            pytest.param(
                "CAPIError: 400 Bad Request",
                True,
                id="capi-400",
            ),
            pytest.param(
                "CAPIError: context window exceeded",
                True,
                id="capi-context",
            ),
            pytest.param(
                "capierror: something",
                False,
                id="lowercase-rejected",
            ),
            pytest.param(
                "rate limit exceeded",
                False,
                id="not-capi-error",
            ),
            pytest.param("", False, id="empty-string"),
        ],
    )
    def test_detect(self, content: str, expected: bool) -> None:
        from duo.transport import is_capi_context_error

        assert is_capi_context_error(content) is expected


class TestLabelValidationParametrized:
    """Parametrized tests for _validate_label (via resolve_label)."""

    @pytest.mark.parametrize(
        "label",
        [
            pytest.param("lab;rm -rf /", id="semicolon"),
            pytest.param("pane$(whoami)", id="dollar-paren"),
            pytest.param("a b", id="space"),
            pytest.param("foo&bar", id="ampersand"),
            pytest.param("x|y", id="pipe"),
            pytest.param("a`id`b", id="backtick"),
            pytest.param("a\nb", id="newline"),
            pytest.param("a>b", id="redirect"),
        ],
    )
    def test_unsafe_label_rejected(self, label: str) -> None:
        with pytest.raises(ValueError, match="Unsafe pane label"):
            resolve_label(label)

    @pytest.mark.parametrize(
        "label",
        [
            pytest.param("task-fix.auth_01", id="dots-underscores"),
            pytest.param("duo-copilot-standby", id="hyphens"),
            pytest.param("simple", id="simple-alpha"),
            pytest.param("UPPER_CASE.v2", id="upper-dots"),
        ],
    )
    def test_safe_label_accepted(self, label: str) -> None:
        from unittest.mock import MagicMock, patch

        with patch(
            "subprocess.run",
            return_value=MagicMock(returncode=0, stdout="%1\n", stderr=""),
        ):
            result = resolve_label(label)
            assert result == "%1"


class TestDialogBoundaryParametrized:
    """Parametrized tests for detect_dialog_kind with various content shapes."""

    @pytest.mark.parametrize(
        ("content", "expected_kind"),
        [
            pytest.param(
                "normal output text\n❯ Type @ to mention files",
                DialogKind.NONE,
                id="main-prompt-not-dialog",
            ),
            pytest.param(
                "╭─ Choose ─╮\n❯ 1. Yes\n  2. No\n╰───────────╯",
                DialogKind.OPTION,
                id="yes-no-option",
            ),
            pytest.param(
                "╭─ Provide details ─╮\nType your answer\n╰────────────────────╯",
                DialogKind.TEXT,
                id="text-input",
            ),
            pytest.param(
                "1. Fix\n2. Deploy\n3. Test\n\n◉ Working on step 1...",
                DialogKind.NONE,
                id="numbered-list-not-dialog",
            ),
            pytest.param(
                "",
                DialogKind.NONE,
                id="empty-content",
            ),
        ],
    )
    def test_kind_detection(self, content: str, expected_kind: DialogKind) -> None:
        from duo.transport import detect_dialog_kind

        assert detect_dialog_kind(content) == expected_kind
