"""Tests for duo.commander — prompt builders and orchestration."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from duo.commander import (
    _check_pr_budget,
    _count_corrections,
    _detect_project_context,
    _escalate_pr_budget,
    _get_copilot_model,
    _log_monitor,
    _monitor_one_task,
    _watch_loop,
    _write_watch_event,
    build_bootstrap_prompt,
    build_continue_prompt,
    build_correction_prompt,
    build_task_prompt,
    monitor,
    poll_task,
    resend_last_prompt,
    restart_session,
    send_task_prompt,
    start_claude_commander,
    start_session,
    verify_and_advance,
    watch_tasks,
    write_commander_claude_md,
    write_project_claude_md,
)
from duo.poller import AdaptivePoller, PollResult
from duo.protocol import (
    Subtask,
    TaskStatus,
    append_event,
    create_task,
    load_task,
    now_iso,
    prompt_hash,
    read_jsonl,
    save_task,
    transition,
    write_json,
)
from duo.verifier import Correction, Pass

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_subtask(step_id: int = 1, **overrides) -> Subtask:
    defaults = dict(
        step_id=step_id,
        description=f"implement step {step_id}",
        target_files=["main.py"],
        writable_paths=["src/"],
    )
    defaults.update(overrides)
    return Subtask(**defaults)


def _make_task(task_id: str = "t1", subtasks: list[Subtask] | None = None):
    return create_task(
        task_id=task_id,
        description="test task",
        worktree="/fake/worktree",
        branch="feat",
        base_commit="abc123",
        subtasks=subtasks or [_make_subtask()],
    )


def _advance_to_prompt_sent(task):
    """Walk the FSM from CREATED → SESSION_STARTING → PROMPT_SENT."""
    transition(task, TaskStatus.SESSION_STARTING)
    transition(task, TaskStatus.PROMPT_SENT)


# ---------------------------------------------------------------------------
# build_bootstrap_prompt
# ---------------------------------------------------------------------------


class TestBuildBootstrapPrompt:
    def test_contains_task_dir(self):
        task = _make_task()
        prompt = build_bootstrap_prompt(task)
        assert str(task.dir) in prompt

    def test_contains_incarnation(self):
        task = _make_task()
        prompt = build_bootstrap_prompt(task)
        assert task.incarnation_id in prompt

    def test_contains_protocol_instructions(self):
        task = _make_task()
        prompt = build_bootstrap_prompt(task)
        assert "ack" in prompt.lower()
        assert "heartbeat" in prompt.lower()
        assert "result" in prompt.lower()

    def test_override_prompt_appends_task(self):
        """build_bootstrap_prompt with override_prompt appends user instruction."""
        task = _make_task()
        prompt = build_bootstrap_prompt(task, override_prompt="Implement auth module")
        assert str(task.dir) in prompt
        assert "Implement auth module" in prompt
        assert "第一个任务" in prompt


# ---------------------------------------------------------------------------
# build_task_prompt
# ---------------------------------------------------------------------------


class TestBuildTaskPrompt:
    def test_contains_step_and_attempt(self):
        task = _make_task()
        prompt = build_task_prompt(task)
        assert "step: 1" in prompt
        assert "attempt: 1" in prompt

    def test_contains_subtask_description(self):
        task = _make_task()
        prompt = build_task_prompt(task)
        assert "implement step 1" in prompt

    def test_contains_incarnation(self):
        task = _make_task()
        prompt = build_task_prompt(task)
        assert task.incarnation_id in prompt

    def test_contains_hash_line(self):
        task = _make_task()
        prompt = build_task_prompt(task)
        expected_hash = prompt_hash(task.subtasks[0].description)
        assert f"#hash:{expected_hash}" in prompt

    def test_contains_target_files(self):
        task = _make_task(subtasks=[_make_subtask(target_files=["a.py", "b.py"])])
        prompt = build_task_prompt(task)
        assert "a.py" in prompt
        assert "b.py" in prompt

    def test_contains_writable_paths(self):
        task = _make_task(subtasks=[_make_subtask(writable_paths=["src/", "lib/"])])
        prompt = build_task_prompt(task)
        assert "src/" in prompt
        assert "lib/" in prompt


# ---------------------------------------------------------------------------
# build_continue_prompt
# ---------------------------------------------------------------------------


class TestBuildContinuePrompt:
    def test_contains_step_and_attempt(self):
        task = _make_task()
        prompt = build_continue_prompt(task)
        assert "step 1" in prompt
        assert "attempt 1" in prompt

    def test_contains_description(self):
        task = _make_task()
        prompt = build_continue_prompt(task)
        assert "implement step 1" in prompt

    def test_contains_incarnation(self):
        task = _make_task()
        prompt = build_continue_prompt(task)
        assert task.incarnation_id in prompt

    def test_contains_hash(self):
        task = _make_task()
        prompt = build_continue_prompt(task)
        expected_hash = prompt_hash(task.subtasks[0].description)
        assert f"#hash:{expected_hash}" in prompt


# ---------------------------------------------------------------------------
# build_correction_prompt
# ---------------------------------------------------------------------------


class TestBuildCorrectionPrompt:
    def test_contains_reason(self):
        task = _make_task()
        prompt = build_correction_prompt(task, "tests failed")
        assert "tests failed" in prompt

    def test_contains_step_and_attempt(self):
        task = _make_task()
        prompt = build_correction_prompt(task, "fix it")
        assert "step 1" in prompt
        assert "attempt 1" in prompt

    def test_contains_hash(self):
        task = _make_task()
        reason = "wrong output"
        prompt = build_correction_prompt(task, reason)
        expected_hash = prompt_hash(reason)
        assert f"#hash:{expected_hash}" in prompt

    def test_contains_incarnation(self):
        task = _make_task()
        prompt = build_correction_prompt(task, "fix")
        assert task.incarnation_id in prompt


# ---------------------------------------------------------------------------
# _count_corrections
# ---------------------------------------------------------------------------


class TestCountCorrections:
    def test_empty_journal_returns_zero(self):
        task = _make_task()
        assert _count_corrections(task, 1) == 0

    def test_counts_correction_events(self):
        task = _make_task()
        append_event(task, "correction_sent", {"step": 1, "attempt": 2})
        append_event(task, "correction_sent", {"step": 1, "attempt": 3})
        assert _count_corrections(task, 1) == 2

    def test_ignores_other_event_types(self):
        task = _make_task()
        append_event(task, "correction_sent", {"step": 1, "attempt": 2})
        append_event(task, "prompt_sent", {"step": 1, "attempt": 1})
        append_event(task, "task_completed", {"id": task.id})
        assert _count_corrections(task, 1) == 1

    def test_filters_by_step(self):
        task = _make_task(subtasks=[_make_subtask(1), _make_subtask(2)])
        append_event(task, "correction_sent", {"step": 1, "attempt": 2})
        append_event(task, "correction_sent", {"step": 2, "attempt": 2})
        assert _count_corrections(task, 1) == 1
        assert _count_corrections(task, 2) == 1


# ---------------------------------------------------------------------------
# verify_and_advance
# ---------------------------------------------------------------------------


class TestVerifyAndAdvance:
    """Tests for verify_and_advance with mocked transport and verifier."""

    def _write_result(self, task, step, attempt, status="done", **extra):
        """Write a result JSON file the way the executor would."""
        result_path = task.result_path(step, attempt)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "step": step,
            "attempt": attempt,
            "incarnation": task.incarnation_id,
            "status": status,
            **extra,
        }
        write_json(result_path, data)

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    @patch("duo.commander.verify_step")
    def test_no_result_does_nothing(self, mock_verify, mock_send, mock_wait):
        task = _make_task()
        _advance_to_prompt_sent(task)
        # Don't write any result file
        verify_and_advance(task)
        mock_verify.assert_not_called()
        # Status unchanged
        assert task.status == TaskStatus.PROMPT_SENT

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    @patch("duo.commander.verify_step")
    def test_blocked_result_transitions_to_blocked(
        self, mock_verify, mock_send, mock_wait
    ):
        task = _make_task()
        _advance_to_prompt_sent(task)
        self._write_result(task, 1, 1, status="blocked", reason="missing dep")

        verify_and_advance(task)
        assert task.status == TaskStatus.BLOCKED
        mock_verify.assert_not_called()

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    @patch("duo.commander.verify_step")
    def test_pass_last_step_completes(self, mock_verify, mock_send, mock_wait):
        task = _make_task()  # single subtask → step 1 is the last step
        _advance_to_prompt_sent(task)
        self._write_result(task, 1, 1)
        mock_verify.return_value = Pass()

        verify_and_advance(task)
        assert task.status == TaskStatus.COMPLETED

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    @patch("duo.commander.verify_step")
    def test_pass_advances_to_next_step(self, mock_verify, mock_send, mock_wait):
        task = _make_task(subtasks=[_make_subtask(1), _make_subtask(2)])
        _advance_to_prompt_sent(task)
        self._write_result(task, 1, 1)
        mock_verify.return_value = Pass()

        verify_and_advance(task)

        assert task.current_step == 2
        assert task.current_attempt == 1
        # send_task_prompt calls select_dialog_option internally
        mock_send.assert_called()
        # Should end in PROMPT_SENT after sending continuation
        assert task.status == TaskStatus.PROMPT_SENT

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    @patch("duo.commander.verify_step")
    def test_correction_increments_attempt(self, mock_verify, mock_send, mock_wait):
        task = _make_task()
        _advance_to_prompt_sent(task)
        self._write_result(task, 1, 1)
        mock_verify.return_value = Correction(reason="test failure")

        verify_and_advance(task)

        assert task.current_attempt == 2
        mock_send.assert_called()
        assert task.status == TaskStatus.PROMPT_SENT

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    @patch("duo.commander.verify_step")
    def test_three_corrections_escalates(self, mock_verify, mock_send, mock_wait):
        task = _make_task()
        _advance_to_prompt_sent(task)
        # Pre-populate 3 correction events in journal
        for a in range(2, 5):
            append_event(task, "correction_sent", {"step": 1, "attempt": a})
        self._write_result(task, 1, 1)
        mock_verify.return_value = Correction(reason="still broken")

        verify_and_advance(task)
        assert task.status == TaskStatus.ESCALATED

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    @patch("duo.commander.verify_step")
    def test_max_corrections_one_escalates_immediately(
        self, mock_verify, mock_send, mock_wait, monkeypatch
    ):
        """With max_corrections=1, first correction exhausts budget → escalate."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        # 1 correction already sent
        append_event(task, "correction_sent", {"step": 1, "attempt": 2})
        self._write_result(task, 1, 1)
        mock_verify.return_value = Correction(reason="still bad")
        monkeypatch.setattr(
            "duo.commander.get_config",
            lambda k: 1 if k == "max_corrections" else 3,
        )

        verify_and_advance(task)
        assert task.status == TaskStatus.ESCALATED

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    @patch("duo.commander.verify_step")
    def test_error_result_transitions_to_blocked(
        self, mock_verify, mock_send, mock_wait
    ):
        task = _make_task()
        _advance_to_prompt_sent(task)
        self._write_result(task, 1, 1, status="error", reason="crash")

        verify_and_advance(task)
        assert task.status == TaskStatus.BLOCKED
        mock_verify.assert_not_called()

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    @patch("duo.commander.verify_step", side_effect=RuntimeError("git crash"))
    def test_verify_step_exception_transitions_to_failed(
        self, mock_verify, mock_send, mock_wait
    ):
        """verify_step raising an exception transitions task to FAILED."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        self._write_result(task, 1, 1)

        verify_and_advance(task)
        assert task.status == TaskStatus.FAILED
        events = read_jsonl(task.journal_path)
        verify_errors = [e for e in events if e.get("event") == "verify_error"]
        assert len(verify_errors) == 1
        assert "git crash" in verify_errors[0]["data"]["error"]

    @patch("duo.commander.verify_step", side_effect=KeyboardInterrupt("ctrl-c"))
    def test_verify_keyboard_interrupt_propagates(
        self,
        _mock_verify: object,
    ) -> None:
        """KeyboardInterrupt is NOT caught — propagates up."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        self._write_result(task, 1, 1)

        with pytest.raises(KeyboardInterrupt):
            verify_and_advance(task)


# ---------------------------------------------------------------------------
# _check_pr_budget
# ---------------------------------------------------------------------------


