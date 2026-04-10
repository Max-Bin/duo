"""Edge case tests for duo.protocol — symlinks, long names, empty dirs, partial data."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import duo.protocol
from duo.protocol import (
    _clear_task_cache,
    atomic_write_text,
    create_task,
    list_tasks,
    load_task,
    read_json,
    write_json,
)

# ---------------------------------------------------------------------------
# Fixtures (same as test_protocol.py)
# ---------------------------------------------------------------------------


def _make_subtask(step_id: int = 1) -> duo.protocol.Subtask:
    from duo.protocol import Subtask

    return Subtask(
        step_id=step_id,
        description=f"step-{step_id}",
        target_files=["main.py"],
        writable_paths=["src/"],
    )


@pytest.fixture(autouse=True)
def _isolate_tasks_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect TASKS_DIR to a temporary directory for every test."""
    tasks = tmp_path / "tasks"
    monkeypatch.setattr("duo.protocol.TASKS_DIR", tasks)
    monkeypatch.setattr("duo.protocol._CORRUPTED_DIR", tasks / "_corrupted")


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    """Clear task cache between tests."""
    _clear_task_cache()
    yield
    _clear_task_cache()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestProtocolEdgeCases:
    """Edge case tests for protocol module."""

    def test_read_json_through_symlink(self, tmp_path: Path) -> None:
        """read_json() follows symlinks to read the target file."""
        real_file = tmp_path / "real.json"
        write_json(real_file, {"key": "symlink_value"})
        link = tmp_path / "link.json"
        link.symlink_to(real_file)
        result = read_json(link)
        assert result == {"key": "symlink_value"}

    def test_atomic_write_text_long_filename(self, tmp_path: Path) -> None:
        """atomic_write_text handles filenames near the OS name limit."""
        long_name = "x" * 200 + ".txt"
        path = tmp_path / long_name
        atomic_write_text(path, "long name content")
        assert path.read_text() == "long name content"

    def test_list_tasks_with_empty_task_directory(self) -> None:
        """A directory in TASKS_DIR without task.json is silently skipped."""
        duo.protocol.TASKS_DIR.mkdir(parents=True, exist_ok=True)
        (duo.protocol.TASKS_DIR / "empty-task-dir").mkdir()
        _clear_task_cache()
        result = list_tasks()
        assert result == []

    def test_load_task_missing_subtasks_field(self) -> None:
        """load_task returns None when task.json is missing subtasks."""
        task_dir = duo.protocol.TASKS_DIR / "partial-task"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task.json").write_text(
            json.dumps({"id": "partial-task", "description": "incomplete"})
        )
        assert load_task("partial-task") is None

    def test_load_task_missing_current_step(self) -> None:
        """load_task returns None when current_step is missing."""
        task_dir = duo.protocol.TASKS_DIR / "no-step"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task.json").write_text(
            json.dumps({"id": "no-step", "subtasks": [], "description": "d"})
        )
        assert load_task("no-step") is None

    def test_load_task_invalid_status_value(self) -> None:
        """load_task returns None when status is not a valid TaskStatus."""
        task = create_task("bad-st", "desc", "/w", "b", "c", [_make_subtask()])
        data = read_json(task.dir / "task.json")
        assert data is not None
        data["status"] = "definitely_not_a_real_status"
        (task.dir / "task.json").write_text(json.dumps(data, indent=2))
        assert load_task("bad-st") is None

    def test_create_task_when_tasks_dir_missing(self) -> None:
        """create_task creates TASKS_DIR when it does not exist yet."""
        assert not duo.protocol.TASKS_DIR.exists()
        task = create_task("fresh-t", "d", "/w", "b", "c", [_make_subtask()])
        assert duo.protocol.TASKS_DIR.exists()
        assert task.dir.exists()
        assert (task.dir / "task.json").exists()
