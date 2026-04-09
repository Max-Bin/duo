"""Regression tests for the 3-layer send_keys defense against tmux layout bugs.

Layer 1: Raw hex bytes via tmux send-keys -H (bypasses key-name translation)
Layer 2: send_keys_verified() with retry + content comparison
Layer 3: Auto-resize pane to minimum dimensions when verified send fails

These tests reproduce the specific failure mode observed during CEO sessions:
pane resize from 224x56 → 224x32 caused Copilot's Ink TUI to silently drop
key-name events while raw hex bytes continued to work.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

from duo.transport import (
    _KEY_TO_HEX,
    MINIMUM_PANE_COLS,
    MINIMUM_PANE_ROWS,
    _tmux_send_hex,
    ensure_minimum_pane_size,
    send_keys,
    send_keys_verified,
)


class TestLayer1HexBypass:
    """Layer 1: Hex byte path bypasses key-name translation."""

    def test_enter_uses_hex_not_key_name(self) -> None:
        """send_keys('Enter') should use -H 0d, not key-name 'Enter'."""
        with (
            patch("duo.transport.resolve_label", return_value="%1"),
            patch("duo.transport._tmux_send_hex") as mock_hex,
        ):
            send_keys("test-pane", "Enter")
            mock_hex.assert_called_once_with("%1", "0d")

    def test_arrows_use_hex_escape_sequences(self) -> None:
        """Arrow keys should use ANSI escape hex sequences."""
        with (
            patch("duo.transport.resolve_label", return_value="%1"),
            patch("duo.transport._tmux_send_hex") as mock_hex,
        ):
            send_keys("test-pane", "Up")
            mock_hex.assert_called_once_with("%1", "1b 5b 41")

    def test_unknown_key_falls_through_to_bridge(self) -> None:
        """Keys not in _KEY_TO_HEX fall through to tmux-bridge name-based path."""
        with (
            patch("duo.transport.resolve_label", return_value="%1"),
            patch("duo.transport._tmux_send_hex") as mock_hex,
            patch("duo.transport.bridge") as mock_bridge,
        ):
            send_keys("test-pane", "F12")
            mock_hex.assert_not_called()
            mock_bridge.assert_called_once_with(["keys", "test-pane", "F12"])

    def test_all_mapped_keys_have_hex(self) -> None:
        """Every key in _KEY_TO_HEX has a non-empty hex string."""
        for key, hex_code in _KEY_TO_HEX.items():
            assert hex_code, f"Key {key!r} has empty hex mapping"
            parts = hex_code.split()
            for part in parts:
                assert len(part) == 2, f"Key {key!r} hex part {part!r} not 2 chars"
                int(part, 16)  # validates it's valid hex

    def test_tmux_send_hex_invokes_correct_command(self) -> None:
        """_tmux_send_hex calls tmux with -H flag and split hex bytes."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _tmux_send_hex("%42", "1b 5b 41")
            mock_run.assert_called_once_with(
                ["tmux", "send-keys", "-t", "%42", "-H", "1b", "5b", "41"],
                check=True,
                capture_output=True,
                text=True,
            )

    def test_key_name_would_fail_but_hex_succeeds(self) -> None:
        """Simulate: key-name path accepted by tmux but Ink ignores it.

        The hex path (Layer 1) bypasses the failure by writing raw bytes
        directly to the PTY master fd. send_keys_verified detects the
        content change and returns True.
        """
        with (
            patch("duo.transport._time") as mock_time,
            patch("duo.transport.resolve_label", return_value="%1"),
            patch("duo.transport._tmux_send_hex") as mock_hex,
            # Simulate: pane content changes after hex send
            patch("duo.transport.read_pane", side_effect=["before", "after"]),
            patch("duo.transport.ensure_minimum_pane_size", return_value=False),
        ):
            mock_time.sleep = MagicMock()
            mock_hex.return_value = None
            result = send_keys_verified("test-pane", "Enter", settle=0.05, retries=1)
            assert result is True
            # Verify hex path was used (not bridge)
            mock_hex.assert_called_once_with("%1", "0d")


class TestLayer2VerifiedRetry:
    """Layer 2: send_keys_verified retries and detects content changes."""

    @patch("duo.transport._time")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.ensure_minimum_pane_size", return_value=False)
    def test_retries_on_no_change(self, _ensure, mock_read, mock_send, mock_time):
        """Retries up to N times when content doesn't change."""
        mock_read.return_value = "same"
        mock_time.sleep = MagicMock()

        result = send_keys_verified("test", "Enter", settle=0.05, retries=3)
        assert result is False
        assert mock_send.call_count == 3

    @patch("duo.transport._time")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.ensure_minimum_pane_size", return_value=False)
    def test_succeeds_on_delayed_change(self, _ensure, mock_read, mock_send, mock_time):
        """Returns True when pane content changes on second attempt."""
        mock_read.side_effect = ["before", "before", "changed"]
        mock_time.sleep = MagicMock()

        result = send_keys_verified("test", "Enter", settle=0.05, retries=3)
        assert result is True
        assert mock_send.call_count == 2

    @patch("duo.transport._time")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.ensure_minimum_pane_size", return_value=False)
    def test_backoff_increases_with_attempts(
        self, _ensure, mock_read, mock_send, mock_time
    ):
        """Sleep time increases between retries (backoff)."""
        mock_read.return_value = "same"
        mock_time.sleep = MagicMock()
        settle = 0.1

        send_keys_verified("test", "Enter", settle=settle, retries=3)

        sleep_calls = mock_time.sleep.call_args_list
        sleep_args = [c[0][0] for c in sleep_calls]
        # Pattern per attempt: settle (post-send), settle*(attempt+1) (backoff)
        # attempt 0: settle, settle*1
        # attempt 1: settle, settle*2
        # attempt 2: settle, settle*3
        assert sleep_args[0] == settle  # post-send attempt 0
        assert sleep_args[1] == settle * 1  # backoff attempt 0
        assert sleep_args[3] == settle * 2  # backoff attempt 1
        assert sleep_args[5] == settle * 3  # backoff attempt 2


