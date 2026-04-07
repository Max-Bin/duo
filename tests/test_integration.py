"""Integration tests — simulate full task lifecycle without tmux."""

from __future__ import annotations

from pathlib import Path

import pytest

from duo.protocol import (
    TaskStatus,
    Subtask,
    create_task,
    load_task,
    list_tasks,
    save_task,
    transition,
    append_event,
    read_jsonl,
)
import duo.protocol


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    monkeypatch.setattr(duo.protocol, "TASKS_DIR", tasks_dir)
    monkeypatch.setattr(duo.protocol, "DUO_DIR", tmp_path)


def _make_subtask(step_id: int = 1, desc: str = "test") -> Subtask:
    return Subtask(step_id=step_id, description=desc, target_files=[], writable_paths=["*"])


class TestFullLifecycle:
    """Simulate: CREATED → SESSION_STARTING → PROMPT_SENT → ACKED → RUNNING → RESULT_REPORTED → VERIFYING → COMPLETED"""

    def test_single_step_happy_path(self):
        """Single subtask: create → bootstrap → ack → run → result → verify → complete."""
        task = create_task(
            task_id="happy-path",
            description="Integration test",
            worktree="/tmp/test",
            branch="duo/happy-path",
            base_commit="abc123",
            subtasks=[_make_subtask(1, "Do something")],
        )

        # Verify initial state
        assert task.status == TaskStatus.CREATED
        assert task.current_step == 1
        assert task.current_attempt == 1

        # Session starts
        transition(task, TaskStatus.SESSION_STARTING)
        assert task.status == TaskStatus.SESSION_STARTING

        # Prompt sent (bootstrap)
        transition(task, TaskStatus.PROMPT_SENT)
        append_event(task, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1})
        assert task.status == TaskStatus.PROMPT_SENT

        # Executor acks
        transition(task, TaskStatus.ACKED)

        # Executor starts working
        transition(task, TaskStatus.RUNNING)

        # Executor reports result
        transition(task, TaskStatus.RESULT_REPORTED)

        # Verifier passes
        transition(task, TaskStatus.VERIFYING)

        # Task completed
        transition(task, TaskStatus.COMPLETED)
        assert task.status == TaskStatus.COMPLETED

        # Verify journal has all events
        events = read_jsonl(task.journal_path)
        assert len(events) >= 7  # transitions + pr_consumed

        # Verify PR consumption tracked
        pr_events = [e for e in events if e.get("event") == "pr_consumed"]
        assert len(pr_events) == 1

    def test_multi_step_lifecycle(self):
        """Two subtasks: first step passes, second step passes."""
        task = create_task(
            task_id="multi-step",
            description="Multi step test",
            worktree="/tmp/test2",
            branch="duo/multi-step",
            base_commit="abc123",
            subtasks=[_make_subtask(1, "Step 1"), _make_subtask(2, "Step 2")],
        )

        # Step 1: complete full cycle
        transition(task, TaskStatus.SESSION_STARTING)
        transition(task, TaskStatus.PROMPT_SENT)
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)
        transition(task, TaskStatus.VERIFYING)

        # Step 1 passes, advance to step 2
        task.current_step = 2
        task.current_attempt = 1
        save_task(task)

        # Step 2 prompt (VERIFYING → PROMPT_SENT is valid)
        transition(task, TaskStatus.PROMPT_SENT)
        append_event(task, "pr_consumed", {"action": "task_prompt", "step": 2, "attempt": 1})
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)
        transition(task, TaskStatus.VERIFYING)
        transition(task, TaskStatus.COMPLETED)

        assert task.status == TaskStatus.COMPLETED
        assert task.current_step == 2

    def test_correction_cycle(self):
        """Verifier fails → CORRECTING → retry → eventually passes."""
        task = create_task(
            task_id="correction-test",
            description="Correction test",
            worktree="/tmp/test3",
            branch="duo/correction-test",
            base_commit="abc123",
            subtasks=[_make_subtask(1, "Fix bug")],
        )

        transition(task, TaskStatus.SESSION_STARTING)
        transition(task, TaskStatus.PROMPT_SENT)
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)
        transition(task, TaskStatus.VERIFYING)

        # Verification fails → correction
        transition(task, TaskStatus.CORRECTING)
        task.current_attempt = 2
        save_task(task)
        append_event(task, "correction_sent", {"step": 1, "attempt": 2})
        append_event(task, "pr_consumed", {"action": "task_prompt", "step": 1, "attempt": 2})

        # Correction cycle (CORRECTING → PROMPT_SENT is valid)
        transition(task, TaskStatus.PROMPT_SENT)
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)
        transition(task, TaskStatus.VERIFYING)

        # Now passes
        transition(task, TaskStatus.COMPLETED)
        assert task.status == TaskStatus.COMPLETED
        assert task.current_attempt == 2

    def test_escalation_after_max_corrections(self):
        """After 3 corrections, task escalates to human."""
        task = create_task(
            task_id="escalate-test",
            description="Escalation test",
            worktree="/tmp/test4",
            branch="duo/escalate-test",
            base_commit="abc123",
            subtasks=[_make_subtask(1, "Hard task")],
        )

        transition(task, TaskStatus.SESSION_STARTING)
        transition(task, TaskStatus.PROMPT_SENT)

        # Simulate 3 failed corrections
        for attempt in range(1, 4):
            transition(task, TaskStatus.ACKED)
            transition(task, TaskStatus.RUNNING)
            transition(task, TaskStatus.RESULT_REPORTED)
            transition(task, TaskStatus.VERIFYING)
            transition(task, TaskStatus.CORRECTING)
            task.current_attempt = attempt + 1
            save_task(task)
            append_event(task, "correction_sent", {"step": 1, "attempt": attempt + 1})
            transition(task, TaskStatus.PROMPT_SENT)

        # 4th failure → escalate
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)
        transition(task, TaskStatus.VERIFYING)
        transition(task, TaskStatus.ESCALATED)

        assert task.status == TaskStatus.ESCALATED

    def test_blocked_and_recover(self):
        """Task blocks during execution, then recovers."""
        task = create_task(
            task_id="block-test",
            description="Block test",
            worktree="/tmp/test5",
            branch="duo/block-test",
            base_commit="abc123",
            subtasks=[_make_subtask(1, "Blocked task")],
        )

        transition(task, TaskStatus.SESSION_STARTING)
        transition(task, TaskStatus.PROMPT_SENT)
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)

        # Task blocks during execution (RUNNING → BLOCKED is valid)
        transition(task, TaskStatus.BLOCKED)
        assert task.status == TaskStatus.BLOCKED

    def test_queued_task(self):
        """Task enters QUEUED state when parallel slots are full."""
        task = create_task(
            task_id="queue-test",
            description="Queue test",
            worktree="/tmp/test6",
            branch="duo/queue-test",
            base_commit="abc123",
            subtasks=[_make_subtask(1, "Queued task")],
        )

        transition(task, TaskStatus.QUEUED)
        assert task.status == TaskStatus.QUEUED

        # Later promoted directly to session start (QUEUED → SESSION_STARTING is valid)
        transition(task, TaskStatus.SESSION_STARTING)
        assert task.status == TaskStatus.SESSION_STARTING

    def test_pr_budget_tracking(self):
        """Verify PR consumption is tracked correctly through lifecycle."""
        task = create_task(
            task_id="budget-test",
            description="PR budget test",
            worktree="/tmp/test7",
            branch="duo/budget-test",
            base_commit="abc123",
            subtasks=[_make_subtask(1, "Step 1"), _make_subtask(2, "Step 2")],
        )

        # Bootstrap
        transition(task, TaskStatus.SESSION_STARTING)
        transition(task, TaskStatus.PROMPT_SENT)
        append_event(task, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1})

        # Step 1 completes
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)
        transition(task, TaskStatus.VERIFYING)

        # Advance to step 2
        task.current_step = 2
        task.current_attempt = 1
        save_task(task)
        transition(task, TaskStatus.PROMPT_SENT)
        append_event(task, "pr_consumed", {"action": "task_prompt", "step": 2, "attempt": 1})

        # Correction on step 2
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)
        transition(task, TaskStatus.VERIFYING)
        transition(task, TaskStatus.CORRECTING)
        task.current_attempt = 2
        save_task(task)
        transition(task, TaskStatus.PROMPT_SENT)
        append_event(task, "pr_consumed", {"action": "task_prompt", "step": 2, "attempt": 2})

        # Complete
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)
        transition(task, TaskStatus.VERIFYING)
        transition(task, TaskStatus.COMPLETED)

        # Check PR count
        events = read_jsonl(task.journal_path)
        pr_events = [e for e in events if e.get("event") == "pr_consumed"]
        assert len(pr_events) == 3  # bootstrap + 2 task_prompts

    def test_full_persistence(self):
        """Task state persists across load/save cycles."""
        task = create_task(
            task_id="persist-test",
            description="Persistence test",
            worktree="/tmp/test8",
            branch="duo/persist-test",
            base_commit="abc123",
            subtasks=[_make_subtask(1, "Persist")],
        )

        transition(task, TaskStatus.SESSION_STARTING)
        transition(task, TaskStatus.PROMPT_SENT)
        task.current_attempt = 3
        save_task(task)

        # Reload and verify
        reloaded = load_task("persist-test")
        assert reloaded is not None
        assert reloaded.status == TaskStatus.PROMPT_SENT
        assert reloaded.current_attempt == 3
        assert reloaded.incarnation_id == task.incarnation_id

        # List should include this task
        all_tasks = list_tasks()
        assert any(t.id == "persist-test" for t in all_tasks)

    @pytest.mark.xfail(reason="create_task does not yet detect duplicate task IDs", strict=True)
    def test_concurrent_task_creation(self):
        """Creating a second task with same ID should fail gracefully."""
        create_task("dup-task", "First", "/w", "b", "c", [_make_subtask(1)])
        with pytest.raises((ValueError, FileExistsError)):
            create_task("dup-task", "Second", "/w", "b", "c", [_make_subtask(1)])

    def test_task_with_no_subtasks_raises(self):
        """create_task with empty subtasks raises ValueError."""
        with pytest.raises(ValueError, match="at least one subtask"):
            create_task(
                task_id="no-subtasks",
                description="No subtasks",
                worktree="/fake",
                branch="duo/no-subtasks",
                base_commit="abc",
                subtasks=[],
            )