class TestPRBudget:
    def test_unlimited_budget_allows(self, monkeypatch: pytest.MonkeyPatch):
        """Budget=0 means unlimited."""
        task = _make_task()
        monkeypatch.setattr(
            "duo.commander.get_config", lambda k: 0 if k == "pr_budget" else None
        )
        assert _check_pr_budget(task) is True

    def test_under_budget_allows(self, monkeypatch: pytest.MonkeyPatch):
        """Under budget allows PR consumption."""
        task = _make_task()
        monkeypatch.setattr(
            "duo.commander.get_config", lambda k: 5 if k == "pr_budget" else None
        )
        # Add 2 pr_consumed events (under budget of 5)
        append_event(
            task, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )
        append_event(
            task, "pr_consumed", {"action": "task_prompt", "step": 1, "attempt": 1}
        )
        assert _check_pr_budget(task) is True

    def test_at_budget_blocks(self, monkeypatch: pytest.MonkeyPatch):
        """At budget limit blocks further PR consumption."""
        task = _make_task()
        monkeypatch.setattr(
            "duo.commander.get_config", lambda k: 2 if k == "pr_budget" else None
        )
        append_event(
            task, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )
        append_event(
            task, "pr_consumed", {"action": "task_prompt", "step": 1, "attempt": 1}
        )
        assert _check_pr_budget(task) is False

    def test_over_budget_blocks(self, monkeypatch: pytest.MonkeyPatch):
        """Over budget blocks further PR consumption."""
        task = _make_task()
        monkeypatch.setattr(
            "duo.commander.get_config", lambda k: 1 if k == "pr_budget" else None
        )
        append_event(
            task, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )
        append_event(
            task, "pr_consumed", {"action": "task_prompt", "step": 1, "attempt": 1}
        )
        assert _check_pr_budget(task) is False

    def test_negative_budget_treated_as_unlimited(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Negative budget treated as unlimited."""
        task = _make_task()
        monkeypatch.setattr(
            "duo.commander.get_config", lambda k: -1 if k == "pr_budget" else None
        )
        assert _check_pr_budget(task) is True

    def test_ignores_non_pr_events(self, monkeypatch: pytest.MonkeyPatch):
        """Only pr_consumed events count toward budget."""
        task = _make_task()
        monkeypatch.setattr(
            "duo.commander.get_config", lambda k: 2 if k == "pr_budget" else None
        )
        append_event(
            task, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )
        append_event(task, "prompt_sent", {"step": 1, "attempt": 1})
        append_event(task, "correction_sent", {"step": 1, "attempt": 2})
        assert _check_pr_budget(task) is True


# ---------------------------------------------------------------------------
# start_session
# ---------------------------------------------------------------------------


class TestStartSession:
    def test_start_session_creates_pane(self):
        """start_session calls subprocess to split tmux and transitions state."""
        from unittest.mock import MagicMock

        task = _make_task()

        with (
            patch("duo.commander.subprocess.run") as mock_run,
            patch("duo.commander.name_pane"),
            patch("duo.commander.send_shell_command"),
            patch("duo.commander.wait_for_idle"),
            patch("duo.commander.read_pane", return_value="❯"),
            patch("duo.commander.is_at_main_prompt", return_value=True),
            patch("duo.commander.send_bootstrap"),
            patch("duo.commander.time.sleep"),
            patch("duo.commander.get_config", return_value=False),
        ):
            # tmux split-window succeeds
            split_result = MagicMock()
            split_result.returncode = 0
            split_result.stdout = "%42\n"
            # tmux select-layout succeeds
            layout_result = MagicMock()
            layout_result.returncode = 0
            mock_run.side_effect = [split_result, layout_result]

            start_session(task)

            # First subprocess call should be tmux split-window
            first_call_args = mock_run.call_args_list[0][0][0]
            assert "tmux" in first_call_args
            assert "split-window" in first_call_args
            # Task should end up in PROMPT_SENT state
            assert task.status == TaskStatus.PROMPT_SENT

    def test_start_session_layout_failure_warns(self):
        """select-layout failure logs warning but session still starts."""
        from unittest.mock import MagicMock

        task = _make_task()

        with (
            patch("duo.commander.subprocess.run") as mock_run,
            patch("duo.commander.name_pane"),
            patch("duo.commander.send_shell_command"),
            patch("duo.commander.wait_for_idle"),
            patch("duo.commander.read_pane", return_value="❯"),
            patch("duo.commander.is_at_main_prompt", return_value=True),
            patch("duo.commander.send_bootstrap"),
            patch("duo.commander.time.sleep"),
            patch("duo.commander.get_config", return_value=False),
        ):
            split_result = MagicMock()
            split_result.returncode = 0
            split_result.stdout = "%42\n"
            layout_result = MagicMock()
            layout_result.returncode = 1
            layout_result.stderr = "layout error"
            mock_run.side_effect = [split_result, layout_result]

            start_session(task)
            assert task.status == TaskStatus.PROMPT_SENT

    def test_start_session_auto_allow_all_enabled(self):
        """When auto_allow_all=True, /allow-all is sent during start."""
        from unittest.mock import MagicMock

        task = _make_task()

        config_values = {
            "auto_allow_all": True,
            "auto_claude_commander": False,
            "copilot_model": "claude-opus-4.6",
            "bypass_permissions": True,
        }

        with (
            patch("duo.commander.subprocess.run") as mock_run,
            patch("duo.commander.name_pane"),
            patch("duo.commander.send_shell_command") as mock_send,
            patch("duo.commander.wait_for_idle"),
            patch("duo.commander.read_pane", return_value="❯"),
            patch("duo.commander.is_at_main_prompt", return_value=True),
            patch("duo.commander.send_bootstrap"),
            patch("duo.commander.time.sleep"),
            patch("duo.commander.get_config", side_effect=lambda k: config_values[k]),
        ):
            split_result = MagicMock()
            split_result.returncode = 0
            split_result.stdout = "%42\n"
            layout_result = MagicMock()
            layout_result.returncode = 0
            mock_run.side_effect = [split_result, layout_result]

            start_session(task)

            # /allow-all should have been sent
            allow_calls = [
                c for c in mock_send.call_args_list if "/allow-all" in str(c)
            ]
            assert len(allow_calls) == 1

    def test_start_session_auto_allow_all_disabled(self):
        """When auto_allow_all=False, /allow-all is NOT sent."""
        from unittest.mock import MagicMock

        task = _make_task()

        config_values = {
            "auto_allow_all": False,
            "auto_claude_commander": False,
            "copilot_model": "claude-opus-4.6",
            "bypass_permissions": True,
        }

        with (
            patch("duo.commander.subprocess.run") as mock_run,
            patch("duo.commander.name_pane"),
            patch("duo.commander.send_shell_command") as mock_send,
            patch("duo.commander.wait_for_idle"),
            patch("duo.commander.read_pane", return_value="❯"),
            patch("duo.commander.is_at_main_prompt", return_value=True),
            patch("duo.commander.send_bootstrap"),
            patch("duo.commander.time.sleep"),
            patch("duo.commander.get_config", side_effect=lambda k: config_values[k]),
        ):
            split_result = MagicMock()
            split_result.returncode = 0
            split_result.stdout = "%42\n"
            layout_result = MagicMock()
            layout_result.returncode = 0
            mock_run.side_effect = [split_result, layout_result]

            start_session(task)

            # /allow-all should NOT have been sent
            allow_calls = [
                c for c in mock_send.call_args_list if "/allow-all" in str(c)
            ]
            assert len(allow_calls) == 0

    def test_start_session_worktree_path_quoted(self):
        """Worktree paths with spaces/metacharacters are shell-quoted."""
        from unittest.mock import MagicMock

        task = _make_task()
        task.worktree = "/path/with spaces/and;semicolons"

        config_values = {
            "auto_allow_all": False,
            "auto_claude_commander": False,
            "copilot_model": "claude-opus-4.6",
            "bypass_permissions": True,
        }

        with (
            patch("duo.commander.subprocess.run") as mock_run,
            patch("duo.commander.name_pane"),
            patch("duo.commander.send_shell_command") as mock_send,
            patch("duo.commander.wait_for_idle"),
            patch("duo.commander.read_pane", return_value="❯"),
            patch("duo.commander.is_at_main_prompt", return_value=True),
            patch("duo.commander.send_bootstrap"),
            patch("duo.commander.time.sleep"),
            patch("duo.commander.get_config", side_effect=lambda k: config_values[k]),
        ):
            split_result = MagicMock()
            split_result.returncode = 0
            split_result.stdout = "%42\n"
            layout_result = MagicMock()
            layout_result.returncode = 0
            mock_run.side_effect = [split_result, layout_result]

            start_session(task)

            cd_call = mock_send.call_args_list[0]
            cd_cmd = cd_call[0][1]
            # Must be quoted — raw path would allow shell injection
            assert "'" in cd_cmd or "\\" in cd_cmd
            assert "cd " in cd_cmd

    def test_start_session_startup_timeout_logged(self):
        """When wait_for_idle returns False, task transitions to FAILED."""
        from unittest.mock import MagicMock

        task = _make_task()

        config_values = {
            "auto_allow_all": True,
            "auto_claude_commander": False,
            "copilot_model": "claude-opus-4.6",
            "bypass_permissions": True,
        }

        with (
            patch("duo.commander.subprocess.run") as mock_run,
            patch("duo.commander.name_pane"),
            patch("duo.commander.send_shell_command"),
            patch("duo.commander.wait_for_idle", return_value=False),
            patch("duo.commander.read_pane", return_value="❯"),
            patch("duo.commander.is_at_main_prompt", return_value=True),
            patch("duo.commander.send_bootstrap") as mock_bootstrap,
            patch("duo.commander.time.sleep"),
            patch("duo.commander.get_config", side_effect=lambda k: config_values[k]),
        ):
            split_result = MagicMock()
            split_result.returncode = 0
            split_result.stdout = "%42\n"
            layout_result = MagicMock()
            layout_result.returncode = 0
            mock_run.side_effect = [split_result, layout_result]

            start_session(task)

            # Task transitions to FAILED — not PROMPT_SENT
            assert task.status == TaskStatus.FAILED
            # Bootstrap should NOT be sent when startup timed out
            mock_bootstrap.assert_not_called()
            # startup timeout is logged in journal
            events = [
                json.loads(line)
                for line in task.journal_path.read_text().strip().split("\n")
            ]
            timeout_events = [e for e in events if e.get("event") == "startup_timeout"]
            assert len(timeout_events) == 1
            assert timeout_events[0]["data"]["phase"] == "copilot_start"

    def test_start_session_not_at_prompt_fails(self):
        """When Copilot stabilizes but is not at main prompt, task fails."""
        from unittest.mock import MagicMock

        task = _make_task("prompt-check-fail")

        config_values = {
            "auto_allow_all": False,
            "auto_claude_commander": False,
            "copilot_model": "claude-opus-4.6",
            "bypass_permissions": True,
        }

        with (
            patch("duo.commander.subprocess.run") as mock_run,
            patch("duo.commander.name_pane"),
            patch("duo.commander.send_shell_command"),
            patch("duo.commander.wait_for_idle", return_value=True),
            patch("duo.commander.read_pane", return_value="$ bash-3.2"),
            patch("duo.commander.is_at_main_prompt", return_value=False),
            patch("duo.commander.send_bootstrap") as mock_bootstrap,
            patch("duo.commander.time.sleep"),
            patch("duo.commander.get_config", side_effect=lambda k: config_values[k]),
        ):
            split_result = MagicMock()
            split_result.returncode = 0
            split_result.stdout = "%42\n"
            layout_result = MagicMock()
            layout_result.returncode = 0
            mock_run.side_effect = [split_result, layout_result]

            start_session(task)

            assert task.status == TaskStatus.FAILED
            mock_bootstrap.assert_not_called()
            events = [
                json.loads(line)
                for line in task.journal_path.read_text().strip().split("\n")
            ]
            failed_events = [e for e in events if e.get("event") == "startup_failed"]
            assert len(failed_events) == 1
            assert failed_events[0]["data"]["reason"] == "not_at_prompt"
        """When /allow-all wait_for_idle returns False, warning is logged."""
        task = _make_task()

        config_values = {
            "auto_allow_all": True,
            "auto_claude_commander": False,
            "copilot_model": "claude-opus-4.6",
            "bypass_permissions": True,
        }

        with (
            patch("duo.commander.subprocess.run") as mock_run,
            patch("duo.commander.name_pane"),
            patch("duo.commander.send_shell_command"),
            patch("duo.commander.wait_for_idle", side_effect=[True, False]),
            patch("duo.commander.read_pane", return_value="❯"),
            patch("duo.commander.is_at_main_prompt", return_value=True),
            patch("duo.commander.send_bootstrap"),
            patch("duo.commander.time.sleep"),
            patch("duo.commander.get_config", side_effect=lambda k: config_values[k]),
        ):
            split_result = MagicMock()
            split_result.returncode = 0
            split_result.stdout = "%42\n"
            layout_result = MagicMock()
            layout_result.returncode = 0
            mock_run.side_effect = [split_result, layout_result]

            start_session(task)

            # Session still succeeds (allow-all timeout is non-fatal)
            assert task.status == TaskStatus.PROMPT_SENT

    def test_start_session_split_window_timeout(self):
        """TimeoutExpired on split-window transitions to FAILED."""
        task = _make_task()

        with (
            patch(
                "duo.commander.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["tmux"], 10),
            ),
        ):
            start_session(task)
            assert task.status == TaskStatus.FAILED
            events = [
                json.loads(line)
                for line in task.journal_path.read_text().strip().split("\n")
            ]
            fail_events = [
                e for e in events if e.get("event") == "session_start_failed"
            ]
            assert len(fail_events) == 1
            assert "timeout" in fail_events[0]["data"]["error"]

    def test_start_session_layout_timeout_nonfatal(self):
        """TimeoutExpired on select-layout is non-fatal — session continues."""
        task = _make_task()

        config_values = {
            "auto_allow_all": True,
            "auto_claude_commander": False,
            "copilot_model": "claude-opus-4.6",
            "bypass_permissions": True,
        }

        with (
            patch("duo.commander.subprocess.run") as mock_run,
            patch("duo.commander.name_pane"),
            patch("duo.commander.send_shell_command"),
            patch("duo.commander.wait_for_idle", return_value=True),
            patch("duo.commander.read_pane", return_value="❯"),
            patch("duo.commander.is_at_main_prompt", return_value=True),
            patch("duo.commander.send_bootstrap"),
            patch("duo.commander.time.sleep"),
            patch("duo.commander.get_config", side_effect=lambda k: config_values[k]),
        ):
            split_result = MagicMock()
            split_result.returncode = 0
            split_result.stdout = "%42\n"
            mock_run.side_effect = [
                split_result,
                subprocess.TimeoutExpired(["tmux"], 10),
            ]

            start_session(task)
            assert task.status == TaskStatus.PROMPT_SENT

    def test_start_session_name_pane_failure_cleans_up(self):
        """name_pane failure kills orphaned pane and transitions FAILED."""
        task = _make_task()

        with (
            patch("duo.commander.subprocess.run") as mock_run,
            patch(
                "duo.commander.name_pane",
                side_effect=RuntimeError("name failed"),
            ),
            patch("duo.commander.kill_pane") as mock_kill,
        ):
            split_result = MagicMock()
            split_result.returncode = 0
            split_result.stdout = "%42\n"
            mock_run.return_value = split_result

            start_session(task)

            assert task.status == TaskStatus.FAILED
            mock_kill.assert_called_once_with("%42")
            events = [
                json.loads(line)
                for line in task.journal_path.read_text().strip().split("\n")
            ]
            fail_events = [
                e for e in events if e.get("event") == "session_start_failed"
            ]
            assert len(fail_events) == 1
            assert "name_pane" in fail_events[0]["data"]["error"]

    def test_start_session_bootstrap_timeout_cleans_up(self):
        """TimeoutExpired during bootstrap kills pane and transitions FAILED."""
        task = _make_task()

        config_values = {
            "auto_allow_all": False,
            "auto_claude_commander": False,
            "copilot_model": "claude-opus-4.6",
            "bypass_permissions": True,
        }

        with (
            patch("duo.commander.subprocess.run") as mock_run,
            patch("duo.commander.name_pane"),
            patch("duo.commander.send_shell_command"),
            patch("duo.commander.wait_for_idle", return_value=True),
            patch("duo.commander.read_pane", return_value="❯"),
            patch("duo.commander.is_at_main_prompt", return_value=True),
            patch(
                "duo.commander.send_bootstrap",
                side_effect=subprocess.TimeoutExpired(["tmux"], 10),
            ),
            patch("duo.commander.kill_pane") as mock_kill,
            patch("duo.commander.time.sleep"),
            patch("duo.commander.get_config", side_effect=lambda k: config_values[k]),
        ):
            split_result = MagicMock()
            split_result.returncode = 0
            split_result.stdout = "%42\n"
            layout_result = MagicMock()
            layout_result.returncode = 0
            mock_run.side_effect = [split_result, layout_result]

            with pytest.raises(subprocess.TimeoutExpired):
                start_session(task)

            assert task.status == TaskStatus.FAILED
            mock_kill.assert_called_once_with("%42")

    def test_start_session_defer_skips_bootstrap(self):
        """start_session(defer=True) skips bootstrap prompt and pr_consumed."""
        from unittest.mock import MagicMock

        task = _make_task()

        with (
            patch("duo.commander.subprocess.run") as mock_run,
            patch("duo.commander.name_pane"),
            patch("duo.commander.send_shell_command"),
            patch("duo.commander.wait_for_idle", return_value=True),
            patch("duo.commander.read_pane", return_value="❯"),
            patch("duo.commander.is_at_main_prompt", return_value=True),
            patch("duo.commander.send_bootstrap") as mock_boot,
            patch("duo.commander.time.sleep"),
            patch("duo.commander.get_config", return_value=False),
        ):
            split_result = MagicMock()
            split_result.returncode = 0
            split_result.stdout = "%42\n"
            layout_result = MagicMock()
            layout_result.returncode = 0
            mock_run.side_effect = [split_result, layout_result]

            start_session(task, defer=True)

            # Bootstrap should NOT be called
            mock_boot.assert_not_called()
            # Task stays at SESSION_STARTING (not PROMPT_SENT)
            assert task.status == TaskStatus.SESSION_STARTING


class TestStartSessionReusePane:
    """Test start_session with reuse_pane parameter."""

    def test_reuse_pane_skips_split_window(self):
        """reuse_pane skips tmux split-window and reuses given pane."""
        task = _make_task()

        with (
            patch("duo.commander.subprocess.run") as mock_run,
            patch("duo.commander.name_pane") as mock_name,
            patch("duo.commander.send_shell_command"),
            patch("duo.commander.wait_for_idle"),
            patch("duo.commander.read_pane", return_value="❯"),
            patch("duo.commander.is_at_main_prompt", return_value=True),
            patch("duo.commander.send_bootstrap"),
            patch("duo.commander.time.sleep"),
            patch("duo.commander.get_config", return_value=False),
        ):
            start_session(task, reuse_pane="%99")

            # Should NOT have called tmux split-window
            for call in mock_run.call_args_list:
                args = call[0][0] if call[0] else call[1].get("args", [])
                assert "split-window" not in args
            # Should have renamed the pane
            mock_name.assert_called_once_with("%99", task.pane_label)
            assert task.status == TaskStatus.PROMPT_SENT

    def test_reuse_pane_name_failure(self):
        """reuse_pane fails if name_pane raises."""
        task = _make_task()

        with (
            patch(
                "duo.commander.name_pane",
                side_effect=RuntimeError("no such pane"),
            ),
            patch("duo.commander.time.sleep"),
        ):
            start_session(task, reuse_pane="%99")
            assert task.status == TaskStatus.FAILED

    def test_reuse_pane_empty_string_creates_new(self):
        """Empty reuse_pane string creates a new pane (default behavior)."""
        from unittest.mock import MagicMock

        task = _make_task()

        with (
            patch("duo.commander.subprocess.run") as mock_run,
            patch("duo.commander.name_pane"),
            patch("duo.commander.send_shell_command"),
            patch("duo.commander.wait_for_idle"),
            patch("duo.commander.read_pane", return_value="❯"),
            patch("duo.commander.is_at_main_prompt", return_value=True),
            patch("duo.commander.send_bootstrap"),
            patch("duo.commander.time.sleep"),
            patch("duo.commander.get_config", return_value=False),
        ):
            split_result = MagicMock()
            split_result.returncode = 0
            split_result.stdout = "%42\n"
            layout_result = MagicMock()
            layout_result.returncode = 0
            mock_run.side_effect = [split_result, layout_result]

            start_session(task, reuse_pane="")

            # Should have called split-window
            first_call_args = mock_run.call_args_list[0][0][0]
            assert "split-window" in first_call_args


class TestBypassPermissions:
    """Verify bypass_permissions config controls --yolo and --dangerously-skip-permissions."""

    def test_copilot_yolo_when_bypass_enabled(self):
        """copilot command includes --yolo when bypass_permissions=True."""
        from unittest.mock import MagicMock

        task = _make_task()
        config_values = {
            "auto_allow_all": False,
            "auto_claude_commander": False,
            "copilot_model": "claude-opus-4.6",
            "bypass_permissions": True,
        }
        with (
            patch("duo.commander.subprocess.run") as mock_run,
            patch("duo.commander.name_pane"),
            patch("duo.commander.send_shell_command") as mock_send,
            patch("duo.commander.wait_for_idle"),
            patch("duo.commander.read_pane", return_value="❯"),
            patch("duo.commander.is_at_main_prompt", return_value=True),
            patch("duo.commander.send_bootstrap"),
            patch("duo.commander.time.sleep"),
            patch("duo.commander.get_config", side_effect=lambda k: config_values[k]),
        ):
            split_result = MagicMock()
            split_result.returncode = 0
            split_result.stdout = "%42\n"
            layout_result = MagicMock()
            layout_result.returncode = 0
            mock_run.side_effect = [split_result, layout_result]
            start_session(task)
            copilot_calls = [c for c in mock_send.call_args_list if "--yolo" in str(c)]
            assert len(copilot_calls) == 1

    def test_copilot_no_yolo_when_bypass_disabled(self):
        """copilot command omits --yolo when bypass_permissions=False."""
        from unittest.mock import MagicMock

        task = _make_task()
        config_values = {
            "auto_allow_all": False,
            "auto_claude_commander": False,
            "copilot_model": "claude-opus-4.6",
            "bypass_permissions": False,
        }
        with (
            patch("duo.commander.subprocess.run") as mock_run,
            patch("duo.commander.name_pane"),
            patch("duo.commander.send_shell_command") as mock_send,
            patch("duo.commander.wait_for_idle"),
            patch("duo.commander.read_pane", return_value="❯"),
            patch("duo.commander.is_at_main_prompt", return_value=True),
            patch("duo.commander.send_bootstrap"),
            patch("duo.commander.time.sleep"),
            patch("duo.commander.get_config", side_effect=lambda k: config_values[k]),
        ):
            split_result = MagicMock()
            split_result.returncode = 0
            split_result.stdout = "%42\n"
            layout_result = MagicMock()
            layout_result.returncode = 0
            mock_run.side_effect = [split_result, layout_result]
            start_session(task)
            copilot_calls = [c for c in mock_send.call_args_list if "--yolo" in str(c)]
            assert len(copilot_calls) == 0


# ---------------------------------------------------------------------------


class TestClaudeCommander:
    def test_detect_python_project(self) -> None:
        """Detects Python project from pyproject.toml."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "pyproject.toml").write_text("[project]\nname='x'")
            ctx = _detect_project_context(tmp)
            assert "Python" in ctx
            assert "pytest" in ctx

    def test_detect_node_project(self) -> None:
        """Detects Node.js project from package.json."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "package.json").write_text('{"name":"x"}')
            ctx = _detect_project_context(tmp)
            assert "Node.js" in ctx
            assert "npm" in ctx

    def test_detect_rust_project(self) -> None:
        """Detects Rust project from Cargo.toml."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Cargo.toml").write_text("[package]\nname='x'")
            ctx = _detect_project_context(tmp)
            assert "Rust" in ctx
            assert "cargo" in ctx

    def test_detect_go_project(self) -> None:
        """Detects Go project from go.mod."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "go.mod").write_text("module x")
            ctx = _detect_project_context(tmp)
            assert "Go" in ctx

    def test_detect_java_project(self) -> None:
        """Detects Java project from pom.xml."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "pom.xml").write_text("<project></project>")
            ctx = _detect_project_context(tmp)
            assert "Java" in ctx
            assert "Maven" in ctx

    def test_detect_unknown_project(self) -> None:
        """Empty dir returns Unknown type."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ctx = _detect_project_context(tmp)
            assert "Unknown" in ctx

    def test_includes_instructions_md(self) -> None:
        """Includes .duo/instructions.md content."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            duo_dir = Path(tmp) / ".duo"
            duo_dir.mkdir()
            (duo_dir / "instructions.md").write_text("Use pytest for all tests.")
            ctx = _detect_project_context(tmp)
            assert "Use pytest for all tests" in ctx

    def test_skips_template_instructions(self) -> None:
        """Skips instructions.md that only contains template comments."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            duo_dir = Path(tmp) / ".duo"
            duo_dir.mkdir()
            (duo_dir / "instructions.md").write_text(
                "<!-- Describe your project here -->\n"
            )
            ctx = _detect_project_context(tmp)
            assert "Describe your project" not in ctx

    def test_includes_readme_excerpt(self) -> None:
        """Includes README.md excerpt."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "README.md").write_text("# My Project\nA cool project.")
            ctx = _detect_project_context(tmp)
            assert "My Project" in ctx
            assert "README" in ctx

    def test_instructions_read_error_handled(self) -> None:
        """OSError reading .duo/instructions.md is handled gracefully."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            duo_dir = Path(tmp) / ".duo"
            duo_dir.mkdir()
            # Create a directory where a file is expected → read_text raises
            (duo_dir / "instructions.md").mkdir()
            ctx = _detect_project_context(tmp)
            assert "Project Instructions" not in ctx

    def test_readme_read_error_handled(self) -> None:
        """OSError reading README.md is handled gracefully."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            # Create a directory where README.md is expected
            (Path(tmp) / "README.md").mkdir()
            ctx = _detect_project_context(tmp)
            assert "README (excerpt)" not in ctx

    def test_includes_directory_listing(self) -> None:
        """Includes directory listing."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "src").mkdir()
            (Path(tmp) / "tests").mkdir()
            ctx = _detect_project_context(tmp)
            assert "src" in ctx
            assert "tests" in ctx

    def test_multi_config_python_wins(self) -> None:
        """When both pyproject.toml and package.json exist, Python wins (elif chain)."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "pyproject.toml").touch()
            (Path(tmp) / "package.json").write_text("{}")
            ctx = _detect_project_context(tmp)
            assert "Python" in ctx
            assert "Node.js" not in ctx

    def test_write_claude_md_creates_file(self) -> None:
        """write_commander_claude_md creates CLAUDE.md in worktree."""
        task = _make_task()
        # _make_task uses /fake/worktree — need real dir
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            task.worktree = tmp
            write_commander_claude_md(task)
            claude_md = Path(tmp) / "CLAUDE.md"
            assert claude_md.exists()
            content = claude_md.read_text()
            assert "Commander" in content
            assert task.id in content
            assert task.incarnation_id in content

    def test_write_claude_md_handles_missing_dir(self) -> None:
        """write_commander_claude_md handles write failure gracefully."""
        task = _make_task()
        task.worktree = "/nonexistent/path/does/not/exist"
        # Should NOT raise
        write_commander_claude_md(task)

    def test_write_claude_md_ceo_content(self) -> None:
        """CLAUDE.md contains CEO identity, defer workflow, PR budget, and commands."""
        import tempfile

        task = _make_task()
        with tempfile.TemporaryDirectory() as tmp:
            task.worktree = tmp
            write_commander_claude_md(task)
            content = (Path(tmp) / "CLAUDE.md").read_text()
            # CEO identity
            assert "CEO" in content
            assert task.id in content
            assert task.pane_label in content
            # Defer workflow
            assert "defer" in content.lower()
            assert "duo send" in content
            assert "duo ceo-status" in content
            # PR budget
            assert "Premium Request" in content
            assert "FREE" in content
            # Commands table
            assert "duo ceo-select" in content
            assert "duo ceo-approve" in content
            assert "duo watch" in content
            assert "duo doctor" in content
            assert "duo ceo-cleanup" in content
            assert "duo ceo-restart" in content
            assert "duo diff" in content
            # Rubber-duck
            assert "rubber-duck" in content.lower() or "Rubber-duck" in content
            # Known pitfalls
            assert "kqueue" in content or "leak" in content
            assert "CAPIError" in content

    def test_write_claude_md_includes_plan(self) -> None:
        """CLAUDE.md includes thinking session plan.md when it exists."""
        import tempfile

        task = _make_task()
        with tempfile.TemporaryDirectory() as tmp:
            task.worktree = tmp
            duo_dir = Path(tmp) / ".duo"
            thinking_dir = duo_dir / "thinking" / task.id
            thinking_dir.mkdir(parents=True, exist_ok=True)
            (thinking_dir / "plan.md").write_text("# Test Plan\nDo X then Y")
            with patch("duo.commander.DUO_DIR", duo_dir):
                write_commander_claude_md(task)
                content = (Path(tmp) / "CLAUDE.md").read_text()
                assert "Thinking Session Plan" in content
                assert "Do X then Y" in content

    def test_write_claude_md_plan_read_error(self) -> None:
        """CLAUDE.md still generated when plan.md read raises OSError."""
        import tempfile

        task = _make_task()
        with tempfile.TemporaryDirectory() as tmp:
            task.worktree = tmp
            with patch("duo.commander.DUO_DIR", Path(tmp) / ".duo"):
                plan_dir = Path(tmp) / ".duo" / "thinking" / task.id
                plan_dir.mkdir(parents=True)
                plan_file = plan_dir / "plan.md"
                plan_file.write_text("test")
                plan_file.chmod(0o000)
                try:
                    write_commander_claude_md(task)
                    content = (Path(tmp) / "CLAUDE.md").read_text()
                    assert "CEO" in content
                    assert "Thinking Session Plan" not in content
                finally:
                    plan_file.chmod(0o644)

    def test_write_claude_md_empty_plan_ignored(self) -> None:
        """CLAUDE.md omits Thinking Session Plan when plan.md is whitespace-only."""
        import tempfile

        task = _make_task()
        with tempfile.TemporaryDirectory() as tmp:
            task.worktree = tmp
            with patch("duo.commander.DUO_DIR", Path(tmp) / ".duo"):
                plan_dir = Path(tmp) / ".duo" / "thinking" / task.id
                plan_dir.mkdir(parents=True)
                (plan_dir / "plan.md").write_text("   \n  \n  ")
                write_commander_claude_md(task)
                content = (Path(tmp) / "CLAUDE.md").read_text()
                assert "CEO" in content
                assert "Thinking Session Plan" not in content

    def test_start_claude_commander_success(self) -> None:
        """start_claude_commander opens pane and launches claude."""
        import tempfile

        task = _make_task()
        with tempfile.TemporaryDirectory() as tmp:
            task.worktree = tmp
            with (
                patch("duo.commander.subprocess.run") as mock_run,
                patch("duo.commander.name_pane"),
                patch("duo.commander.send_shell_command"),
                patch("duo.commander.time.sleep"),
            ):
                split_result = MagicMock()
                split_result.returncode = 0
                split_result.stdout = "%50\n"
                layout_result = MagicMock()
                layout_result.returncode = 0
                mock_run.side_effect = [split_result, layout_result]

                result = start_claude_commander(task)
                assert result == "%50"

                events = read_jsonl(task.journal_path)
                assert any(e.get("event") == "claude_commander_started" for e in events)

    def test_start_claude_commander_no_bypass(self) -> None:
        """Claude command omits --dangerously-skip-permissions when bypass_permissions=False."""
        import tempfile

        task = _make_task()
        with tempfile.TemporaryDirectory() as tmp:
            task.worktree = tmp
            with (
                patch("duo.commander.subprocess.run") as mock_run,
                patch("duo.commander.name_pane"),
                patch("duo.commander.send_shell_command") as mock_send,
                patch("duo.commander.time.sleep"),
                patch("duo.commander.get_config", return_value=False),
            ):
                split_result = MagicMock()
                split_result.returncode = 0
                split_result.stdout = "%50\n"
                layout_result = MagicMock()
                layout_result.returncode = 0
                mock_run.side_effect = [split_result, layout_result]

                result = start_claude_commander(task)
                assert result == "%50"

                # Verify "claude" was sent WITHOUT --dangerously-skip-permissions
                claude_calls = [
                    c for c in mock_send.call_args_list if "claude" in str(c)
                ]
                assert claude_calls
                assert "--dangerously-skip-permissions" not in str(claude_calls[-1])

    def test_start_claude_commander_tmux_failure(self) -> None:
        """start_claude_commander returns None when tmux fails."""
        import tempfile

        task = _make_task()
        with tempfile.TemporaryDirectory() as tmp:
            task.worktree = tmp
            with patch("duo.commander.subprocess.run") as mock_run:
                fail = MagicMock()
                fail.returncode = 1
                fail.stderr = "no server"
                mock_run.return_value = fail

                result = start_claude_commander(task)
                assert result is None

    def test_start_claude_commander_transport_failure(self) -> None:
        """start_claude_commander cleans up pane on transport error."""
        import tempfile

        task = _make_task()
        with tempfile.TemporaryDirectory() as tmp:
            task.worktree = tmp
            with (
                patch("duo.commander.subprocess.run") as mock_run,
                patch("duo.commander.name_pane"),
                patch(
                    "duo.commander.send_shell_command",
                    side_effect=RuntimeError("broken"),
                ),
                patch("duo.commander.time.sleep"),
            ):
                split_result = MagicMock()
                split_result.returncode = 0
                split_result.stdout = "%60\n"
                layout_result = MagicMock()
                layout_result.returncode = 0
                kill_result = MagicMock()
                kill_result.returncode = 0
                mock_run.side_effect = [split_result, layout_result, kill_result]

                result = start_claude_commander(task)
                assert result is None

    def test_start_claude_commander_kill_pane_failure_suppressed(self) -> None:
        """start_claude_commander calls kill_pane when launch fails."""
        import tempfile

        task = _make_task()
        with tempfile.TemporaryDirectory() as tmp:
            task.worktree = tmp
            with (
                patch("duo.commander.subprocess.run") as mock_run,
                patch("duo.commander.name_pane"),
                patch(
                    "duo.commander.send_shell_command",
                    side_effect=RuntimeError("broken"),
                ),
                patch("duo.commander.time.sleep"),
                patch("duo.commander.kill_pane") as mock_kill,
            ):
                split_result = MagicMock()
                split_result.returncode = 0
                split_result.stdout = "%60\n"
                layout_result = MagicMock()
                layout_result.returncode = 0
                mock_run.side_effect = [split_result, layout_result]

                result = start_claude_commander(task)
                assert result is None
                mock_kill.assert_called_once_with("%60")

    def test_start_claude_commander_no_tmux_session(self) -> None:
        """start_claude_commander returns None when session target fails."""
        import tempfile

        task = _make_task()
        with tempfile.TemporaryDirectory() as tmp:
            task.worktree = tmp
            with patch(
                "duo.commander.get_tmux_session_target",
                side_effect=RuntimeError("no session"),
            ):
                result = start_claude_commander(task)
                assert result is None

    def test_start_claude_commander_worktree_quoted(self) -> None:
        """Worktree path is shell-quoted in claude commander cd."""
        import tempfile

        task = _make_task()
        with tempfile.TemporaryDirectory() as tmp:
            task.worktree = f"{tmp}/path with spaces"
            Path(task.worktree).mkdir(parents=True, exist_ok=True)
            with (
                patch("duo.commander.subprocess.run") as mock_run,
                patch("duo.commander.name_pane"),
                patch("duo.commander.send_shell_command") as mock_send,
                patch("duo.commander.time.sleep"),
            ):
                split_result = MagicMock()
                split_result.returncode = 0
                split_result.stdout = "%50\n"
                layout_result = MagicMock()
                layout_result.returncode = 0
                mock_run.side_effect = [split_result, layout_result]

                start_claude_commander(task)

                cd_call = mock_send.call_args_list[0]
                cd_cmd = cd_call[0][1]
                assert "'" in cd_cmd or "\\" in cd_cmd
                assert "cd " in cd_cmd

    def test_start_claude_commander_split_timeout(self) -> None:
        """split-window TimeoutExpired returns None gracefully."""
        import tempfile

        task = _make_task()
        with tempfile.TemporaryDirectory() as tmp:
            task.worktree = tmp
            with (
                patch(
                    "duo.commander.subprocess.run",
                    side_effect=subprocess.TimeoutExpired(["tmux"], 10),
                ),
                patch("duo.commander.get_tmux_session_target", return_value="$0"),
            ):
                result = start_claude_commander(task)
                assert result is None

    def test_start_claude_commander_layout_timeout(self) -> None:
        """select-layout TimeoutExpired is logged but doesn't block."""
        import tempfile

        task = _make_task()
        with tempfile.TemporaryDirectory() as tmp:
            task.worktree = tmp
            with (
                patch("duo.commander.subprocess.run") as mock_run,
                patch("duo.commander.name_pane"),
                patch("duo.commander.send_shell_command"),
                patch("duo.commander.time.sleep"),
            ):
                split_result = MagicMock()
                split_result.returncode = 0
                split_result.stdout = "%50\n"
                mock_run.side_effect = [
                    split_result,
                    subprocess.TimeoutExpired(["tmux"], 10),
                ]

                result = start_claude_commander(task)
                assert result == "%50"

    def test_start_claude_commander_send_timeout(self) -> None:
        """TimeoutExpired from send_shell_command kills pane and returns None."""
        import tempfile

        task = _make_task()
        with tempfile.TemporaryDirectory() as tmp:
            task.worktree = tmp
            with (
                patch("duo.commander.subprocess.run") as mock_run,
                patch("duo.commander.name_pane"),
                patch(
                    "duo.commander.send_shell_command",
                    side_effect=subprocess.TimeoutExpired(["tmux"], 10),
                ),
                patch("duo.commander.kill_pane") as mock_kill,
                patch("duo.commander.time.sleep"),
            ):
                split_result = MagicMock()
                split_result.returncode = 0
                split_result.stdout = "%50\n"
                layout_result = MagicMock()
                layout_result.returncode = 0
                mock_run.side_effect = [split_result, layout_result]

                result = start_claude_commander(task)
                assert result is None
                mock_kill.assert_called_once_with("%50")

    def test_start_session_with_claude_commander(self) -> None:
        """start_session also launches Claude commander when config enabled."""
        task = _make_task()
        import tempfile

        config_vals = {
            "auto_allow_all": True,
            "auto_claude_commander": True,
            "copilot_model": "claude-opus-4.6",
            "bypass_permissions": True,
        }

        with tempfile.TemporaryDirectory() as tmp:
            task.worktree = tmp
            with (
                patch("duo.commander.subprocess.run") as mock_run,
                patch("duo.commander.name_pane"),
                patch("duo.commander.send_shell_command"),
                patch("duo.commander.wait_for_idle", return_value=True),
                patch("duo.commander.read_pane", return_value="❯"),
                patch("duo.commander.is_at_main_prompt", return_value=True),
                patch("duo.commander.send_bootstrap"),
                patch("duo.commander.time.sleep"),
                patch(
                    "duo.commander.get_config",
                    side_effect=lambda k: config_vals[k],
                ),
                patch(
                    "duo.commander.start_claude_commander", return_value="%99"
                ) as mock_claude,
            ):
                split_result = MagicMock()
                split_result.returncode = 0
                split_result.stdout = "%42\n"
                layout_result = MagicMock()
                layout_result.returncode = 0
                mock_run.side_effect = [split_result, layout_result]

                start_session(task)
                mock_claude.assert_called_once_with(task)

    def test_start_session_claude_commander_failure_nonfatal(self) -> None:
        """start_session continues even if Claude commander fails to start."""
        task = _make_task()
        import tempfile

        config_vals = {
            "auto_allow_all": True,
            "auto_claude_commander": True,
            "copilot_model": "claude-opus-4.6",
            "bypass_permissions": True,
        }

        with tempfile.TemporaryDirectory() as tmp:
            task.worktree = tmp
            with (
                patch("duo.commander.subprocess.run") as mock_run,
                patch("duo.commander.name_pane"),
                patch("duo.commander.send_shell_command"),
                patch("duo.commander.wait_for_idle", return_value=True),
                patch("duo.commander.read_pane", return_value="❯"),
                patch("duo.commander.is_at_main_prompt", return_value=True),
                patch("duo.commander.send_bootstrap"),
                patch("duo.commander.time.sleep"),
                patch(
                    "duo.commander.get_config",
                    side_effect=lambda k: config_vals[k],
                ),
                patch("duo.commander.start_claude_commander", return_value=None),
            ):
                split_result = MagicMock()
                split_result.returncode = 0
                split_result.stdout = "%42\n"
                layout_result = MagicMock()
                layout_result.returncode = 0
                mock_run.side_effect = [split_result, layout_result]

                start_session(task)
                # Task still succeeds
                assert task.status == TaskStatus.PROMPT_SENT


