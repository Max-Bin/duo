"""Tests for scheduler module — parallel task execution with FIFO queue."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from duo.protocol import (
    Subtask,
    Task,
    TaskStatus,
    create_task,
    save_task,
    transition,
)
from duo.scheduler import (
    ACTIVE_STATUSES,
    _queue_position,
    _sorted_queued,
    active_count,
    enqueue_or_start,
    has_slot,
    max_parallel,
    promote_queued,
    queue_status,
)


def _make_subtask(step_id: int = 1) -> Subtask:
    return Subtask(
        step_id=step_id,
        description=f"step-{step_id}",
        target_files=["main.py"],
        writable_paths=["src/"],
    )


def _make_task(task_id: str = "t1") -> Task:
    return create_task(
        task_id=task_id,
        description="test task",
        worktree="/fake/worktree",
        branch="feat",
        base_commit="abc123",
        subtasks=[_make_subtask()],
    )


def _force_status(task: Task, status: TaskStatus) -> None:
    """Force a task to a given status (bypasses FSM for test setup)."""
    task.status = status
    save_task(task)


class TestActiveCount:
    NON_ACTIVE = {
        TaskStatus.CREATED,
        TaskStatus.QUEUED,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
        TaskStatus.COMPLETED,
        TaskStatus.ESCALATED,
    }

    def test_active_status_counted(self) -> None:
        """Each ACTIVE_STATUSES member increments active_count."""
        for i, status in enumerate(sorted(ACTIVE_STATUSES, key=lambda s: s.value)):
            t = _make_task(f"t1-{i}")
            _force_status(t, status)
            assert active_count() == i + 1, f"status={status}"

    def test_non_active_status_not_counted(self) -> None:
        """Non-active statuses do NOT increment active_count."""
        for i, status in enumerate(sorted(self.NON_ACTIVE, key=lambda s: s.value)):
            t = _make_task(f"t1-{i}")
            _force_status(t, status)
        assert active_count() == 0

    def test_no_tasks(self) -> None:
        assert active_count() == 0

    def test_multiple_active(self) -> None:
        for i, status in enumerate(ACTIVE_STATUSES):
            t = _make_task(f"t{i}")
            _force_status(t, status)
        assert active_count() == len(ACTIVE_STATUSES)

    def test_mixed_statuses(self) -> None:
        t1 = _make_task("t1")
        _force_status(t1, TaskStatus.RUNNING)
        t2 = _make_task("t2")
        _force_status(t2, TaskStatus.COMPLETED)
        t3 = _make_task("t3")
        _force_status(t3, TaskStatus.QUEUED)
        t4 = _make_task("t4")
        _force_status(t4, TaskStatus.PROMPT_SENT)
        assert active_count() == 2


class TestMaxParallel:
    def test_default_is_three(self) -> None:
        assert max_parallel() == 3

    def test_reads_from_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("duo.scheduler.get_config", lambda k: 5)
        assert max_parallel() == 5

    def test_none_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("duo.scheduler.get_config", lambda k: None)
        assert max_parallel() == 3


class TestHasSlot:
    def test_empty_has_slot(self) -> None:
        assert has_slot() is True

    def test_at_limit_no_slot(self) -> None:
        for i in range(3):
            t = _make_task(f"t{i}")
            _force_status(t, TaskStatus.RUNNING)
        assert has_slot() is False

    def test_under_limit_has_slot(self) -> None:
        for i in range(2):
            t = _make_task(f"t{i}")
            _force_status(t, TaskStatus.RUNNING)
        assert has_slot() is True

    def test_custom_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("duo.scheduler.get_config", lambda k: 1)
        t = _make_task("t1")
        _force_status(t, TaskStatus.RUNNING)
        assert has_slot() is False


class TestEnqueueOrStart:
    def test_starts_when_slot_available(self) -> None:
        task = _make_task("t1")
        assert enqueue_or_start(task) == "started"
        # Task should still be CREATED (caller handles start_session)
        assert task.status == TaskStatus.CREATED

    def test_queues_when_full(self) -> None:
        for i in range(3):
            t = _make_task(f"active{i}")
            _force_status(t, TaskStatus.RUNNING)

        task = _make_task("overflow")
        result = enqueue_or_start(task)
        assert result == "queued"
        assert task.status == TaskStatus.QUEUED


class TestPromoteQueued:
    def test_no_queued_tasks(self) -> None:
        assert promote_queued() == []

    def test_promotes_when_slot_opens(self) -> None:
        t1 = _make_task("t1")
        _force_status(t1, TaskStatus.QUEUED)

        promoted = promote_queued()
        assert len(promoted) == 1
        assert promoted[0].id == "t1"

    def test_promotes_fifo_order(self) -> None:
        import time

        t1 = _make_task("first")
        _force_status(t1, TaskStatus.QUEUED)
        time.sleep(0.01)
        t2 = _make_task("second")
        _force_status(t2, TaskStatus.QUEUED)
        time.sleep(0.01)
        t3 = _make_task("third")
        _force_status(t3, TaskStatus.QUEUED)

        promoted = promote_queued()
        assert len(promoted) == 3
        assert promoted[0].id == "first"
        assert promoted[1].id == "second"
        assert promoted[2].id == "third"

    def test_respects_max_parallel(self) -> None:
        # Fill 2 slots
        for i in range(2):
            t = _make_task(f"active{i}")
            _force_status(t, TaskStatus.RUNNING)

        # Queue 3 tasks
        import time

        for i in range(3):
            t = _make_task(f"queued{i}")
            _force_status(t, TaskStatus.QUEUED)
            time.sleep(0.01)

        # Only 1 slot available (max=3, 2 active)
        promoted = promote_queued()
        assert len(promoted) == 1
        assert promoted[0].id == "queued0"

    def test_promote_queued_respects_custom_max_parallel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """promote_queued only promotes up to max_parallel - active_count."""
        import time

        monkeypatch.setattr("duo.scheduler.get_config", lambda k: 4)
        # Create 3 active tasks
        for i in range(3):
            t = _make_task(f"active-{i}")
            _force_status(t, TaskStatus.RUNNING)
        # Queue 5 tasks
        for i in range(5):
            t = _make_task(f"queued-{i}")
            _force_status(t, TaskStatus.QUEUED)
            time.sleep(0.01)
        # Only 1 slot available (max=4, 3 active)
        promoted = promote_queued()
        assert len(promoted) == 1

    def test_promote_queued_zero_max_parallel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """max_parallel=0 blocks all promotion."""
        monkeypatch.setattr(
            "duo.scheduler.get_config",
            lambda k: 0 if k == "max_parallel" else None,
        )
        t = _make_task("blocked")
        _force_status(t, TaskStatus.QUEUED)

        promoted = promote_queued()
        assert promoted == []
        # Task must remain QUEUED
        from duo.protocol import load_task

        reloaded = load_task("blocked")
        assert reloaded is not None
        assert reloaded.status == TaskStatus.QUEUED

    def test_promote_queued_unlimited_when_high_max(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With a very high max_parallel, all queued tasks are promoted."""
        import time

        monkeypatch.setattr("duo.scheduler.get_config", lambda k: 1000)
        for i in range(3):
            t = _make_task(f"unlim-{i}")
            _force_status(t, TaskStatus.QUEUED)
            time.sleep(0.01)
        promoted = promote_queued()
        assert len(promoted) == 3


