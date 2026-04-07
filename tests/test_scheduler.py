"""Tests for scheduler module — parallel task execution with FIFO queue."""

from __future__ import annotations

from pathlib import Path

import pytest

import duo.protocol as protocol_mod
from duo.protocol import (
    Subtask,
    Task,
    TaskStatus,
    create_task,
    list_tasks,
    save_task,
    transition,
)
from duo.scheduler import (
    ACTIVE_STATUSES,
    active_count,
    enqueue_or_start,
    has_slot,
    max_parallel,
    promote_queued,
    queue_status,
    _next_queued,
    _queue_position,
)
import duo.config as config_mod


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect TASKS_DIR and CONFIG_PATH to temp directories."""
    monkeypatch.setattr(protocol_mod, "TASKS_DIR", tmp_path / "tasks")
    fake_config = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)


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
    def test_no_tasks(self) -> None:
        assert active_count() == 0

    def test_created_not_active(self) -> None:
        _make_task("t1")
        assert active_count() == 0

    def test_queued_not_active(self) -> None:
        t = _make_task("t1")
        _force_status(t, TaskStatus.QUEUED)
        assert active_count() == 0

    def test_completed_not_active(self) -> None:
        t = _make_task("t1")
        _force_status(t, TaskStatus.COMPLETED)
        assert active_count() == 0

    def test_failed_not_active(self) -> None:
        t = _make_task("t1")
        _force_status(t, TaskStatus.FAILED)
        assert active_count() == 0

    def test_running_is_active(self) -> None:
        t = _make_task("t1")
        _force_status(t, TaskStatus.RUNNING)
        assert active_count() == 1

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


class TestNextQueued:
    def test_empty(self) -> None:
        assert _next_queued() is None

    def test_returns_oldest(self) -> None:
        import time
        t1 = _make_task("newer")
        _force_status(t1, TaskStatus.QUEUED)
        time.sleep(0.01)
        t2 = _make_task("oldest")
        # Force older timestamp
        t2.created_at = "2020-01-01T00:00:00+00:00"
        _force_status(t2, TaskStatus.QUEUED)
        save_task(t2)

        result = _next_queued()
        assert result is not None
        assert result.id == "oldest"


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