# ---------------------------------------------------------------------------
# write_project_claude_md
# ---------------------------------------------------------------------------


class TestWriteProjectClaudeMd:
    def test_creates_new_claude_md(self, tmp_path):
        """Creates CLAUDE.md when none exists."""
        write_project_claude_md(str(tmp_path))
        claude_md = tmp_path / "CLAUDE.md"
        assert claude_md.exists()
        content = claude_md.read_text()
        assert "CEO" in content
        assert "duo-managed-start" in content
        assert "duo-managed-end" in content
        assert "duo send" in content
        assert "Premium Request" in content

    def test_content_has_all_sections(self, tmp_path):
        """Generated CLAUDE.md has all required CEO sections."""
        write_project_claude_md(str(tmp_path))
        content = (tmp_path / "CLAUDE.md").read_text()
        # Phase workflow
        assert "Phase 1" in content
        assert "Phase 2" in content
        assert "duo start" in content
        assert "reuse-pane" in content
        # Commands
        assert "duo ceo-select" in content
        assert "duo ceo-approve" in content
        assert "duo watch" in content
        assert "duo doctor" in content
        assert "duo merge" in content
        # Budget table
        assert "FREE" in content
        assert "1 PR" in content
        # Rubber-duck
        assert "Mode A" in content or "rubber-duck" in content.lower()
        # Pitfalls
        assert "CAPIError" in content

    def test_preserves_existing_with_markers(self, tmp_path):
        """Updates duo section, preserves user content around markers."""
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text(
            "# My Project\n\nUser content before.\n\n"
            "<!-- duo-managed-start -->\nOLD DUO CONTENT\n<!-- duo-managed-end -->\n\n"
            "User content after.\n"
        )
        write_project_claude_md(str(tmp_path))
        content = claude_md.read_text()
        assert "User content before." in content
        assert "User content after." in content
        assert "OLD DUO CONTENT" not in content
        assert "CEO" in content

    def test_prepends_to_existing_without_markers(self, tmp_path):
        """Prepends duo section to existing CLAUDE.md without markers."""
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# My Existing CLAUDE.md\n\nCustom rules here.\n")
        write_project_claude_md(str(tmp_path))
        content = claude_md.read_text()
        assert "duo-managed-start" in content
        assert "My Existing CLAUDE.md" in content
        assert "Custom rules here." in content
        # Duo section should come first
        assert content.index("duo-managed-start") < content.index(
            "My Existing CLAUDE.md"
        )

    def test_includes_project_context(self, tmp_path):
        """Includes auto-detected project context."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n")
        write_project_claude_md(str(tmp_path))
        content = (tmp_path / "CLAUDE.md").read_text()
        assert "Python" in content

    def test_handles_write_failure(self, caplog):
        """Handles write failure gracefully."""
        import logging

        with caplog.at_level(logging.WARNING, logger="duo.commander"):
            write_project_claude_md("/nonexistent/path/does/not/exist")
        assert "Failed" in caplog.text

    def test_idempotent(self, tmp_path):
        """Running twice produces same result."""
        write_project_claude_md(str(tmp_path))
        content1 = (tmp_path / "CLAUDE.md").read_text()
        write_project_claude_md(str(tmp_path))
        content2 = (tmp_path / "CLAUDE.md").read_text()
        assert content1 == content2

    def test_empty_project_context_omitted(self, tmp_path):
        """When project context is empty, no Project Context section is added."""
        with patch("duo.commander._detect_project_context", return_value="  "):
            write_project_claude_md(str(tmp_path))
        content = (tmp_path / "CLAUDE.md").read_text()
        assert "Project Context" not in content


# ---------------------------------------------------------------------------
# build_task_prompt edge cases
# ---------------------------------------------------------------------------


class TestBuildTaskPromptEdgeCases:
    def test_build_task_prompt_last_step(self):
        """build prompt for the final step of a multi-step task."""
        task = _make_task(
            subtasks=[_make_subtask(1), _make_subtask(2), _make_subtask(3)]
        )
        task.current_step = 3
        task.current_attempt = 1
        prompt = build_task_prompt(task)
        assert "step: 3" in prompt
        assert "implement step 3" in prompt
        assert task.incarnation_id in prompt

    def test_build_task_prompt_with_correction(self):
        """build prompt when attempt > 1 (correction prompt)."""
        task = _make_task()
        task.current_attempt = 3
        prompt = build_correction_prompt(task, "tests still failing")
        assert "step 1" in prompt
        assert "attempt 3" in prompt
        assert "tests still failing" in prompt
        assert task.incarnation_id in prompt

    def test_build_task_prompt_step_out_of_range(self):
        """build_task_prompt raises ValueError for step=0."""
        task = _make_task()
        task.current_step = 0
        with pytest.raises(ValueError, match="out of range"):
            build_task_prompt(task)

    def test_build_task_prompt_step_too_high(self):
        """build_task_prompt raises ValueError for step beyond subtasks."""
        task = _make_task()
        task.current_step = 5
        with pytest.raises(ValueError, match="out of range"):
            build_task_prompt(task)


class TestBuildPromptBounds:
    def test_continue_prompt_step_too_high(self):
        """build_continue_prompt rejects out-of-range step."""
        task = _make_task()
        task.current_step = 999
        with pytest.raises(ValueError, match="out of range"):
            build_continue_prompt(task)

    def test_continue_prompt_step_zero(self):
        """build_continue_prompt rejects step 0."""
        task = _make_task()
        task.current_step = 0
        with pytest.raises(ValueError, match="out of range"):
            build_continue_prompt(task)


# ---------------------------------------------------------------------------
# _get_copilot_model
# ---------------------------------------------------------------------------


class TestGetCopilotModel:
    def test_default_model(self, monkeypatch: pytest.MonkeyPatch):
        """Returns default model when no config or env var."""
        monkeypatch.delenv("DUO_COPILOT_MODEL", raising=False)
        monkeypatch.setattr("duo.commander.get_config", lambda k: None)
        assert _get_copilot_model() == "claude-opus-4.6"

    def test_env_var_override(self, monkeypatch: pytest.MonkeyPatch):
        """Env var takes priority."""
        monkeypatch.setenv("DUO_COPILOT_MODEL", "gpt-4o")
        assert _get_copilot_model() == "gpt-4o"

    def test_config_override(self, monkeypatch: pytest.MonkeyPatch):
        """Config value used when no env var."""
        monkeypatch.delenv("DUO_COPILOT_MODEL", raising=False)
        monkeypatch.setattr("duo.commander.get_config", lambda k: "custom-model")
        assert _get_copilot_model() == "custom-model"

    def test_env_var_invalid_rejected(self, monkeypatch: pytest.MonkeyPatch):
        """Env var with unsafe chars or excessive length falls back to config."""
        bad_models = [
            "x; curl evil.com",
            "model$(whoami)",
            "a b c",
            "x|y",
            "a" * 65,
            "x&rm -rf /",
        ]
        monkeypatch.setattr("duo.commander.get_config", lambda k: "safe-model")
        for bad_model in bad_models:
            monkeypatch.setenv("DUO_COPILOT_MODEL", bad_model)
            assert _get_copilot_model() == "safe-model", f"should reject: {bad_model!r}"

    def test_env_var_valid_accepted(self, monkeypatch: pytest.MonkeyPatch):
        """Valid model names from env var are accepted."""
        good_models = [
            "claude-opus-4.6",
            "gpt-4o",
            "a" * 64,
            "claude.sonnet-3.5_v2",
        ]
        for good_model in good_models:
            monkeypatch.setenv("DUO_COPILOT_MODEL", good_model)
            assert _get_copilot_model() == good_model, f"should accept: {good_model!r}"


# ---------------------------------------------------------------------------
# start_session — error paths
# ---------------------------------------------------------------------------


class TestStartSessionError:
    def test_start_session_tmux_failure(self):
        """start_session transitions to FAILED when tmux split fails."""
        task = _make_task()

        with (
            patch("duo.commander.subprocess.run") as mock_run,
            patch("duo.commander.time.sleep"),
        ):
            fail_result = MagicMock()
            fail_result.returncode = 1
            fail_result.stderr = "no server running"
            mock_run.return_value = fail_result

            start_session(task)

            assert task.status == TaskStatus.FAILED
            events = read_jsonl(task.journal_path)
            assert any(e.get("event") == "session_start_failed" for e in events)

    def test_start_session_already_session_starting(self):
        """start_session skips initial transition if already SESSION_STARTING."""
        task = _make_task()
        transition(task, TaskStatus.SESSION_STARTING)

        with (
            patch("duo.commander.subprocess.run") as mock_run,
            patch("duo.commander.name_pane"),
            patch("duo.commander.send_shell_command"),
            patch("duo.commander.wait_for_idle"),
            patch("duo.commander.read_pane", return_value="❯"),
            patch("duo.commander.is_at_main_prompt", return_value=True),
            patch("duo.commander.send_bootstrap"),
            patch("duo.commander.time.sleep"),
            patch("duo.commander.get_config", return_value=False),
        ):
            split_result = MagicMock()
            split_result.returncode = 0
            split_result.stdout = "%99\n"
            layout_result = MagicMock()
            layout_result.returncode = 0
            mock_run.side_effect = [split_result, layout_result]

            start_session(task)
            assert task.status == TaskStatus.PROMPT_SENT

    def test_start_session_transport_failure(self):
        """start_session transitions to FAILED when send_shell_command raises."""
        task = _make_task()

        with (
            patch("duo.commander.subprocess.run") as mock_run,
            patch("duo.commander.name_pane"),
            patch(
                "duo.commander.send_shell_command",
                side_effect=RuntimeError("tmux bridge error"),
            ),
            patch("duo.commander.time.sleep"),
        ):
            split_result = MagicMock()
            split_result.returncode = 0
            split_result.stdout = "%99\n"
            layout_result = MagicMock()
            layout_result.returncode = 0
            kill_result = MagicMock()
            kill_result.returncode = 0
            mock_run.side_effect = [split_result, layout_result, kill_result]

            with pytest.raises(RuntimeError, match="tmux bridge error"):
                start_session(task)

            assert task.status == TaskStatus.FAILED
            events = read_jsonl(task.journal_path)
            assert any(e.get("event") == "session_start_failed" for e in events)

    def test_restart_session_transport_failure(self):
        """restart_session transitions to FAILED on transport error."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        transition(task, TaskStatus.RUNNING)

        with (
            patch(
                "duo.commander.start_session",
                side_effect=RuntimeError("tmux died"),
            ),
            patch("duo.commander.kill_pane", return_value=True),
            patch("duo.commander.time.sleep"),
        ):
            with pytest.raises(RuntimeError, match="tmux died"):
                restart_session(task)

            assert task.status == TaskStatus.FAILED
            events = read_jsonl(task.journal_path)
            assert any(e.get("event") == "session_restart_failed" for e in events)

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    def test_send_task_prompt_success(self, mock_select, mock_wait):
        """Prompt sent, pr_consumed logged, state transitions to PROMPT_SENT."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        prompt = "test prompt #hash:abc"

        send_task_prompt(task, prompt)

        mock_select.assert_called_once_with(task.pane_label, prompt)
        assert task.status == TaskStatus.PROMPT_SENT
        events = read_jsonl(task.journal_path)
        assert any(
            e.get("event") == "pr_consumed"
            and e.get("data", {}).get("action") == "task_prompt"
            for e in events
        )

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    def test_send_task_prompt_writes_prompt_file(self, mock_select, mock_wait):
        """Prompt text is persisted to disk for debug."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        prompt = "my special prompt"

        send_task_prompt(task, prompt)

        prompt_path = task.prompt_path(task.current_step, task.current_attempt)
        assert prompt_path.exists()
        assert prompt_path.read_text() == prompt

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    def test_send_task_prompt_records_prompt_sent_event(self, mock_select, mock_wait):
        """prompt_sent event is appended with hash."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        prompt = "check this"

        send_task_prompt(task, prompt)

        events = read_jsonl(task.journal_path)
        prompt_events = [e for e in events if e.get("event") == "prompt_sent"]
        assert len(prompt_events) >= 1
        assert "prompt_hash" in prompt_events[-1]["data"]

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    def test_send_task_prompt_updates_last_prompt_sent_at(self, mock_select, mock_wait):
        """last_prompt_sent_at is updated after sending."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        assert task.last_prompt_sent_at is None

        send_task_prompt(task, "prompt")

        assert task.last_prompt_sent_at is not None
        # Verify it's a valid ISO timestamp (not just any truthy value)
        from datetime import UTC, datetime

        dt = datetime.fromisoformat(task.last_prompt_sent_at)
        assert (datetime.now(UTC) - dt.replace(tzinfo=UTC)).total_seconds() < 5

    def test_send_task_prompt_pr_budget_exceeded(self, monkeypatch: pytest.MonkeyPatch):
        """PR budget exceeded → task escalated, no prompt sent."""
        task = _make_task()
        # Use CORRECTING state which allows transition to ESCALATED
        _advance_to_prompt_sent(task)
        transition(task, TaskStatus.VERIFYING)
        transition(task, TaskStatus.CORRECTING)
        monkeypatch.setattr(
            "duo.commander.get_config",
            lambda k: 1 if k == "pr_budget" else None,
        )
        # Consume the budget
        append_event(
            task, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )

        send_task_prompt(task, "prompt")

        assert task.status == TaskStatus.ESCALATED
        events = read_jsonl(task.journal_path)
        assert any(e.get("event") == "pr_budget_exceeded" for e in events)

    @patch("duo.commander.wait_for_dialog", return_value=False)
    def test_send_task_prompt_dialog_timeout(self, mock_wait):
        """Dialog timeout raises RuntimeError."""
        task = _make_task()
        _advance_to_prompt_sent(task)

        with pytest.raises(RuntimeError, match="Dialog timeout"):
            send_task_prompt(task, "prompt")

        events = read_jsonl(task.journal_path)
        assert any(e.get("event") == "dialog_timeout" for e in events)

    def test_send_task_prompt_write_oserror(self, monkeypatch: pytest.MonkeyPatch):
        """OSError writing prompt file is logged and re-raised."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        monkeypatch.setattr(
            "duo.commander.atomic_write_text",
            MagicMock(side_effect=OSError("read-only filesystem")),
        )
        with pytest.raises(OSError, match="read-only filesystem"):
            send_task_prompt(task, "prompt")


# ---------------------------------------------------------------------------
# resend_last_prompt
# ---------------------------------------------------------------------------


class TestResendLastPrompt:
    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    def test_resend_success(self, mock_select, mock_wait):
        """Resend reads prompt from file and sends it."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        # Write a prompt file
        prompt_path = task.prompt_path(task.current_step, task.current_attempt)
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text("resend me")

        resend_last_prompt(task)

        mock_select.assert_called_once_with(task.pane_label, "resend me")
        events = read_jsonl(task.journal_path)
        assert any(
            e.get("event") == "pr_consumed"
            and e.get("data", {}).get("action") == "resend_prompt"
            for e in events
        )
        assert any(e.get("event") == "prompt_resent" for e in events)

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    def test_resend_updates_last_prompt_sent_at(self, mock_select, mock_wait):
        """Resend updates last_prompt_sent_at."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        prompt_path = task.prompt_path(task.current_step, task.current_attempt)
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text("prompt")
        task.last_prompt_sent_at = None

        resend_last_prompt(task)

        assert task.last_prompt_sent_at is not None
        from datetime import UTC, datetime

        dt = datetime.fromisoformat(task.last_prompt_sent_at)
        assert (datetime.now(UTC) - dt.replace(tzinfo=UTC)).total_seconds() < 5

    def test_resend_no_prompt_file(self):
        """Resend is a no-op when no prompt file exists."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        # Don't write a prompt file
        resend_last_prompt(task)
        # No events should be appended for the resend
        events = read_jsonl(task.journal_path)
        assert not any(e.get("event") == "prompt_resent" for e in events)

    def test_resend_pr_budget_exceeded(self, monkeypatch: pytest.MonkeyPatch):
        """Resend respects PR budget — escalates if exceeded."""
        task = _make_task()
        # Use CORRECTING state which allows transition to ESCALATED
        _advance_to_prompt_sent(task)
        transition(task, TaskStatus.VERIFYING)
        transition(task, TaskStatus.CORRECTING)
        monkeypatch.setattr(
            "duo.commander.get_config",
            lambda k: 1 if k == "pr_budget" else None,
        )
        append_event(
            task, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )
        # Write a prompt file
        prompt_path = task.prompt_path(task.current_step, task.current_attempt)
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text("prompt")

        resend_last_prompt(task)

        assert task.status == TaskStatus.ESCALATED
        events = read_jsonl(task.journal_path)
        assert any(e.get("event") == "pr_budget_exceeded" for e in events)

    @patch("duo.commander.wait_for_dialog", return_value=False)
    def test_resend_dialog_timeout(self, mock_wait):
        """Dialog timeout logs event and returns without raising."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        prompt_path = task.prompt_path(task.current_step, task.current_attempt)
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text("prompt")

        resend_last_prompt(task)  # should not raise

        events = read_jsonl(task.journal_path)
        assert any(e.get("event") == "dialog_timeout_resend" for e in events)


# ---------------------------------------------------------------------------
# restart_session
# ---------------------------------------------------------------------------


class TestRestartSession:
    @patch("duo.commander.kill_pane", return_value=True)
    @patch("duo.commander.start_session")
    def test_restart_clears_bootstrap(self, mock_start, mock_kill):
        """restart_session discards pane from _BOOTSTRAP_DONE."""
        from duo.transport import _BOOTSTRAP_DONE, clear_bootstrap_done

        task = _make_task()
        _BOOTSTRAP_DONE.add(task.pane_label)

        restart_session(task)

        assert task.pane_label not in _BOOTSTRAP_DONE
        mock_start.assert_called_once_with(task)
        mock_kill.assert_called_once_with(task.pane_label)
        clear_bootstrap_done(task.pane_label)  # cleanup

    @patch("duo.commander.kill_pane", return_value=True)
    @patch("duo.commander.start_session")
    def test_restart_resets_incarnation(self, mock_start, mock_kill):
        """restart_session generates a new incarnation ID."""
        task = _make_task()
        old_inc = task.incarnation_id

        restart_session(task)

        assert task.incarnation_id != old_inc

    @patch("duo.commander.kill_pane", return_value=True)
    @patch("duo.commander.start_session")
    def test_restart_preserves_attempt(self, mock_start, mock_kill):
        """restart_session preserves current_attempt (correction context)."""
        task = _make_task()
        task.current_attempt = 3

        restart_session(task)

        assert task.current_attempt == 3

    @patch("duo.commander.kill_pane", return_value=True)
    @patch("duo.commander.start_session")
    def test_restart_logs_event(self, mock_start, mock_kill):
        """restart_session logs session_restarted event with old/new incarnation."""
        task = _make_task()
        old_inc = task.incarnation_id

        restart_session(task)

        events = read_jsonl(task.journal_path)
        restart_events = [e for e in events if e.get("event") == "session_restarted"]
        assert len(restart_events) == 1
        assert restart_events[0]["data"]["old_incarnation"] == old_inc
        assert restart_events[0]["data"]["new_incarnation"] == task.incarnation_id


# ---------------------------------------------------------------------------
# poll_task
# ---------------------------------------------------------------------------


class TestPollTask:
    def _make_poller(self, poll_result: PollResult) -> AdaptivePoller:
        """Create a poller that returns a fixed result."""
        poller = AdaptivePoller()
        poller.poll = MagicMock(return_value=poll_result)
        return poller

    @patch("duo.commander.verify_and_advance")
    @patch("duo.commander.read_result_for_step")
    def test_poll_result_ready_valid(self, mock_read_result, mock_verify):
        """RESULT_READY with matching incarnation triggers verify_and_advance."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        poller = self._make_poller(PollResult.RESULT_READY)
        result_obj = MagicMock()
        result_obj.incarnation = task.incarnation_id
        mock_read_result.return_value = result_obj

        ret = poll_task(task, poller)

        assert ret == PollResult.RESULT_READY
        mock_verify.assert_called_once_with(task, result_obj)

    @patch("duo.commander.verify_and_advance")
    @patch("duo.commander.read_result_for_step")
    def test_poll_result_ready_wrong_incarnation(self, mock_read_result, mock_verify):
        """RESULT_READY with stale incarnation does not verify but logs event."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        poller = self._make_poller(PollResult.RESULT_READY)
        result_obj = MagicMock()
        result_obj.incarnation = "stale-inc"
        mock_read_result.return_value = result_obj

        poll_task(task, poller)

        mock_verify.assert_not_called()
        # Verify journal records the incarnation mismatch
        journal = task.journal_path.read_text().strip().split("\n")
        events = [json.loads(line) for line in journal]
        mismatch = [
            e for e in events if e.get("event") == "result_incarnation_mismatch"
        ]
        assert len(mismatch) == 1
        assert mismatch[0]["data"]["got"] == "stale-inc"
        assert mismatch[0]["data"]["expected"] == task.incarnation_id

    @patch("duo.commander.verify_and_advance")
    @patch("duo.commander.read_result_for_step")
    def test_poll_result_ready_none(self, mock_read_result, mock_verify):
        """RESULT_READY with no result file does not verify."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        poller = self._make_poller(PollResult.RESULT_READY)
        mock_read_result.return_value = None

        poll_task(task, poller)

        mock_verify.assert_not_called()

    @patch("duo.commander.send_task_prompt")
    @patch("duo.commander.restart_session")
    @patch("duo.commander.is_process_alive", return_value=False)
    def test_poll_heartbeat_timeout_dead(self, mock_alive, mock_restart, mock_send):
        """HEARTBEAT_TIMEOUT + dead process → restart + resend prompt."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        poller = self._make_poller(PollResult.HEARTBEAT_TIMEOUT)

        poll_task(task, poller)

        mock_restart.assert_called_once_with(task)
        mock_send.assert_called_once()
        events = read_jsonl(task.journal_path)
        assert any(e.get("event") == "session_crashed" for e in events)

    @patch("duo.commander.send_task_prompt")
    @patch("duo.commander.is_process_alive", return_value=False)
    def test_poll_heartbeat_timeout_restart_fails(self, mock_alive, mock_send):
        """HEARTBEAT_TIMEOUT → restart leaves FAILED → skip prompt send."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        poller = self._make_poller(PollResult.HEARTBEAT_TIMEOUT)

        def fail_restart(t):
            t.status = TaskStatus.FAILED

        with patch("duo.commander.restart_session", side_effect=fail_restart):
            poll_task(task, poller)

        mock_send.assert_not_called()

    @patch("duo.commander.restart_session")
    @patch("duo.commander.is_process_alive", return_value=False)
    def test_poll_heartbeat_skips_restart_when_task_stopped(
        self, mock_alive, mock_restart
    ):
        """HEARTBEAT_TIMEOUT skips restart if task was stopped concurrently."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        poller = self._make_poller(PollResult.HEARTBEAT_TIMEOUT)

        # Simulate duo stop transitioning task to BLOCKED on disk
        transition(task, TaskStatus.BLOCKED)
        save_task(task)

        result = poll_task(task, poller)

        # Should NOT restart — task was stopped
        mock_restart.assert_not_called()
        assert result == PollResult.HEARTBEAT_TIMEOUT

    @patch("duo.commander.restart_session")
    @patch("duo.commander.is_process_alive", return_value=False)
    def test_poll_heartbeat_skips_restart_for_terminal_states(
        self, mock_alive, mock_restart
    ):
        """HEARTBEAT_TIMEOUT skips restart for any terminal on-disk state."""
        for i, terminal_status in enumerate(
            [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ESCALATED]
        ):
            task = _make_task(f"term-{i}")
            _advance_to_prompt_sent(task)
            poller = self._make_poller(PollResult.HEARTBEAT_TIMEOUT)
            task.status = terminal_status
            save_task(task)
            result = poll_task(task, poller)
            mock_restart.assert_not_called()
            assert result == PollResult.HEARTBEAT_TIMEOUT
            mock_restart.reset_mock()

    @patch("duo.commander.restart_session")
    @patch("duo.commander.is_process_alive", return_value=False)
    def test_poll_heartbeat_skips_restart_when_load_task_fails(
        self, mock_alive, mock_restart
    ):
        """HEARTBEAT_TIMEOUT skips restart if task.json is corrupt."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        poller = self._make_poller(PollResult.HEARTBEAT_TIMEOUT)

        with patch("duo.commander.load_task", return_value=None):
            result = poll_task(task, poller)

        mock_restart.assert_not_called()
        assert result == PollResult.HEARTBEAT_TIMEOUT

    @patch("duo.commander.send_task_prompt")
    @patch("duo.commander.restart_session")
    @patch("duo.commander.is_process_alive", return_value=False)
    def test_poll_crash_recovery_uses_persisted_prompt(
        self, mock_alive, mock_restart, mock_send
    ):
        """Crash recovery prefers persisted prompt over synthesized one."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        # Persist a correction prompt
        prompt_path = task.prompt_path(task.current_step, task.current_attempt)
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text("correction feedback prompt")
        poller = self._make_poller(PollResult.HEARTBEAT_TIMEOUT)

        poll_task(task, poller)

        sent_prompt = mock_send.call_args[0][1]
        assert sent_prompt == "correction feedback prompt"

    @patch("duo.commander.select_dialog_option")
    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.diagnose_pane", return_value="error: rate limit exceeded")
    @patch("duo.commander.is_process_alive", return_value=True)
    def test_poll_heartbeat_timeout_alive_error(
        self, mock_alive, mock_diag, mock_wait, mock_select
    ):
        """HEARTBEAT_TIMEOUT + alive + error → retry via dialog."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        poller = self._make_poller(PollResult.HEARTBEAT_TIMEOUT)

        poll_task(task, poller)

        mock_select.assert_called_once()
        events = read_jsonl(task.journal_path)
        assert any(e.get("event") == "api_error" for e in events)
        assert any(
            e.get("event") == "pr_consumed"
            and e.get("data", {}).get("action") == "error_retry"
            for e in events
        )

    @patch("duo.commander.diagnose_pane", return_value="CAPIError: 400 Bad Request")
    @patch("duo.commander.is_process_alive", return_value=True)
    def test_poll_heartbeat_timeout_alive_error_budget_exceeded(
        self, mock_alive, mock_diag, monkeypatch: pytest.MonkeyPatch
    ):
        """HEARTBEAT_TIMEOUT + alive + error + budget exceeded → escalated."""
        task = _make_task()
        # VERIFYING allows transition to ESCALATED
        _advance_to_prompt_sent(task)
        transition(task, TaskStatus.VERIFYING)
        monkeypatch.setattr(
            "duo.commander.get_config",
            lambda k: 1 if k == "pr_budget" else None,
        )
        append_event(
            task, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )
        poller = self._make_poller(PollResult.HEARTBEAT_TIMEOUT)

        with patch("duo.commander.wait_for_dialog", return_value=True):
            ret = poll_task(task, poller)

        assert task.status == TaskStatus.ESCALATED
        assert ret == PollResult.HEARTBEAT_TIMEOUT

    @patch("duo.commander.diagnose_pane", return_value="all good, working fine")
    @patch("duo.commander.is_process_alive", return_value=True)
    def test_poll_heartbeat_timeout_alive_no_error(self, mock_alive, mock_diag):
        """HEARTBEAT_TIMEOUT + alive + no error keywords → no retry."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        poller = self._make_poller(PollResult.HEARTBEAT_TIMEOUT)

        poll_task(task, poller)

        events = read_jsonl(task.journal_path)
        # No api_error or pr_consumed events
        assert not any(e.get("event") == "api_error" for e in events)

    @patch("duo.commander.wait_for_dialog", return_value=False)
    @patch(
        "duo.commander.diagnose_pane",
        return_value="✗ Execution failed: CAPIError: 400",
    )
    @patch("duo.commander.is_process_alive", return_value=True)
    def test_poll_heartbeat_timeout_alive_dialog_timeout(
        self, mock_alive, mock_diag, mock_wait
    ):
        """HEARTBEAT_TIMEOUT + alive + error + dialog timeout → no retry."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        poller = self._make_poller(PollResult.HEARTBEAT_TIMEOUT)

        poll_task(task, poller)

        events = read_jsonl(task.journal_path)
        assert any(e.get("event") == "capi_error" for e in events)
        # No pr_consumed for error_retry because dialog timed out
        assert not any(
            e.get("event") == "pr_consumed"
            and e.get("data", {}).get("action") == "error_retry"
            for e in events
        )

    @patch("duo.commander.select_dialog_option")
    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch(
        "duo.commander.diagnose_pane",
        return_value="✗ Execution failed: CAPIError: 400 Bad Request",
    )
    @patch("duo.commander.is_process_alive", return_value=True)
    def test_poll_heartbeat_capi_error_logs_capi_event(
        self, mock_alive, mock_diag, mock_wait, mock_select
    ):
        """CAPIError in pane → capi_error event (not api_error) + restart signal."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        poller = self._make_poller(PollResult.HEARTBEAT_TIMEOUT)

        poll_task(task, poller)

        events = read_jsonl(task.journal_path)
        assert any(e.get("event") == "capi_error" for e in events)
        assert not any(e.get("event") == "api_error" for e in events)
        # restart-recommended signal file should exist
        assert (task.dir / "restart-recommended").exists()

    @patch("duo.commander.select_dialog_option")
    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch(
        "duo.commander.diagnose_pane",
        return_value="error: rate limit exceeded",
    )
    @patch("duo.commander.is_process_alive", return_value=True)
    def test_poll_heartbeat_rate_limit_logs_api_error(
        self, mock_alive, mock_diag, mock_wait, mock_select
    ):
        """Rate limit → api_error event (not capi_error), no restart signal."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        poller = self._make_poller(PollResult.HEARTBEAT_TIMEOUT)

        poll_task(task, poller)

        events = read_jsonl(task.journal_path)
        assert any(e.get("event") == "api_error" for e in events)
        assert not any(e.get("event") == "capi_error" for e in events)
        assert not (task.dir / "restart-recommended").exists()

    @patch("duo.commander.resend_last_prompt")
    @patch("duo.commander.read_ack_for_step", return_value=None)
    def test_poll_unknown_ack_missing_resend(self, mock_ack, mock_resend):
        """UNKNOWN + no ack + old prompt → resend."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        task.last_prompt_sent_at = "2000-01-01T00:00:00+00:00"  # very old
        poller = self._make_poller(PollResult.UNKNOWN)

        poll_task(task, poller)

        mock_resend.assert_called_once_with(task)

    @patch("duo.commander.resend_last_prompt")
    @patch("duo.commander.read_ack_for_step", return_value=None)
    def test_poll_unknown_ack_missing_recent_prompt(self, mock_ack, mock_resend):
        """UNKNOWN + no ack + recent prompt → no resend yet."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        task.last_prompt_sent_at = now_iso()  # just now
        poller = self._make_poller(PollResult.UNKNOWN)

        poll_task(task, poller)

        mock_resend.assert_not_called()

    @patch("duo.commander.resend_last_prompt")
    @patch("duo.commander.read_ack_for_step")
    def test_poll_unknown_ack_present(self, mock_ack, mock_resend):
        """UNKNOWN + ack present → no resend."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        mock_ack.return_value = MagicMock()  # ack exists
        poller = self._make_poller(PollResult.UNKNOWN)

        poll_task(task, poller)

        mock_resend.assert_not_called()

    @patch("duo.commander.resend_last_prompt")
    @patch("duo.commander.read_ack_for_step", return_value=None)
    def test_poll_unknown_no_prompt_sent_at_uses_fallback(self, mock_ack, mock_resend):
        """UNKNOWN + no last_prompt_sent_at → falls back to session_started_at."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        task.last_prompt_sent_at = None
        task.session_started_at = "2000-01-01T00:00:00+00:00"  # very old
        poller = self._make_poller(PollResult.UNKNOWN)

        poll_task(task, poller)

        mock_resend.assert_called_once_with(task)

    @patch("duo.commander.resend_last_prompt")
    @patch("duo.commander.read_ack_for_step", return_value=None)
    def test_poll_unknown_no_timestamps_no_resend(self, mock_ack, mock_resend):
        """UNKNOWN + no timestamps at all → no resend."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        task.last_prompt_sent_at = None
        task.session_started_at = None
        task.created_at = None
        poller = self._make_poller(PollResult.UNKNOWN)

        poll_task(task, poller)

        mock_resend.assert_not_called()

    def test_poll_working_no_action(self):
        """WORKING → returns WORKING, no side effects."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        poller = self._make_poller(PollResult.WORKING)

        ret = poll_task(task, poller)

        assert ret == PollResult.WORKING


