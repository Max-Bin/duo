"""Tests for duo.dashboard module."""

from __future__ import annotations

import pytest

import duo.protocol
from duo.protocol import create_task, Subtask, TaskStatus


@pytest.fixture(autouse=True)
def isolated_tasks(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    monkeypatch.setattr(duo.protocol, "TASKS_DIR", tasks_dir)
    monkeypatch.setattr(duo.protocol, "DUO_DIR", tmp_path)
    return tasks_dir


def _make_task(task_id="test-task", description="Test task"):
    return create_task(
        task_id=task_id,
        description=description,
        worktree="/fake/worktree",
        branch=f"duo/{task_id}",
        base_commit="abc123",
        subtasks=[Subtask(step_id=1, description=description, target_files=[], writable_paths=["*"])],
    )


class TestBuildTasksTable:
    def test_empty_tasks(self):
        from duo.dashboard import _build_tasks_table
        table = _build_tasks_table([])
        assert table.row_count == 0

    def test_with_tasks(self):
        from duo.dashboard import _build_tasks_table
        task = _make_task()
        table = _build_tasks_table([task])
        assert table.row_count == 1

    def test_multiple_tasks(self):
        from duo.dashboard import _build_tasks_table
        t1 = _make_task("task-1", "First task")
        t2 = _make_task("task-2", "Second task")
        table = _build_tasks_table([t1, t2])
        assert table.row_count == 2


class TestBuildQueuePanel:
    def test_returns_panel(self):
        from duo.dashboard import _build_queue_panel
        panel = _build_queue_panel()
        assert panel is not None


class TestBuildEventsPanel:
    def test_no_events(self):
        from duo.dashboard import _build_events_panel
        panel = _build_events_panel([])
        assert panel is not None

    def test_with_events(self):
        from duo.dashboard import _build_events_panel
        task = _make_task()
        panel = _build_events_panel([task])
        assert panel is not None


class TestStatusText:
    def test_all_statuses(self):
        from duo.dashboard import _status_text
        for status in TaskStatus:
            text = _status_text(status)
            assert str(text) == status.value
