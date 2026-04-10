"""Shared pytest fixtures for Duo test suite."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_tasks_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect TASKS_DIR, _CORRUPTED_DIR, and CONFIG_PATH to tmp_path.

    Applied automatically to every test so no test touches ``~/.duo``.
    """
    tasks = tmp_path / "tasks"
    monkeypatch.setattr("duo.protocol.TASKS_DIR", tasks)
    monkeypatch.setattr("duo.protocol._CORRUPTED_DIR", tasks / "_corrupted")
    monkeypatch.setattr("duo.config.CONFIG_PATH", tmp_path / "config.json")