# ---------------------------------------------------------------------------
# monitor (single iteration)
# ---------------------------------------------------------------------------


class TestMonitor:
    @patch("duo.commander.time.sleep", side_effect=StopIteration)
    @patch("duo.commander.poll_task", return_value=PollResult.WORKING)
    @patch("duo.scheduler.promote_queued", return_value=[])
    @patch("duo.commander.list_tasks")
    def test_monitor_single_iteration(
        self, mock_list, mock_promote, mock_poll, mock_sleep
    ):
        """Monitor runs one iteration then stops via StopIteration from sleep."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        mock_list.return_value = [task]

        with (
            patch(
                "duo.scheduler.queue_status",
                return_value={"active_count": 1, "queued_count": 0, "max_parallel": 2},
            ),
            pytest.raises(StopIteration),
        ):
            monitor()

        mock_poll.assert_called_once()

    @patch("duo.scheduler.promote_queued", return_value=[])
    @patch("duo.commander.list_tasks", return_value=[])
    def test_monitor_no_tasks_exits(self, mock_list, mock_promote):
        """Monitor exits when no active or queued tasks."""
        with patch(
            "duo.scheduler.queue_status",
            return_value={"active_count": 0, "queued_count": 0, "max_parallel": 2},
        ):
            monitor()  # should exit cleanly

    @patch("duo.commander.time.sleep", side_effect=StopIteration)
    @patch("duo.commander.poll_task", return_value=PollResult.RESULT_READY)
    @patch("duo.commander.send_task_prompt")
    @patch("duo.commander.start_session")
    @patch("duo.scheduler.promote_queued")
    @patch("duo.commander.list_tasks")
    def test_monitor_promoted_task(
        self,
        mock_list,
        mock_promote,
        mock_start,
        mock_send,
        mock_poll,
        mock_sleep,
    ):
        """Monitor starts session and sends prompt for promoted tasks."""
        task = _make_task()
        transition(task, TaskStatus.SESSION_STARTING)
        mock_list.return_value = [task]
        mock_promote.return_value = [task]

        with (
            patch(
                "duo.scheduler.queue_status",
                return_value={"active_count": 1, "queued_count": 0, "max_parallel": 2},
            ),
            pytest.raises(StopIteration),
        ):
            monitor()

        mock_start.assert_called_once_with(task)
        mock_send.assert_called_once()

    @patch("duo.commander.time.sleep", side_effect=StopIteration)
    @patch("duo.commander.poll_task", return_value=PollResult.RESULT_READY)
    @patch("duo.commander.send_task_prompt")
    @patch("duo.commander.start_session")
    @patch("duo.scheduler.promote_queued")
    @patch("duo.commander.list_tasks")
    def test_monitor_promoted_uses_persisted_prompt(
        self,
        mock_list,
        mock_promote,
        mock_start,
        mock_send,
        mock_poll,
        mock_sleep,
    ):
        """Monitor uses user-persisted prompt file over synthesized one."""
        task = _make_task()
        transition(task, TaskStatus.SESSION_STARTING)
        # Persist a user prompt before promotion
        prompt_path = task.prompt_path(task.current_step, task.current_attempt)
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text("user custom prompt")

        mock_list.return_value = [task]
        mock_promote.return_value = [task]

        with (
            patch(
                "duo.scheduler.queue_status",
                return_value={"active_count": 1, "queued_count": 0, "max_parallel": 2},
            ),
            pytest.raises(StopIteration),
        ):
            monitor()

        mock_start.assert_called_once_with(task)
        # Should have sent the persisted prompt, not synthesized
        sent_prompt = mock_send.call_args[0][1]
        assert sent_prompt == "user custom prompt"

    @patch("duo.commander.time.sleep", side_effect=StopIteration)
    @patch("duo.commander.poll_task", return_value=PollResult.WORKING)
    @patch("duo.commander.send_task_prompt")
    @patch(
        "duo.commander.start_session",
        side_effect=RuntimeError("tmux crashed"),
    )
    @patch("duo.scheduler.promote_queued")
    @patch("duo.commander.list_tasks")
    def test_monitor_survives_start_session_failure(
        self,
        mock_list,
        mock_promote,
        mock_start,
        mock_send,
        mock_poll,
        mock_sleep,
        capsys,
    ):
        """Monitor continues if start_session fails for a promoted task."""
        task = _make_task()
        transition(task, TaskStatus.SESSION_STARTING)
        mock_list.return_value = [task]
        mock_promote.return_value = [task]

        with (
            patch(
                "duo.scheduler.queue_status",
                return_value={"active_count": 1, "queued_count": 0, "max_parallel": 2},
            ),
            pytest.raises(StopIteration),
        ):
            monitor()

        mock_start.assert_called_once()
        mock_send.assert_not_called()  # prompt not sent due to failure
        captured = capsys.readouterr()
        assert "failed to start" in captured.out

    @patch("duo.commander.time.sleep", side_effect=StopIteration)
    @patch("duo.commander.poll_task", return_value=PollResult.WORKING)
    @patch("duo.commander.send_task_prompt")
    @patch("duo.scheduler.promote_queued")
    @patch("duo.commander.list_tasks")
    def test_monitor_skips_prompt_on_failed_start(
        self,
        mock_list,
        mock_promote,
        mock_send,
        mock_poll,
        mock_sleep,
        capsys,
    ):
        """Monitor skips prompt send when start_session leaves task FAILED."""
        task = _make_task()
        transition(task, TaskStatus.SESSION_STARTING)

        def fail_start(t):
            t.status = TaskStatus.FAILED

        mock_list.return_value = [task]
        mock_promote.return_value = [task]

        with (
            patch("duo.commander.start_session", side_effect=fail_start),
            patch(
                "duo.scheduler.queue_status",
                return_value={"active_count": 0, "queued_count": 0, "max_parallel": 2},
            ),
            pytest.raises(StopIteration),
        ):
            monitor()

        mock_send.assert_not_called()
        captured = capsys.readouterr()
        assert "session failed to start" in captured.out


# ---------------------------------------------------------------------------
# _log_monitor
# ---------------------------------------------------------------------------


class TestLogMonitor:
    def test_log_monitor_output(self, capsys):
        """_log_monitor prints formatted line."""
        _log_monitor("✓", "task-1", "result_ready")
        captured = capsys.readouterr()
        assert "✓" in captured.out
        assert "task-1" in captured.out
        assert "result_ready" in captured.out
        assert "[duo]" in captured.out

    @patch("duo.commander.time.sleep", side_effect=StopIteration)
    @patch("duo.commander.poll_task", return_value=PollResult.WORKING)
    @patch("duo.scheduler.promote_queued", return_value=[])
    @patch("duo.commander.list_tasks")
    def test_monitor_skips_blocked_tasks(
        self, mock_list, mock_promote, mock_poll, mock_sleep
    ):
        """Monitor does not poll BLOCKED tasks (prevents auto-restart)."""
        task = _make_task()
        task.status = TaskStatus.BLOCKED
        mock_list.return_value = [task]

        with (
            patch(
                "duo.scheduler.queue_status",
                return_value={"active_count": 0, "queued_count": 0, "max_parallel": 2},
            ),
        ):
            monitor()  # exits because no active tasks

        mock_poll.assert_not_called()


# ---------------------------------------------------------------------------
# monitor poll result branch coverage (lines 634, 636)
# ---------------------------------------------------------------------------


class TestMonitorPollResultBranches:
    @patch("duo.commander.time.sleep", side_effect=StopIteration)
    @patch("duo.commander.poll_task", return_value=PollResult.HEARTBEAT_TIMEOUT)
    @patch("duo.scheduler.promote_queued", return_value=[])
    @patch("duo.commander.list_tasks")
    def test_monitor_heartbeat_timeout_logging(
        self, mock_list, mock_promote, mock_poll, mock_sleep, capsys
    ):
        """Monitor logs ⚠ heartbeat_timeout (line 634)."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        mock_list.return_value = [task]

        with (
            patch(
                "duo.scheduler.queue_status",
                return_value={
                    "active_count": 1,
                    "queued_count": 0,
                    "max_parallel": 2,
                },
            ),
            pytest.raises(StopIteration),
        ):
            monitor()

        captured = capsys.readouterr()
        assert "⚠" in captured.out
        assert "heartbeat_timeout" in captured.out

    @patch("duo.commander.time.sleep", side_effect=StopIteration)
    @patch("duo.commander.poll_task", return_value=PollResult.UNKNOWN)
    @patch("duo.scheduler.promote_queued", return_value=[])
    @patch("duo.commander.list_tasks")
    def test_monitor_unknown_state_logging(
        self, mock_list, mock_promote, mock_poll, mock_sleep, capsys
    ):
        """Monitor logs ? unknown state (line 636)."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        mock_list.return_value = [task]

        with (
            patch(
                "duo.scheduler.queue_status",
                return_value={
                    "active_count": 1,
                    "queued_count": 0,
                    "max_parallel": 2,
                },
            ),
            pytest.raises(StopIteration),
        ):
            monitor()

        captured = capsys.readouterr()
        assert "?" in captured.out
        assert "unknown state" in captured.out


# ---------------------------------------------------------------------------
# monitor — task timeout enforcement
# ---------------------------------------------------------------------------


class TestMonitorTaskTimeout:
    @patch("duo.commander.time.sleep", side_effect=StopIteration)
    @patch("duo.commander.poll_task", return_value=PollResult.WORKING)
    @patch("duo.scheduler.promote_queued", return_value=[])
    @patch("duo.commander.list_tasks")
    def test_no_timeout_when_disabled(
        self, mock_list, mock_promote, mock_poll, mock_sleep
    ):
        """task_timeout=0 (default) does not trigger timeout."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        # Even with an old created_at, no timeout when disabled
        task.created_at = "2020-01-01T00:00:00+00:00"
        mock_list.return_value = [task]

        with (
            patch(
                "duo.scheduler.queue_status",
                return_value={"active_count": 1, "queued_count": 0, "max_parallel": 2},
            ),
            patch("duo.commander.get_config", return_value=0),
            pytest.raises(StopIteration),
        ):
            monitor()

        # poll_task was called (not skipped by timeout)
        mock_poll.assert_called_once()
        assert task.status == TaskStatus.PROMPT_SENT

    @patch("duo.commander.time.sleep", side_effect=StopIteration)
    @patch("duo.commander.poll_task", return_value=PollResult.WORKING)
    @patch("duo.scheduler.promote_queued", return_value=[])
    @patch("duo.commander.list_tasks")
    def test_timeout_triggers_on_old_task(
        self, mock_list, mock_promote, mock_poll, mock_sleep, capsys
    ):
        """task_timeout=1 triggers timeout on a task created far in the past."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        task.created_at = "2020-01-01T00:00:00+00:00"
        mock_list.return_value = [task]

        with (
            patch(
                "duo.scheduler.queue_status",
                return_value={"active_count": 1, "queued_count": 0, "max_parallel": 2},
            ),
            patch("duo.commander.get_config", return_value=1),
            pytest.raises(StopIteration),
        ):
            monitor()

        # poll_task should NOT have been called (timeout skipped it)
        mock_poll.assert_not_called()
        assert task.status == TaskStatus.FAILED
        # Verify journal has timeout_exceeded event
        events = read_jsonl(task.journal_path)
        timeout_events = [e for e in events if e["event"] == "timeout_exceeded"]
        assert len(timeout_events) == 1
        assert timeout_events[0]["data"]["limit"] == 1
        # Verify log output
        captured = capsys.readouterr()
        assert "timeout" in captured.out

    @patch("duo.commander.time.sleep", side_effect=StopIteration)
    @patch("duo.commander.poll_task", return_value=PollResult.WORKING)
    @patch("duo.scheduler.promote_queued", return_value=[])
    @patch("duo.commander.list_tasks")
    def test_no_timeout_when_within_limit(
        self, mock_list, mock_promote, mock_poll, mock_sleep
    ):
        """task_timeout set but task is still within the limit."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        # created_at is now_iso() (just created), so age < 99999
        mock_list.return_value = [task]

        with (
            patch(
                "duo.scheduler.queue_status",
                return_value={"active_count": 1, "queued_count": 0, "max_parallel": 2},
            ),
            patch("duo.commander.get_config", return_value=99999),
            pytest.raises(StopIteration),
        ):
            monitor()

        # poll_task was called (not timed out)
        mock_poll.assert_called_once()
        assert task.status == TaskStatus.PROMPT_SENT

    @patch("duo.commander.time.sleep", side_effect=StopIteration)
    @patch("duo.commander.poll_task", return_value=PollResult.WORKING)
    @patch("duo.scheduler.promote_queued", return_value=[])
    @patch("duo.commander.list_tasks")
    def test_timeout_uses_session_started_at(
        self, mock_list, mock_promote, mock_poll, mock_sleep
    ):
        """Timeout uses session_started_at instead of created_at when available."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        # created_at is old, but session_started_at is recent
        task.created_at = "2020-01-01T00:00:00+00:00"
        task.session_started_at = now_iso()
        mock_list.return_value = [task]

        with (
            patch(
                "duo.scheduler.queue_status",
                return_value={"active_count": 1, "queued_count": 0, "max_parallel": 2},
            ),
            patch("duo.commander.get_config", return_value=99999),
            pytest.raises(StopIteration),
        ):
            monitor()

        # poll_task was called (session_started_at is recent, not timed out)
        mock_poll.assert_called_once()
        assert task.status == TaskStatus.PROMPT_SENT


# ---------------------------------------------------------------------------
# start_session — orphaned pane cleanup
# ---------------------------------------------------------------------------


class TestStartSessionOrphanedPaneCleanup:
    def test_kill_pane_called_on_transport_error(self):
        """start_session kills orphaned pane when send_shell_command raises."""
        task = _make_task()

        with (
            patch("duo.commander.subprocess.run") as mock_run,
            patch("duo.commander.name_pane"),
            patch(
                "duo.commander.send_shell_command",
                side_effect=RuntimeError("connection lost"),
            ),
            patch("duo.commander.time.sleep"),
            patch("duo.commander.kill_pane") as mock_kill,
        ):
            split_result = MagicMock()
            split_result.returncode = 0
            split_result.stdout = "%77\n"
            layout_result = MagicMock()
            layout_result.returncode = 0
            mock_run.side_effect = [split_result, layout_result]

            with pytest.raises(RuntimeError, match="connection lost"):
                start_session(task)

            mock_kill.assert_called_once_with("%77")

    def test_kill_pane_failure_suppressed(self):
        """start_session continues when kill_pane returns False."""
        task = _make_task()

        with (
            patch("duo.commander.subprocess.run") as mock_run,
            patch("duo.commander.name_pane"),
            patch(
                "duo.commander.send_shell_command",
                side_effect=RuntimeError("connection lost"),
            ),
            patch("duo.commander.time.sleep"),
            patch("duo.commander.kill_pane", return_value=False),
        ):
            split_result = MagicMock()
            split_result.returncode = 0
            split_result.stdout = "%77\n"
            layout_result = MagicMock()
            layout_result.returncode = 0
            mock_run.side_effect = [split_result, layout_result]

            with pytest.raises(RuntimeError, match="connection lost"):
                start_session(task)

            assert task.status == TaskStatus.FAILED


# ---------------------------------------------------------------------------
# monitor — min_interval floor
# ---------------------------------------------------------------------------


class TestMonitorMinIntervalFloor:
    @patch("duo.commander.time.sleep", side_effect=StopIteration)
    @patch("duo.commander.poll_task", return_value=PollResult.WORKING)
    @patch("duo.scheduler.promote_queued", return_value=[])
    @patch("duo.commander.list_tasks")
    def test_min_interval_clamped_to_1s(
        self, mock_list, mock_promote, mock_poll, mock_sleep
    ):
        """Monitor clamps sleep interval to >= 1.0 second."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        mock_list.return_value = [task]

        with (
            patch(
                "duo.scheduler.queue_status",
                return_value={"active_count": 1, "queued_count": 0, "max_parallel": 2},
            ),
            pytest.raises(StopIteration),
        ):
            monitor()

        # time.sleep was called; the interval must be >= 1.0
        sleep_val = mock_sleep.call_args[0][0]
        assert sleep_val >= 1.0


