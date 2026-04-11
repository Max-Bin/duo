"""Tests for duo.ceo_log — CEO session replay logging."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import duo.ceo_log
from duo.ceo_log import (
    list_sessions,
    log_decision,
    log_dialog_detected,
    log_outcome,
    replay_session,
    session_stats,
    start_ceo_session,
)


@pytest.fixture(autouse=True)
def isolated_ceo_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect CEO_SESSIONS_DIR to a temporary directory."""
    sessions_dir = tmp_path / "ceo-sessions"
    monkeypatch.setattr(duo.ceo_log, "CEO_SESSIONS_DIR", sessions_dir)
    return sessions_dir


class TestStartCeoSession:
    def test_creates_dir_and_returns_valid_id(self) -> None:
        session_id = start_ceo_session()
        assert session_id
        assert "-" in session_id
        # Should have at least the timestamp part and 6-char hex suffix
        parts = session_id.rsplit("-", 1)
        assert len(parts) == 2
        assert len(parts[1]) == 6

    def test_events_jsonl_has_session_started(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions_dir = tmp_path / "ceo-sessions"
        monkeypatch.setattr(duo.ceo_log, "CEO_SESSIONS_DIR", sessions_dir)
        session_id = start_ceo_session()
        events_path = sessions_dir / session_id / "events.jsonl"
        assert events_path.exists()
        events = [
            json.loads(line) for line in events_path.read_text().strip().split("\n")
        ]
        assert len(events) == 1
        assert events[0]["event"] == "session_started"
        assert "ts" in events[0]

    def test_unique_ids(self) -> None:
        id1 = start_ceo_session()
        id2 = start_ceo_session()
        assert id1 != id2


class TestLogDialogDetected:
    def test_appends_event_with_correct_fields(self) -> None:
        session_id = start_ceo_session()
        log_dialog_detected(session_id, "my-task", "Which option?", "option")
        events = replay_session(session_id)
        assert len(events) == 2  # session_started + dialog_detected
        ev = events[1]
        assert ev["event"] == "dialog_detected"
        assert ev["task"] == "my-task"
        assert ev["dialog_kind"] == "option"
        assert ev["content"] == "Which option?"
        assert "ts" in ev


class TestLogDecision:
    def test_appends_decision_event_with_elapsed_ms(self) -> None:
        session_id = start_ceo_session()
        log_decision(
            session_id, "my-task", "approve", "permission approved", elapsed_ms=42
        )
        events = replay_session(session_id)
        assert len(events) == 2
        ev = events[1]
        assert ev["event"] == "decision"
        assert ev["task"] == "my-task"
        assert ev["decision_type"] == "approve"
        assert ev["content"] == "permission approved"
        assert ev["elapsed_ms"] == 42


class TestLogOutcome:
    def test_appends_outcome_event(self) -> None:
        session_id = start_ceo_session()
        log_outcome(session_id, "my-task", "success")
        events = replay_session(session_id)
        assert len(events) == 2
        ev = events[1]
        assert ev["event"] == "outcome"
        assert ev["task"] == "my-task"
        assert ev["outcome"] == "success"


class TestListSessions:
    def test_empty_when_no_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(duo.ceo_log, "CEO_SESSIONS_DIR", tmp_path / "nonexistent")
        assert list_sessions() == []

    def test_sorted_newest_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions_dir = tmp_path / "ceo-sessions"
        sessions_dir.mkdir()
        monkeypatch.setattr(duo.ceo_log, "CEO_SESSIONS_DIR", sessions_dir)
        # Create dirs with different names (alphabetical = chronological)
        (sessions_dir / "20250101-120000-aaa111").mkdir()
        (sessions_dir / "20250102-120000-bbb222").mkdir()
        (sessions_dir / "20250103-120000-ccc333").mkdir()
        result = list_sessions()
        assert result == [
            "20250103-120000-ccc333",
            "20250102-120000-bbb222",
            "20250101-120000-aaa111",
        ]

    def test_ignores_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions_dir = tmp_path / "ceo-sessions"
        sessions_dir.mkdir()
        monkeypatch.setattr(duo.ceo_log, "CEO_SESSIONS_DIR", sessions_dir)
        (sessions_dir / "some-session").mkdir()
        (sessions_dir / "not-a-dir.txt").write_text("hi")
        result = list_sessions()
        assert result == ["some-session"]


class TestReplaySession:
    def test_reads_back_all_events(self) -> None:
        session_id = start_ceo_session()
        log_dialog_detected(session_id, "t1", "dialog1", "option")
        log_decision(session_id, "t1", "select", "option 1", elapsed_ms=10)
        log_outcome(session_id, "t1", "done")
        events = replay_session(session_id)
        assert len(events) == 4
        assert [e["event"] for e in events] == [
            "session_started",
            "dialog_detected",
            "decision",
            "outcome",
        ]

    def test_missing_session_returns_empty(self) -> None:
        events = replay_session("nonexistent-session-id")
        assert events == []


class TestSessionStats:
    def test_correct_counts_and_averages(self) -> None:
        session_id = start_ceo_session()
        log_dialog_detected(session_id, "t1", "d1", "option")
        log_dialog_detected(session_id, "t1", "d2", "text")
        log_decision(session_id, "t1", "approve", "yes", elapsed_ms=100)
        log_decision(session_id, "t1", "select", "opt1", elapsed_ms=200)
        log_decision(session_id, "t1", "approve", "yes2", elapsed_ms=300)
        stats = session_stats(session_id)
        assert stats["session_id"] == session_id
        assert stats["total_events"] == 6  # 1 started + 2 dialogs + 3 decisions
        assert stats["dialogs_detected"] == 2
        assert stats["decisions_made"] == 3
        assert stats["decision_types"] == {"approve": 2, "select": 1}
        assert stats["avg_decision_ms"] == 200  # (100+200+300)//3

    def test_no_decisions_gives_zero_avg(self) -> None:
        session_id = start_ceo_session()
        stats = session_stats(session_id)
        assert stats["decisions_made"] == 0
        assert stats["avg_decision_ms"] == 0
        assert stats["decision_types"] == {}


class TestContentTruncation:
    def test_dialog_content_truncated_at_500(self) -> None:
        session_id = start_ceo_session()
        long_content = "x" * 1000
        log_dialog_detected(session_id, "t1", long_content, "option")
        events = replay_session(session_id)
        assert len(events[1]["content"]) == 500

    def test_decision_content_truncated_at_500(self) -> None:
        session_id = start_ceo_session()
        long_content = "y" * 1000
        log_decision(session_id, "t1", "select", long_content, elapsed_ms=0)
        events = replay_session(session_id)
        assert len(events[1]["content"]) == 500


class TestSessionStatsEdgeCases:
    def test_decision_without_elapsed_ms(self) -> None:
        """session_stats handles decisions missing elapsed_ms."""
        session_id = start_ceo_session()
        # Manually append a decision event without elapsed_ms
        from duo.ceo_log import _append_event
        from duo.protocol import now_iso

        _append_event(
            session_id,
            {
                "event": "decision",
                "ts": now_iso(),
                "task": "t1",
                "decision_type": "approve",
                "content": "yes",
            },
        )
        stats = session_stats(session_id)
        assert stats["decisions_made"] == 1
        assert stats["avg_decision_ms"] == 0

    def test_decision_with_non_numeric_elapsed_ms(self) -> None:
        """session_stats skips non-numeric elapsed_ms values."""
        session_id = start_ceo_session()
        from duo.ceo_log import _append_event
        from duo.protocol import now_iso

        _append_event(
            session_id,
            {
                "event": "decision",
                "ts": now_iso(),
                "task": "t1",
                "decision_type": "approve",
                "content": "yes",
                "elapsed_ms": "not-a-number",
            },
        )
        _append_event(
            session_id,
            {
                "event": "decision",
                "ts": now_iso(),
                "task": "t1",
                "decision_type": "approve",
                "content": "yes",
                "elapsed_ms": 100,
            },
        )
        stats = session_stats(session_id)
        assert stats["decisions_made"] == 2
        assert stats["avg_decision_ms"] == 100


# ---------------------------------------------------------------------------
# Session ID validation (path traversal prevention)
# ---------------------------------------------------------------------------


class TestSessionIdValidation:
    """Defence-in-depth: reject unsafe session IDs."""

    def test_replay_rejects_unsafe_id(self) -> None:
        for bad_id in [
            "..",
            "../evil",
            "../../etc",
            ".hidden",
            "has space",
            "has;semi",
            "",
            "-starts-dash",
        ]:
            with pytest.raises(ValueError, match="Invalid CEO session ID"):
                replay_session(bad_id)

    def test_session_stats_rejects_unsafe_id(self) -> None:
        for bad_id in ["..", "../x", "a b", ""]:
            with pytest.raises(ValueError, match="Invalid CEO session ID"):
                session_stats(bad_id)

    def test_start_session_produces_valid_id(self) -> None:
        """IDs from start_ceo_session always pass validation."""
        session_id = start_ceo_session()
        # Should not raise
        replay_session(session_id)
