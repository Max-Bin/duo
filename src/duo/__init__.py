"""Duo — Agent Orchestration Runtime."""

from __future__ import annotations

from importlib.metadata import version as _pkg_version

try:
    __version__: str = _pkg_version("duo")
except Exception:  # pragma: no cover – editable install may not have metadata yet
    __version__ = "0.0.0-dev"