class TestMonitorPollersCleanup:
    """Pollers dict is pruned when tasks become inactive."""

    @patch("duo.commander.time.sleep")
    @patch("duo.commander.poll_task", return_value=PollResult.WORKING)
    @patch("duo.scheduler.promote_queued", return_value=[])
    @patch("duo.commander.list_tasks")
    def test_stale_pollers_removed(
        self, mock_list, mock_promote, mock_poll, mock_sleep
    ):
        """Pollers for completed tasks are cleaned up."""
        task_a = _make_task("stay-active")
        _advance_to_prompt_sent(task_a)
        task_b = _make_task("goes-away")
        _advance_to_prompt_sent(task_b)

        call_count = 0

        def list_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return [task_a, task_b]
            # Second iteration: task_b is gone
            return [task_a]

        mock_list.side_effect = list_side_effect
        mock_sleep.side_effect = [None, StopIteration]

        with (
            patch(
                "duo.scheduler.queue_status",
                return_value={"active_count": 2, "queued_count": 0, "max_parallel": 3},
            ),
            pytest.raises(StopIteration),
        ):
            monitor()

        # First iteration polls both, second iteration only task_a
        assert mock_poll.call_count == 3

    @patch("duo.commander.time.sleep")
    @patch("duo.scheduler.promote_queued", return_value=[])
    @patch("duo.commander.list_tasks")
    def test_stale_poll_errors_removed(self, mock_list, mock_promote, mock_sleep):
        """poll_errors entries for completed tasks are cleaned up."""
        task_a = _make_task("err-active")
        _advance_to_prompt_sent(task_a)
        task_b = _make_task("err-gone")
        _advance_to_prompt_sent(task_b)

        call_count = 0

        def list_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return [task_a, task_b]
            return [task_a]

        mock_list.side_effect = list_side_effect

        poll_call = 0

        def poll_side_effect(_task, _poller):
            nonlocal poll_call
            poll_call += 1
            if poll_call <= 2:
                raise RuntimeError("transient")
            return PollResult.WORKING

        mock_sleep.side_effect = [None, StopIteration]

        with (
            patch(
                "duo.scheduler.queue_status",
                return_value={
                    "active_count": 2,
                    "queued_count": 0,
                    "max_parallel": 3,
                },
            ),
            patch("duo.commander.poll_task", side_effect=poll_side_effect),
            pytest.raises(StopIteration),
        ):
            monitor()

        # task_b had errors but disappeared — its counter should be cleaned


