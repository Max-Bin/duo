"""CEO focus state — persistent context across Claude Code restarts."""

from __future__ import annotations

import json
from typing import Any

from duo.protocol import DUO_DIR, atomic_write_text, now_iso, read_json

CEO_STATE_PATH = DUO_DIR / "ceo-state.json"


def save_ceo_focus(task_id: str, session_id: str = "", notes: str = "") -> None:
    """Save the current CEO focus task."""
    data = {
        "task_id": task_id,
        "session_id": session_id,
        "notes": notes,
        "started_at": now_iso(),
    }
    CEO_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(CEO_STATE_PATH, json.dumps(data, indent=2) + "\n")


def load_ceo_focus() -> dict[str, Any] | None:
    """Load the current CEO focus, or None if not set or invalid."""
    data = read_json(CEO_STATE_PATH)
    if not isinstance(data, dict):
        return None
    if "task_id" not in data or not isinstance(data.get("task_id"), str):
        return None
    return dict(data)


def clear_ceo_focus() -> None:
    """Clear the current CEO focus."""
    CEO_STATE_PATH.unlink(missing_ok=True)


__all__ = [
    "CEO_STATE_PATH",
    "clear_ceo_focus",
    "load_ceo_focus",
    "save_ceo_focus",
]
