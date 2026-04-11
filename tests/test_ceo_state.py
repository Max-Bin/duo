"""Tests for duo.ceo_state — CEO focus persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

import duo.ceo_state
from duo.ceo_state import clear_ceo_focus, load_ceo_focus, save_ceo_focus


@pytest.fixture(autouse=True)
def _isolated_ceo_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect CEO_STATE_PATH to a temporary directory."""
    monkeypatch.setattr(duo.ceo_state, "CEO_STATE_PATH", tmp_path / "ceo-state.json")


class TestCeoState:
    """Tests for CEO focus state persistence."""

    def test_save_and_load_focus(self) -> None:
        save_ceo_focus("my-task", session_id="sess-1", notes="working on it")
        focus = load_ceo_focus()
        assert focus is not None
        assert focus["task_id"] == "my-task"
        assert focus["session_id"] == "sess-1"
        assert focus["notes"] == "working on it"
        assert "started_at" in focus

    def test_load_focus_not_set(self) -> None:
        assert load_ceo_focus() is None

    def test_clear_focus(self) -> None:
        save_ceo_focus("task-to-clear")
        assert load_ceo_focus() is not None
        clear_ceo_focus()
        assert load_ceo_focus() is None

    def test_clear_focus_not_set(self) -> None:
        clear_ceo_focus()  # should not raise

    def test_save_overwrites(self) -> None:
        save_ceo_focus("first-task")
        save_ceo_focus("second-task")
        focus = load_ceo_focus()
        assert focus is not None
        assert focus["task_id"] == "second-task"

    def test_focus_fields(self) -> None:
        save_ceo_focus("field-task", session_id="s1", notes="n1")
        focus = load_ceo_focus()
        assert focus is not None
        assert set(focus.keys()) == {"task_id", "session_id", "notes", "started_at"}

    def test_load_focus_invalid_schema_no_task_id(self) -> None:
        """load_ceo_focus returns None for data missing task_id."""
        import json

        path = duo.ceo_state.CEO_STATE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"notes": "orphan"}))
        assert load_ceo_focus() is None

    def test_load_focus_invalid_schema_non_string_task_id(self) -> None:
        """load_ceo_focus returns None for non-string task_id."""
        import json

        path = duo.ceo_state.CEO_STATE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"task_id": 123}))
        assert load_ceo_focus() is None
