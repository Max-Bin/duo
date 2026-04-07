"""Tests for duo.commander — prompt builders and orchestration."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from duo.commander import (
    _count_corrections,
    build_bootstrap_prompt,
    build_continue_prompt,
    build_correction_prompt,
    build_task_prompt,
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

    @patch("duo.commander.send_prompt")
    @patch("duo.commander.verify_step")
    def test_no_result_does_nothing(self, mock_verify, mock_send):
        task = _make_task()
        _advance_to_prompt_sent(task)
        # Don't write any result file
        verify_and_advance(task)
        mock_verify.assert_not_called()
        # Status unchanged
        assert task.status == TaskStatus.PROMPT_SENT

    @patch("duo.commander.send_prompt")
    @patch("duo.commander.verify_step")
    def test_blocked_result_transitions_to_blocked(self, mock_verify, mock_send):
        task = _make_task()
        _advance_to_prompt_sent(task)
        self._write_result(task, 1, 1, status="blocked", reason="missing dep")

        verify_and_advance(task)
        assert task.status == TaskStatus.BLOCKED
        mock_verify.assert_not_called()

    @patch("duo.commander.send_prompt")
    @patch("duo.commander.verify_step")
    def test_pass_last_step_completes(self, mock_verify, mock_send):
        task = _make_task()  # single subtask → step 1 is the last step
        _advance_to_prompt_sent(task)
        self._write_result(task, 1, 1)
        mock_verify.return_value = Pass()

        verify_and_advance(task)
        assert task.status == TaskStatus.COMPLETED

    @patch("duo.commander.send_prompt")
    @patch("duo.commander.verify_step")
    def test_pass_advances_to_next_step(self, mock_verify, mock_send):
        task = _make_task(subtasks=[_make_subtask(1), _make_subtask(2)])
        _advance_to_prompt_sent(task)
        self._write_result(task, 1, 1)
        mock_verify.return_value = Pass()

        verify_and_advance(task)

        assert task.current_step == 2
        assert task.current_attempt == 1
        # send_task_prompt calls send_prompt internally
        mock_send.assert_called()
        # Should end in PROMPT_SENT after sending continuation
        assert task.status == TaskStatus.PROMPT_SENT

    @patch("duo.commander.send_prompt")
    @patch("duo.commander.verify_step")
    def test_correction_increments_attempt(self, mock_verify, mock_send):
        task = _make_task()
        _advance_to_prompt_sent(task)
        self._write_result(task, 1, 1)
        mock_verify.return_value = Correction(reason="test failure")

        verify_and_advance(task)

        assert task.current_attempt == 2
        mock_send.assert_called()
        # FSM: CORRECTING cannot transition to PROMPT_SENT, stays CORRECTING
        assert task.status == TaskStatus.CORRECTING

    @patch("duo.commander.send_prompt")
    @patch("duo.commander.verify_step")
    def test_three_corrections_escalates(self, mock_verify, mock_send):
        task = _make_task()
        _advance_to_prompt_sent(task)
        # Pre-populate 3 correction events in journal
        for a in range(2, 5):
            append_event(task, "correction_sent", {"step": 1, "attempt": a})
        self._write_result(task, 1, 1)
        mock_verify.return_value = Correction(reason="still broken")

        verify_and_advance(task)
        assert task.status == TaskStatus.ESCALATED

    @patch("duo.commander.send_prompt")
    @patch("duo.commander.verify_step")
    def test_error_result_transitions_to_blocked(self, mock_verify, mock_send):
        task = _make_task()
        _advance_to_prompt_sent(task)
        self._write_result(task, 1, 1, status="error", reason="crash")

        verify_and_advance(task)
        assert task.status == TaskStatus.BLOCKED
        mock_verify.assert_not_called()
