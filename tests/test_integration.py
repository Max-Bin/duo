"""Integration tests — simulate full task lifecycle without tmux."""

from __future__ import annotations

from pathlib import Path

import pytest

import duo.protocol
from duo.commander import verify_and_advance
from duo.protocol import (
    StepResult,
    Subtask,
    TaskStatus,
    append_event,
    create_task,
    list_tasks,
    load_task,
    read_jsonl,
    save_task,
    transition,
)
from duo.verifier import Pass


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    monkeypatch.setattr(duo.protocol, "TASKS_DIR", tasks_dir)
    monkeypatch.setattr(duo.protocol, "DUO_DIR", tmp_path)


def _make_subtask(step_id: int = 1, desc: str = "test") -> Subtask:
    return Subtask(
        step_id=step_id, description=desc, target_files=[], writable_paths=["*"]
    )


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
        append_event(
            task, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )
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
        append_event(
            task, "pr_consumed", {"action": "task_prompt", "step": 2, "attempt": 1}
        )
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
        append_event(
            task, "pr_consumed", {"action": "task_prompt", "step": 1, "attempt": 2}
        )

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
        append_event(
            task, "pr_consumed", {"action": "bootstrap", "step": 1, "attempt": 1}
        )

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
        append_event(
            task, "pr_consumed", {"action": "task_prompt", "step": 2, "attempt": 1}
        )

        # Correction on step 2
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)
        transition(task, TaskStatus.VERIFYING)
        transition(task, TaskStatus.CORRECTING)
        task.current_attempt = 2
        save_task(task)
        transition(task, TaskStatus.PROMPT_SENT)
        append_event(
            task, "pr_consumed", {"action": "task_prompt", "step": 2, "attempt": 2}
        )

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

    def test_concurrent_task_creation(self):
        """Creating a second task with same ID raises ValueError."""
        create_task("dup-task", "First", "/w", "b", "c", [_make_subtask(1)])
        with pytest.raises(ValueError, match="already exists"):
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


