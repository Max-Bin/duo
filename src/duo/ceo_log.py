"""CEO session logging — structured trace of CEO decisions."""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

from duo.protocol import DUO_DIR, now_iso, read_jsonl

CEO_SESSIONS_DIR = DUO_DIR / "ceo-sessions"

_SAFE_SESSION_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


def _validate_session_id(session_id: str) -> None:
    """Reject session IDs that could cause path traversal."""
    if not _SAFE_SESSION_ID.match(session_id):
        raise ValueError(
            f"Invalid CEO session ID: {session_id!r} "
            "(must start with alphanumeric, then alphanumeric/underscore/hyphen/dot)"
        )


def start_ceo_session() -> str:
    """Create a new CEO session, return session_id."""
    ts = now_iso()[:19].replace(":", "").replace("-", "").replace("T", "-")
    session_id = ts + "-" + uuid.uuid4().hex[:6]
    session_dir = CEO_SESSIONS_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    _append_event(session_id, {"event": "session_started", "ts": now_iso()})
    return session_id


def log_dialog_detected(
    session_id: str, task: str, dialog_content: str, dialog_kind: str
) -> None:
    """Log a dialog detection event."""
    _append_event(
        session_id,
        {
            "event": "dialog_detected",
            "ts": now_iso(),
            "task": task,
            "dialog_kind": dialog_kind,
            "content": dialog_content[:500],
        },
    )


def log_decision(
    session_id: str,
    task: str,
    decision_type: str,
    decision_content: str,
    *,
    elapsed_ms: int,
) -> None:
    """Log a CEO decision."""
    _append_event(
        session_id,
        {
            "event": "decision",
            "ts": now_iso(),
            "task": task,
            "decision_type": decision_type,
            "content": decision_content[:500],
            "elapsed_ms": elapsed_ms,
        },
    )


def log_outcome(session_id: str, task: str, outcome: str) -> None:
    """Log the outcome after a decision."""
    _append_event(
        session_id,
        {
            "event": "outcome",
            "ts": now_iso(),
            "task": task,
            "outcome": outcome,
        },
    )


def list_sessions() -> list[str]:
    """List all CEO session IDs (directory names), sorted newest first."""
    if not CEO_SESSIONS_DIR.exists():
        return []
    return sorted(
        [d.name for d in CEO_SESSIONS_DIR.iterdir() if d.is_dir()],
        reverse=True,
    )


def replay_session(session_id: str) -> list[dict[str, Any]]:
    """Read all events from a session."""
    _validate_session_id(session_id)
    events_path = CEO_SESSIONS_DIR / session_id / "events.jsonl"
    return read_jsonl(events_path)


def session_stats(session_id: str) -> dict[str, Any]:
    """Compute stats for a session."""
    _validate_session_id(session_id)
    events = replay_session(session_id)
    dialogs = [e for e in events if e.get("event") == "dialog_detected"]
    decisions = [e for e in events if e.get("event") == "decision"]
    decision_types: dict[str, int] = {}
    elapsed_times: list[int] = []
    for d in decisions:
        dt = d.get("decision_type", "unknown")
        decision_types[dt] = decision_types.get(dt, 0) + 1
        if "elapsed_ms" in d:
            elapsed_times.append(d["elapsed_ms"])
    avg_ms = sum(elapsed_times) // len(elapsed_times) if elapsed_times else 0
    return {
        "session_id": session_id,
        "total_events": len(events),
        "dialogs_detected": len(dialogs),
        "decisions_made": len(decisions),
        "decision_types": decision_types,
        "avg_decision_ms": avg_ms,
    }


def _append_event(session_id: str, event: dict[str, Any]) -> None:
    """Append an event to the session's events.jsonl."""
    _validate_session_id(session_id)
    events_path = CEO_SESSIONS_DIR / session_id / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False) + "\n"
    with open(events_path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


__all__ = [
    "CEO_SESSIONS_DIR",
    "list_sessions",
    "log_decision",
    "log_dialog_detected",
    "log_outcome",
    "replay_session",
    "session_stats",
    "start_ceo_session",
]
