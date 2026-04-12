"""Shared pytest fixtures for Duo test suite."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from duo.protocol import Subtask, create_task


@pytest.fixture(autouse=True)
def _isolate_tasks_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect TASKS_DIR, DUO_DIR, _CORRUPTED_DIR, and CONFIG_PATH to tmp_path.

    Applied automatically to every test so no test touches ``~/.duo``.
    """
    tasks_dir = tmp_path / "tasks"
    monkeypatch.setattr("duo.protocol.TASKS_DIR", tasks_dir)
    monkeypatch.setattr("duo.protocol._CORRUPTED_DIR", tasks_dir / "_corrupted")
    monkeypatch.setattr("duo.protocol.DUO_DIR", tmp_path)
    monkeypatch.setattr("duo.protocol._GO_SESSION_FILE", tmp_path / "go-session.json")
    monkeypatch.setattr("duo.config.CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr("duo.cli.TASKS_DIR", tasks_dir)
    monkeypatch.setattr("duo.cli.DUO_DIR", tmp_path)
    monkeypatch.setattr("duo.cli.doctor.TASKS_DIR", tasks_dir)
    monkeypatch.setattr("duo.cli.doctor.DUO_DIR", tmp_path)
    return tasks_dir


@pytest.fixture
def isolated_tasks(_isolate_tasks_dir: Path) -> Path:
    """Alias so tests can request *isolated_tasks* by name and get tasks_dir."""
    _isolate_tasks_dir.mkdir(exist_ok=True)
    return _isolate_tasks_dir


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _make_task(task_id: str = "test-task", description: str = "Test task"):
    """Create a task in the isolated TASKS_DIR and return it."""
    return create_task(
        task_id=task_id,
        description=description,
        worktree="/fake/worktree",
        branch=f"duo/{task_id}",
        base_commit="abc123",
        subtasks=[
            Subtask(
                step_id=1,
                description=description,
                target_files=[],
                writable_paths=["*"],
            )
        ],
    )


@pytest.fixture
def make_task():
    """Fixture wrapper around _make_task for use in test classes."""
    return _make_task