class TestQueuePosition:
    def test_position(self) -> None:
        import time

        t1 = _make_task("first")
        _force_status(t1, TaskStatus.QUEUED)
        time.sleep(0.01)
        t2 = _make_task("second")
        _force_status(t2, TaskStatus.QUEUED)

        assert _queue_position(t1) == 1
        assert _queue_position(t2) == 2

    def test_not_in_queue(self) -> None:
        t = _make_task("t1")
        # Task not queued — returns len+1
        pos = _queue_position(t)
        assert pos == 1  # empty queue + 1


class TestQueueStatus:
    def test_empty(self) -> None:
        qs = queue_status()
        assert qs["active_count"] == 0
        assert qs["queued_count"] == 0
        assert qs["max_parallel"] == 3
        assert qs["active_tasks"] == []
        assert qs["queued_tasks"] == []

    def test_mixed(self) -> None:
        t1 = _make_task("active1")
        _force_status(t1, TaskStatus.RUNNING)
        t2 = _make_task("queued1")
        _force_status(t2, TaskStatus.QUEUED)
        t3 = _make_task("done1")
        _force_status(t3, TaskStatus.COMPLETED)

        qs = queue_status()
        assert qs["active_count"] == 1
        assert qs["queued_count"] == 1
        assert "active1" in qs["active_tasks"]
        assert "queued1" in qs["queued_tasks"]


# === Edge-case tests ===


