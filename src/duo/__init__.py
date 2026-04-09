"""Duo — Agent Orchestration Runtime."""

from __future__ import annotations

from importlib.metadata import version as _pkg_version

try:
    __version__: str = _pkg_version("duo")
except Exception:  # noqa: BLE001  # pragma: no cover – editable install may not have metadata yet
    __version__ = "0.0.0-dev"

from duo.config import get_config, load_config, set_config
from duo.protocol import (
    SecurityPolicy,
    Subtask,
    Task,
    TaskStatus,
    create_task,
    list_tasks,
    load_task,
    save_task,
    transition,
)

__all__ = [
    "__version__",
    "Task",
    "Subtask",
    "SecurityPolicy",
    "TaskStatus",
    "create_task",
    "load_task",
    "list_tasks",
    "save_task",
    "transition",
    "get_config",
    "set_config",
    "load_config",
]
