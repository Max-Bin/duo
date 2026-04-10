"""Scheduler — manages parallel task execution with FIFO queue.

Respects max_parallel concurrency limit. Tasks beyond the limit
are queued and started automatically as slots free up.
"""

from __future__ import annotations

from typing import Any

__all__ = ["enqueue_or_start", "promote_queued", "queue_status"]

from duo.config import get_config
from duo.protocol import (
    Task,
    TaskStatus,
    append_event,
    list_tasks,
    transition,
)

# Active statuses (consuming a slot — only states with a live executor)
ACTIVE_STATUSES = {
    TaskStatus.SESSION_STARTING,
    TaskStatus.PROMPT_SENT,
    TaskStatus.ACKED,
    TaskStatus.RUNNING,
    TaskStatus.RESULT_REPORTED,
    TaskStatus.VERIFYING,
    TaskStatus.CORRECTING,
}


def active_count() -> int:
    """Count currently active (slot-consuming) tasks."""
    return sum(1 for t in list_tasks() if t.status in ACTIVE_STATUSES)


def max_parallel() -> int:
    """Get max parallel executor count from config."""
    val = get_config("max_parallel")
    return int(val) if val is not None else 3


def has_slot() -> bool:
    """Check if there's a free slot for a new task."""
    return active_count() < max_parallel()


def enqueue_or_start(task: Task) -> str:
    """Either start a task immediately or queue it.

    Returns "started" or "queued".
    """
    if has_slot():
        return "started"
    transition(task, TaskStatus.QUEUED)
    append_event(
        task,
        "task_queued",
        {
            "position": _queue_position(task),
            "active": active_count(),
            "max": max_parallel(),
        },
    )
    return "queued"


def promote_queued() -> list[Task]:
    """Start queued tasks if slots are available.

    Returns list of tasks that were promoted from queue.
    Called by the monitor loop after each poll cycle.
    """
    promoted: list[Task] = []
    mp = max_parallel()
    tasks = list_tasks()
    n_active = sum(1 for t in tasks if t.status in ACTIVE_STATUSES)
    queued = sorted(
        (t for t in tasks if t.status == TaskStatus.QUEUED),
        key=lambda t: t.created_at,
    )

    for next_task in queued:
        if n_active >= mp:
            break
        transition(next_task, TaskStatus.SESSION_STARTING)
        n_active += 1
        promoted.append(next_task)
        append_event(
            next_task,
            "task_promoted",
            {
                "from": "queued",
                "active": n_active,
            },
        )

    return promoted


def _sorted_queued() -> list[Task]:
    """Get queued tasks sorted by creation time (FIFO)."""
    queued = [t for t in list_tasks() if t.status == TaskStatus.QUEUED]
    queued.sort(key=lambda t: t.created_at)
    return queued


def _next_queued() -> Task | None:
    """Get the next queued task (FIFO by created_at)."""
    queued = _sorted_queued()
    return queued[0] if queued else None


def _queue_position(task: Task) -> int:
    """Get a task's position in the queue (1-based)."""
    queued = _sorted_queued()
    for i, t in enumerate(queued):
        if t.id == task.id:
            return i + 1
    return len(queued) + 1


def queue_status() -> dict[str, Any]:
    """Get queue status summary."""
    tasks = list_tasks()
    active = [t for t in tasks if t.status in ACTIVE_STATUSES]
    queued = _sorted_queued()

    return {
        "active_count": len(active),
        "queued_count": len(queued),
        "max_parallel": max_parallel(),
        "active_tasks": [t.id for t in active],
        "queued_tasks": [t.id for t in queued],
    }
