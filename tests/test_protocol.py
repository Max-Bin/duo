"""Tests for duo.protocol — helpers, file I/O, FSM, and task CRUD."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from duo.protocol import (
    TRANSITIONS,
    Subtask,
    TaskStatus,
    append_event,
    create_task,
    load_task,
    new_incarnation,
    now_iso,
    prompt_hash,
    read_json,
    read_jsonl,
    replay_state,
    save_task,
    transition,
    write_json,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_tasks_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect TASKS_DIR to a temporary directory for every test."""
    monkeypatch.setattr("duo.protocol.TASKS_DIR", tmp_path / "tasks")


def _make_subtask(step_id: int = 1) -> Subtask:
    return Subtask(
        step_id=step_id,
        description=f"step-{step_id}",
        target_files=["main.py"],
        writable_paths=["src/"],
    )


# ---------------------------------------------------------------------------
# now_iso
# ---------------------------------------------------------------------------

class TestNowIso:
    def test_returns_valid_iso_timestamp(self):
        ts = now_iso()
        dt = datetime.fromisoformat(ts)
        assert dt.tzinfo is not None

    def test_timestamp_is_utc(self):
        ts = now_iso()
        dt = datetime.fromisoformat(ts)
        assert dt.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# new_incarnation
# ---------------------------------------------------------------------------

class TestNewIncarnation:
    def test_returns_8_char_hex(self):
        inc = new_incarnation()
        assert len(inc) == 8
        assert re.fullmatch(r"[0-9a-f]{8}", inc)

    def test_successive_calls_unique(self):
        results = {new_incarnation() for _ in range(50)}
        assert len(results) == 50


# ---------------------------------------------------------------------------
# prompt_hash
# ---------------------------------------------------------------------------

class TestPromptHash:
    def test_returns_8_char_hex(self):
        h = prompt_hash("hello world")
        assert len(h) == 8
        assert re.fullmatch(r"[0-9a-f]{8}", h)

    def test_deterministic(self):
        assert prompt_hash("test") == prompt_hash("test")

    def test_different_input_different_hash(self):
        assert prompt_hash("aaa") != prompt_hash("bbb")


# ---------------------------------------------------------------------------
# read_json / write_json
# ---------------------------------------------------------------------------

class TestJsonIO:
    def test_round_trip(self, tmp_path: Path):
        path = tmp_path / "data.json"
        payload = {"key": "value", "nested": {"n": 42}}
        write_json(path, payload)
        assert read_json(path) == payload

    def test_read_missing_file(self, tmp_path: Path):
        assert read_json(tmp_path / "nope.json") is None

    def test_read_invalid_json(self, tmp_path: Path):
        bad = tmp_path / "bad.json"
        bad.write_text("not json {{{")
        assert read_json(bad) is None

    def test_write_creates_parent_dirs(self, tmp_path: Path):
        path = tmp_path / "a" / "b" / "c.json"
        write_json(path, {"ok": True})
        assert read_json(path) == {"ok": True}


# ---------------------------------------------------------------------------
# read_jsonl
# ---------------------------------------------------------------------------

class TestReadJsonl:
    def test_valid_lines(self, tmp_path: Path):
        path = tmp_path / "events.jsonl"
        lines = [json.dumps({"i": i}) for i in range(3)]
        path.write_text("\n".join(lines) + "\n")
        result = read_jsonl(path)
        assert len(result) == 3
        assert result[1] == {"i": 1}

    def test_skips_bad_lines(self, tmp_path: Path):
        path = tmp_path / "mixed.jsonl"
        path.write_text('{"a":1}\nBAD LINE\n{"b":2}\n')
        result = read_jsonl(path)
        assert result == [{"a": 1}, {"b": 2}]

    def test_empty_file(self, tmp_path: Path):
        path = tmp_path / "empty.jsonl"
        path.write_text("")
        assert read_jsonl(path) == []

    def test_missing_file(self, tmp_path: Path):
        assert read_jsonl(tmp_path / "missing.jsonl") == []


# ---------------------------------------------------------------------------
# TaskStatus FSM transitions
# ---------------------------------------------------------------------------

class TestTaskStatusTransitions:
    @pytest.mark.parametrize(
        "src, dst",
        [
            (TaskStatus.CREATED, TaskStatus.SESSION_STARTING),
            (TaskStatus.PROMPT_SENT, TaskStatus.VERIFYING),
            (TaskStatus.PROMPT_SENT, TaskStatus.BLOCKED),
            (TaskStatus.VERIFYING, TaskStatus.COMPLETED),
            (TaskStatus.FAILED, TaskStatus.SESSION_STARTING),
        ],
    )
    def test_allowed_transitions(self, src, dst):
        assert dst in TRANSITIONS[src]

    def test_completed_is_terminal(self):
        assert TRANSITIONS[TaskStatus.COMPLETED] == set()

    @pytest.mark.parametrize(
        "src, dst",
        [
            (TaskStatus.COMPLETED, TaskStatus.CREATED),
            (TaskStatus.COMPLETED, TaskStatus.RUNNING),
            (TaskStatus.CREATED, TaskStatus.COMPLETED),
            (TaskStatus.RUNNING, TaskStatus.CREATED),
        ],
    )
    def test_rejected_transitions(self, src, dst):
        assert dst not in TRANSITIONS[src]

    def test_transition_function_applies_valid(self, tmp_path: Path):
        task = create_task("fsm-ok", "desc", "/w", "b", "abc", [_make_subtask()])
        assert task.status == TaskStatus.CREATED
        transition(task, TaskStatus.SESSION_STARTING)
        assert task.status == TaskStatus.SESSION_STARTING

    def test_transition_function_rejects_invalid(self, tmp_path: Path):
        task = create_task("fsm-bad", "desc", "/w", "b", "abc", [_make_subtask()])
        transition(task, TaskStatus.COMPLETED)  # CREATED→COMPLETED is illegal
        assert task.status == TaskStatus.CREATED  # unchanged

        events = read_jsonl(task.journal_path)
        invalid_events = [e for e in events if e["event"] == "invalid_transition"]
        assert len(invalid_events) == 1