class TestLayer3AutoResize:
    """Layer 3: Auto-resize pane when verified send fails."""

    @patch("duo.transport._time")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.ensure_minimum_pane_size")
    def test_small_pane_triggers_auto_resize(
        self, mock_ensure, mock_read, mock_send, mock_time
    ):
        """When all retries fail, ensure_minimum_pane_size is called."""
        # All normal reads return same content, then resize + retry succeeds
        mock_read.side_effect = ["before", "before", "before", "changed"]
        mock_time.sleep = MagicMock()
        mock_ensure.return_value = True  # pane was undersized and resized

        result = send_keys_verified("test-pane", "Enter", settle=0.05, retries=2)
        assert result is True
        mock_ensure.assert_called_once_with("test-pane")

    @patch("duo.transport._time")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.ensure_minimum_pane_size", side_effect=RuntimeError("gone"))
    def test_auto_resize_failure_is_graceful(
        self, mock_ensure, mock_read, mock_send, mock_time
    ):
        """If auto-resize raises, caller gets False, no crash."""
        mock_read.return_value = "same"
        mock_time.sleep = MagicMock()

        result = send_keys_verified("test-pane", "Enter", settle=0.05, retries=1)
        assert result is False
        # No exception propagated

    @patch("duo.transport._time")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.ensure_minimum_pane_size", side_effect=OSError("tmux dead"))
    def test_auto_resize_oserror_is_graceful(
        self, mock_ensure, mock_read, mock_send, mock_time
    ):
        """OSError from auto-resize is also caught gracefully."""
        mock_read.return_value = "same"
        mock_time.sleep = MagicMock()

        result = send_keys_verified("test-pane", "Enter", settle=0.05, retries=1)
        assert result is False

    @patch("duo.transport._time")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.ensure_minimum_pane_size")
    def test_auto_resize_disabled(self, mock_ensure, mock_read, mock_send, mock_time):
        """auto_resize=False skips Layer 3 entirely."""
        mock_read.return_value = "same"
        mock_time.sleep = MagicMock()

        result = send_keys_verified(
            "test-pane", "Enter", settle=0.05, retries=1, auto_resize=False
        )
        assert result is False
        mock_ensure.assert_not_called()

    @patch("duo.transport._time")
    @patch("duo.transport.send_keys")
    @patch("duo.transport.read_pane")
    @patch("duo.transport.ensure_minimum_pane_size")
    def test_consecutive_resizes_dont_oscillate(
        self, mock_ensure, mock_read, mock_send, mock_time
    ):
        """Multiple send_keys_verified calls each trigger auto-resize
        independently but the minimum sizes stay constant."""
        mock_time.sleep = MagicMock()
        mock_ensure.return_value = True  # always resizes

        for _ in range(3):
            # Each call: initial read + 1 retry read + resize retry read
            mock_read.side_effect = ["before", "before", "changed"]
            result = send_keys_verified("test", "Enter", settle=0.05, retries=1)
            assert result is True

        # ensure_minimum_pane_size called with same label each time
        assert mock_ensure.call_count == 3
        for c in mock_ensure.call_args_list:
            assert c == call("test")

    @patch("duo.transport.resolve_label", return_value="%1")
    @patch("subprocess.run")
    @patch("duo.transport.get_pane_size")
    def test_ensure_minimum_size_values(self, mock_size, mock_run, mock_resolve):
        """ensure_minimum_pane_size uses the correct constant values."""
        mock_size.return_value = (50, 10)  # way too small
        mock_run.return_value = MagicMock(returncode=0)

        ensure_minimum_pane_size("test")

        # Should resize both width and height
        resize_calls = [c for c in mock_run.call_args_list]
        # Width resize uses MINIMUM_PANE_COLS
        width_call = [c for c in resize_calls if "-x" in str(c)]
        assert len(width_call) == 1
        assert str(MINIMUM_PANE_COLS) in str(width_call[0])
        # Height resize uses MINIMUM_PANE_ROWS
        height_call = [c for c in resize_calls if "-y" in str(c)]
        assert len(height_call) == 1
        assert str(MINIMUM_PANE_ROWS) in str(height_call[0])


class TestMinimumDimensionsConstants:
    """Sanity checks for MINIMUM_PANE_COLS / MINIMUM_PANE_ROWS."""

    def test_minimum_cols_sane(self) -> None:
        assert MINIMUM_PANE_COLS >= 80, "Minimum cols too small for dialog display"
        assert MINIMUM_PANE_COLS <= 200, "Minimum cols unreasonably large"

    def test_minimum_rows_sane(self) -> None:
        assert MINIMUM_PANE_ROWS >= 20, "Minimum rows too small for dialog display"
        assert MINIMUM_PANE_ROWS <= 60, "Minimum rows unreasonably large"

    def test_cols_greater_than_rows(self) -> None:
        """Terminals are always wider than tall."""
        assert MINIMUM_PANE_COLS > MINIMUM_PANE_ROWS

    def test_hex_map_covers_critical_keys(self) -> None:
        """The hex map must cover all keys that Copilot dialogs use."""
        critical = {"Enter", "Up", "Down", "Tab", "Escape", "C-c", "C-d"}
        missing = critical - set(_KEY_TO_HEX.keys())
        assert not missing, f"Critical keys missing from _KEY_TO_HEX: {missing}"
