"""End-to-end integration tests for the duo protocol flow.

Tests the full protocol lifecycle WITHOUT real tmux — all transport and
external subprocess calls are mocked.  Covers:

  - Happy path: create → prompt_sent → ack → result → verify → complete
  - Error/correction path: verifier rejection → correction loop → escalation
  - Multi-step task: 3 subtasks each completing in sequence
  - Quarantine integration: corrupted task auto-quarantined by list_tasks
  - Watch event flow: _write_watch_event creates signal files
  - Journal replay: replay_state reconstructs FSM state from journal
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import duo.protocol as protocol
from duo.commander import (
    _write_watch_event,
    verify_and_advance,
)
from duo.protocol import (
    AckResult,
    Heartbeat,
    StepResult,
    Subtask,
    TaskStatus,
    _clear_task_cache,
    append_event,
    create_task,
    list_corrupted,
    list_tasks,
    load_task,
    read_ack_for_step,
    read_heartbeat,
    read_jsonl,
    read_result_for_step,
    replay_state,
    save_task,
    transition,
    write_json,
)
from duo.verifier import Correction, Pass

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect TASKS_DIR, _CORRUPTED_DIR, _WATCH_EVENTS_DIR, and CONFIG_PATH
    to temp directories so every test is fully isolated."""
    tasks = tmp_path / "tasks"
    monkeypatch.setattr("duo.protocol.TASKS_DIR", tasks)
    monkeypatch.setattr("duo.protocol._CORRUPTED_DIR", tasks / "_corrupted")
    monkeypatch.setattr("duo.commander._WATCH_EVENTS_DIR", tmp_path / "watch-events")
    monkeypatch.setattr("duo.config.CONFIG_PATH", tmp_path / "config.json")


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear the task-list mtime cache between tests."""
    _clear_task_cache()
    yield
    _clear_task_cache()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sub(step_id: int = 1, **overrides) -> Subtask:
    defaults = dict(
        step_id=step_id,
        description=f"step-{step_id}",
        target_files=["f.py"],
        writable_paths=["/w"],
    )
    defaults.update(overrides)
    return Subtask(**defaults)


def _task(task_id: str = "e2e", subtasks: list[Subtask] | None = None):
    return create_task(
        task_id=task_id,
        description="e2e test task",
        worktree="/fake/wt",
        branch="feat",
        base_commit="abc123",
        subtasks=subtasks or [_sub()],
    )


def _advance(task, *statuses: TaskStatus):
    """Walk the FSM through a sequence of transitions."""
    for s in statuses:
        transition(task, s)


def _write_result(task, step, attempt, status="done", **extra):
    """Write a result JSON file the way the executor would."""
    path = task.result_path(step, attempt)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(
        path,
        {
            "step": step,
            "attempt": attempt,
            "incarnation": task.incarnation_id,
            "status": status,
            **extra,
        },
    )


def _write_ack(task, step, attempt):
    """Write an ack JSON file the way the executor would."""
    path = task.ack_path(step, attempt)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(
        path,
        {
            "step": step,
            "attempt": attempt,
            "incarnation": task.incarnation_id,
            "prompt_hash": "sha256:deadbeef",
            "acked_at": "2025-01-01T00:00:00+00:00",
        },
    )


def _write_heartbeat(task, step, status="working"):
    """Write a heartbeat JSON file the way the executor would."""
    write_json(
        task.heartbeat_path,
        {
            "ts": "2025-01-01T00:00:01+00:00",
            "incarnation": task.incarnation_id,
            "step": step,
            "status": status,
            "current_file": "f.py",
        },
    )


# ---------------------------------------------------------------------------
# Happy path — full lifecycle
# ---------------------------------------------------------------------------


class TestHappyPath:
    """Create → prompt_sent → ack → heartbeat → result → verify → completed."""

    def test_create_task_initial_status(self):
        task = _task()
        assert task.status == TaskStatus.CREATED
        assert task.current_step == 1
        assert task.current_attempt == 1
        assert task.id == "e2e"

    def test_task_persists_and_reloads(self):
        task = _task()
        loaded = load_task(task.id)
        assert loaded is not None
        assert loaded.id == task.id
        assert loaded.status == TaskStatus.CREATED

    def test_transition_to_prompt_sent(self):
        task = _task()
        _advance(task, TaskStatus.SESSION_STARTING, TaskStatus.PROMPT_SENT)
        assert task.status == TaskStatus.PROMPT_SENT

    def test_ack_file_roundtrip(self):
        task = _task()
        _advance(task, TaskStatus.SESSION_STARTING, TaskStatus.PROMPT_SENT)
        _write_ack(task, step=1, attempt=1)

        ack = read_ack_for_step(task, step=1, attempt=1)
        assert ack is not None
        assert isinstance(ack, AckResult)
        assert ack.step == 1
        assert ack.attempt == 1
        assert ack.incarnation == task.incarnation_id

    def test_heartbeat_file_roundtrip(self):
        task = _task()
        _advance(task, TaskStatus.SESSION_STARTING, TaskStatus.PROMPT_SENT)
        _write_heartbeat(task, step=1)

        hb = read_heartbeat(task)
        assert hb is not None
        assert isinstance(hb, Heartbeat)
        assert hb.step == 1
        assert hb.status == "working"
        assert hb.incarnation == task.incarnation_id

    def test_result_file_roundtrip(self):
        task = _task()
        _advance(task, TaskStatus.SESSION_STARTING, TaskStatus.PROMPT_SENT)
        _write_result(task, step=1, attempt=1, files_changed=["f.py"], summary="ok")

        result = read_result_for_step(task, step=1, attempt=1)
        assert result is not None
        assert isinstance(result, StepResult)
        assert result.status == "done"
        assert result.files_changed == ["f.py"]
        assert result.summary == "ok"

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    @patch("duo.commander.verify_step", return_value=Pass())
    def test_full_happy_lifecycle(self, mock_verify, mock_send, mock_wait):
        """Walk through the entire happy path from create to completed."""
        task = _task()
        assert task.status == TaskStatus.CREATED

        # 1. Session starting
        _advance(task, TaskStatus.SESSION_STARTING)
        assert task.status == TaskStatus.SESSION_STARTING

        # 2. Prompt sent
        _advance(task, TaskStatus.PROMPT_SENT)
        assert task.status == TaskStatus.PROMPT_SENT

        # 3. Executor writes ack
        _write_ack(task, 1, 1)
        ack = read_ack_for_step(task, 1, 1)
        assert ack is not None

        # 4. Executor writes heartbeat
        _write_heartbeat(task, 1)
        hb = read_heartbeat(task)
        assert hb is not None

        # 5. Executor writes result
        _write_result(task, 1, 1, files_changed=["f.py"])

        # 6. verify_and_advance completes the task
        verify_and_advance(task)
        assert task.status == TaskStatus.COMPLETED

        # 7. Journal records the full lifecycle
        events = read_jsonl(task.journal_path)
        event_types = [e["event"] for e in events]
        assert "task_created" in event_types
        assert "status_changed" in event_types
        assert "task_completed" in event_types

    def test_journal_records_transitions(self):
        task = _task()
        _advance(task, TaskStatus.SESSION_STARTING, TaskStatus.PROMPT_SENT)

        events = read_jsonl(task.journal_path)
        status_events = [e for e in events if e["event"] == "status_changed"]
        assert len(status_events) == 2
        assert status_events[0]["data"]["to"] == "session_starting"
        assert status_events[1]["data"]["to"] == "prompt_sent"


# ---------------------------------------------------------------------------
# Error / correction path
# ---------------------------------------------------------------------------


class TestCorrectionPath:
    """Verifier rejection → CORRECTING → re-verify → eventually complete or escalate."""

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    @patch("duo.commander.verify_step", return_value=Correction(reason="test failed"))
    def test_correction_increments_attempt(self, mock_verify, mock_send, mock_wait):
        task = _task()
        _advance(task, TaskStatus.SESSION_STARTING, TaskStatus.PROMPT_SENT)
        _write_result(task, 1, 1)

        verify_and_advance(task)

        assert task.current_attempt == 2
        # After correction prompt is sent, status ends at PROMPT_SENT
        assert task.status == TaskStatus.PROMPT_SENT

        # Journal records correction
        events = read_jsonl(task.journal_path)
        corrections = [e for e in events if e["event"] == "correction_sent"]
        assert len(corrections) == 1
        assert corrections[0]["data"]["reason"] == "test failed"

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    @patch("duo.commander.verify_step")
    def test_correction_then_pass(self, mock_verify, mock_send, mock_wait):
        """First attempt fails verification, second attempt passes."""
        task = _task()
        _advance(task, TaskStatus.SESSION_STARTING, TaskStatus.PROMPT_SENT)

        # First attempt: verifier rejects
        _write_result(task, 1, 1)
        mock_verify.return_value = Correction(reason="bug")
        verify_and_advance(task)
        assert task.current_attempt == 2
        assert task.status == TaskStatus.PROMPT_SENT

        # Second attempt: verifier accepts
        _write_result(task, 1, 2)
        mock_verify.return_value = Pass()
        verify_and_advance(task)
        assert task.status == TaskStatus.COMPLETED

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    @patch("duo.commander.verify_step", return_value=Correction(reason="still broken"))
    def test_max_corrections_escalates(self, mock_verify, mock_send, mock_wait):
        """After max_corrections (default 3) corrections, task escalates."""
        task = _task()
        _advance(task, TaskStatus.SESSION_STARTING, TaskStatus.PROMPT_SENT)

        # Pre-populate 3 correction events
        for a in range(2, 5):
            append_event(task, "correction_sent", {"step": 1, "attempt": a})

        _write_result(task, 1, 1)
        verify_and_advance(task)
        assert task.status == TaskStatus.ESCALATED

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    @patch("duo.commander.verify_step", return_value=Correction(reason="bad"))
    def test_max_corrections_one_escalates_immediately(
        self, mock_verify, mock_send, mock_wait, monkeypatch
    ):
        """With max_corrections=1, first correction exhausts budget."""
        task = _task()
        _advance(task, TaskStatus.SESSION_STARTING, TaskStatus.PROMPT_SENT)
        append_event(task, "correction_sent", {"step": 1, "attempt": 2})
        _write_result(task, 1, 1)
        monkeypatch.setattr(
            "duo.commander.get_config",
            lambda k: 1 if k == "max_corrections" else 3,
        )

        verify_and_advance(task)
        assert task.status == TaskStatus.ESCALATED

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    @patch("duo.commander.verify_step")
    def test_blocked_result_transitions_to_blocked(
        self, mock_verify, mock_send, mock_wait
    ):
        task = _task()
        _advance(task, TaskStatus.SESSION_STARTING, TaskStatus.PROMPT_SENT)
        _write_result(task, 1, 1, status="blocked", reason="missing dep")

        verify_and_advance(task)
        assert task.status == TaskStatus.BLOCKED
        mock_verify.assert_not_called()

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    @patch("duo.commander.verify_step", side_effect=RuntimeError("git crash"))
    def test_verify_exception_transitions_to_failed(
        self, mock_verify, mock_send, mock_wait
    ):
        task = _task()
        _advance(task, TaskStatus.SESSION_STARTING, TaskStatus.PROMPT_SENT)
        _write_result(task, 1, 1)

        verify_and_advance(task)
        assert task.status == TaskStatus.FAILED

        events = read_jsonl(task.journal_path)
        errors = [e for e in events if e["event"] == "verify_error"]
        assert len(errors) == 1
        assert "git crash" in errors[0]["data"]["error"]


# ---------------------------------------------------------------------------
# Multi-step task
# ---------------------------------------------------------------------------


class TestMultiStepTask:
    """Task with 3 subtasks — each step completes in sequence."""

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    @patch("duo.commander.verify_step", return_value=Pass())
    def test_three_step_completion(self, mock_verify, mock_send, mock_wait):
        subs = [_sub(1), _sub(2), _sub(3)]
        task = _task(subtasks=subs)
        _advance(task, TaskStatus.SESSION_STARTING, TaskStatus.PROMPT_SENT)

        # Step 1 completes → advances to step 2
        _write_result(task, 1, 1)
        verify_and_advance(task)
        assert task.current_step == 2
        assert task.current_attempt == 1
        assert task.status == TaskStatus.PROMPT_SENT

        # Step 2 completes → advances to step 3
        _write_result(task, 2, 1)
        verify_and_advance(task)
        assert task.current_step == 3
        assert task.current_attempt == 1
        assert task.status == TaskStatus.PROMPT_SENT

        # Step 3 completes → task completed (last step)
        _write_result(task, 3, 1)
        verify_and_advance(task)
        assert task.status == TaskStatus.COMPLETED

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    @patch("duo.commander.verify_step")
    def test_correction_mid_flow(self, mock_verify, mock_send, mock_wait):
        """Step 2 fails verification, gets corrected, then all steps complete."""
        subs = [_sub(1), _sub(2), _sub(3)]
        task = _task(subtasks=subs)
        _advance(task, TaskStatus.SESSION_STARTING, TaskStatus.PROMPT_SENT)

        # Step 1 passes
        mock_verify.return_value = Pass()
        _write_result(task, 1, 1)
        verify_and_advance(task)
        assert task.current_step == 2

        # Step 2 attempt 1 fails
        mock_verify.return_value = Correction(reason="wrong output")
        _write_result(task, 2, 1)
        verify_and_advance(task)
        assert task.current_step == 2
        assert task.current_attempt == 2

        # Step 2 attempt 2 passes
        mock_verify.return_value = Pass()
        _write_result(task, 2, 2)
        verify_and_advance(task)
        assert task.current_step == 3
        assert task.current_attempt == 1

        # Step 3 passes
        _write_result(task, 3, 1)
        verify_and_advance(task)
        assert task.status == TaskStatus.COMPLETED

    @patch("duo.commander.wait_for_dialog", return_value=True)
    @patch("duo.commander.select_dialog_option")
    @patch("duo.commander.verify_step", return_value=Pass())
    def test_each_step_has_own_directory(self, mock_verify, mock_send, mock_wait):
        """Verify that step directories are created for each step."""
        subs = [_sub(1), _sub(2)]
        task = _task(subtasks=subs)
        _advance(task, TaskStatus.SESSION_STARTING, TaskStatus.PROMPT_SENT)

        # Step dirs exist from create_task
        assert task.step_dir(1).exists()
        assert task.step_dir(2).exists()

        _write_result(task, 1, 1)
        verify_and_advance(task)

        # After advancing, step 2 dir should also exist
        assert task.step_dir(2).exists()


# ---------------------------------------------------------------------------
# Quarantine integration
# ---------------------------------------------------------------------------


class TestQuarantineIntegration:
    """Corrupted tasks are auto-quarantined by list_tasks."""

    def test_corrupted_task_auto_quarantined(self):
        """A task with invalid JSON is quarantined on list_tasks()."""
        # Create a valid task first
        good = _task(task_id="good-task")
        assert good.status == TaskStatus.CREATED

        # Create a corrupted task directory with invalid JSON
        bad_dir = protocol.TASKS_DIR / "bad-task"
        bad_dir.mkdir(parents=True, exist_ok=True)
        (bad_dir / "task.json").write_text("{invalid json!!!")

        tasks = list_tasks()
        task_ids = [t.id for t in tasks]
        assert "good-task" in task_ids
        assert "bad-task" not in task_ids

        # bad-task should be in quarantine
        corrupted = list_corrupted()
        assert len(corrupted) == 1
        assert "bad-task" in corrupted[0].name

    def test_missing_subtasks_quarantined(self):
        """A task.json missing subtasks field is quarantined."""
        bad_dir = protocol.TASKS_DIR / "no-subs"
        bad_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            bad_dir / "task.json",
            {
                "id": "no-subs",
                "description": "missing subtasks",
                "worktree": "/w",
                "branch": "b",
                "base_commit": "abc",
                "pane_label": "p",
                "incarnation_id": "12345678",
                "status": "created",
                "current_step": 1,
                "current_attempt": 1,
                "created_at": "2025-01-01T00:00:00+00:00",
            },
        )

        tasks = list_tasks()
        assert all(t.id != "no-subs" for t in tasks)
        corrupted = list_corrupted()
        assert any("no-subs" in c.name for c in corrupted)

    def test_quarantine_idempotent(self):
        """Quarantining a non-existent task returns None."""
        from duo.protocol import quarantine_task

        result = quarantine_task("does-not-exist", "test")
        assert result is None


# ---------------------------------------------------------------------------
# Watch event flow
# ---------------------------------------------------------------------------


class TestWatchEventFlow:
    """_write_watch_event creates signal files in the watch-events directory."""

    def test_write_watch_event_creates_file(self, tmp_path, monkeypatch):
        watch_dir = tmp_path / "watch-events"
        monkeypatch.setattr("duo.commander._WATCH_EVENTS_DIR", watch_dir)

        task = _task()
        pane_content = "? Do you want to proceed?"

        path = _write_watch_event(task, pane_content)

        assert path.exists()
        assert path.parent == watch_dir
        assert task.id in path.name
        assert path.suffix == ".json"

        data = json.loads(path.read_text())
        assert data["task_id"] == task.id
        assert data["pane_label"] == task.pane_label
        assert data["pane_content"] == pane_content
        assert "detected_at" in data

    def test_watch_event_truncates_long_content(self, tmp_path, monkeypatch):
        watch_dir = tmp_path / "watch-events"
        monkeypatch.setattr("duo.commander._WATCH_EVENTS_DIR", watch_dir)

        task = _task()
        # Content longer than 2000 chars
        long_content = "x" * 5000

        path = _write_watch_event(task, long_content)
        data = json.loads(path.read_text())
        # _write_watch_event truncates to last 2000 chars
        assert len(data["pane_content"]) == 2000

    def test_multiple_watch_events_different_files(self, tmp_path, monkeypatch):
        watch_dir = tmp_path / "watch-events"
        monkeypatch.setattr("duo.commander._WATCH_EVENTS_DIR", watch_dir)

        task = _task()
        p1 = _write_watch_event(task, "event1")
        p2 = _write_watch_event(task, "event2")

        assert p1 != p2
        assert len(list(watch_dir.iterdir())) >= 2


# ---------------------------------------------------------------------------
# Journal replay
# ---------------------------------------------------------------------------


class TestJournalReplay:
    """replay_state reconstructs FSM state from the journal."""

    def test_empty_journal_returns_created(self):
        task = _task()
        # Wipe the journal (create_task wrote to it)
        task.journal_path.write_text("")
        assert replay_state(task) == TaskStatus.CREATED

    def test_replay_matches_transitions(self):
        task = _task()
        _advance(task, TaskStatus.SESSION_STARTING, TaskStatus.PROMPT_SENT)
        assert replay_state(task) == TaskStatus.PROMPT_SENT

    def test_replay_through_correction_cycle(self):
        task = _task()
        _advance(task, TaskStatus.SESSION_STARTING, TaskStatus.PROMPT_SENT)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)
        transition(task, TaskStatus.VERIFYING)
        transition(task, TaskStatus.CORRECTING)
        transition(task, TaskStatus.PROMPT_SENT)
        assert replay_state(task) == TaskStatus.PROMPT_SENT

    def test_replay_completed(self):
        task = _task()
        _advance(task, TaskStatus.SESSION_STARTING, TaskStatus.PROMPT_SENT)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)
        transition(task, TaskStatus.VERIFYING)
        transition(task, TaskStatus.COMPLETED)
        assert replay_state(task) == TaskStatus.COMPLETED

    def test_replay_ignores_non_status_events(self):
        task = _task()
        _advance(task, TaskStatus.SESSION_STARTING)
        # Append custom events — they shouldn't affect replay
        append_event(task, "custom_event", {"info": "test"})
        append_event(task, "heartbeat_received", {"step": 1})
        assert replay_state(task) == TaskStatus.SESSION_STARTING


# ---------------------------------------------------------------------------
# FSM guard — invalid transitions
# ---------------------------------------------------------------------------


class TestFSMGuards:
    """Invalid transitions are logged but don't mutate state."""

    def test_invalid_transition_noop(self):
        task = _task()
        # CREATED → COMPLETED is not allowed
        transition(task, TaskStatus.COMPLETED)
        assert task.status == TaskStatus.CREATED

        # Journal records the invalid attempt
        events = read_jsonl(task.journal_path)
        invalid = [e for e in events if e["event"] == "invalid_transition"]
        assert len(invalid) == 1
        assert invalid[0]["data"]["from"] == "created"
        assert invalid[0]["data"]["to"] == "completed"

    def test_completed_is_terminal(self):
        task = _task()
        _advance(
            task,
            TaskStatus.SESSION_STARTING,
            TaskStatus.PROMPT_SENT,
            TaskStatus.RUNNING,
            TaskStatus.RESULT_REPORTED,
            TaskStatus.VERIFYING,
            TaskStatus.COMPLETED,
        )
        # No transitions from COMPLETED
        transition(task, TaskStatus.PROMPT_SENT)
        assert task.status == TaskStatus.COMPLETED

    def test_transition_chain_integrity(self):
        """A full valid FSM chain works without error."""
        task = _task()
        _advance(
            task,
            TaskStatus.SESSION_STARTING,
            TaskStatus.PROMPT_SENT,
            TaskStatus.ACKED,
            TaskStatus.RUNNING,
            TaskStatus.RESULT_REPORTED,
            TaskStatus.VERIFYING,
            TaskStatus.COMPLETED,
        )
        assert task.status == TaskStatus.COMPLETED

        events = read_jsonl(task.journal_path)
        status_events = [e for e in events if e["event"] == "status_changed"]
        assert len(status_events) == 7