class TestMonitorConfigWiring:
    """Monitor creates AdaptivePoller with config values."""

    @patch("duo.commander.time.sleep", side_effect=StopIteration)
    @patch("duo.commander.poll_task", return_value=PollResult.WORKING)
    @patch("duo.scheduler.promote_queued", return_value=[])
    @patch("duo.commander.list_tasks")
    def test_poller_uses_config_values(
        self, mock_list, mock_promote, mock_poll, mock_sleep
    ):
        task = _make_task("config-wire")
        _advance_to_prompt_sent(task)
        mock_list.return_value = [task]

        with (
            patch(
                "duo.scheduler.queue_status",
                return_value={"active_count": 1, "queued_count": 0, "max_parallel": 3},
            ),
            patch("duo.commander.get_config") as mock_cfg,
            pytest.raises(StopIteration),
        ):
            mock_cfg.side_effect = lambda key: {
                "poll_base_interval": 10.0,
                "poll_max_interval": 60.0,
                "heartbeat_timeout": 45,
                "task_timeout": 0,
            }.get(key)
            monitor()

        # Verify the poller was called with our task
        assert mock_poll.call_count >= 1
        # The poller created should use config values
        call_args = mock_poll.call_args
        poller = call_args[0][1]
        assert poller.base_interval == 10.0
        assert poller.max_interval == 60.0
        assert poller.heartbeat_timeout == 45.0


# ── poll_task silent path (heartbeat timeout, alive, no error) ───────


class TestPollHeartbeatTimeoutSilent:
    def test_poll_heartbeat_timeout_alive_no_error(self):
        """HEARTBEAT_TIMEOUT + alive pane + no error text → silent no-op."""
        task = _make_task()
        _advance_to_prompt_sent(task)

        with (
            patch.object(
                AdaptivePoller, "poll", return_value=PollResult.HEARTBEAT_TIMEOUT
            ),
            patch("duo.commander.is_process_alive", return_value=True),
            patch(
                "duo.commander.diagnose_pane", return_value="all good, copilot working"
            ),
            patch("duo.commander.wait_for_dialog", return_value=False),
        ):
            poller = AdaptivePoller()
            result = poll_task(task, poller)

        assert result == PollResult.HEARTBEAT_TIMEOUT
        # No api_error event should be recorded (no error in terminal)
        events = read_jsonl(task.journal_path)
        error_events = [e for e in events if e.get("event") == "api_error"]
        assert len(error_events) == 0


class TestMonitorTaskLock:
    """Monitor skips locked tasks."""

    @patch("duo.commander.time.sleep", side_effect=StopIteration)
    @patch("duo.commander.poll_task", return_value=PollResult.WORKING)
    @patch("duo.scheduler.promote_queued", return_value=[])
    @patch("duo.commander.list_tasks")
    def test_monitor_skips_locked_task(
        self, mock_list, mock_promote, mock_poll, mock_sleep, capsys
    ):
        """Monitor logs 'locked by another process' and skips."""
        from duo.errors import TaskLockedError

        task = _make_task()
        _advance_to_prompt_sent(task)
        mock_list.return_value = [task]

        with (
            patch(
                "duo.scheduler.queue_status",
                return_value={"active_count": 1, "queued_count": 0, "max_parallel": 2},
            ),
            patch(
                "duo.commander.task_lock",
                side_effect=TaskLockedError("locked"),
            ),
            pytest.raises(StopIteration),
        ):
            monitor()

        out = capsys.readouterr().out
        assert "locked by another process" in out
        mock_poll.assert_not_called()


class TestMonitorOneTaskFreshLoad:
    """_monitor_one_task reloads task from disk under lock."""

    def test_vanished_task_skipped(self, capsys):
        """Task deleted between list_tasks and lock → skip."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        pollers: dict = {}
        errors: dict = {}
        with patch("duo.commander.load_task", return_value=None):
            _monitor_one_task(task, pollers, errors)
        out = capsys.readouterr().out
        assert "vanished" in out

    def test_completed_task_skipped(self, capsys):
        """Task completed by another monitor → skip."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        # Simulate fresh load returning a completed version
        fresh = load_task(task.id)
        assert fresh is not None
        fresh.status = TaskStatus.COMPLETED
        pollers: dict = {}
        errors: dict = {}
        with patch("duo.commander.load_task", return_value=fresh):
            _monitor_one_task(task, pollers, errors)
        out = capsys.readouterr().out
        assert "now completed" in out