class TestSchedulerEdgeCases:
    """Edge cases for scheduler robustness."""

    def test_promote_after_multiple_failures(self) -> None:
        """When multiple active tasks fail, all freed slots get filled."""
        # Fill up 3 slots
        t1 = _make_task("active1")
        _force_status(t1, TaskStatus.RUNNING)
        t2 = _make_task("active2")
        _force_status(t2, TaskStatus.RUNNING)
        t3 = _make_task("active3")
        _force_status(t3, TaskStatus.RUNNING)
        # Queue 3 more
        t4 = _make_task("queued1")
        _force_status(t4, TaskStatus.QUEUED)
        t5 = _make_task("queued2")
        _force_status(t5, TaskStatus.QUEUED)
        t6 = _make_task("queued3")
        _force_status(t6, TaskStatus.QUEUED)
        # All 3 active tasks fail
        _force_status(t1, TaskStatus.FAILED)
        _force_status(t2, TaskStatus.FAILED)
        _force_status(t3, TaskStatus.FAILED)
        # Promote should fill all 3 freed slots
        promoted = promote_queued()
        assert len(promoted) == 3

    def test_queue_position_deterministic_with_ties(self) -> None:
        """Tasks with the same created_at timestamp have stable queue positions."""
        import time

        now = time.time()
        tasks = []
        for i in range(5):
            t = _make_task(f"tie{i}")
            t.created_at = now  # same timestamp
            save_task(t)
            _force_status(t, TaskStatus.QUEUED)
            tasks.append(t)
        # Verify all have valid, unique positions
        positions = [_queue_position(t) for t in tasks]
        assert sorted(positions) == list(range(1, 6))

    def test_enqueue_or_start_returns_queued_when_full(self) -> None:
        """enqueue_or_start returns 'queued' status when all slots occupied."""
        for i in range(3):
            t = _make_task(f"active{i}")
            _force_status(t, TaskStatus.RUNNING)
        new_task = _make_task("overflow")
        result = enqueue_or_start(new_task)
        assert result == "queued"
        assert new_task.status == TaskStatus.QUEUED

    def test_promote_queued_empty_returns_empty_list(self) -> None:
        """promote_queued with no queued tasks returns empty list."""
        promoted = promote_queued()
        assert promoted == []

    def test_active_statuses_constant(self) -> None:
        """ACTIVE_STATUSES includes expected states."""
        assert TaskStatus.RUNNING in ACTIVE_STATUSES
        assert TaskStatus.VERIFYING in ACTIVE_STATUSES
        assert TaskStatus.CORRECTING in ACTIVE_STATUSES
        assert TaskStatus.COMPLETED not in ACTIVE_STATUSES
        assert TaskStatus.FAILED not in ACTIVE_STATUSES
        # BLOCKED/ESCALATED don't have live executors — shouldn't consume slots
        assert TaskStatus.BLOCKED not in ACTIVE_STATUSES
        assert TaskStatus.ESCALATED not in ACTIVE_STATUSES


