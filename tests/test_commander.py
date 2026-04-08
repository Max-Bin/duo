"""Tests for duo.commander — prompt builders and orchestration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from duo.commander import (
    _check_pr_budget,
    _count_corrections,
    _get_copilot_model,
    _log_monitor,
    _watch_loop,
    build_bootstrap_prompt,
    build_continue_prompt,
    build_correction_prompt,
    build_task_prompt,
    monitor,
    poll_task,
    resend_last_prompt,
    restart_session,
    send_task_prompt,
    start_session,
    verify_and_advance,
    watch_tasks,
)
from duo.poller import AdaptivePoller, PollResult
from duo.protocol import (
    Subtask,
    TaskStatus,
    append_event,
    create_task,
    now_iso,
    prompt_hash,
    read_jsonl,
    transition,
    write_json,
)
from duo.verifier import Correction, Pass

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_tasks_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect TASKS_DIR and CONFIG_PATH so every test gets a fresh directory."""
    monkeypatch.setattr("duo.protocol.TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr("duo.config.CONFIG_PATH", tmp_path / "config.json")


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
            patch("duo.commander.send_bootstrap"),
            patch("duo.commander.time.sleep"),
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
            patch("duo.commander.send_bootstrap"),
            patch("duo.commander.time.sleep"),
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
            patch("duo.commander.send_bootstrap"),
            patch("duo.commander.time.sleep"),
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
            assert any(
                e.get("event") == "session_start_failed" for e in events
            )

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
            patch("duo.commander.time.sleep"),
        ):
            with pytest.raises(RuntimeError, match="tmux died"):
                restart_session(task)

            assert task.status == TaskStatus.FAILED
            events = read_jsonl(task.journal_path)
            assert any(
                e.get("event") == "session_restart_failed" for e in events
            )
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
    @patch("duo.commander.start_session")
    def test_restart_clears_bootstrap(self, mock_start):
        """restart_session discards pane from _BOOTSTRAP_DONE."""
        from duo.transport import _BOOTSTRAP_DONE, clear_bootstrap_done

        task = _make_task()
        _BOOTSTRAP_DONE.add(task.pane_label)

        restart_session(task)

        assert task.pane_label not in _BOOTSTRAP_DONE
        mock_start.assert_called_once_with(task)
        clear_bootstrap_done(task.pane_label)  # cleanup

    @patch("duo.commander.start_session")
    def test_restart_resets_incarnation(self, mock_start):
        """restart_session generates a new incarnation ID."""
        task = _make_task()
        old_inc = task.incarnation_id

        restart_session(task)

        assert task.incarnation_id != old_inc

    @patch("duo.commander.start_session")
    def test_restart_resets_attempt(self, mock_start):
        """restart_session resets current_attempt to 1."""
        task = _make_task()
        task.current_attempt = 5

        restart_session(task)

        assert task.current_attempt == 1

    @patch("duo.commander.start_session")
    def test_restart_logs_event(self, mock_start):
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
        mock_verify.assert_called_once_with(task)

    @patch("duo.commander.verify_and_advance")
    @patch("duo.commander.read_result_for_step")
    def test_poll_result_ready_wrong_incarnation(self, mock_read_result, mock_verify):
        """RESULT_READY with stale incarnation does not verify."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        poller = self._make_poller(PollResult.RESULT_READY)
        result_obj = MagicMock()
        result_obj.incarnation = "stale-inc"
        mock_read_result.return_value = result_obj

        poll_task(task, poller)

        mock_verify.assert_not_called()

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

    @patch("duo.commander.diagnose_pane", return_value="error: API limit")
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
    @patch("duo.commander.diagnose_pane", return_value="error: something")
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
        assert any(e.get("event") == "api_error" for e in events)
        # No pr_consumed for error_retry because dialog timed out
        assert not any(
            e.get("event") == "pr_consumed"
            and e.get("data", {}).get("action") == "error_retry"
            for e in events
        )

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
    def test_poll_unknown_no_prompt_sent_at(self, mock_ack, mock_resend):
        """UNKNOWN + no last_prompt_sent_at → no resend."""
        task = _make_task()
        _advance_to_prompt_sent(task)
        task.last_prompt_sent_at = None
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
        ):
            split_result = MagicMock()
            split_result.returncode = 0
            split_result.stdout = "%77\n"
            layout_result = MagicMock()
            layout_result.returncode = 0
            kill_result = MagicMock()
            kill_result.returncode = 0
            mock_run.side_effect = [split_result, layout_result, kill_result]

            with pytest.raises(RuntimeError, match="connection lost"):
                start_session(task)

            # Verify tmux kill-pane was called for the orphaned pane
            kill_calls = [
                c for c in mock_run.call_args_list
                if c[0][0][:3] == ["tmux", "kill-pane", "-t"]
            ]
            assert len(kill_calls) == 1
            assert kill_calls[0][0][0][3] == "%77"

    def test_kill_pane_failure_suppressed(self):
        """start_session suppresses OSError when kill-pane itself fails."""
        task = _make_task()

        with (
            patch("duo.commander.subprocess.run") as mock_run,
            patch("duo.commander.name_pane"),
            patch(
                "duo.commander.send_shell_command",
                side_effect=RuntimeError("connection lost"),
            ),
            patch("duo.commander.time.sleep"),
        ):
            split_result = MagicMock()
            split_result.returncode = 0
            split_result.stdout = "%77\n"
            layout_result = MagicMock()
            layout_result.returncode = 0
            # kill-pane raises OSError (e.g. tmux not found)
            mock_run.side_effect = [split_result, layout_result, OSError("tmux gone")]

            with pytest.raises(RuntimeError, match="connection lost"):
                start_session(task)

            # Should not crash — the OSError from kill-pane is suppressed
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


# ── poll_task silent path (heartbeat timeout, alive, no error) ───────


class TestPollHeartbeatTimeoutSilent:
    def test_poll_heartbeat_timeout_alive_no_error(self):
        """HEARTBEAT_TIMEOUT + alive pane + no error text → silent no-op."""
        task = _make_task()
        _advance_to_prompt_sent(task)

        with (
            patch.object(AdaptivePoller, "poll", return_value=PollResult.HEARTBEAT_TIMEOUT),
            patch("duo.commander.is_process_alive", return_value=True),
            patch("duo.commander.diagnose_pane", return_value="all good, copilot working"),
            patch("duo.commander.wait_for_dialog", return_value=False),
        ):
            poller = AdaptivePoller()
            result = poll_task(task, poller)

        assert result == PollResult.HEARTBEAT_TIMEOUT
        # No api_error event should be recorded (no error in terminal)
        events = read_jsonl(task.journal_path)
        error_events = [e for e in events if e.get("event") == "api_error"]
        assert len(error_events) == 0


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
        """Returns 0 when no active tasks."""
        with patch("duo.commander.list_tasks", return_value=[]):
            result = watch_tasks()
        assert result == 0

    def test_unknown_task_ids_warned(self, capsys) -> None:
        """Unknown task IDs produce a warning."""
        with patch("duo.commander.list_tasks", return_value=[]):
            result = watch_tasks(["nonexistent"])
        assert result == 0
        captured = capsys.readouterr()
        assert "Unknown task(s): nonexistent" in captured.err

    def test_filters_terminal_states(self) -> None:
        """Completed/failed/escalated tasks are excluded."""
        tasks = [self._finished(f"t{i}") for i in range(3)]
        with patch("duo.commander.list_tasks", return_value=tasks):
            result = watch_tasks()
        assert result == 0

    def test_filters_by_task_ids(self) -> None:
        """Only watches specified task IDs."""
        t1 = self._active("watch-me")
        t2 = self._active("skip-me")
        with (
            patch("duo.commander.list_tasks", return_value=[t1, t2]),
            patch("duo.commander.wait_for_dialog", return_value=False),
            patch("duo.commander.load_task", return_value=None),
        ):
            result = watch_tasks(["watch-me"], timeout=0.01, interval=0.01)
        assert result == 1

    def test_dialog_handled(self) -> None:
        """Dialog is detected and approved, then task finishes."""
        task = self._active("dlg")
        finished = self._finished("dlg-fin")

        call_count = 0

        def _fake_wait(label: str, timeout: float = 300, interval: float = 5) -> bool:
            nonlocal call_count
            call_count += 1
            return call_count == 1

        with (
            patch("duo.commander.list_tasks", return_value=[task]),
            patch("duo.commander.wait_for_dialog", side_effect=_fake_wait),
            patch("duo.commander.approve_permission") as mock_approve,
            patch("duo.commander.append_event"),
            patch("duo.commander.load_task", return_value=finished),
        ):
            result = watch_tasks(timeout=0.1, interval=0.01)
        assert result == 1
        mock_approve.assert_called_once_with(task.pane_label)

    def test_dialog_error_logged(self) -> None:
        """Error during approve_permission is logged, watch continues."""
        task = self._active("err")
        finished = self._finished("err-fin")

        call_count = 0

        def _fake_wait(label: str, timeout: float = 300, interval: float = 5) -> bool:
            nonlocal call_count
            call_count += 1
            return call_count == 1

        with (
            patch("duo.commander.list_tasks", return_value=[task]),
            patch("duo.commander.wait_for_dialog", side_effect=_fake_wait),
            patch(
                "duo.commander.approve_permission",
                side_effect=RuntimeError("not in dialog"),
            ),
            patch("duo.commander.append_event"),
            patch("duo.commander.load_task", return_value=finished),
        ):
            result = watch_tasks(timeout=0.1, interval=0.01)
        assert result == 1

    def test_pane_unavailable(self) -> None:
        """Watch loop exits when pane raises exception."""
        task = self._active("gone")
        with (
            patch("duo.commander.list_tasks", return_value=[task]),
            patch(
                "duo.commander.wait_for_dialog", side_effect=OSError("no pane")
            ),
        ):
            result = watch_tasks(timeout=0.1, interval=0.01)
        assert result == 1

    def test_once_flag(self) -> None:
        """--once stops after first dialog."""
        task = self._active("once")

        with (
            patch("duo.commander.list_tasks", return_value=[task]),
            patch("duo.commander.wait_for_dialog", return_value=True),
            patch("duo.commander.approve_permission"),
            patch("duo.commander.append_event"),
        ):
            result = watch_tasks(once=True, timeout=0.1, interval=0.01)
        assert result == 1

    def test_stop_set_during_wait(self) -> None:
        """Watch loop exits when stop event set during wait_for_dialog."""
        import threading as _th

        task = self._active("stop-mid")

        def _fake_wait(label: str, timeout: float = 300, interval: float = 5) -> bool:
            # Simulate stop being set by another thread
            for t in _th.enumerate():
                if t.name.startswith("Thread"):
                    # The watch_tasks function uses stop.set() — we need
                    # to find it via the _watch_loop closure.  Easier:
                    # just return False after a short sleep.
                    pass
            return False

        # Task still active → continue branch; then stop_set breaks
        call_count = 0

        def _wait_then_stop(label: str, timeout: float = 300, interval: float = 5) -> bool:
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                # Signal we should stop by raising
                raise OSError("done")
            return False

        with (
            patch("duo.commander.list_tasks", return_value=[task]),
            patch("duo.commander.wait_for_dialog", side_effect=_wait_then_stop),
            patch("duo.commander.load_task", return_value=task),
        ):
            result = watch_tasks(timeout=0.01, interval=0.01)
        assert result == 1

    def test_task_still_active_continues(self) -> None:
        """Watch loop continues polling when task still active after timeout."""
        task = self._active("cont")

        call_count = 0

        def _fake_wait(label: str, timeout: float = 300, interval: float = 5) -> bool:
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                raise OSError("stop")
            return False

        with (
            patch("duo.commander.list_tasks", return_value=[task]),
            patch("duo.commander.wait_for_dialog", side_effect=_fake_wait),
            patch("duo.commander.load_task", return_value=task),
        ):
            result = watch_tasks(timeout=0.01, interval=0.01)
        assert result == 1
        assert call_count >= 3

    def test_stop_event_after_wait_returns(self) -> None:
        """Break when stop event is set after wait_for_dialog returns."""
        task = self._active("stop-after")
        # We need two tasks so that when once=True triggers on the first,
        # the second sees stop.is_set() == True after its wait returns.
        task2 = self._active("stop-after2")

        def _fake_wait(label: str, timeout: float = 300, interval: float = 5) -> bool:
            # Return True for first task, this triggers once→stop.set()
            # Second task will see stop.is_set() after returning
            return True

        with (
            patch("duo.commander.list_tasks", return_value=[task, task2]),
            patch("duo.commander.wait_for_dialog", side_effect=_fake_wait),
            patch("duo.commander.approve_permission"),
            patch("duo.commander.append_event"),
        ):
            result = watch_tasks(once=True, timeout=0.1, interval=0.01)
        assert result == 2

    def test_stop_set_during_wait_direct(self) -> None:
        """_watch_loop breaks when stop is set during wait_for_dialog."""
        import threading as _th

        task = self._active("mid-stop")
        stop = _th.Event()

        def _fake_wait(label: str, timeout: float = 300, interval: float = 5) -> bool:
            stop.set()  # Simulate stop being set while waiting
            return False

        with patch("duo.commander.wait_for_dialog", side_effect=_fake_wait):
            _watch_loop(task, stop, timeout=1, interval=0.01, once=False)
