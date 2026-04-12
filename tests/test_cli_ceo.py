"""CLI tests for ceo commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from duo.ceo_log import start_ceo_session
from duo.cli import main
from duo.transport import DialogKind


class TestCeoWait:
    """Tests for duo ceo-wait."""

    def test_task_not_found(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ceo-wait", "nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_pane_dead(self, runner: CliRunner, make_task) -> None:
        task = make_task("wait-dead")
        with patch("duo.transport.is_process_alive", return_value=False):
            result = runner.invoke(main, ["ceo-wait", task.id])
        assert result.exit_code != 0
        assert "not alive" in result.output

    def test_timeout(self, runner: CliRunner, make_task) -> None:
        task = make_task("wait-timeout")
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.wait_for_dialog", return_value=False),
        ):
            result = runner.invoke(main, ["ceo-wait", task.id, "--timeout", "10"])
        assert result.exit_code != 0
        assert "Timeout" in result.output

    def test_dialog_found(self, runner: CliRunner, make_task) -> None:
        task = make_task("wait-ok")
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.wait_for_dialog", return_value=True),
            patch("duo.transport.read_pane", return_value="dialog content here"),
            patch("duo.commander._write_watch_event") as mock_write,
        ):
            result = runner.invoke(main, ["ceo-wait", task.id])
        assert result.exit_code == 0
        assert "dialog content here" in result.output
        mock_write.assert_called_once()

    def test_custom_interval(self, runner: CliRunner, make_task) -> None:
        task = make_task("wait-interval")
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.wait_for_dialog", return_value=True) as mock_wait,
            patch("duo.transport.read_pane", return_value="content"),
            patch("duo.commander._write_watch_event"),
        ):
            runner.invoke(main, ["ceo-wait", task.id, "--interval", "2"])
        mock_wait.assert_called_once_with(task.pane_label, timeout=300, interval=2.0)

    def test_session_logging(
        self,
        runner: CliRunner,
        make_task,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import duo.ceo_log

        sessions_dir = tmp_path / "ceo-sessions"
        monkeypatch.setattr(duo.ceo_log, "CEO_SESSIONS_DIR", sessions_dir)
        sid = start_ceo_session()
        monkeypatch.setenv("DUO_CEO_SESSION", sid)
        task = make_task("wait-log")
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.wait_for_dialog", return_value=True),
            patch("duo.transport.read_pane", return_value="dialog!"),
            patch("duo.commander._write_watch_event"),
        ):
            result = runner.invoke(main, ["ceo-wait", task.id])
        assert result.exit_code == 0
        events = duo.ceo_log.replay_session(sid)
        assert any(e["event"] == "dialog_detected" for e in events)


class TestCeoSelect:
    """Tests for duo ceo-select."""

    def test_task_not_found(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ceo-select", "nope", "1"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_non_numeric_option_rejected(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ceo-select", "any-task", "abc"])
        assert result.exit_code != 0
        assert "must be a number" in result.output

    def test_not_in_dialog(self, runner: CliRunner, make_task) -> None:
        task = make_task("sel-nodlg")
        with (
            patch("duo.transport.is_in_dialog_stable", return_value=False),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.read_pane", return_value=""),
        ):
            result = runner.invoke(main, ["ceo-select", task.id, "1"])
        assert result.exit_code != 0
        assert "not in a stable dialog" in result.output

    def test_select_number(self, runner: CliRunner, make_task) -> None:
        task = make_task("sel-num")
        with (
            patch("duo.transport.is_in_dialog_stable", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.read_pane", return_value=""),
            patch("duo.transport.select_dialog_option") as mock_sel,
        ):
            result = runner.invoke(main, ["ceo-select", task.id, "2"])
        assert result.exit_code == 0
        assert "Selected option 2" in result.output
        mock_sel.assert_called_once_with(task.pane_label, "2")

    def test_select_other(self, runner: CliRunner, make_task) -> None:
        task = make_task("sel-other")
        with (
            patch("duo.transport.is_in_dialog_stable", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.read_pane", return_value=""),
            patch(
                "duo.transport.send_option_other_message", return_value=True
            ) as mock_send,
        ):
            result = runner.invoke(
                main, ["ceo-select", task.id, "--other", "my custom text"]
            )
        assert result.exit_code == 0
        assert "Other" in result.output
        mock_send.assert_called_once_with(task.pane_label, "my custom text")

    def test_ceo_select_other_dialog_persists(
        self, runner: CliRunner, make_task
    ) -> None:
        """--other shows warning when dialog persists after retries."""
        task = make_task("sel-other-fail")
        with (
            patch("duo.transport.is_in_dialog_stable", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.read_pane", return_value=""),
            patch("duo.transport.send_option_other_message", return_value=False),
        ):
            result = runner.invoke(
                main, ["ceo-select", task.id, "--other", "my custom text"]
            )
        assert result.exit_code == 0
        assert "may still be active" in result.output

    def test_bad_task_name(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ceo-select", "bad name!!", "1"])
        assert result.exit_code != 0

    def test_both_option_and_other_is_error(self, runner: CliRunner, make_task) -> None:
        task = make_task("sel-both")
        result = runner.invoke(main, ["ceo-select", task.id, "2", "--other", "text"])
        assert result.exit_code != 0
        assert "Cannot specify both" in result.output

    def test_neither_option_nor_other_is_error(
        self, runner: CliRunner, make_task
    ) -> None:
        task = make_task("sel-none")
        result = runner.invoke(main, ["ceo-select", task.id])
        assert result.exit_code != 0
        assert "Must specify" in result.output

    def test_refused_at_main_prompt(self, runner: CliRunner, make_task) -> None:
        """ceo-select REFUSES if pane is at main ❯ prompt."""
        task = make_task("sel-prompt")
        with (
            patch("duo.transport.is_at_main_prompt", return_value=True),
            patch("duo.transport.read_pane", return_value="❯ Type @"),
        ):
            result = runner.invoke(main, ["ceo-select", task.id, "1"])
        assert result.exit_code != 0
        assert "REFUSED" in result.output
        assert "Premium Request" in result.output

    def test_force_new_session_bypasses_assert(
        self, runner: CliRunner, make_task
    ) -> None:
        """--force-new-session bypasses the main-prompt check but logs."""
        task = make_task("sel-force")
        with (
            patch("duo.transport.is_in_dialog_stable", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=True),
            patch("duo.transport.read_pane", return_value="❯ Type @"),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.OPTION),
            patch("duo.transport.select_dialog_option"),
        ):
            result = runner.invoke(
                main,
                ["ceo-select", task.id, "1", "--force-new-session"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        # Check budget log was written
        from duo.protocol import (
            DUO_DIR,
        )

        log_path = DUO_DIR / "pr-budget.log"
        assert log_path.exists()
        assert "--force-new-session" in log_path.read_text()

    def test_text_dialog_option_number_rejected(
        self, runner: CliRunner, make_task
    ) -> None:
        """Selecting a number in a TEXT dialog is rejected."""
        task = make_task("sel-text-num")
        with (
            patch("duo.transport.is_in_dialog_stable", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch(
                "duo.transport.read_pane", return_value="╭─ Q ─╮\n Type your answer\n╰─"
            ),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.TEXT),
        ):
            result = runner.invoke(main, ["ceo-select", task.id, "1"])
        assert result.exit_code != 0
        assert "text-input dialog" in result.output

    def test_text_dialog_other_works(self, runner: CliRunner, make_task) -> None:
        """--other in a TEXT dialog uses send_text_dialog_message."""
        task = make_task("sel-text-ok")
        with (
            patch("duo.transport.is_in_dialog_stable", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch(
                "duo.transport.read_pane", return_value="╭─ Q ─╮\n Type your answer\n╰─"
            ),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.TEXT),
            patch(
                "duo.transport.send_text_dialog_message", return_value=True
            ) as mock_send,
        ):
            result = runner.invoke(
                main, ["ceo-select", task.id, "--other", "my answer"]
            )
        assert result.exit_code == 0
        assert "Typed text" in result.output
        mock_send.assert_called_once_with(task.pane_label, "my answer")

    def test_text_dialog_other_retry_warning(
        self, runner: CliRunner, make_task
    ) -> None:
        """--other shows warning when dialog persists after retries."""
        task = make_task("sel-text-retry")
        with (
            patch("duo.transport.is_in_dialog_stable", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch(
                "duo.transport.read_pane", return_value="╭─ Q ─╮\n Type your answer\n╰─"
            ),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.TEXT),
            patch("duo.transport.send_text_dialog_message", return_value=False),
        ):
            result = runner.invoke(
                main, ["ceo-select", task.id, "--other", "my answer"]
            )
        assert result.exit_code == 0
        assert "may still be active" in result.output

    def test_bullet_dialog_select_option(self, runner: CliRunner, make_task) -> None:
        """Selecting a number in a BULLET dialog calls select_bullet_option."""
        task = make_task("sel-bullet")
        with (
            patch("duo.transport.is_in_dialog_stable", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.read_pane", return_value=""),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.BULLET),
            patch("duo.transport.select_bullet_option") as mock_sel,
        ):
            result = runner.invoke(main, ["ceo-select", task.id, "2"])
        assert result.exit_code == 0
        assert "Selected bullet option 2" in result.output
        mock_sel.assert_called_once_with(task.pane_label, 2)

    def test_bullet_dialog_other_text(self, runner: CliRunner, make_task) -> None:
        """--other in a BULLET dialog uses send_text_dialog_message."""
        task = make_task("sel-bullet-other")
        with (
            patch("duo.transport.is_in_dialog_stable", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.read_pane", return_value=""),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.BULLET),
            patch(
                "duo.transport.send_text_dialog_message", return_value=True
            ) as mock_send,
        ):
            result = runner.invoke(
                main, ["ceo-select", task.id, "--other", "custom answer"]
            )
        assert result.exit_code == 0
        assert "Typed text in bullet dialog" in result.output
        mock_send.assert_called_once_with(task.pane_label, "custom answer")

    """Tests for duo ceo-approve."""

    def test_approve_task_not_found(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ceo-approve", "nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_approve_success(self, runner: CliRunner, make_task) -> None:
        task = make_task("appr-ok")
        with (
            patch("duo.transport.is_permission_dialog", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.read_pane", return_value=""),
            patch("duo.transport.approve_permission") as mock_approve,
        ):
            result = runner.invoke(main, ["ceo-approve", task.id])
        assert result.exit_code == 0
        assert "Approved" in result.output
        mock_approve.assert_called_once_with(task.pane_label)

    def test_approve_not_permission_dialog(self, runner: CliRunner, make_task) -> None:
        task = make_task("appr-ask")
        with (
            patch("duo.transport.is_permission_dialog", return_value=False),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.read_pane", return_value=""),
        ):
            result = runner.invoke(main, ["ceo-approve", task.id])
        assert result.exit_code != 0
        assert "not showing a permission dialog" in result.output
        assert "ceo-select" in result.output

    def test_approve_runtime_error(self, runner: CliRunner, make_task) -> None:
        task = make_task("appr-fail")
        with (
            patch("duo.transport.is_permission_dialog", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.read_pane", return_value=""),
            patch(
                "duo.transport.approve_permission",
                side_effect=RuntimeError("SAFETY: not in dialog"),
            ),
        ):
            result = runner.invoke(main, ["ceo-approve", task.id])
        assert result.exit_code != 0

    def test_approve_oserror(self, runner: CliRunner, make_task) -> None:
        task = make_task("appr-os")
        with (
            patch("duo.transport.is_permission_dialog", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.read_pane", return_value=""),
            patch("duo.transport.approve_permission", side_effect=OSError("pane gone")),
        ):
            result = runner.invoke(main, ["ceo-approve", task.id])
        assert result.exit_code != 0

    def test_approve_bad_task_name(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ceo-approve", "inv@lid"])
        assert result.exit_code != 0

    def test_approve_refused_at_main_prompt(self, runner: CliRunner, make_task) -> None:
        """ceo-approve REFUSES if pane is at main ❯ prompt."""
        task = make_task("appr-prompt")
        with (
            patch("duo.transport.is_at_main_prompt", return_value=True),
            patch("duo.transport.read_pane", return_value="❯ Type @"),
        ):
            result = runner.invoke(main, ["ceo-approve", task.id])
        assert result.exit_code != 0
        assert "REFUSED" in result.output
        assert "Premium Request" in result.output

    def test_approve_force_new_session_bypasses_assert(
        self, runner: CliRunner, make_task
    ) -> None:
        """--force-new-session bypasses the main-prompt check but logs."""
        task = make_task("appr-force")
        with (
            patch("duo.transport.is_permission_dialog", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=True),
            patch("duo.transport.read_pane", return_value="❯ Type @"),
            patch("duo.transport.approve_permission"),
        ):
            result = runner.invoke(
                main, ["ceo-approve", task.id, "--force-new-session"]
            )
        assert result.exit_code == 0
        from duo.protocol import DUO_DIR

        log_path = DUO_DIR / "pr-budget.log"
        assert log_path.exists()
        assert "--force-new-session" in log_path.read_text()

    def test_approve_session_logging(
        self,
        runner: CliRunner,
        make_task,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import duo.ceo_log

        sessions_dir = tmp_path / "ceo-sessions"
        monkeypatch.setattr(duo.ceo_log, "CEO_SESSIONS_DIR", sessions_dir)
        sid = start_ceo_session()
        monkeypatch.setenv("DUO_CEO_SESSION", sid)
        task = make_task("appr-log")
        with (
            patch("duo.transport.is_permission_dialog", return_value=True),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.read_pane", return_value=""),
            patch("duo.transport.approve_permission"),
        ):
            result = runner.invoke(main, ["ceo-approve", task.id])
        assert result.exit_code == 0
        events = duo.ceo_log.replay_session(sid)
        assert any(
            e["event"] == "decision" and e["decision_type"] == "approve" for e in events
        )

    def test_select_session_logging(
        self,
        runner: CliRunner,
        make_task,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import duo.ceo_log

        sessions_dir = tmp_path / "ceo-sessions"
        monkeypatch.setattr(duo.ceo_log, "CEO_SESSIONS_DIR", sessions_dir)
        sid = start_ceo_session()
        monkeypatch.setenv("DUO_CEO_SESSION", sid)
        task = make_task("sel-log")
        with (
            patch("duo.transport.is_in_dialog_stable", return_value=True),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.OPTION),
            patch("duo.transport.is_at_main_prompt", return_value=False),
            patch("duo.transport.read_pane", return_value=""),
            patch("duo.transport.select_dialog_option"),
        ):
            result = runner.invoke(main, ["ceo-select", task.id, "2"])
        assert result.exit_code == 0
        events = duo.ceo_log.replay_session(sid)
        assert any(
            e["event"] == "decision" and e["decision_type"] == "select" for e in events
        )


class TestCeoStatus:
    """Tests for duo ceo-status."""

    def test_task_not_found(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ceo-status", "nope"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_dead_pane(self, runner: CliRunner, make_task) -> None:
        task = make_task("stat-dead")
        with patch("duo.transport.is_process_alive", return_value=False):
            result = runner.invoke(main, ["ceo-status", task.id])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {"task": task.id, "state": "dead"}

    def test_dialog_state(self, runner: CliRunner, make_task) -> None:
        task = make_task("stat-dlg")
        pane_content = "╭─ Question ─╮\n│ 1. Yes  \n│ 2. No   \n│ 3. Other\n╰─"
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value=pane_content),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.OPTION),
        ):
            result = runner.invoke(main, ["ceo-status", task.id])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["state"] == "dialog"
        assert data["options"] == 3

    def test_processing_state(self, runner: CliRunner, make_task) -> None:
        task = make_task("stat-proc")
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value="◉ Thinking..."),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.NONE),
        ):
            result = runner.invoke(main, ["ceo-status", task.id])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {"task": task.id, "state": "processing"}

    def test_idle_state(self, runner: CliRunner, make_task) -> None:
        task = make_task("stat-idle")
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value="❯ "),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.NONE),
        ):
            result = runner.invoke(main, ["ceo-status", task.id])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {"task": task.id, "state": "idle"}

    def test_bad_task_name(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["ceo-status", "bad name"])
        assert result.exit_code != 0

    def test_assert_in_dialog_passes_when_dialog(
        self, runner: CliRunner, make_task
    ) -> None:
        """--assert-in-dialog exits 0 when pane IS in a dialog."""
        task = make_task("stat-aid-ok")
        pane_content = "╭─ Question ─╮\n│ 1. Yes  \n│ 2. No\n╰─"
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value=pane_content),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.OPTION),
        ):
            result = runner.invoke(main, ["ceo-status", task.id, "--assert-in-dialog"])
        assert result.exit_code == 0

    def test_assert_in_dialog_fails_when_idle(
        self, runner: CliRunner, make_task
    ) -> None:
        """--assert-in-dialog exits non-zero when pane is idle."""
        task = make_task("stat-aid-idle")
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value="❯ "),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.NONE),
        ):
            result = runner.invoke(main, ["ceo-status", task.id, "--assert-in-dialog"])
        # SystemExit(1) — Click wraps as exit_code=1
        assert result.exit_code == 1

    def test_assert_in_dialog_fails_when_processing(
        self, runner: CliRunner, make_task
    ) -> None:
        """--assert-in-dialog exits non-zero when pane is processing."""
        task = make_task("stat-aid-proc")
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value="◉ Thinking..."),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.NONE),
        ):
            result = runner.invoke(main, ["ceo-status", task.id, "--assert-in-dialog"])
        assert result.exit_code == 1

    def test_assert_in_dialog_fails_when_dead(
        self, runner: CliRunner, make_task
    ) -> None:
        """--assert-in-dialog exits non-zero when pane is dead."""
        task = make_task("stat-aid-dead")
        with patch("duo.transport.is_process_alive", return_value=False):
            result = runner.invoke(main, ["ceo-status", task.id, "--assert-in-dialog"])
        assert result.exit_code == 1

    def test_text_dialog_state(self, runner: CliRunner, make_task) -> None:
        """ceo-status reports text_dialog for text-input dialogs."""
        task = make_task("stat-text")
        pane_content = "╭─ Question ─╮\n Type your answer\n╰────────────╯"
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value=pane_content),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.TEXT),
        ):
            result = runner.invoke(main, ["ceo-status", task.id])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {"task": task.id, "state": "text_dialog"}

    def test_assert_in_dialog_passes_for_text_dialog(
        self, runner: CliRunner, make_task
    ) -> None:
        """--assert-in-dialog exits 0 for text_dialog (it IS a dialog)."""
        task = make_task("stat-aid-text")
        pane_content = "╭─ Q ─╮\n Type your answer\n╰─"
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value=pane_content),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.TEXT),
        ):
            result = runner.invoke(main, ["ceo-status", task.id, "--assert-in-dialog"])
        assert result.exit_code == 0

    def test_dialog_options_not_counted_outside_box(
        self, runner: CliRunner, make_task
    ) -> None:
        """Options in scrollback ABOVE the dialog box are not counted."""
        task = make_task("stat-box-above")
        # Scrollback has "1. foo", "2. bar" before the dialog box
        pane_content = (
            "Here are some steps:\n"
            "1. Install deps\n"
            "2. Run tests\n"
            "3. Deploy\n"
            "\n"
            "╭─ Permission ─╮\n"
            "│ ❯ 1. Yes\n"
            "│   2. No\n"
            "╰──────────────╯"
        )
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value=pane_content),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.OPTION),
        ):
            result = runner.invoke(main, ["ceo-status", task.id])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["options"] == 2  # only 2 inside box, not 5

    def test_dialog_options_not_counted_below_box(
        self, runner: CliRunner, make_task
    ) -> None:
        """Options BELOW the dialog box are not counted."""
        task = make_task("stat-box-below")
        pane_content = (
            "╭─ Run? ─╮\n"
            "│ ❯ 1. Yes\n"
            "│   2. No\n"
            "│   3. Other\n"
            "╰─────────╯\n"
            "4. Some other numbered text\n"
            "5. More numbered text\n"
        )
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value=pane_content),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.OPTION),
        ):
            result = runner.invoke(main, ["ceo-status", task.id])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["options"] == 3  # only 3 inside box

    def test_dialog_options_only_box(self, runner: CliRunner, make_task) -> None:
        """Pure dialog box with no surrounding noise."""
        task = make_task("stat-box-only")
        pane_content = (
            "╭─ Allow? ─╮\n"
            "│ ❯ 1. Allow once\n"
            "│   2. Allow for session\n"
            "│   3. Allow + add to allowed\n"
            "│   4. Deny\n"
            "│   5. Tell differently\n"
            "╰───────────╯"
        )
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value=pane_content),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.OPTION),
        ):
            result = runner.invoke(main, ["ceo-status", task.id])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["options"] == 5

    def test_bullet_dialog_state(self, runner: CliRunner, make_task) -> None:
        """ceo-status reports bullet_dialog for bullet-style dialogs."""
        task = make_task("stat-bullet")
        pane_content = (
            "╭─ Pick branch ─╮\n❯ main\n  develop\n  feature-x\n╰───────────────╯"
        )
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value=pane_content),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.BULLET),
            patch("duo.transport.strip_ansi", side_effect=lambda x: x),
        ):
            result = runner.invoke(main, ["ceo-status", task.id])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["state"] == "bullet_dialog"
        assert data["items"] == 3
        assert data["cursor"] == 1

    def test_assert_in_dialog_passes_for_bullet(
        self, runner: CliRunner, make_task
    ) -> None:
        """--assert-in-dialog exits 0 for bullet_dialog."""
        task = make_task("stat-aid-bullet")
        pane_content = "╭─ Q ─╮\n❯ A\n  B\n╰─────╯"
        with (
            patch("duo.transport.is_process_alive", return_value=True),
            patch("duo.transport.read_pane", return_value=pane_content),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.BULLET),
            patch("duo.transport.strip_ansi", side_effect=lambda x: x),
        ):
            result = runner.invoke(main, ["ceo-status", task.id, "--assert-in-dialog"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# duo think
# ---------------------------------------------------------------------------