class TestSortedQueuedWithPreFetchedTasks:
    """Tests for _sorted_queued with optional pre-fetched task list."""

    def test_pre_fetched_avoids_list_tasks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When tasks list is provided, list_tasks() is NOT called."""
        t1 = _make_task("q1")
        _force_status(t1, TaskStatus.QUEUED)
        t2 = _make_task("q2")
        _force_status(t2, TaskStatus.RUNNING)

        all_tasks = [t1, t2]
        # Monkeypatch list_tasks to blow up — proves we don't call it
        monkeypatch.setattr(
            "duo.scheduler.list_tasks",
            lambda: (_ for _ in ()).throw(AssertionError("should not be called")),
        )

        result = _sorted_queued(all_tasks)
        assert len(result) == 1
        assert result[0].id == "q1"

    def test_none_falls_back_to_list_tasks(self) -> None:
        """When tasks is None, falls back to list_tasks()."""
        t1 = _make_task("fb1")
        _force_status(t1, TaskStatus.QUEUED)

        result = _sorted_queued(None)
        assert any(t.id == "fb1" for t in result)


class TestTransitionGuards:
    """Tests for transition return-value checks in scheduler."""

    def test_promote_skips_illegal_transition(self) -> None:
        """promote_queued skips a task whose transition to SESSION_STARTING fails."""
        t1 = _make_task("stuck")
        # Force to COMPLETED — can't go to SESSION_STARTING from there
        _force_status(t1, TaskStatus.COMPLETED)
        # But also give it QUEUED status to appear in the queue
        # We need to mock _sorted_queued to return it as queued
        # Actually, promote_queued filters from list_tasks by QUEUED status
        # So force to QUEUED, but monkeypatch transition to fail
        _force_status(t1, TaskStatus.QUEUED)

        from unittest.mock import patch

        def always_fail(t, s):
            return False

        with patch("duo.scheduler.transition", side_effect=always_fail):
            promoted = promote_queued()

        assert promoted == []

    def test_enqueue_returns_started_on_failed_queued_transition(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """enqueue_or_start returns 'started' if QUEUED transition fails."""
        # Fill all slots so enqueue path is taken
        for i in range(3):
            t = _make_task(f"fill-{i}")
            _force_status(t, TaskStatus.RUNNING)

        new_task = _make_task("should-queue")

        from unittest.mock import patch

        with patch("duo.scheduler.transition", return_value=False):
            result = enqueue_or_start(new_task)

        assert result == "started"


class TestActiveStatusesExhaustiveness:
    """Verify ACTIVE_STATUSES is intentional for every TaskStatus member."""

    # Statuses that should NOT consume a slot (no live executor)
    NON_ACTIVE = {
        TaskStatus.CREATED,
        TaskStatus.QUEUED,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
        TaskStatus.COMPLETED,
        TaskStatus.ESCALATED,
    }

    def test_every_status_classified(self) -> None:
        """Every TaskStatus is either in ACTIVE_STATUSES or NON_ACTIVE."""
        all_statuses = set(TaskStatus)
        classified = ACTIVE_STATUSES | self.NON_ACTIVE
        assert classified == all_statuses, (
            f"Unclassified statuses: {all_statuses - classified}"
        )

    def test_no_overlap(self) -> None:
        """ACTIVE and NON_ACTIVE sets are disjoint."""
        overlap = ACTIVE_STATUSES & self.NON_ACTIVE
        assert overlap == set(), f"Overlap: {overlap}"


class TestFifoTiebreaker:
    """Verify deterministic FIFO ordering when created_at ties."""

    def test_same_timestamp_sorted_by_id(self) -> None:
        """Tasks with identical created_at are sorted by task id."""
        tasks = []
        for name in ["charlie", "alpha", "bravo"]:
            t = _make_task(name)
            t.created_at = "2024-01-01T00:00:00+00:00"
            save_task(t)
            _force_status(t, TaskStatus.QUEUED)
            tasks.append(t)

        queued = _sorted_queued()
        assert [t.id for t in queued] == ["alpha", "bravo", "charlie"]


class TestSchedulerEdgeCases:
    """Edge case value coverage for scheduler."""

    def test_enqueue_when_transition_fails(self) -> None:
        """enqueue_or_start returns 'started' if transition to QUEUED fails."""
        t = _make_task("queue-fail")
        # Force into COMPLETED — transition to QUEUED is illegal
        _force_status(t, TaskStatus.COMPLETED)
        # Mock has_slot to return False so it tries to queue
        with patch("duo.scheduler.has_slot", return_value=False):
            result = enqueue_or_start(t)
        assert result == "started"

    def test_promote_skips_failed_transition(self) -> None:
        """promote_queued silently skips tasks whose transition fails."""
        t1 = _make_task("skip-1")
        t2 = _make_task("skip-2")
        _force_status(t1, TaskStatus.QUEUED)
        _force_status(t2, TaskStatus.QUEUED)
        # Patch transition to fail for t1 but succeed for t2
        original_transition = transition

        def selective_transition(task, status):
            if task.id == "skip-1":
                return False
            return original_transition(task, status)

        with (
            patch("duo.scheduler.max_parallel", return_value=10),
            patch("duo.scheduler.transition", side_effect=selective_transition),
        ):
            promoted = promote_queued()
        assert len(promoted) == 1
        assert promoted[0].id == "skip-2"

    def test_queue_position_not_queued(self) -> None:
        """_queue_position for a non-queued task returns len+1."""
        t = _make_task("not-queued")
        # Not in QUEUED status — position is 1 (empty queue len + 1 = 1)
        pos = _queue_position(t)
        assert pos == 1

    def test_queue_status_no_tasks(self) -> None:
        """queue_status with no tasks returns zero counts."""
        qs = queue_status()
        assert qs["active_count"] == 0
        assert qs["queued_count"] == 0
        assert qs["active_tasks"] == []
        assert qs["queued_tasks"] == []

    def test_sorted_queued_with_none_tasks(self) -> None:
        """_sorted_queued(None) calls list_tasks internally."""
        queued = _sorted_queued(None)
        assert isinstance(queued, list)