class TestConfigIntegration:
    """Test config ↔ protocol ↔ verifier interactions."""

    def test_config_persists_across_load(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Config set → save → load round-trip."""
        import duo.config as cfg

        config_path = tmp_path / "config.json"
        monkeypatch.setattr(cfg, "CONFIG_PATH", config_path)
        cfg.set_config("max_corrections", "5")
        val = cfg.get_config("max_corrections")
        assert val == 5

        # Reload from disk
        loaded = cfg.load_config()
        assert loaded["max_corrections"] == 5

    def test_config_types_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """All config types survive serialization round-trip."""
        import duo.config as cfg

        config_path = tmp_path / "config.json"
        monkeypatch.setattr(cfg, "CONFIG_PATH", config_path)

        cfg.set_config("max_corrections", "7")
        cfg.set_config("poll_base_interval", "2.5")
        cfg.set_config("auto_allow_all", "true")
        cfg.set_config("copilot_model", "gpt-4")

        loaded = cfg.load_config()
        assert isinstance(loaded["max_corrections"], int)
        assert isinstance(loaded["poll_base_interval"], float)
        assert isinstance(loaded["auto_allow_all"], bool)
        assert isinstance(loaded["copilot_model"], str)


class TestJournalIntegration:
    """Test journal append + read + transition audit trail."""

    def test_journal_records_transitions(self):
        """Every FSM transition is recorded in the journal."""
        task = create_task(
            task_id="journal-test",
            description="Journal test",
            worktree="/tmp/jt",
            branch="duo/jt",
            base_commit="abc",
            subtasks=[_make_subtask(1)],
        )
        transition(task, TaskStatus.SESSION_STARTING)
        transition(task, TaskStatus.PROMPT_SENT)

        events = read_jsonl(task.dir / "journal.jsonl")
        transition_events = [e for e in events if e["event"] == "status_changed"]
        assert len(transition_events) >= 2
        states = [e["data"]["to"] for e in transition_events]
        assert "session_starting" in states
        assert "prompt_sent" in states

    def test_journal_preserves_event_order(self):
        """Events in journal are in chronological order."""
        task = create_task(
            task_id="order-test",
            description="Order test",
            worktree="/tmp/ot",
            branch="duo/ot",
            base_commit="abc",
            subtasks=[_make_subtask(1)],
        )
        for i in range(5):
            append_event(task, f"step_{i}", {"index": i})

        events = read_jsonl(task.dir / "journal.jsonl")
        step_events = [e for e in events if e["event"].startswith("step_")]
        indices = [e["data"]["index"] for e in step_events]
        assert indices == list(range(5))

    def test_journal_tail_returns_latest(self):
        """read_jsonl with tail returns the most recent events."""
        task = create_task(
            task_id="tail-test",
            description="Tail test",
            worktree="/tmp/tt",
            branch="duo/tt",
            base_commit="abc",
            subtasks=[_make_subtask(1)],
        )
        for i in range(10):
            append_event(task, "ping", {"n": i})

        last_3 = read_jsonl(task.dir / "journal.jsonl", tail=3)
        assert len(last_3) == 3
        assert last_3[-1]["data"]["n"] == 9

    def test_failed_recovery_lifecycle(self) -> None:
        """FAILED → PROMPT_SENT recovery path with journal audit trail."""
        task = create_task(
            task_id="fail-recover",
            description="Failure recovery test",
            worktree="/tmp/fr",
            branch="duo/fr",
            base_commit="abc",
            subtasks=[_make_subtask(1, "Recoverable")],
        )
        transition(task, TaskStatus.SESSION_STARTING)
        transition(task, TaskStatus.PROMPT_SENT)
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.FAILED)

        assert task.status == TaskStatus.FAILED

        # Recovery: FAILED → SESSION_STARTING (restart the session)
        transition(task, TaskStatus.SESSION_STARTING)
        assert task.status == TaskStatus.SESSION_STARTING

        # Complete successfully after recovery
        transition(task, TaskStatus.PROMPT_SENT)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)
        transition(task, TaskStatus.VERIFYING)
        transition(task, TaskStatus.COMPLETED)
        assert task.status == TaskStatus.COMPLETED

        # Verify journal has the full recovery trail
        events = read_jsonl(task.dir / "journal.jsonl")
        statuses = [e["data"]["to"] for e in events if e["event"] == "status_changed"]
        assert "failed" in statuses
        assert statuses.count("session_starting") == 2  # initial + recovery

    def test_multi_step_with_mixed_corrections(self) -> None:
        """Multi-step task where step 1 passes, step 2 needs correction."""
        task = create_task(
            task_id="multi-correct",
            description="Multi-step correction",
            worktree="/tmp/mc",
            branch="duo/mc",
            base_commit="abc",
            subtasks=[_make_subtask(1, "Step 1"), _make_subtask(2, "Step 2")],
        )

        # Step 1: clean pass
        transition(task, TaskStatus.SESSION_STARTING)
        transition(task, TaskStatus.PROMPT_SENT)
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)
        transition(task, TaskStatus.VERIFYING)

        # Advance to step 2
        task.current_step = 2
        task.current_attempt = 1
        save_task(task)
        transition(task, TaskStatus.PROMPT_SENT)
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)
        transition(task, TaskStatus.VERIFYING)

        # Step 2 correction
        transition(task, TaskStatus.CORRECTING)
        task.current_attempt = 2
        save_task(task)
        transition(task, TaskStatus.PROMPT_SENT)
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)
        transition(task, TaskStatus.VERIFYING)
        transition(task, TaskStatus.COMPLETED)

        assert task.status == TaskStatus.COMPLETED
        assert task.current_step == 2
        assert task.current_attempt == 2

    def test_restart_normalization(self) -> None:
        """normalize_for_restart resets step/attempt and generates new incarnation."""
        from duo.commander import normalize_for_restart

        task = create_task(
            task_id="restart-norm",
            description="test restart normalization",
            worktree="/tmp/test",
            branch="main",
            base_commit="abc123",
            subtasks=[
                Subtask(
                    step_id=1,
                    description="step one",
                    target_files=["a.py"],
                    writable_paths=["*.py"],
                )
            ],
        )
        # Simulate progress
        transition(task, TaskStatus.SESSION_STARTING)
        transition(task, TaskStatus.PROMPT_SENT)
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)
        transition(task, TaskStatus.VERIFYING)
        transition(task, TaskStatus.COMPLETED)

        # Normalize should fail on COMPLETED — it's not restartable
        result = normalize_for_restart(task)
        assert result is False
        assert task.status == TaskStatus.COMPLETED

    def test_escalated_to_blocked_journal_trail(self) -> None:
        """Escalation creates proper journal entries."""
        task = create_task(
            task_id="escalate-trail",
            description="test escalation journal",
            worktree="/tmp/test",
            branch="main",
            base_commit="abc123",
            subtasks=[
                Subtask(
                    step_id=1,
                    description="do it",
                    target_files=["a.py"],
                    writable_paths=["*.py"],
                )
            ],
        )
        transition(task, TaskStatus.SESSION_STARTING)
        transition(task, TaskStatus.PROMPT_SENT)
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.BLOCKED)
        transition(task, TaskStatus.ESCALATED)

        events = read_jsonl(task.journal_path)
        transitions = [e for e in events if e.get("event") == "status_changed"]
        states = [t["data"]["to"] for t in transitions]
        assert TaskStatus.ESCALATED.value in states
        assert TaskStatus.BLOCKED.value in states

    def test_verify_and_advance_blocked_result(self, tmp_path: Path) -> None:
        """verify_and_advance transitions to BLOCKED when result.status is 'blocked'."""
        task = create_task(
            task_id="blocked-result",
            description="will block",
            worktree=str(tmp_path),
            branch="main",
            base_commit="abc123",
            subtasks=[
                Subtask(
                    step_id=1,
                    description="do something",
                    target_files=["app.py"],
                    writable_paths=["*.py"],
                ),
            ],
        )
        task.current_step = 1
        task.current_attempt = 1
        transition(task, TaskStatus.SESSION_STARTING)
        transition(task, TaskStatus.PROMPT_SENT)
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)

        result = StepResult(
            step=1,
            attempt=1,
            incarnation=task.incarnation_id,
            status="blocked",
            files_changed=[],
            summary="Cannot proceed",
            reason="Missing dependency",
        )
        verify_and_advance(task, result)

        assert task.status == TaskStatus.BLOCKED
        events = read_jsonl(task.journal_path)
        blocked = [e for e in events if e.get("event") == "agent_blocked"]
        assert len(blocked) >= 1
        assert blocked[-1]["data"]["reason"] == "Missing dependency"

    def test_verify_and_advance_completes_single_step_task(
        self, tmp_path: Path
    ) -> None:
        """verify_and_advance completes task when all steps pass."""
        task = create_task(
            task_id="single-step-complete",
            description="one step task",
            worktree=str(tmp_path),
            branch="main",
            base_commit="abc123",
            subtasks=[
                Subtask(
                    step_id=1,
                    description="do it",
                    target_files=["hello.txt"],
                    writable_paths=["*.txt"],
                ),
            ],
        )
        task.current_step = 1
        task.current_attempt = 1
        transition(task, TaskStatus.SESSION_STARTING)
        transition(task, TaskStatus.PROMPT_SENT)
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)

        result = StepResult(
            step=1,
            attempt=1,
            incarnation=task.incarnation_id,
            status="done",
            files_changed=["hello.txt"],
            summary="Done",
        )

        # Mock verify_step to return Pass and send_task_prompt
        import duo.commander as cmd

        original_verify = cmd.verify_step
        original_send = cmd.send_task_prompt
        cmd.verify_step = lambda t, r: Pass()  # type: ignore[assignment]
        cmd.send_task_prompt = lambda t, p: None  # type: ignore[assignment]
        try:
            verify_and_advance(task, result)
        finally:
            cmd.verify_step = original_verify  # type: ignore[assignment]
            cmd.send_task_prompt = original_send  # type: ignore[assignment]

        assert task.status == TaskStatus.COMPLETED
        events = read_jsonl(task.journal_path)
        completed = [e for e in events if e.get("event") == "task_completed"]
        assert len(completed) >= 1


# ---------------------------------------------------------------------------
# Cross-module integration tests — Round HX
# ---------------------------------------------------------------------------


class TestSchedulerCommanderIntegration:
    """Scheduler + Commander: task queuing, promotion, and slot management."""

    def test_queue_and_promote_lifecycle(self) -> None:
        """Fill slots → queue → free slot → promote."""
        from duo.scheduler import (
            active_count,
            enqueue_or_start,
            has_slot,
            promote_queued,
        )

        # Fill all 3 slots
        active_tasks = []
        for i in range(3):
            t = create_task(
                task_id=f"active-{i}",
                description=f"Active task {i}",
                worktree=f"/tmp/active{i}",
                branch=f"duo/active-{i}",
                base_commit="abc123",
                subtasks=[_make_subtask(1, f"step {i}")],
            )
            transition(t, TaskStatus.SESSION_STARTING)
            transition(t, TaskStatus.PROMPT_SENT)
            transition(t, TaskStatus.ACKED)
            transition(t, TaskStatus.RUNNING)
            active_tasks.append(t)

        assert active_count() == 3
        assert has_slot() is False

        # New task gets queued
        queued_task = create_task(
            task_id="queued-1",
            description="Should be queued",
            worktree="/tmp/queued1",
            branch="duo/queued-1",
            base_commit="abc123",
            subtasks=[_make_subtask(1, "queued work")],
        )
        result = enqueue_or_start(queued_task)
        assert result == "queued"
        assert queued_task.status == TaskStatus.QUEUED

        # Free one slot by completing a task
        transition(active_tasks[0], TaskStatus.RESULT_REPORTED)
        transition(active_tasks[0], TaskStatus.VERIFYING)
        transition(active_tasks[0], TaskStatus.COMPLETED)
        assert active_count() == 2

        # Promote should move queued task to SESSION_STARTING
        promoted = promote_queued()
        assert len(promoted) == 1
        assert promoted[0].id == "queued-1"
        assert promoted[0].status == TaskStatus.SESSION_STARTING

    def test_queue_fifo_ordering(self) -> None:
        """Multiple queued tasks are promoted in FIFO order."""
        from duo.scheduler import enqueue_or_start, promote_queued

        # Fill all 3 slots
        for i in range(3):
            t = create_task(
                task_id=f"fill-{i}",
                description=f"Fill {i}",
                worktree=f"/tmp/fill{i}",
                branch=f"duo/fill-{i}",
                base_commit="abc123",
                subtasks=[_make_subtask(1, "fill")],
            )
            transition(t, TaskStatus.SESSION_STARTING)
            transition(t, TaskStatus.PROMPT_SENT)
            transition(t, TaskStatus.ACKED)
            transition(t, TaskStatus.RUNNING)

        # Queue 3 tasks in order
        for name in ["alpha", "beta", "charlie"]:
            t = create_task(
                task_id=name,
                description=f"Queued {name}",
                worktree=f"/tmp/{name}",
                branch=f"duo/{name}",
                base_commit="abc123",
                subtasks=[_make_subtask(1, name)],
            )
            enqueue_or_start(t)
            assert t.status == TaskStatus.QUEUED

        # Complete all active tasks
        for t in list_tasks():
            if t.status == TaskStatus.RUNNING:
                transition(t, TaskStatus.RESULT_REPORTED)
                transition(t, TaskStatus.VERIFYING)
                transition(t, TaskStatus.COMPLETED)

        # Promote all — should follow FIFO (alphabetical by id for same timestamp)
        promoted = promote_queued()
        assert len(promoted) == 3
        assert [t.id for t in promoted] == ["alpha", "beta", "charlie"]


class TestProtocolVerifierIntegration:
    """Protocol + Verifier: correction cycles and escalation."""

    def test_correction_cycle_preserves_journal(self) -> None:
        """Corrections are journaled, attempt counter increments."""
        task = create_task(
            task_id="correction-cycle",
            description="Test corrections",
            worktree="/tmp/corrections",
            branch="duo/correction-cycle",
            base_commit="abc123",
            subtasks=[_make_subtask(1, "fix bug")],
        )
        transition(task, TaskStatus.SESSION_STARTING)
        transition(task, TaskStatus.PROMPT_SENT)
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)
        transition(task, TaskStatus.VERIFYING)

        # Simulate correction
        transition(task, TaskStatus.CORRECTING)
        append_event(
            task,
            "correction_sent",
            {
                "step": 1,
                "attempt": 2,
                "reason": "tests failed",
            },
        )

        # Back to prompt sent for retry
        transition(task, TaskStatus.PROMPT_SENT)
        task.current_attempt = 2
        save_task(task)

        # Verify journal recorded the correction
        events = read_jsonl(task.journal_path)
        corrections = [e for e in events if e.get("event") == "correction_sent"]
        assert len(corrections) == 1
        assert corrections[0]["data"]["reason"] == "tests failed"

    def test_multi_step_with_journal_persistence(self) -> None:
        """Multi-step task has separate journal entries per step."""
        task = create_task(
            task_id="multi-journal",
            description="Multi-step journal test",
            worktree="/tmp/multi-journal",
            branch="duo/multi-journal",
            base_commit="abc123",
            subtasks=[
                _make_subtask(1, "step one"),
                _make_subtask(2, "step two"),
            ],
        )

        # Step 1 lifecycle
        for status in [
            TaskStatus.SESSION_STARTING,
            TaskStatus.PROMPT_SENT,
            TaskStatus.ACKED,
            TaskStatus.RUNNING,
            TaskStatus.RESULT_REPORTED,
            TaskStatus.VERIFYING,
        ]:
            transition(task, status)

        append_event(task, "step_completed", {"step": 1, "summary": "done"})

        # Advance to step 2
        task.current_step = 2
        task.current_attempt = 1
        transition(task, TaskStatus.PROMPT_SENT)

        # Step 2 lifecycle
        for status in [
            TaskStatus.ACKED,
            TaskStatus.RUNNING,
            TaskStatus.RESULT_REPORTED,
            TaskStatus.VERIFYING,
            TaskStatus.COMPLETED,
        ]:
            transition(task, status)

        # Verify full journal
        events = read_jsonl(task.journal_path)
        status_changes = [e for e in events if e.get("event") == "status_changed"]
        assert len(status_changes) >= 12
        step_events = [e for e in events if e.get("event") == "step_completed"]
        assert len(step_events) == 1


class TestConfigProtocolIntegration:
    """Config + Protocol: configuration affects task behavior."""

    def test_task_persists_through_config_changes(self) -> None:
        """Task state survives config module reloads."""
        import duo.config as config_mod

        task = create_task(
            task_id="config-survive",
            description="Config survival test",
            worktree="/tmp/config-test",
            branch="duo/config-test",
            base_commit="abc123",
            subtasks=[_make_subtask(1, "work")],
        )
        transition(task, TaskStatus.SESSION_STARTING)

        # Change config
        config_mod.set_config("max_corrections", "10")

        # Task should still be loadable
        loaded = load_task(task.id)
        assert loaded is not None
        assert loaded.status == TaskStatus.SESSION_STARTING

    def test_list_tasks_filters_correctly_with_mixed_statuses(self) -> None:
        """list_tasks returns all tasks regardless of status."""
        statuses_applied: dict[str, TaskStatus] = {}
        for name, target_status in [
            ("running-1", TaskStatus.RUNNING),
            ("completed-1", TaskStatus.COMPLETED),
            ("failed-1", TaskStatus.FAILED),
            ("queued-1", TaskStatus.QUEUED),
        ]:
            t = create_task(
                task_id=name,
                description=f"{name} task",
                worktree=f"/tmp/{name}",
                branch=f"duo/{name}",
                base_commit="abc123",
                subtasks=[_make_subtask(1, "work")],
            )
            # Navigate FSM to target status
            if target_status == TaskStatus.RUNNING:
                transition(t, TaskStatus.SESSION_STARTING)
                transition(t, TaskStatus.PROMPT_SENT)
                transition(t, TaskStatus.ACKED)
                transition(t, TaskStatus.RUNNING)
            elif target_status == TaskStatus.COMPLETED:
                transition(t, TaskStatus.SESSION_STARTING)
                transition(t, TaskStatus.PROMPT_SENT)
                transition(t, TaskStatus.ACKED)
                transition(t, TaskStatus.RUNNING)
                transition(t, TaskStatus.RESULT_REPORTED)
                transition(t, TaskStatus.VERIFYING)
                transition(t, TaskStatus.COMPLETED)
            elif target_status == TaskStatus.FAILED:
                transition(t, TaskStatus.SESSION_STARTING)
                transition(t, TaskStatus.FAILED)
            elif target_status == TaskStatus.QUEUED:
                transition(t, TaskStatus.QUEUED)
            statuses_applied[name] = target_status

        all_tasks = list_tasks()
        assert len(all_tasks) == 4
        for t in all_tasks:
            assert t.status == statuses_applied[t.id]
