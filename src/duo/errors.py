"""Unified error hierarchy for Duo."""

from __future__ import annotations

import click


class DuoError(Exception):
    """Base class for all Duo errors."""


class DuoUserError(click.ClickException):
    """User-facing errors with actionable fix suggestions."""

    def __init__(self, message: str, *, fix: str = "") -> None:
        super().__init__(message)
        self.fix = fix

    def format_message(self) -> str:
        msg = self.message
        if self.fix:
            msg += f"\n  Fix: {self.fix}"
        return msg


class DuoSystemError(DuoError):
    """Internal system errors (tmux, subprocess, file I/O)."""


class DuoDataError(DuoError):
    """Data corruption or schema validation errors."""

    def __init__(self, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.path = path


class TaskLockedError(DuoError):
    """Raised when a task lock cannot be acquired (another process holds it)."""


__all__ = [
    "DuoDataError",
    "DuoError",
    "DuoSystemError",
    "DuoUserError",
    "TaskLockedError",
]