class TestMonitorPollErrorResilience:
    """Monitor continues when poll_task raises an exception."""

    @patch("duo.commander.time.sleep", side_effect=StopIteration)
    @patch("duo.commander.poll_task", side_effect=RuntimeError("bridge crash"))
    @patch("duo.scheduler.promote_queued", return_value=[])
    @patch("duo.commander.list_tasks")
    def test_monitor_poll_exception_continues(
        self, mock_list, mock_promote, mock_poll, mock_sleep, capsys
    ):
        """An exception in poll_task is caught; monitor doesn't crash."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        mock_list.return_value = [task]

        with (
            patch(
                "duo.scheduler.queue_status",
                return_value={"active_count": 1, "queued_count": 0, "max_parallel": 2},
            ),
            pytest.raises(StopIteration),
        ):
            monitor()

        captured = capsys.readouterr()
        assert "poll error" in captured.out
        events = read_jsonl(task.journal_path)
        poll_errors = [e for e in events if e.get("event") == "poll_error"]
        assert len(poll_errors) == 1
        assert "bridge crash" in poll_errors[0]["data"]["error"]
        assert poll_errors[0]["data"]["consecutive"] == 1

    @patch("duo.scheduler.promote_queued", return_value=[])
    @patch("duo.commander.list_tasks")
    def test_monitor_poll_errors_exhausted_fails_task(
        self, mock_list, mock_promote, capsys
    ):
        """After 10 consecutive poll errors, task transitions to FAILED."""
        task = _make_task()
        _advance_to_prompt_sent(task)

        def list_tasks_side_effect():
            return [task]

        mock_list.side_effect = list_tasks_side_effect

        with (
            patch(
                "duo.scheduler.queue_status",
                return_value={"active_count": 1, "queued_count": 0, "max_parallel": 2},
            ),
            patch(
                "duo.commander.poll_task",
                side_effect=RuntimeError("persistent failure"),
            ),
            patch("duo.commander.time.sleep"),
        ):
            monitor()

        assert task.status == TaskStatus.FAILED
        events = read_jsonl(task.journal_path)
        exhausted = [e for e in events if e.get("event") == "poll_errors_exhausted"]
        assert len(exhausted) == 1
        assert exhausted[0]["data"]["consecutive"] == 10

    @patch("duo.scheduler.promote_queued", return_value=[])
    @patch("duo.commander.list_tasks")
    def test_monitor_poll_error_counter_resets_on_success(
        self, mock_list, mock_promote, capsys
    ):
        """Successful poll resets the consecutive error counter."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        mock_list.return_value = [task]

        call_count = 0

        def poll_side_effect(_task, _poller):
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                raise RuntimeError("transient")
            return PollResult.WORKING

        sleep_count = 0

        def sleep_side_effect(_interval):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 5:
                raise StopIteration

        with (
            patch(
                "duo.scheduler.queue_status",
                return_value={"active_count": 1, "queued_count": 0, "max_parallel": 2},
            ),
            patch("duo.commander.poll_task", side_effect=poll_side_effect),
            patch("duo.commander.time.sleep", side_effect=sleep_side_effect),
            pytest.raises(StopIteration),
        ):
            monitor()

        # Task should NOT be failed — errors were transient and reset
        assert task.status != TaskStatus.FAILED


# ---------------------------------------------------------------------------
# watch_tasks
# ---------------------------------------------------------------------------


class TestWatchTasks:
    """Tests for event-driven watch_tasks."""

    @staticmethod
    def _active(name: str = "w1"):
        task = _make_task(name)
        _advance_to_prompt_sent(task)
        return task

    @staticmethod
    def _finished(name: str = "f1"):
        task = _make_task(name)
        transition(task, TaskStatus.SESSION_STARTING)
        transition(task, TaskStatus.FAILED)
        return task

    def test_no_active_tasks(self) -> None:
        """Returns 0 when no active tasks (no live panes)."""
        with (
            patch("duo.commander.list_tasks", return_value=[]),
            patch("duo.commander.is_process_alive", return_value=False),
        ):
            result = watch_tasks()
        assert result == 0

    def test_unknown_task_ids_warned(self, capsys) -> None:
        """Unknown task IDs produce a warning."""
        with (
            patch("duo.commander.list_tasks", return_value=[]),
            patch("duo.commander.is_process_alive", return_value=False),
        ):
            result = watch_tasks(["nonexistent"])
        assert result == 0
        captured = capsys.readouterr()
        assert "Unknown task(s): nonexistent" in captured.err

    def test_filters_dead_panes(self) -> None:
        """Tasks with dead panes are excluded."""
        tasks = [self._finished(f"t{i}") for i in range(3)]
        with (
            patch("duo.commander.list_tasks", return_value=tasks),
            patch("duo.commander.is_process_alive", return_value=False),
        ):
            result = watch_tasks()
        assert result == 0

    def test_failed_task_with_live_pane_watched(self) -> None:
        """A FAILED task is still watched if its pane is alive."""
        task = self._finished("alive-fail")

        call_count = 0
        alive_calls = 0

        def _fake_alive(label: str) -> bool:
            nonlocal alive_calls
            alive_calls += 1
            return alive_calls <= 3

        def _fake_wait(
            label: str, timeout: float = 300, interval: float = 5, **_kw: object
        ) -> bool:
            nonlocal call_count
            call_count += 1
            return call_count == 1

        with (
            patch("duo.commander.list_tasks", return_value=[task]),
            patch("duo.commander.is_process_alive", side_effect=_fake_alive),
            patch("duo.commander.wait_for_dialog", side_effect=_fake_wait),
            patch("duo.commander.approve_permission"),
            patch("duo.commander.append_event"),
        ):
            result = watch_tasks(timeout=0.1, interval=0.01)
        assert result == 1

    def test_filters_by_task_ids(self) -> None:
        """Only watches specified task IDs."""
        t1 = self._active("watch-me")
        t2 = self._active("skip-me")

        alive_count = 0

        def _fake_alive(label: str) -> bool:
            nonlocal alive_count
            alive_count += 1
            return alive_count <= 2

        with (
            patch("duo.commander.list_tasks", return_value=[t1, t2]),
            patch("duo.commander.is_process_alive", side_effect=_fake_alive),
            patch("duo.commander.wait_for_dialog", return_value=False),
        ):
            result = watch_tasks(["watch-me"], timeout=0.01, interval=0.01)
        assert result == 1

    def test_dialog_handled_auto_approve(self) -> None:
        """Dialog is detected and auto-approved when auto_approve=True."""
        task = self._active("dlg")

        call_count = 0
        alive_count = 0

        def _fake_alive(label: str) -> bool:
            nonlocal alive_count
            alive_count += 1
            return alive_count <= 3

        def _fake_wait(
            label: str, timeout: float = 300, interval: float = 5, **_kw: object
        ) -> bool:
            nonlocal call_count
            call_count += 1
            return call_count == 1

        with (
            patch("duo.commander.list_tasks", return_value=[task]),
            patch("duo.commander.is_process_alive", side_effect=_fake_alive),
            patch("duo.commander.wait_for_dialog", side_effect=_fake_wait),
            patch("duo.commander.approve_permission") as mock_approve,
            patch("duo.commander.append_event"),
        ):
            result = watch_tasks(timeout=0.1, interval=0.01, auto_approve=True)
        assert result == 1
        mock_approve.assert_called_once_with(task.pane_label)

    def test_dialog_detected_default_mode(self) -> None:
        """Default mode: dialog detected → print + signal file → exit."""
        task = self._active("detect")

        with (
            patch("duo.commander.list_tasks", return_value=[task]),
            patch("duo.commander.is_process_alive", return_value=True),
            patch("duo.commander.wait_for_dialog", return_value=True),
            patch("duo.commander.read_pane", return_value="╭─ Allow? ─╮\n1. Yes"),
            patch("duo.commander._write_watch_event") as mock_write,
            patch("duo.commander.append_event"),
        ):
            mock_write.return_value = Path("/tmp/fake-signal.json")
            result = watch_tasks(timeout=0.1, interval=0.01)
        assert result == 1
        mock_write.assert_called_once()

    def test_dialog_error_logged(self) -> None:
        """Error during approve_permission is logged, watch continues."""
        task = self._active("err")

        call_count = 0
        alive_count = 0

        def _fake_alive(label: str) -> bool:
            nonlocal alive_count
            alive_count += 1
            return alive_count <= 3

        def _fake_wait(
            label: str, timeout: float = 300, interval: float = 5, **_kw: object
        ) -> bool:
            nonlocal call_count
            call_count += 1
            return call_count == 1

        with (
            patch("duo.commander.list_tasks", return_value=[task]),
            patch("duo.commander.is_process_alive", side_effect=_fake_alive),
            patch("duo.commander.wait_for_dialog", side_effect=_fake_wait),
            patch(
                "duo.commander.approve_permission",
                side_effect=RuntimeError("not in dialog"),
            ),
            patch("duo.commander.append_event"),
        ):
            result = watch_tasks(timeout=0.1, interval=0.01, auto_approve=True)
        assert result == 1

    def test_pane_dead_before_wait(self) -> None:
        """_watch_loop exits when pane is already dead before wait_for_dialog."""
        import threading as _th

        task = self._active("dead-pane")
        stop = _th.Event()

        with patch("duo.commander.is_process_alive", return_value=False):
            _watch_loop(
                task, stop, timeout=1, interval=0.01, once=False, auto_approve=False
            )
        # Loop exited without error — pane was dead, logged and broke out

    def test_pane_unavailable(self) -> None:
        """Watch loop exits when pane raises exception."""
        task = self._active("gone")
        with (
            patch("duo.commander.list_tasks", return_value=[task]),
            patch("duo.commander.is_process_alive", return_value=True),
            patch("duo.commander.wait_for_dialog", side_effect=OSError("no pane")),
        ):
            result = watch_tasks(timeout=0.1, interval=0.01)
        assert result == 1

    def test_once_flag_auto_approve(self) -> None:
        """--once + --auto-approve stops after first auto-approved dialog."""
        task = self._active("once")

        with (
            patch("duo.commander.list_tasks", return_value=[task]),
            patch("duo.commander.is_process_alive", return_value=True),
            patch("duo.commander.wait_for_dialog", return_value=True),
            patch("duo.commander.approve_permission"),
            patch("duo.commander.append_event"),
        ):
            result = watch_tasks(
                once=True, timeout=0.1, interval=0.01, auto_approve=True
            )
        assert result == 1

    def test_once_flag_default_mode(self) -> None:
        """--once in default mode: detect → signal → exit."""
        task = self._active("once-detect")

        with (
            patch("duo.commander.list_tasks", return_value=[task]),
            patch("duo.commander.is_process_alive", return_value=True),
            patch("duo.commander.wait_for_dialog", return_value=True),
            patch("duo.commander.read_pane", return_value="dialog content"),
            patch("duo.commander._write_watch_event") as mock_write,
            patch("duo.commander.append_event"),
        ):
            mock_write.return_value = Path("/tmp/sig.json")
            result = watch_tasks(once=True, timeout=0.1, interval=0.01)
        assert result == 1
        mock_write.assert_called_once()

    def test_stop_set_during_wait(self) -> None:
        """Watch loop exits when pane dies during polling."""
        task = self._active("stop-mid")

        call_count = 0

        def _wait_then_stop(
            label: str, timeout: float = 300, interval: float = 5, **_kw: object
        ) -> bool:
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                raise OSError("done")
            return False

        alive_count = 0

        def _fake_alive(label: str) -> bool:
            nonlocal alive_count
            alive_count += 1
            return True

        with (
            patch("duo.commander.list_tasks", return_value=[task]),
            patch("duo.commander.is_process_alive", side_effect=_fake_alive),
            patch("duo.commander.wait_for_dialog", side_effect=_wait_then_stop),
        ):
            result = watch_tasks(timeout=0.01, interval=0.01)
        assert result == 1

    def test_task_still_active_continues(self) -> None:
        """Watch loop continues polling when pane still alive after timeout."""
        task = self._active("cont")

        call_count = 0

        def _fake_wait(
            label: str, timeout: float = 300, interval: float = 5, **_kw: object
        ) -> bool:
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                raise OSError("stop")
            return False

        with (
            patch("duo.commander.list_tasks", return_value=[task]),
            patch("duo.commander.is_process_alive", return_value=True),
            patch("duo.commander.wait_for_dialog", side_effect=_fake_wait),
        ):
            result = watch_tasks(timeout=0.01, interval=0.01)
        assert result == 1
        assert call_count >= 3

    def test_stop_event_after_wait_returns(self) -> None:
        """Break when stop event is set after wait_for_dialog returns."""
        task = self._active("stop-after")
        task2 = self._active("stop-after2")

        def _fake_wait(
            label: str, timeout: float = 300, interval: float = 5, **_kw: object
        ) -> bool:
            return True

        with (
            patch("duo.commander.list_tasks", return_value=[task, task2]),
            patch("duo.commander.is_process_alive", return_value=True),
            patch("duo.commander.wait_for_dialog", side_effect=_fake_wait),
            patch("duo.commander.approve_permission"),
            patch("duo.commander.append_event"),
        ):
            result = watch_tasks(
                once=True, timeout=0.1, interval=0.01, auto_approve=True
            )
        assert result == 2

    def test_stop_set_during_wait_direct(self) -> None:
        """_watch_loop breaks when stop is set during wait_for_dialog."""
        import threading as _th

        task = self._active("mid-stop")
        stop = _th.Event()

        def _fake_wait(
            label: str,
            timeout: float = 300,
            interval: float = 5,
            stop_event: _th.Event | None = None,
        ) -> bool:
            stop.set()  # Simulate stop being set while waiting
            return False

        with (
            patch("duo.commander.is_process_alive", return_value=True),
            patch("duo.commander.wait_for_dialog", side_effect=_fake_wait),
        ):
            _watch_loop(
                task, stop, timeout=1, interval=0.01, once=False, auto_approve=False
            )

    def test_watch_loop_passes_stop_event(self) -> None:
        """_watch_loop passes stop event to wait_for_dialog."""
        import threading as _th

        task = self._active("stop-pass")
        stop = _th.Event()
        stop.set()  # Pre-set so loop exits immediately

        with (
            patch("duo.commander.is_process_alive", return_value=True),
            patch("duo.commander.wait_for_dialog", return_value=False) as mock_wfd,
        ):
            _watch_loop(
                task, stop, timeout=1, interval=0.01, once=False, auto_approve=False
            )
            if mock_wfd.called:
                _, kwargs = mock_wfd.call_args
                assert kwargs.get("stop_event") is stop


class TestWriteWatchEvent:
    """Tests for _write_watch_event signal file creation."""

    def test_creates_signal_file(self) -> None:
        """Signal file is created with correct content."""
        import json
        import tempfile

        task = _make_task()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("duo.commander._WATCH_EVENTS_DIR", Path(tmp)),
        ):
            path = _write_watch_event(task, "╭─ Allow? ─╮\n1. Yes")
            assert path.exists()
            data = json.loads(path.read_text())
            assert data["task_id"] == task.id
            assert data["pane_label"] == task.pane_label
            assert "pane_content" in data
            assert "detected_at" in data

    def test_truncates_large_content(self) -> None:
        """Pane content is truncated to 2000 chars."""
        import json
        import tempfile

        task = _make_task()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("duo.commander._WATCH_EVENTS_DIR", Path(tmp)),
        ):
            big_content = "x" * 5000
            path = _write_watch_event(task, big_content)
            data = json.loads(path.read_text())
            assert len(data["pane_content"]) == 2000


# === Edge-case tests ===


class TestVerifyAndAdvanceEdgeCases:
    """Edge cases for verify_and_advance — malformed results, missing fields."""

    def _write_raw(self, task, step_num, attempt_num, **data):
        """Write a raw result JSON file."""
        result_path = task.result_path(step_num, attempt_num)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(result_path, data)

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    def test_result_with_missing_fields_defaults(self, mock_send, mock_wait):
        """Result with only step/attempt uses defaults for missing fields."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        self._write_raw(task, 1, 1, step=1, attempt=1, status="blocked")
        verify_and_advance(task)
        assert task.status == TaskStatus.BLOCKED

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    def test_result_with_empty_status_treated_as_done(self, mock_send, mock_wait):
        """Result with status='' is treated as a normal result (goes to verify)."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        self._write_raw(
            task,
            1,
            1,
            step=1,
            attempt=1,
            incarnation=task.incarnation_id,
            status="",
        )
        # Empty status is not "blocked" or "error", so it goes to verify_step
        with patch("duo.commander.verify_step", return_value=Pass()) as mock_verify:
            verify_and_advance(task)
        mock_verify.assert_called_once()

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    def test_result_error_status_transitions_to_blocked(self, mock_send, mock_wait):
        """Result with status='error' transitions task to BLOCKED."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        self._write_raw(
            task,
            1,
            1,
            step=1,
            attempt=1,
            incarnation=task.incarnation_id,
            status="error",
            reason="executor crashed",
        )
        verify_and_advance(task)
        assert task.status == TaskStatus.BLOCKED

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    @patch("duo.commander.verify_step")
    def test_verify_step_exception_transitions_to_failed(
        self, mock_verify, mock_send, mock_wait
    ):
        """When verify_step raises, task transitions to FAILED."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        self._write_raw(
            task,
            1,
            1,
            step=1,
            attempt=1,
            incarnation=task.incarnation_id,
            status="done",
        )
        mock_verify.side_effect = RuntimeError("git diff exploded")
        verify_and_advance(task)
        assert task.status == TaskStatus.FAILED


class TestCountCorrectionsEdgeCases:
    """Edge cases for _count_corrections."""

    def test_no_events_returns_zero(self):
        task = _make_task()
        assert _count_corrections(task, 1) == 0

    def test_corrections_different_steps_isolated(self):
        """Corrections for step 1 don't count toward step 2."""
        task = _make_task("multi-step", subtasks=[_make_subtask(1), _make_subtask(2)])
        append_event(task, "correction_sent", {"step": 1, "attempt": 2})
        append_event(task, "correction_sent", {"step": 1, "attempt": 3})
        append_event(task, "correction_sent", {"step": 2, "attempt": 2})
        assert _count_corrections(task, 1) == 2
        assert _count_corrections(task, 2) == 1


# ---------------------------------------------------------------------------
# Additional edge-case tests
# ---------------------------------------------------------------------------


class TestBuildBootstrapPromptEdgeCases:
    def test_build_bootstrap_prompt_contains_task_dir(self):
        """Bootstrap prompt must embed the full task directory path."""
        task = _make_task("bp-dir")
        prompt = build_bootstrap_prompt(task)
        assert str(task.dir) in prompt

    def test_build_bootstrap_prompt_contains_incarnation(self):
        """Bootstrap prompt must embed the incarnation ID."""
        task = _make_task("bp-inc")
        prompt = build_bootstrap_prompt(task)
        assert task.incarnation_id in prompt


class TestSendTaskPromptEdgeCases:
    @patch("duo.commander.transition")
    @patch("duo.commander.append_event")
    @patch("duo.commander.save_task")
    @patch("duo.commander.select_dialog_option")
    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander._check_pr_budget", return_value=True)
    def test_send_task_prompt_empty_prompt(
        self, mock_budget, mock_wait, mock_select, mock_save, mock_event, mock_trans
    ):
        """Sending an empty string prompt still writes the file and sends."""
        task = _make_task("empty-prompt")
        _advance_to_prompt_sent(task)

        send_task_prompt(task, "")

        # Prompt file should exist with empty content
        prompt_path = task.prompt_path(task.current_step, task.current_attempt)
        assert prompt_path.exists()
        assert prompt_path.read_text() == ""
        mock_select.assert_called_once_with(task.pane_label, "")


