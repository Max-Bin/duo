"""Tests for duo.commander — prompt builders and orchestration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from duo.commander import (
    _check_pr_budget,
    _count_corrections,
    build_bootstrap_prompt,
    build_continue_prompt,
    build_correction_prompt,
    build_task_prompt,
    start_session,
    verify_and_advance,
)
from duo.protocol import (
    Subtask,
    TaskStatus,
    append_event,
    create_task,
    prompt_hash,
    transition,
    write_json,
)
from duo.verifier import Correction, Pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_tasks_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect TASKS_DIR so every test gets a fresh directory."""
    monkeypatch.setattr("duo.protocol.TASKS_DIR", tmp_path / "tasks")


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
    def test_error_result_transitions_to_blocked(
        self, mock_verify, mock_send, mock_wait
    ):
        task = _make_task()
        _advance_to_prompt_sent(task)
        self._write_result(task, 1, 1, status="error", reason="crash")

        verify_and_advance(task)
        assert task.status == TaskStatus.BLOCKED
        mock_verify.assert_not_called()


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