# ---------------------------------------------------------------------------
# create_task / save_task / load_task
# ---------------------------------------------------------------------------

class TestTaskCRUD:
    def test_create_and_load_roundtrip(self):
        subtasks = [_make_subtask(1), _make_subtask(2)]
        task = create_task(
            task_id="roundtrip-1",
            description="Do something",
            worktree="/work",
            branch="feature",
            base_commit="deadbeef",
            subtasks=subtasks,
        )

        loaded = load_task("roundtrip-1")
        assert loaded is not None
        assert loaded.id == task.id
        assert loaded.description == task.description
        assert loaded.worktree == task.worktree
        assert loaded.branch == task.branch
        assert loaded.base_commit == task.base_commit
        assert loaded.status == TaskStatus.CREATED
        assert loaded.current_step == 1
        assert loaded.current_attempt == 1
        assert len(loaded.subtasks) == 2
        assert loaded.subtasks[0].step_id == 1
        assert loaded.subtasks[1].description == "step-2"
        assert loaded.incarnation_id == task.incarnation_id
        assert loaded.created_at == task.created_at

    def test_load_missing_task(self):
        assert load_task("nonexistent-task") is None

    def test_save_updates_persisted_state(self):
        task = create_task("save-test", "d", "/w", "b", "c", [_make_subtask()])
        task.current_step = 5
        task.last_prompt_sent_at = now_iso()
        save_task(task)

        loaded = load_task("save-test")
        assert loaded is not None
        assert loaded.current_step == 5
        assert loaded.last_prompt_sent_at is not None

    def test_create_builds_directory_structure(self):
        task = create_task("dirs-test", "d", "/w", "b", "c", [_make_subtask(1), _make_subtask(2)])
        assert task.dir.is_dir()
        assert (task.dir / "task.json").is_file()
        assert task.step_dir(1).is_dir()
        assert task.step_dir(2).is_dir()

    def test_security_policy_round_trip(self):
        create_task("sec-test", "d", "/w", "b", "c", [_make_subtask()])
        loaded = load_task("sec-test")
        assert loaded is not None
        assert loaded.security_policy.allow_network is False
        assert "API_KEY=" in loaded.security_policy.secret_patterns


# ---------------------------------------------------------------------------
# append_event
# ---------------------------------------------------------------------------

class TestAppendEvent:
    def test_events_appear_in_journal(self):
        task = create_task("ev-test", "d", "/w", "b", "c", [_make_subtask()])
        append_event(task, "custom_event", {"foo": "bar"})
        append_event(task, "another_event")

        events = read_jsonl(task.journal_path)
        custom = [e for e in events if e["event"] == "custom_event"]
        assert len(custom) == 1
        assert custom[0]["data"] == {"foo": "bar"}

        another = [e for e in events if e["event"] == "another_event"]
        assert len(another) == 1
        assert another[0]["data"] == {}

    def test_event_has_timestamp(self):
        task = create_task("ev-ts", "d", "/w", "b", "c", [_make_subtask()])
        append_event(task, "ping")
        events = read_jsonl(task.journal_path)
        ping = [e for e in events if e["event"] == "ping"][0]
        # Should parse as valid ISO timestamp
        datetime.fromisoformat(ping["ts"])


# ---------------------------------------------------------------------------
# replay_state
# ---------------------------------------------------------------------------

class TestReplayState:
    def test_empty_journal_returns_created(self, tmp_path: Path):
        task = create_task("replay-empty", "d", "/w", "b", "c", [_make_subtask()])
        # Overwrite journal to be empty
        task.journal_path.write_text("")
        assert replay_state(task) == TaskStatus.CREATED

    def test_replays_through_transitions(self):
        task = create_task("replay-multi", "d", "/w", "b", "c", [_make_subtask()])
        transition(task, TaskStatus.SESSION_STARTING)
        transition(task, TaskStatus.PROMPT_SENT)
        transition(task, TaskStatus.VERIFYING)
        transition(task, TaskStatus.COMPLETED)
        assert replay_state(task) == TaskStatus.COMPLETED

    def test_replay_matches_task_status(self):
        task = create_task("replay-match", "d", "/w", "b", "c", [_make_subtask()])
        transition(task, TaskStatus.SESSION_STARTING)
        transition(task, TaskStatus.PROMPT_SENT)
        assert replay_state(task) == task.status