# ---------------------------------------------------------------------------
# Append event and journal operations
# ---------------------------------------------------------------------------


class TestAppendEvent:
    """append_event writes to the journal correctly."""

    def test_append_event_creates_journal(self):
        task = _task()
        # Journal exists from create_task
        assert task.journal_path.exists()
        events = read_jsonl(task.journal_path)
        assert len(events) >= 1  # at least task_created

    def test_append_custom_event(self):
        task = _task()
        append_event(task, "custom", {"key": "value"})
        events = read_jsonl(task.journal_path)
        custom = [e for e in events if e["event"] == "custom"]
        assert len(custom) == 1
        assert custom[0]["data"]["key"] == "value"
        assert "ts" in custom[0]

    def test_multiple_appends_are_ordered(self):
        task = _task()
        for i in range(5):
            append_event(task, f"event_{i}", {"idx": i})
        events = read_jsonl(task.journal_path)
        custom = [e for e in events if e["event"].startswith("event_")]
        assert len(custom) == 5
        for i, ev in enumerate(custom):
            assert ev["data"]["idx"] == i


# ---------------------------------------------------------------------------
# File protocol readers — missing files
# ---------------------------------------------------------------------------


class TestMissingFiles:
    """Readers return None for missing files."""

    def test_read_ack_missing(self):
        task = _task()
        assert read_ack_for_step(task, 1, 1) is None

    def test_read_result_missing(self):
        task = _task()
        assert read_result_for_step(task, 1, 1) is None

    def test_read_heartbeat_missing(self):
        task = _task()
        assert read_heartbeat(task) is None

    def test_no_result_verify_noop(self):
        """verify_and_advance with no result file is a no-op."""
        task = _task()
        _advance(task, TaskStatus.SESSION_STARTING, TaskStatus.PROMPT_SENT)
        # Don't write a result
        with patch("duo.commander.verify_step") as mock_verify:
            verify_and_advance(task)
            mock_verify.assert_not_called()
        assert task.status == TaskStatus.PROMPT_SENT