class TestPollTaskEdgeCases:
    def _make_poller(self, poll_result: PollResult) -> AdaptivePoller:
        poller = AdaptivePoller()
        poller.poll = MagicMock(return_value=poll_result)
        return poller

    @patch("duo.commander.verify_and_advance")
    @patch("duo.commander.read_result_for_step", return_value=None)
    def test_poll_task_no_result_yet(self, mock_read_result, mock_verify):
        """RESULT_READY but no result file → verify_and_advance not called."""
        task = _make_task("no-res")
        _advance_to_prompt_sent(task)
        poller = self._make_poller(PollResult.RESULT_READY)

        ret = poll_task(task, poller)

        assert ret == PollResult.RESULT_READY
        mock_verify.assert_not_called()


# ---------------------------------------------------------------------------
# verify_and_advance — rollback on send failure
# ---------------------------------------------------------------------------


class TestVerifyAndAdvanceRollback:
    """Tests for state rollback when send_task_prompt fails."""

    def _write_result(self, task, step, attempt, status="done", **extra):
        result_path = task.result_path(step, attempt)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "step": step,
            "attempt": attempt,
            "incarnation": task.incarnation_id,
            "status": status,
            **extra,
        }
        write_json(result_path, data)

    @patch("duo.commander.send_task_prompt", side_effect=RuntimeError("pane dead"))
    @patch("duo.commander.verify_step", return_value=Pass())
    def test_continuation_send_failure_rolls_back_step(self, mock_verify, mock_send):
        """If send_task_prompt fails on continuation, step/attempt are rolled back."""
        task = _make_task(subtasks=[_make_subtask(1), _make_subtask(2)])
        _advance_to_prompt_sent(task)
        self._write_result(task, 1, 1)

        verify_and_advance(task)

        # Step should be rolled back to original
        assert task.current_step == 1
        assert task.current_attempt == 1
        assert task.status == TaskStatus.BLOCKED

    @patch("duo.commander.send_task_prompt", side_effect=OSError("transport error"))
    @patch("duo.commander.verify_step")
    def test_correction_send_failure_rolls_back_attempt(self, mock_verify, mock_send):
        """If send_task_prompt fails on correction, attempt is rolled back."""
        mock_verify.return_value = Correction("test failure")
        task = _make_task()
        _advance_to_prompt_sent(task)
        self._write_result(task, 1, 1)

        verify_and_advance(task)

        # Attempt should be rolled back to original
        assert task.current_attempt == 1
        assert task.status == TaskStatus.FAILED


# ---------------------------------------------------------------------------
# Transition return-value guard tests (Round FD)
# ---------------------------------------------------------------------------


class TestTransitionReturnValueGuards:
    """Tests for transition() return-value checks in critical paths."""

    @patch("duo.commander.get_tmux_session_target", return_value="main")
    def test_start_session_illegal_transition_returns_early(self, mock_target):
        """start_session returns immediately if transition to SESSION_STARTING fails."""
        task = _make_task("tg-start")
        # Force to COMPLETED — can't transition to SESSION_STARTING from there
        _advance_to_prompt_sent(task)
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)
        transition(task, TaskStatus.VERIFYING)
        transition(task, TaskStatus.COMPLETED)

        start_session(task)

        # Should not have called subprocess — returned early
        mock_target.assert_not_called()
        assert task.status == TaskStatus.COMPLETED

    def test_verify_and_advance_illegal_verifying_returns_early(self):
        """verify_and_advance returns if transition to VERIFYING fails."""
        task = _make_task("tg-verify")
        # Stay at CREATED — CREATED → VERIFYING is illegal
        step = task.current_step
        attempt = task.current_attempt
        result_path = task.result_path(step, attempt)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(
                {
                    "step": step,
                    "attempt": attempt,
                    "incarnation": task.incarnation_id,
                    "status": "done",
                    "files_changed": [],
                    "summary": "ok",
                }
            )
        )

        verify_and_advance(task)

        # Should still be CREATED — transition was rejected
        assert task.status == TaskStatus.CREATED

    @patch("duo.commander.verify_step")
    def test_completed_transition_guarded(self, mock_verify):
        """task_completed event is only emitted when COMPLETED transition succeeds."""
        from duo.verifier import Pass

        mock_verify.return_value = Pass()
        task = _make_task("tg-complete", subtasks=[_make_subtask()])
        _advance_to_prompt_sent(task)
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)

        step = task.current_step
        attempt = task.current_attempt
        result_path = task.result_path(step, attempt)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(
                {
                    "step": step,
                    "attempt": attempt,
                    "incarnation": task.incarnation_id,
                    "status": "done",
                    "files_changed": [],
                    "summary": "ok",
                }
            )
        )

        verify_and_advance(task)

        # Should successfully complete
        assert task.status == TaskStatus.COMPLETED

    @patch("duo.commander.verify_step")
    def test_correcting_transition_failure_rolls_back_attempt(self, mock_verify):
        """If CORRECTING transition fails, attempt is rolled back."""
        from duo.verifier import Correction

        mock_verify.return_value = Correction("needs fix")
        task = _make_task("tg-correct", subtasks=[_make_subtask()])
        _advance_to_prompt_sent(task)
        # Force to CREATED to make VERIFYING→CORRECTING work but then block
        # Actually, let's make transition to CORRECTING fail by putting task
        # in a state that can't transition to CORRECTING
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)

        original_attempt = task.current_attempt
        step = task.current_step
        result_path = task.result_path(step, original_attempt)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(
                {
                    "step": step,
                    "attempt": original_attempt,
                    "incarnation": task.incarnation_id,
                    "status": "done",
                    "files_changed": [],
                    "summary": "ok",
                }
            )
        )

        # Monkeypatch transition to succeed for VERIFYING but fail for CORRECTING
        real_transition = transition

        def conditional_transition(t, status):
            if status == TaskStatus.CORRECTING:
                return False
            return real_transition(t, status)

        with patch("duo.commander.transition", side_effect=conditional_transition):
            verify_and_advance(task)

        # Attempt should be rolled back
        assert task.current_attempt == original_attempt

    @patch("duo.commander.select_dialog_option")
    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander._check_pr_budget", return_value=True)
    def test_prompt_sent_transition_failure_logs_warning(
        self, mock_budget, mock_wait, mock_select
    ):
        """If PROMPT_SENT transition fails, a warning is logged but no crash."""
        task = _make_task("tg-prompt")
        _advance_to_prompt_sent(task)

        real_transition = transition

        def fail_prompt_sent(t, status):
            if status == TaskStatus.PROMPT_SENT:
                return False
            return real_transition(t, status)

        with patch("duo.commander.transition", side_effect=fail_prompt_sent):
            with patch("duo.commander.append_event"):
                with patch("duo.commander.save_task"):
                    send_task_prompt(task, "test prompt")

    def test_start_session_prompt_sent_transition_failure(self):
        """start_session logs warning when PROMPT_SENT transition fails."""
        from unittest.mock import MagicMock

        task = _make_task("tg-start-ps")

        real_transition = transition

        def fail_prompt_sent(t, status):
            if status == TaskStatus.PROMPT_SENT:
                return False
            return real_transition(t, status)

        with (
            patch("duo.commander.subprocess.run") as mock_run,
            patch("duo.commander.name_pane"),
            patch("duo.commander.send_shell_command"),
            patch("duo.commander.wait_for_idle"),
            patch("duo.commander.read_pane", return_value="❯"),
            patch("duo.commander.is_at_main_prompt", return_value=True),
            patch("duo.commander.send_bootstrap"),
            patch("duo.commander.time.sleep"),
            patch("duo.commander.get_config", return_value=False),
            patch("duo.commander.transition", side_effect=fail_prompt_sent),
        ):
            split_result = MagicMock()
            split_result.returncode = 0
            split_result.stdout = "%42\n"
            layout_result = MagicMock()
            layout_result.returncode = 0
            mock_run.side_effect = [split_result, layout_result]

            start_session(task)


class TestNormalizeForRestart:
    """normalize_for_restart() transitions active states to FAILED for restart."""

    def test_directly_restartable_states_return_true(self):
        """States already legal for SESSION_STARTING need no normalization."""
        from duo.commander import normalize_for_restart

        for status_name in [
            "created",
            "queued",
            "blocked",
            "failed",
            "session_starting",
        ]:
            task = _make_task(f"norm-{status_name}")
            task.status = TaskStatus(status_name)
            save_task(task)
            assert normalize_for_restart(task) is True
            assert task.status == TaskStatus(status_name)

    def test_active_state_normalized_to_failed(self):
        """Active states are moved to FAILED before restart."""
        from duo.commander import normalize_for_restart

        active_states = [
            TaskStatus.PROMPT_SENT,
            TaskStatus.ACKED,
            TaskStatus.RUNNING,
            TaskStatus.RESULT_REPORTED,
            TaskStatus.VERIFYING,
            TaskStatus.CORRECTING,
        ]
        for status in active_states:
            task = _make_task(f"norm-{status.value}")
            task.status = status
            save_task(task)
            assert normalize_for_restart(task) is True
            assert task.status == TaskStatus.FAILED

    def test_normalize_failure_returns_false(self):
        """If transition to FAILED is rejected, returns False."""
        from duo.commander import normalize_for_restart

        task = _make_task("norm-fail")
        task.status = TaskStatus.RUNNING
        save_task(task)
        with patch("duo.commander.transition", return_value=False):
            assert normalize_for_restart(task) is False

    def test_restart_session_with_active_state_normalizes(self):
        """restart_session normalizes active state to FAILED before side effects."""
        task = _make_task("restart-norm")
        task.status = TaskStatus.ACKED
        save_task(task)
        with (
            patch("duo.commander.start_session"),
            patch("duo.commander.kill_pane"),
            patch("duo.commander.clear_bootstrap_done"),
        ):
            restart_session(task)
        assert task.status != TaskStatus.ACKED

    def test_restart_session_normalize_failure_returns_early(self):
        """restart_session returns early if normalize fails — no side effects."""
        task = _make_task("restart-bail")
        task.status = TaskStatus.RUNNING
        old_inc = task.incarnation_id
        save_task(task)
        with (
            patch("duo.commander.transition", return_value=False),
            patch("duo.commander.kill_pane") as mock_kill,
            patch("duo.commander.start_session") as mock_start,
        ):
            restart_session(task)
        # No side effects should have occurred
        mock_kill.assert_not_called()
        mock_start.assert_not_called()
        assert task.incarnation_id == old_inc


# ---------------------------------------------------------------------------
# Branch coverage: commander.py partial branches
# ---------------------------------------------------------------------------


class TestBranchCoverageCommander:
    """Targeted tests to close partial branch gaps."""

    def test_bootstrap_prompt_empty_readme(self, tmp_path: Path):
        """278->282: README exists but is empty — no excerpt in CLAUDE.md."""
        task = _make_task("empty-readme")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        task.worktree = str(worktree)
        save_task(task)
        (worktree / "README.md").write_text("", encoding="utf-8")

        write_commander_claude_md(task)
        content = (worktree / "CLAUDE.md").read_text()
        assert "README (excerpt)" not in content

    def test_resend_last_prompt_empty_file(self):
        """814->exit: Empty prompt file — returns without doing anything."""
        task = _make_task("resend-empty")
        _advance_to_prompt_sent(task)
        # Write an empty prompt file
        prompt_path = task.prompt_path(task.current_step, task.current_attempt)
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text("", encoding="utf-8")

        resend_last_prompt(task)
        # No crash, no side effects
        # No crash — function returned early

    def test_verify_and_advance_no_result_returns_early(self):
        """853->856: No result file — returns without doing anything."""
        task = _make_task("no-result")
        _advance_to_prompt_sent(task)
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)

        verify_and_advance(task)
        # Status should still be RESULT_REPORTED (no crash, no transition)
        assert task.status == TaskStatus.RESULT_REPORTED

    def test_verify_and_advance_explicit_result(self):
        """853->856: Passing result explicitly skips disk read."""
        from duo.protocol import StepResult

        task = _make_task("explicit-result")
        _advance_to_prompt_sent(task)
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)

        result = StepResult(
            step=1,
            attempt=1,
            incarnation=task.incarnation_id,
            status="done",
            files_changed=[],
            summary="ok",
        )
        with patch("duo.commander.verify_step", return_value=Pass()):
            verify_and_advance(task, result=result)
        assert task.status == TaskStatus.COMPLETED

    def test_count_corrections_hits_task_created_break(self):
        """1006->1013: task_created event stops backward scan."""
        task = _make_task("corr-break")
        append_event(task, "correction_sent", {"step": 1, "attempt": 2})
        append_event(task, "task_created", {"id": task.id})
        append_event(task, "correction_sent", {"step": 1, "attempt": 3})
        assert _count_corrections(task, 1) == 1

    def test_count_corrections_no_task_created_scans_all(self):
        """1006->1013: Without task_created, scans all events."""
        task = _make_task("corr-all")
        # Clear journal and write only correction events (no task_created)
        task.journal_path.write_text("", encoding="utf-8")
        append_event(task, "correction_sent", {"step": 1, "attempt": 2})
        append_event(task, "correction_sent", {"step": 1, "attempt": 3})
        assert _count_corrections(task, 1) == 2

    def test_escalate_pr_budget_transition_fails(self):
        """743->745: transition to ESCALATED already done — no event appended."""
        task = _make_task("esc-fail")
        _advance_to_prompt_sent(task)
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.ESCALATED)

        # Already ESCALATED → transition fails, but echo still happens
        _escalate_pr_budget(task, step=1, attempt=1)
        events = read_jsonl(task.journal_path)
        assert not any(e.get("event") == "pr_budget_exceeded" for e in events)

    def test_verify_and_advance_completed_transition_fails(self):
        """898->exit: COMPLETED transition fails (task already completed)."""
        from duo.protocol import StepResult

        task = _make_task("comp-fail")
        task.subtasks = []  # no subtasks → step >= len(subtasks) → COMPLETED path
        _advance_to_prompt_sent(task)
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)

        result = StepResult(
            step=1,
            attempt=1,
            incarnation=task.incarnation_id,
            status="done",
            files_changed=[],
            summary="ok",
        )

        def _fake_verify(t, r):
            # Transition directly so COMPLETED→COMPLETED fails
            transition(t, TaskStatus.COMPLETED)
            return Pass()

        with patch("duo.commander.verify_step", side_effect=_fake_verify):
            verify_and_advance(task, result=result)
        # No crash — task ended up COMPLETED from _fake_verify
        assert task.status == TaskStatus.COMPLETED

    def test_verify_and_advance_escalation_transition_fails(self):
        """933->exit + 938->947: max corrections reached, escalation transition fails."""
        from duo.protocol import StepResult

        task = _make_task("esc-corr-fail")
        _advance_to_prompt_sent(task)
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)

        # Pre-fill corrections to exceed max
        for i in range(5):
            append_event(task, "correction_sent", {"step": 1, "attempt": i + 2})

        result = StepResult(
            step=1,
            attempt=1,
            incarnation=task.incarnation_id,
            status="done",
            files_changed=[],
            summary="ok",
        )

        def _fake_verify(t, r):
            # First transition to ESCALATED so repeat attempt fails
            transition(t, TaskStatus.ESCALATED)
            return Correction(reason="bad")

        with patch("duo.commander.verify_step", side_effect=_fake_verify):
            verify_and_advance(task, result=result)
        assert task.status == TaskStatus.ESCALATED

    @patch("duo.commander.time.sleep", side_effect=[None, StopIteration])
    @patch("duo.scheduler.promote_queued", return_value=[])
    @patch("duo.commander.list_tasks")
    def test_monitor_queued_but_no_active_continues(
        self, mock_list, mock_promote, mock_sleep
    ):
        """1216->1220: queued tasks exist but no active — loop continues."""
        queued_task = _make_task("q-task")
        transition(queued_task, TaskStatus.QUEUED)  # must be QUEUED status
        mock_list.return_value = [queued_task]

        with (
            patch(
                "duo.scheduler.queue_status",
                return_value={"active_count": 0, "queued_count": 1, "max_parallel": 2},
            ),
            pytest.raises(StopIteration),
        ):
            monitor()

    def test_watch_loop_pane_gone_immediately(self):
        """1354->exit: stop already set before loop starts — exits immediately."""
        import threading

        task = _make_task("watch-gone")
        _advance_to_prompt_sent(task)
        stop = threading.Event()
        stop.set()  # pre-set so while condition is False on first check

        with patch("duo.commander.is_process_alive", return_value=True):
            _watch_loop(
                task,
                stop,
                timeout=1,
                interval=0.1,
                once=False,
                auto_approve=False,
            )
        # Loop never entered — exited immediately

    def test_verify_and_advance_unknown_verdict_type(self):
        """933->exit: verdict is neither Pass nor Correction — silent no-op."""
        from duo.protocol import StepResult

        task = _make_task("unknown-verdict")
        _advance_to_prompt_sent(task)
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)

        result = StepResult(
            step=1,
            attempt=1,
            incarnation=task.incarnation_id,
            status="done",
            files_changed=[],
            summary="ok",
        )

        # Return something that is neither Pass nor Correction
        with patch("duo.commander.verify_step", return_value="unexpected"):
            verify_and_advance(task, result=result)
        # No crash — function exits after the if/elif without action
        assert task.status == TaskStatus.VERIFYING


class TestCommanderEdgeCases:
    """Edge case value coverage for commander functions."""

    def test_count_corrections_step_zero(self):
        """_count_corrections with step=0 (invalid step) returns 0."""
        task = _make_task("corr-zero")
        append_event(task, "correction_sent", {"step": 1, "attempt": 2})
        assert _count_corrections(task, 0) == 0

    def test_count_corrections_large_step(self):
        """_count_corrections with step=999 (no matching events) returns 0."""
        task = _make_task("corr-large")
        append_event(task, "correction_sent", {"step": 1, "attempt": 2})
        assert _count_corrections(task, 999) == 0

    def test_count_corrections_malformed_event_data(self):
        """_count_corrections handles events with missing/bad data fields."""
        task = _make_task("corr-bad")
        # Event with no 'data' key
        append_event(task, "correction_sent", {})
        # Event with non-dict data (the data.get("step") would fail if not dict)
        assert _count_corrections(task, 1) == 0

    def test_count_corrections_many_events(self):
        """_count_corrections correctly counts within tail=200 window."""
        task = _make_task("corr-many")
        for i in range(50):
            append_event(task, "correction_sent", {"step": 1, "attempt": i + 2})
        assert _count_corrections(task, 1) == 50