# ---------------------------------------------------------------------------
# Task listing and persistence
# ---------------------------------------------------------------------------


class TestTaskListing:
    """list_tasks returns valid tasks and quarantines broken ones."""

    def test_list_tasks_empty(self):
        assert list_tasks() == []

    def test_list_tasks_returns_created(self):
        _task(task_id="t1")
        _task(task_id="t2")
        tasks = list_tasks()
        ids = {t.id for t in tasks}
        assert ids == {"t1", "t2"}

    def test_list_tasks_after_transitions(self):
        task = _task()
        _advance(task, TaskStatus.SESSION_STARTING, TaskStatus.PROMPT_SENT)
        tasks = list_tasks()
        assert len(tasks) == 1
        assert tasks[0].status == TaskStatus.PROMPT_SENT

    def test_save_and_reload_preserves_state(self):
        task = _task()
        _advance(task, TaskStatus.SESSION_STARTING, TaskStatus.PROMPT_SENT)
        task.current_step = 2
        task.current_attempt = 3
        save_task(task)

        loaded = load_task(task.id)
        assert loaded is not None
        assert loaded.status == TaskStatus.PROMPT_SENT
        assert loaded.current_step == 2
        assert loaded.current_attempt == 3


# ---------------------------------------------------------------------------
# CEO command family — E2E scenarios
# ---------------------------------------------------------------------------


class TestCeoE2EScenarios:
    """E2E tests for the ceo-* CLI commands using CliRunner."""

    def _make_task(self) -> protocol.Task:
        return _task(task_id="ceo-e2e")

    def test_ceo_wait_then_approve_flow(self) -> None:
        """Simulate: wait → dialog detected → approve → task continues."""
        from unittest.mock import patch as _patch

        from click.testing import CliRunner

        from duo.cli import main

        task = self._make_task()
        runner = CliRunner()

        # Step 1: ceo-wait finds dialog
        with _patch("duo.transport.is_process_alive", return_value=True), \
             _patch("duo.transport.wait_for_dialog", return_value=True), \
             _patch("duo.transport.read_pane", return_value="╭─ Permission ─╮\n│ 1. Yes\n│ 2. No\n╰─"), \
             _patch("duo.commander._write_watch_event"):
            result = runner.invoke(main, ["ceo-wait", task.id])
        assert result.exit_code == 0
        assert "Permission" in result.output

        # Step 2: ceo-status reports dialog
        with _patch("duo.transport.is_process_alive", return_value=True), \
             _patch("duo.transport.read_pane", return_value="╭─ Permission ─╮\n│ 1. Yes\n│ 2. No\n╰─"), \
             _patch("duo.transport.is_in_dialog", return_value=True):
            result = runner.invoke(main, ["ceo-status", task.id])
        assert result.exit_code == 0
        import json
        data = json.loads(result.output)
        assert data["state"] == "dialog"

        # Step 3: ceo-approve
        with _patch("duo.transport.is_permission_dialog", return_value=True), \
             _patch("duo.transport.approve_permission"):
            result = runner.invoke(main, ["ceo-approve", task.id])
        assert result.exit_code == 0
        assert "Approved" in result.output

    def test_ceo_select_other_flow(self) -> None:
        """Simulate: dialog → select --other custom text."""
        from unittest.mock import patch as _patch

        from click.testing import CliRunner

        from duo.cli import main

        task = self._make_task()
        runner = CliRunner()

        with _patch("duo.transport.is_in_dialog_stable", return_value=True), \
             _patch("duo.transport.select_other_option"):
            result = runner.invoke(main, ["ceo-select", task.id, "--other", "custom answer"])
        assert result.exit_code == 0
        assert "Other" in result.output

    def test_ceo_status_dead_then_cleanup(self) -> None:
        """Simulate: pane dead → status reports dead."""
        from unittest.mock import patch as _patch

        from click.testing import CliRunner

        from duo.cli import main

        task = self._make_task()
        runner = CliRunner()

        with _patch("duo.transport.is_process_alive", return_value=False):
            result = runner.invoke(main, ["ceo-status", task.id])
        assert result.exit_code == 0
        import json
        assert json.loads(result.output)["state"] == "dead"

    def test_ceo_approve_rejects_ask_user_dialog(self) -> None:
        """ceo-approve refuses non-permission dialogs."""
        from unittest.mock import patch as _patch

        from click.testing import CliRunner

        from duo.cli import main

        task = self._make_task()
        runner = CliRunner()

        with _patch("duo.transport.is_permission_dialog", return_value=False):
            result = runner.invoke(main, ["ceo-approve", task.id])
        assert result.exit_code != 0
        assert "not showing a permission dialog" in result.output

    def test_ceo_wait_timeout(self) -> None:
        """ceo-wait exits 1 on timeout."""
        from unittest.mock import patch as _patch

        from click.testing import CliRunner

        from duo.cli import main

        task = self._make_task()
        runner = CliRunner()

        with _patch("duo.transport.is_process_alive", return_value=True), \
             _patch("duo.transport.wait_for_dialog", return_value=False):
            result = runner.invoke(main, ["ceo-wait", task.id, "--timeout", "1"])
        assert result.exit_code != 0
        assert "Timeout" in result.output
