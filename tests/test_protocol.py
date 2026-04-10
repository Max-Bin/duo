"""Tests for duo.protocol — helpers, file I/O, FSM, and task CRUD."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from duo.protocol import (
    DEFAULT_SECRET_PATTERNS,
    TASKS_DIR,
    TRANSITIONS,
    AckResult,
    Heartbeat,
    StepResult,
    Subtask,
    TaskStatus,
    _clear_task_cache,
    append_event,
    atomic_write_text,
    create_task,
    list_corrupted,
    list_tasks,
    load_task,
    new_incarnation,
    now_iso,
    prompt_hash,
    quarantine_task,
    read_ack_for_step,
    read_heartbeat,
    read_json,
    read_jsonl,
    read_result_for_step,
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
    tasks = tmp_path / "tasks"
    monkeypatch.setattr("duo.protocol.TASKS_DIR", tasks)
    monkeypatch.setattr("duo.protocol._CORRUPTED_DIR", tasks / "_corrupted")


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear task cache between tests."""
    _clear_task_cache()
    yield
    _clear_task_cache()


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
        assert dt.tzinfo == UTC


# ---------------------------------------------------------------------------
# new_incarnation
# ---------------------------------------------------------------------------


class TestNewIncarnation:
    def test_returns_16_char_hex(self):
        inc = new_incarnation()
        assert len(inc) == 16
        assert re.fullmatch(r"[0-9a-f]{16}", inc)

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

    def test_read_permission_denied(self, tmp_path: Path):
        """read_json returns None on PermissionError (not just FileNotFoundError)."""
        path = tmp_path / "locked.json"
        path.write_text('{"secret": true}')
        path.chmod(0o000)
        try:
            assert read_json(path) is None
        finally:
            path.chmod(0o644)


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

    def test_read_jsonl_truncated_line(self):
        """read_jsonl skips malformed (truncated) lines gracefully."""
        task = create_task("jsonl-trunc", "d", "/w", "b", "c", [_make_subtask()])
        journal = task.journal_path
        journal.write_text('{"event":"ok"}\n{broken json\n{"event":"ok2"}\n')
        events = read_jsonl(journal)
        assert len(events) == 2
        assert events[0]["event"] == "ok"
        assert events[1]["event"] == "ok2"

    def test_read_jsonl_empty_lines(self):
        """read_jsonl handles empty lines without crashing."""
        task = create_task("jsonl-empty", "d", "/w", "b", "c", [_make_subtask()])
        journal = task.journal_path
        journal.write_text('\n\n{"event":"valid"}\n\n')
        events = read_jsonl(journal)
        assert len(events) == 1


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
        assert TRANSITIONS[TaskStatus.COMPLETED] == frozenset()

    def test_transitions_is_immutable(self):
        with pytest.raises(TypeError):
            TRANSITIONS[TaskStatus.COMPLETED] = frozenset({TaskStatus.CREATED})  # type: ignore[index]

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
        assert transition(task, TaskStatus.SESSION_STARTING) is True
        assert task.status == TaskStatus.SESSION_STARTING

    def test_transition_function_rejects_invalid(self, tmp_path: Path):
        task = create_task("fsm-bad", "desc", "/w", "b", "abc", [_make_subtask()])
        assert (
            transition(task, TaskStatus.COMPLETED) is False
        )  # CREATED→COMPLETED illegal
        assert task.status == TaskStatus.CREATED  # unchanged

        events = read_jsonl(task.journal_path)
        invalid_events = [e for e in events if e["event"] == "invalid_transition"]
        assert len(invalid_events) == 1

    def test_transition_from_every_terminal_state_raises(self):
        """COMPLETED rejects all transitions; FAILED and ESCALATED reject invalid ones."""
        # COMPLETED has empty transition set — truly terminal
        task_c = create_task("term-completed", "d", "/w", "b", "c", [_make_subtask()])
        task_c.status = TaskStatus.COMPLETED
        save_task(task_c)
        for target in TaskStatus:
            if target == TaskStatus.COMPLETED:
                continue
            transition(task_c, target)
            assert task_c.status == TaskStatus.COMPLETED

        # FAILED rejects everything except SESSION_STARTING
        task_f = create_task("term-failed", "d", "/w", "b", "c", [_make_subtask()])
        task_f.status = TaskStatus.FAILED
        save_task(task_f)
        transition(task_f, TaskStatus.COMPLETED)
        assert task_f.status == TaskStatus.FAILED
        transition(task_f, TaskStatus.RUNNING)
        assert task_f.status == TaskStatus.FAILED

        # ESCALATED rejects everything except PROMPT_SENT and FAILED
        task_e = create_task("term-escalated", "d", "/w", "b", "c", [_make_subtask()])
        task_e.status = TaskStatus.ESCALATED
        save_task(task_e)
        transition(task_e, TaskStatus.COMPLETED)
        assert task_e.status == TaskStatus.ESCALATED
        transition(task_e, TaskStatus.RUNNING)
        assert task_e.status == TaskStatus.ESCALATED

    def test_transition_all_valid_paths(self):
        """Every transition defined in TRANSITIONS dict succeeds."""
        for src, dsts in TRANSITIONS.items():
            for dst in dsts:
                tid = f"vp-{src.value}-to-{dst.value}"
                task = create_task(tid, "d", "/w", "b", "c", [_make_subtask()])
                task.status = src
                save_task(task)
                transition(task, dst)
                assert task.status == dst, f"{src} → {dst} should be valid"

    def test_every_non_terminal_state_can_reach_failed(self):
        """All non-terminal states must have a path to FAILED."""
        terminal = {TaskStatus.COMPLETED}
        for state in TaskStatus:
            if state in terminal:
                continue
            # BFS to find FAILED from this state
            visited: set[TaskStatus] = set()
            queue = [state]
            found = False
            while queue:
                current = queue.pop(0)
                if current == TaskStatus.FAILED:
                    found = True
                    break
                if current in visited:
                    continue
                visited.add(current)
                queue.extend(TRANSITIONS.get(current, frozenset()))
            assert found, f"{state.value} cannot reach FAILED"

    def test_every_non_terminal_state_can_reach_blocked(self):
        """All active (non-terminal, non-recovery) states allow → BLOCKED."""
        # COMPLETED is terminal, FAILED is a recovery state (only → SESSION_STARTING),
        # BLOCKED is already the target state.
        skip = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED}
        for state in TaskStatus:
            if state in skip:
                continue
            assert TaskStatus.BLOCKED in TRANSITIONS[state], (
                f"{state.value} should allow → BLOCKED"
            )

    def test_completed_has_no_outgoing_transitions(self):
        """COMPLETED is truly terminal — no outgoing edges."""
        assert len(TRANSITIONS[TaskStatus.COMPLETED]) == 0

    def test_every_state_is_in_transitions_dict(self):
        """Every TaskStatus value has an entry in TRANSITIONS."""
        for state in TaskStatus:
            assert state in TRANSITIONS, f"{state.value} missing from TRANSITIONS"


# ---------------------------------------------------------------------------
# create_task / save_task / load_task
# ---------------------------------------------------------------------------


class TestTaskCRUD:
    def test_create_task_empty_subtasks_raises(self):
        with pytest.raises(ValueError, match="Task must have at least one subtask"):
            create_task(
                task_id="empty-sub",
                description="No subtasks",
                worktree="/work",
                branch="feature",
                base_commit="deadbeef",
                subtasks=[],
            )

    def test_create_task_duplicate_raises(self):
        """create_task rejects duplicate task IDs."""
        create_task(
            task_id="dup-detect",
            description="First",
            worktree="/work",
            branch="feature",
            base_commit="deadbeef",
            subtasks=[_make_subtask(1)],
        )
        with pytest.raises(ValueError, match="already exists"):
            create_task(
                task_id="dup-detect",
                description="Second",
                worktree="/work2",
                branch="feature2",
                base_commit="deadbeef2",
                subtasks=[_make_subtask(1)],
            )

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

    def test_load_corrupted_task_json(self):
        """load_task returns None for malformed task.json (missing keys)."""
        import duo.protocol

        task_dir = duo.protocol.TASKS_DIR / "corrupted-task"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task.json").write_text('{"id": "corrupted-task"}')
        assert load_task("corrupted-task") is None

    def test_load_invalid_status_task(self):
        """load_task returns None when status value is invalid."""
        import duo.protocol

        task_dir = duo.protocol.TASKS_DIR / "bad-status"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task.json").write_text(
            '{"id":"bad-status","description":"x","worktree":"/w",'
            '"base_commit":"c","branch":"b","status":"INVALID_STATUS",'
            '"current_step":0,"current_attempt":1,"subtasks":[],'
            '"created_at":"2025-01-01T00:00:00","incarnation_id":"abc",'
            '"pane_label":"p","security_policy":{}}'
        )
        assert load_task("bad-status") is None

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
        task = create_task(
            "dirs-test", "d", "/w", "b", "c", [_make_subtask(1), _make_subtask(2)]
        )
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

    def test_load_task_invalid_subtasks(self):
        """load_task returns None when subtasks is not a list."""
        import duo.protocol

        task_dir = duo.protocol.TASKS_DIR / "bad-subtasks"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task.json").write_text(
            '{"id":"bad-subtasks","description":"x","worktree":"/w",'
            '"base_commit":"c","branch":"b","status":"created",'
            '"current_step":1,"current_attempt":1,"subtasks":"not-a-list",'
            '"created_at":"2025-01-01T00:00:00","incarnation_id":"abc",'
            '"pane_label":"p","security_policy":{}}'
        )
        assert load_task("bad-subtasks") is None

    def test_load_task_invalid_current_step(self):
        """load_task returns None when current_step is not an int."""
        import duo.protocol

        task_dir = duo.protocol.TASKS_DIR / "bad-step"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task.json").write_text(
            '{"id":"bad-step","description":"x","worktree":"/w",'
            '"base_commit":"c","branch":"b","status":"created",'
            '"current_step":"not-an-int","current_attempt":1,'
            '"subtasks":[{"step_id":1,"description":"s","target_files":[],"writable_paths":[]}],'
            '"created_at":"2025-01-01T00:00:00","incarnation_id":"abc",'
            '"pane_label":"p","security_policy":{}}'
        )
        assert load_task("bad-step") is None

    def test_load_task_current_step_zero_out_of_range(self):
        """current_step=0 is out of range (1-based)."""
        import duo.protocol

        task_dir = duo.protocol.TASKS_DIR / "bad-zero"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task.json").write_text(
            '{"id":"bad-zero","description":"x","worktree":"/w",'
            '"base_commit":"c","branch":"b","status":"created",'
            '"current_step":0,"current_attempt":1,'
            '"subtasks":[{"step_id":1,"description":"s","target_files":[],"writable_paths":[]}],'
            '"created_at":"2025-01-01T00:00:00","incarnation_id":"abc",'
            '"pane_label":"p","security_policy":{}}'
        )
        assert load_task("bad-zero") is None

    def test_load_task_current_step_negative_out_of_range(self):
        """current_step=-1 is out of range."""
        import duo.protocol

        task_dir = duo.protocol.TASKS_DIR / "bad-neg"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task.json").write_text(
            '{"id":"bad-neg","description":"x","worktree":"/w",'
            '"base_commit":"c","branch":"b","status":"created",'
            '"current_step":-1,"current_attempt":1,'
            '"subtasks":[{"step_id":1,"description":"s","target_files":[],"writable_paths":[]}],'
            '"created_at":"2025-01-01T00:00:00","incarnation_id":"abc",'
            '"pane_label":"p","security_policy":{}}'
        )
        assert load_task("bad-neg") is None

    def test_load_task_invalid_current_attempt(self):
        """Non-integer current_attempt is rejected."""
        import duo.protocol

        task_dir = duo.protocol.TASKS_DIR / "bad-att"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task.json").write_text(
            '{"id":"bad-att","description":"x","worktree":"/w",'
            '"base_commit":"c","branch":"b","status":"created",'
            '"current_step":1,"current_attempt":"one",'
            '"subtasks":[{"step_id":1,"description":"s","target_files":[],"writable_paths":[]}],'
            '"created_at":"2025-01-01T00:00:00","incarnation_id":"abc",'
            '"pane_label":"p","security_policy":{}}'
        )
        assert load_task("bad-att") is None

    def test_load_task_invalid_status(self):
        """Non-string status is rejected."""
        import duo.protocol

        task_dir = duo.protocol.TASKS_DIR / "bad-stat"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task.json").write_text(
            '{"id":"bad-stat","description":"x","worktree":"/w",'
            '"base_commit":"c","branch":"b","status":42,'
            '"current_step":1,"current_attempt":1,'
            '"subtasks":[{"step_id":1,"description":"s","target_files":[],"writable_paths":[]}],'
            '"created_at":"2025-01-01T00:00:00","incarnation_id":"abc",'
            '"pane_label":"p","security_policy":{}}'
        )
        assert load_task("bad-stat") is None

    @pytest.mark.parametrize("field", ["id", "description", "worktree", "branch"])
    def test_load_task_invalid_string_fields(self, field: str):
        """Non-string value for required string fields is rejected."""
        import duo.protocol

        tid = f"bad-{field}"
        task_dir = duo.protocol.TASKS_DIR / tid
        task_dir.mkdir(parents=True, exist_ok=True)
        base = {
            "id": tid,
            "description": "x",
            "worktree": "/w",
            "base_commit": "c",
            "branch": "b",
            "status": "created",
            "current_step": 1,
            "current_attempt": 1,
            "subtasks": [
                {
                    "step_id": 1,
                    "description": "s",
                    "target_files": [],
                    "writable_paths": [],
                }
            ],
            "created_at": "2025-01-01T00:00:00",
            "incarnation_id": "abc",
            "pane_label": "p",
            "security_policy": {},
        }
        base[field] = 999
        import json

        (task_dir / "task.json").write_text(json.dumps(base))
        assert load_task(tid) is None


class TestLoadTaskSecretPatternMerge:
    """load_task() merges saved secret patterns with current defaults."""

    def test_empty_saved_patterns_get_defaults(self):
        """Task saved with empty secret_patterns gets all current defaults."""
        import duo.protocol

        task_dir = duo.protocol.TASKS_DIR / "merge-empty"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task.json").write_text(
            '{"id":"merge-empty","description":"x","worktree":"/w",'
            '"base_commit":"c","branch":"b","status":"created",'
            '"current_step":1,"current_attempt":1,'
            '"subtasks":[{"step_id":1,"description":"s","target_files":[],"writable_paths":[]}],'
            '"created_at":"2025-01-01T00:00:00","incarnation_id":"abc",'
            '"pane_label":"p","security_policy":{"secret_patterns":[]}}'
        )
        task = load_task("merge-empty")
        assert task is not None
        assert len(task.security_policy.secret_patterns) >= len(DEFAULT_SECRET_PATTERNS)
        for pat in DEFAULT_SECRET_PATTERNS:
            assert pat in task.security_policy.secret_patterns

    def test_custom_patterns_preserved(self):
        """Custom patterns from saved task are preserved alongside defaults."""
        import duo.protocol

        task_dir = duo.protocol.TASKS_DIR / "merge-custom"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task.json").write_text(
            '{"id":"merge-custom","description":"x","worktree":"/w",'
            '"base_commit":"c","branch":"b","status":"created",'
            '"current_step":1,"current_attempt":1,'
            '"subtasks":[{"step_id":1,"description":"s","target_files":[],"writable_paths":[]}],'
            '"created_at":"2025-01-01T00:00:00","incarnation_id":"abc",'
            '"pane_label":"p","security_policy":{"secret_patterns":["MY_CUSTOM_SECRET="]}}'
        )
        task = load_task("merge-custom")
        assert task is not None
        assert "MY_CUSTOM_SECRET=" in task.security_policy.secret_patterns
        for pat in DEFAULT_SECRET_PATTERNS:
            assert pat in task.security_policy.secret_patterns

    def test_no_duplicates_in_merged_patterns(self):
        """Patterns shared between saved and defaults don't duplicate."""
        import duo.protocol

        task_dir = duo.protocol.TASKS_DIR / "merge-dedup"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task.json").write_text(
            '{"id":"merge-dedup","description":"x","worktree":"/w",'
            '"base_commit":"c","branch":"b","status":"created",'
            '"current_step":1,"current_attempt":1,'
            '"subtasks":[{"step_id":1,"description":"s","target_files":[],"writable_paths":[]}],'
            '"created_at":"2025-01-01T00:00:00","incarnation_id":"abc",'
            '"pane_label":"p","security_policy":{"secret_patterns":["API_KEY=","MY_EXTRA="]}}'
        )
        task = load_task("merge-dedup")
        assert task is not None
        # API_KEY= appears in defaults and saved — should only appear once
        assert task.security_policy.secret_patterns.count("API_KEY=") == 1
        assert "MY_EXTRA=" in task.security_policy.secret_patterns


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

    def test_journal_non_ascii(self):
        """Journal handles non-ASCII characters (emoji, CJK)."""
        task = create_task(
            task_id="unicode-test",
            description="🚀 部署 API テスト",
            worktree="/w",
            branch="b",
            base_commit="c",
            subtasks=[_make_subtask()],
        )
        append_event(task, "custom_event", {"msg": "你好世界 🎉"})
        events = read_jsonl(task.journal_path)
        assert any("你好世界" in json.dumps(e, ensure_ascii=False) for e in events)


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

    def test_replay_state_empty_journal(self):
        """replay_state with missing journal file returns CREATED."""
        task = create_task("replay-nojrnl", "d", "/w", "b", "c", [_make_subtask()])
        task.journal_path.unlink()
        assert replay_state(task) == TaskStatus.CREATED

    def test_replay_state_with_events(self):
        """replay_state correctly reconstructs task state from journal."""
        task = create_task("replay-recon", "d", "/w", "b", "c", [_make_subtask()])
        transition(task, TaskStatus.SESSION_STARTING)
        transition(task, TaskStatus.PROMPT_SENT)
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)

        reconstructed = replay_state(task)
        assert reconstructed == TaskStatus.RUNNING
        assert reconstructed == task.status


# ---------------------------------------------------------------------------
# Additional JSON I/O edge cases
# ---------------------------------------------------------------------------


class TestJsonIOEdgeCases:
    def test_write_json_creates_parent_dirs(self, tmp_path: Path):
        """write to deeply nested non-existent path creates parents."""
        path = tmp_path / "deep" / "nested" / "dir" / "data.json"
        assert not path.parent.exists()
        write_json(path, {"created": True})
        assert path.exists()
        assert read_json(path) == {"created": True}

    def test_read_json_malformed_file(self, tmp_path: Path):
        """read file with invalid JSON returns None."""
        bad = tmp_path / "corrupt.json"
        bad.write_text("{{{{not json at all!!")
        result = read_json(bad)
        assert result is None

    def test_read_json_unicode_decode_error(self, tmp_path: Path):
        """read_json returns None when file contains invalid UTF-8."""
        bad = tmp_path / "binary.json"
        bad.write_bytes(b"\x80\x81\x82\x83")
        result = read_json(bad)
        assert result is None

    def test_write_json_creates_parent_dirs_under_tasks_dir(self):
        """write_json creates parent directories under TASKS_DIR if needed."""
        import duo.protocol

        deep_path = duo.protocol.TASKS_DIR / "deep" / "nested" / "file.json"
        write_json(deep_path, {"key": "value"})
        assert deep_path.exists()
        data = read_json(deep_path)
        assert data["key"] == "value"


# ---------------------------------------------------------------------------
# append_event edge cases
# ---------------------------------------------------------------------------


class TestAppendEventEdgeCases:
    def test_append_event_creates_journal(self, tmp_path: Path):
        """append_event to non-existent journal file creates it."""
        task = create_task("journal-new", "d", "/w", "b", "c", [_make_subtask()])
        # Remove the journal that create_task wrote
        if task.journal_path.exists():
            task.journal_path.unlink()
        assert not task.journal_path.exists()

        append_event(task, "test_event", {"key": "value"})

        assert task.journal_path.exists()
        events = read_jsonl(task.journal_path)
        test_events = [e for e in events if e["event"] == "test_event"]
        assert len(test_events) == 1
        assert test_events[0]["data"] == {"key": "value"}

    def test_journal_rotation_on_max_size(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Journal rotates when exceeding MAX_JOURNAL_BYTES."""
        import duo.protocol

        task = create_task("rotate-task", "d", "/w", "b", "c", [_make_subtask()])
        # Write enough data at normal threshold
        for i in range(20):
            append_event(task, f"event_{i}", {"i": i})
        lines_before = len(task.journal_path.read_text().splitlines())
        # Now lower the threshold so next write triggers rotation
        monkeypatch.setattr(duo.protocol, "MAX_JOURNAL_BYTES", 100)
        append_event(task, "trigger_rotate", {"final": True})
        lines_after = len(task.journal_path.read_text().splitlines())
        assert lines_after < lines_before

    def test_journal_rotation_oserror_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Journal rotation OSError is caught and event still appended."""
        import duo.protocol

        task = create_task("rotate-err", "d", "/w", "b", "c", [_make_subtask()])
        for i in range(20):
            append_event(task, f"event_{i}", {"i": i})
        monkeypatch.setattr(duo.protocol, "MAX_JOURNAL_BYTES", 100)
        # Patch Path.read_text to fail only for the journal path
        _orig_read_text = Path.read_text

        def _guarded_read_text(self, *a, **kw):
            if self == task.journal_path:
                raise OSError("simulated disk error")
            return _orig_read_text(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", _guarded_read_text)
        # Should NOT raise — OSError caught in rotation, event still appended
        append_event(task, "after_error", {})
        # Restore read_text only (keep TASKS_DIR patched) to verify journal
        monkeypatch.setattr(Path, "read_text", _orig_read_text)
        content = task.journal_path.read_text()
        assert "after_error" in content


# ---------------------------------------------------------------------------
# list_tasks edge cases
# ---------------------------------------------------------------------------


class TestListTasksEdgeCases:
    def test_list_tasks_empty_dir(self):
        """list_tasks when TASKS_DIR is empty returns empty list."""
        import duo.protocol

        duo.protocol.TASKS_DIR.mkdir(parents=True, exist_ok=True)
        _clear_task_cache()
        assert list_tasks() == []

    def test_list_tasks_skips_non_dir_entries(self):
        """list_tasks ignores regular files in TASKS_DIR."""
        import duo.protocol

        duo.protocol.TASKS_DIR.mkdir(parents=True, exist_ok=True)
        (duo.protocol.TASKS_DIR / "stray-file.txt").write_text("not a task")
        _clear_task_cache()
        assert list_tasks() == []

    def test_list_tasks_skips_dir_without_task_json(self):
        """list_tasks ignores directories missing task.json."""
        import duo.protocol

        duo.protocol.TASKS_DIR.mkdir(parents=True, exist_ok=True)
        (duo.protocol.TASKS_DIR / "broken-task").mkdir()
        _clear_task_cache()
        assert list_tasks() == []


# ---------------------------------------------------------------------------
# save / load round-trip
# ---------------------------------------------------------------------------


class TestSaveTaskRoundtrip:
    def test_save_task_roundtrip(self):
        """save then load, verify all fields match."""
        subtasks = [_make_subtask(1), _make_subtask(2)]
        task = create_task(
            "rt-full",
            "Full roundtrip test",
            "/my/worktree",
            "feat-branch",
            "deadbeef",
            subtasks,
        )
        task.current_step = 2
        task.current_attempt = 3
        task.last_prompt_sent_at = now_iso()
        task.session_started_at = now_iso()
        task.pane_label = "custom-pane"
        save_task(task)

        loaded = load_task("rt-full")
        assert loaded is not None
        assert loaded.id == task.id
        assert loaded.description == task.description
        assert loaded.worktree == task.worktree
        assert loaded.branch == task.branch
        assert loaded.base_commit == task.base_commit
        assert loaded.pane_label == task.pane_label
        assert loaded.incarnation_id == task.incarnation_id
        assert loaded.status == task.status
        assert loaded.current_step == 2
        assert loaded.current_attempt == 3
        assert loaded.last_prompt_sent_at == task.last_prompt_sent_at
        assert loaded.session_started_at == task.session_started_at
        assert loaded.created_at == task.created_at
        assert len(loaded.subtasks) == 2
        assert loaded.subtasks[0].step_id == 1
        assert loaded.subtasks[1].step_id == 2
        assert loaded.subtasks[0].description == "step-1"
        assert loaded.subtasks[1].description == "step-2"
        assert (
            loaded.security_policy.allow_network == task.security_policy.allow_network
        )
        assert (
            loaded.security_policy.secret_patterns
            == task.security_policy.secret_patterns
        )


# ---------------------------------------------------------------------------
# read_heartbeat
# ---------------------------------------------------------------------------


class TestReadHeartbeat:
    def test_read_heartbeat_valid(self):
        """Write a valid heartbeat JSON, read it back, verify fields."""
        task = create_task("hb-valid", "d", "/w", "b", "c", [_make_subtask()])
        write_json(
            task.heartbeat_path,
            {
                "ts": "2025-01-01T00:00:00+00:00",
                "incarnation": "abcd1234",
                "step": 1,
                "status": "running",
                "current_file": "main.py",
            },
        )
        hb = read_heartbeat(task)
        assert hb is not None
        assert isinstance(hb, Heartbeat)
        assert hb.ts == "2025-01-01T00:00:00+00:00"
        assert hb.incarnation == "abcd1234"
        assert hb.step == 1
        assert hb.status == "running"
        assert hb.current_file == "main.py"

    def test_read_heartbeat_missing(self):
        """No heartbeat file exists, returns None."""
        task = create_task("hb-miss", "d", "/w", "b", "c", [_make_subtask()])
        assert read_heartbeat(task) is None

    def test_read_heartbeat_malformed(self):
        """Write invalid JSON, verify graceful handling (returns None)."""
        task = create_task("hb-bad", "d", "/w", "b", "c", [_make_subtask()])
        task.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        task.heartbeat_path.write_text("not valid json {{{")
        assert read_heartbeat(task) is None


# ---------------------------------------------------------------------------
# read_ack_for_step
# ---------------------------------------------------------------------------


class TestReadAckForStep:
    def test_read_ack_valid(self):
        """Write valid ack JSON, read back, verify fields."""
        task = create_task("ack-valid", "d", "/w", "b", "c", [_make_subtask()])
        write_json(
            task.ack_path(1, 1),
            {
                "step": 1,
                "attempt": 1,
                "incarnation": "beef0001",
                "prompt_hash": "aabb1122",
                "acked_at": "2025-01-01T00:00:00+00:00",
            },
        )
        ack = read_ack_for_step(task, 1, 1)
        assert ack is not None
        assert isinstance(ack, AckResult)
        assert ack.step == 1
        assert ack.attempt == 1
        assert ack.incarnation == "beef0001"
        assert ack.prompt_hash == "aabb1122"
        assert ack.acked_at == "2025-01-01T00:00:00+00:00"

    def test_read_ack_missing(self):
        """No ack file, returns None."""
        task = create_task("ack-miss", "d", "/w", "b", "c", [_make_subtask()])
        assert read_ack_for_step(task, 1, 1) is None


# ---------------------------------------------------------------------------
# read_result_for_step
# ---------------------------------------------------------------------------


class TestReadResultForStep:
    def test_read_result_valid(self):
        """Write valid result JSON with status/summary/files_changed, read back."""
        task = create_task("res-valid", "d", "/w", "b", "c", [_make_subtask()])
        write_json(
            task.result_path(1, 1),
            {
                "step": 1,
                "attempt": 1,
                "incarnation": "dead0001",
                "status": "completed",
                "files_changed": ["src/main.py", "tests/test_main.py"],
                "summary": "Implemented feature X",
                "reason": "",
            },
        )
        result = read_result_for_step(task, 1, 1)
        assert result is not None
        assert isinstance(result, StepResult)
        assert result.step == 1
        assert result.attempt == 1
        assert result.incarnation == "dead0001"
        assert result.status == "completed"
        assert result.files_changed == ["src/main.py", "tests/test_main.py"]
        assert result.summary == "Implemented feature X"
        assert result.reason == ""

    def test_read_result_missing(self):
        """No result file, returns None."""
        task = create_task("res-miss", "d", "/w", "b", "c", [_make_subtask()])
        assert read_result_for_step(task, 1, 1) is None

    def test_read_result_malformed(self):
        """Invalid JSON, graceful handling (returns None)."""
        task = create_task("res-bad", "d", "/w", "b", "c", [_make_subtask()])
        step_dir = task.result_path(1, 1).parent
        step_dir.mkdir(parents=True, exist_ok=True)
        task.result_path(1, 1).write_text("corrupt data!!!")
        assert read_result_for_step(task, 1, 1) is None


# ---------------------------------------------------------------------------
# write_json OSError handling (lines 207-209)
# ---------------------------------------------------------------------------


class TestWriteJsonOSError:
    def test_oserror_cleans_tmp_and_reraises(self, tmp_path: Path):
        """write_json removes temp file on OSError and re-raises (lines 207-209)."""
        path = tmp_path / "target.json"
        # Make target a directory so rename raises IsADirectoryError (subclass of OSError)
        path.mkdir()

        with pytest.raises(OSError):
            write_json(path, {"key": "value"})

        # No leftover temp files
        tmp_files = [f for f in tmp_path.iterdir() if ".tmp" in f.name]
        assert len(tmp_files) == 0


# ---------------------------------------------------------------------------
# replay_state malformed events (lines 509-510)
# ---------------------------------------------------------------------------


class TestReplayStateMalformed:
    def test_missing_data_key(self):
        """replay_state handles status_changed event with no 'data' key (line 509)."""
        task = create_task("replay-nodata", "d", "/w", "b", "c", [_make_subtask()])
        with open(task.journal_path, "a") as f:
            f.write(json.dumps({"event": "status_changed"}) + "\n")
        assert replay_state(task) == TaskStatus.CREATED

    def test_missing_to_key(self):
        """replay_state handles status_changed event with no 'to' key (line 509)."""
        task = create_task("replay-noto", "d", "/w", "b", "c", [_make_subtask()])
        with open(task.journal_path, "a") as f:
            f.write(json.dumps({"event": "status_changed", "data": {}}) + "\n")
        assert replay_state(task) == TaskStatus.CREATED

    def test_invalid_status_value(self):
        """replay_state handles invalid status value (line 510)."""
        task = create_task("replay-badval", "d", "/w", "b", "c", [_make_subtask()])
        with open(task.journal_path, "a") as f:
            f.write(
                json.dumps({"event": "status_changed", "data": {"to": "bogus_status"}})
                + "\n"
            )
        assert replay_state(task) == TaskStatus.CREATED

    def test_last_status_wins(self):
        """replay_state uses the last status_changed event."""
        task = create_task("replay-last", "d", "/w", "b", "c", [_make_subtask()])
        with open(task.journal_path, "a") as f:
            f.write(
                json.dumps(
                    {"event": "status_changed", "data": {"to": "session_starting"}}
                )
                + "\n"
            )
            f.write(
                json.dumps({"event": "status_changed", "data": {"to": "failed"}}) + "\n"
            )
        assert replay_state(task) == TaskStatus.FAILED


# ---------------------------------------------------------------------------
# Security: path traversal protection
# ---------------------------------------------------------------------------


class TestWriteJsonPathTraversal:
    """write_json rejects paths containing '..' components."""

    def test_write_json_path_traversal_rejected(self, tmp_path: Path):
        """write_json raises ValueError when path contains '..' parts."""
        bad_path = tmp_path / "safe" / ".." / "escaped" / "data.json"
        with pytest.raises(ValueError, match="Path traversal detected"):
            write_json(bad_path, {"key": "value"})

    def test_write_json_symlink_rejected(self, tmp_path: Path):
        """write_json raises ValueError when path is a symlink."""
        target = tmp_path / "target.json"
        target.write_text("{}")
        link = tmp_path / "link.json"
        link.symlink_to(target)
        with pytest.raises(ValueError, match="symlink"):
            write_json(link, {"key": "value"})


class TestWriteJsonSerialization:
    """write_json validates data serialization and size."""

    def test_non_serializable_raises(self, tmp_path: Path):
        """write_json rejects objects that json.dumps cannot serialize."""
        path = tmp_path / "bad.json"
        with pytest.raises(ValueError, match="not JSON-serializable"):
            write_json(path, {"func": object()})

    def test_large_payload_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """write_json rejects payloads exceeding the size limit."""
        import duo.protocol

        monkeypatch.setattr(duo.protocol, "_MAX_JSON_BYTES", 100)
        path = tmp_path / "big.json"
        with pytest.raises(ValueError, match="too large"):
            write_json(path, {"data": "x" * 200})

    def test_unicode_roundtrips(self, tmp_path: Path):
        """write_json handles non-ASCII content correctly."""
        path = tmp_path / "unicode.json"
        data = {"emoji": "🚀", "chinese": "你好世界", "japanese": "こんにちは"}
        write_json(path, data)
        loaded = read_json(path)
        assert loaded == data


# ---------------------------------------------------------------------------
# Performance: list_tasks with many tasks
# ---------------------------------------------------------------------------


class TestListTasksPerformance:
    def test_list_tasks_performance_many_tasks(self):
        """Create 50 tasks, verify list_tasks completes in <1 second."""
        TASKS_DIR.mkdir(parents=True, exist_ok=True)
        _clear_task_cache()

        for i in range(50):
            create_task(
                f"perf-task-{i:03d}",
                f"Performance test task {i}",
                "/work",
                "feature",
                "deadbeef",
                [_make_subtask(1), _make_subtask(2)],
            )

        start = time.monotonic()
        tasks = list_tasks()
        elapsed = time.monotonic() - start

        assert len(tasks) == 50
        assert elapsed < 1.0, f"list_tasks took {elapsed:.3f}s, expected <1s"

        # Second call should be faster (mtime cache hit)
        start2 = time.monotonic()
        tasks2 = list_tasks()
        elapsed2 = time.monotonic() - start2

        assert len(tasks2) == 50
        # Cached call should not be dramatically slower (allow 3× jitter for system load)
        assert elapsed2 < max(elapsed * 3, 0.1), "Cached call unexpectedly slow"


# ---------------------------------------------------------------------------
# read_jsonl tail parameter
# ---------------------------------------------------------------------------


class TestReadJsonlTail:
    def test_tail_returns_last_n_events(self, tmp_path: Path):
        """Create journal with 100 events, verify tail=10 returns only last 10."""
        path = tmp_path / "big_journal.jsonl"
        lines = [json.dumps({"event": "test", "index": i}) for i in range(100)]
        path.write_text("\n".join(lines) + "\n")

        result = read_jsonl(path, tail=10)
        assert len(result) == 10
        assert result[0]["index"] == 90
        assert result[-1]["index"] == 99

    def test_tail_none_returns_all(self, tmp_path: Path):
        """Without tail, all events are returned."""
        path = tmp_path / "journal.jsonl"
        lines = [json.dumps({"i": i}) for i in range(50)]
        path.write_text("\n".join(lines) + "\n")

        result = read_jsonl(path)
        assert len(result) == 50

    def test_tail_larger_than_file(self, tmp_path: Path):
        """tail=100 on a 5-event file returns all 5."""
        path = tmp_path / "small.jsonl"
        lines = [json.dumps({"i": i}) for i in range(5)]
        path.write_text("\n".join(lines) + "\n")

        result = read_jsonl(path, tail=100)
        assert len(result) == 5

    def test_tail_skips_bad_lines(self, tmp_path: Path):
        """tail correctly skips bad lines and counts only valid ones."""
        path = tmp_path / "mixed.jsonl"
        content = ""
        for i in range(20):
            content += json.dumps({"i": i}) + "\n"
            if i % 5 == 0:
                content += "BAD LINE\n"
        path.write_text(content)

        result = read_jsonl(path, tail=5)
        assert len(result) == 5
        assert result[0]["i"] == 15
        assert result[-1]["i"] == 19

    def test_tail_zero_returns_empty(self, tmp_path: Path):
        """tail=0 returns empty list."""
        path = tmp_path / "journal.jsonl"
        lines = [json.dumps({"i": i}) for i in range(10)]
        path.write_text("\n".join(lines) + "\n")

        result = read_jsonl(path, tail=0)
        assert result == []

    def test_tail_missing_file(self, tmp_path: Path):
        """tail on missing file returns empty list."""
        result = read_jsonl(tmp_path / "nope.jsonl", tail=5)
        assert result == []


# ── Exhaustive illegal transition testing ────────────────────────────


class TestTransitionExhaustiveIllegal:
    def test_all_illegal_transitions_rejected(self):
        """Every (src, dst) pair NOT in TRANSITIONS must be rejected."""
        all_statuses = list(TaskStatus)
        rejected_count = 0
        for src in all_statuses:
            legal_targets = TRANSITIONS.get(src, set())
            for dst in all_statuses:
                if dst in legal_targets or dst == src:
                    continue
                # This transition should be illegal
                task = create_task(
                    f"test-{src.value}-{dst.value}",
                    "d",
                    "/w",
                    "b",
                    "c",
                    [_make_subtask()],
                )
                task.status = src
                save_task(task)
                transition(task, dst)
                # Status should NOT have changed
                assert task.status == src, (
                    f"Illegal transition {src} → {dst} was allowed!"
                )
                # Journal should have invalid_transition event
                events = read_jsonl(task.journal_path)
                invalid = [e for e in events if e.get("event") == "invalid_transition"]
                assert len(invalid) >= 1, (
                    f"No invalid_transition event for {src} → {dst}"
                )
                rejected_count += 1
        # Sanity: we tested a meaningful number of illegal transitions
        assert rejected_count > 100


# ---------------------------------------------------------------------------
# Quarantine / list_corrupted
# ---------------------------------------------------------------------------


class TestQuarantineTask:
    """Tests for quarantine_task()."""

    def test_quarantine_moves_dir(self) -> None:
        import duo.protocol as _p

        create_task("qtask", "d", "/w", "b", "c", [_make_subtask()])
        dst = quarantine_task("qtask", "bad json")
        assert dst is not None
        assert dst.exists()
        assert not (_p.TASKS_DIR / "qtask").exists()
        assert "_corrupted" in str(dst)

    def test_quarantine_nonexistent_returns_none(self) -> None:
        assert quarantine_task("no-such-task") is None

    def test_quarantine_removes_from_cache(self) -> None:
        create_task("cached", "d", "/w", "b", "c", [_make_subtask()])
        list_tasks()  # populate cache
        quarantine_task("cached", "corrupt")
        from duo.protocol import _task_cache

        assert "cached" not in _task_cache


class TestListCorrupted:
    """Tests for list_corrupted()."""

    def test_empty_when_no_corrupted_dir(self) -> None:
        assert list_corrupted() == []

    def test_lists_quarantined_tasks(self) -> None:
        create_task("bad1", "d", "/w", "b", "c", [_make_subtask()])
        create_task("bad2", "d", "/w", "b", "c", [_make_subtask()])
        quarantine_task("bad1", "reason1")
        quarantine_task("bad2", "reason2")
        items = list_corrupted()
        assert len(items) == 2
        names = [p.name for p in items]
        assert any("bad1" in n for n in names)
        assert any("bad2" in n for n in names)


class TestListTasksAutoQuarantine:
    """Test that list_tasks auto-quarantines corrupted tasks."""

    def test_corrupted_task_json_gets_quarantined(self) -> None:
        import duo.protocol as _p

        create_task("good", "d", "/w", "b", "c", [_make_subtask()])
        # Create a corrupted task (invalid JSON)
        bad_dir = _p.TASKS_DIR / "corrupt1"
        bad_dir.mkdir(parents=True, exist_ok=True)
        (bad_dir / "task.json").write_text("{{{invalid json")
        tasks = list_tasks()
        # Only the good task should be returned
        assert len(tasks) == 1
        assert tasks[0].id == "good"
        # The corrupted task should be quarantined
        assert list_corrupted()

    def test_underscore_dirs_skipped(self) -> None:
        import duo.protocol as _p

        create_task("ok", "d", "/w", "b", "c", [_make_subtask()])
        # Manually create _corrupted dir
        (_p.TASKS_DIR / "_corrupted").mkdir(parents=True, exist_ok=True)
        tasks = list_tasks()
        assert len(tasks) == 1
        assert tasks[0].id == "ok"


# === Edge-case tests: robustness of file I/O ===


class TestWriteJsonEdgeCases:
    """Edge cases for atomic JSON writing."""

    def test_unicode_emoji_roundtrip(self, tmp_path: Path) -> None:
        """JSON with emoji and CJK characters survives write+read."""
        data = {"msg": "🚀 部署完成 ✅", "emoji": "💻🔥🎉", "kanji": "漢字テスト"}
        path = tmp_path / "unicode.json"
        write_json(path, data)
        result = read_json(path)
        assert result == data

    def test_rejects_parent_symlink(self, tmp_path: Path) -> None:
        """write_json rejects paths where the file itself is a symlink."""
        real = tmp_path / "real.json"
        real.write_text("{}")
        link = tmp_path / "link.json"
        link.symlink_to(real)
        with pytest.raises(ValueError, match="symlink"):
            write_json(link, {"x": 1})

    def test_concurrent_writes_atomic(self, tmp_path: Path) -> None:
        """Concurrent writes to the same file don't produce corrupt JSON."""
        import threading

        path = tmp_path / "concurrent.json"
        errors: list[str] = []

        def writer(i: int) -> None:
            try:
                write_json(path, {"writer": i, "data": "x" * 100})
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        # File should be valid JSON (one of the writers won)
        result = read_json(path)
        assert result is not None
        assert "writer" in result

    def test_non_serializable_data_raises(self, tmp_path: Path) -> None:
        """Non-JSON-serializable data raises ValueError."""
        path = tmp_path / "bad.json"
        with pytest.raises(ValueError, match="not JSON-serializable"):
            write_json(path, {"fn": lambda: None})  # type: ignore[dict-item]


class TestReadJsonlEdgeCases:
    """Edge cases for JSONL reading."""

    def test_malformed_lines_skipped(self, tmp_path: Path) -> None:
        """Malformed JSON lines are silently skipped."""
        path = tmp_path / "journal.jsonl"
        path.write_text('{"ok": 1}\n{bad json\n{"ok": 2}\nnot json at all\n{"ok": 3}\n')
        events = read_jsonl(path)
        assert len(events) == 3
        assert events[0]["ok"] == 1
        assert events[2]["ok"] == 3

    def test_empty_lines_skipped(self, tmp_path: Path) -> None:
        """Empty lines and whitespace-only lines are skipped."""
        path = tmp_path / "journal.jsonl"
        path.write_text('{"a": 1}\n\n   \n{"b": 2}\n')
        events = read_jsonl(path)
        assert len(events) == 2

    def test_tail_with_malformed_lines(self, tmp_path: Path) -> None:
        """tail parameter works correctly even with malformed lines."""
        path = tmp_path / "journal.jsonl"
        lines = []
        for i in range(10):
            lines.append(f'{{"n": {i}}}')
            if i % 3 == 0:
                lines.append("{bad}")
        path.write_text("\n".join(lines) + "\n")
        events = read_jsonl(path, tail=3)
        assert len(events) == 3
        assert events[-1]["n"] == 9

    def test_nonexistent_file_returns_empty(self, tmp_path: Path) -> None:
        """Reading a nonexistent JSONL file returns empty list."""
        events = read_jsonl(tmp_path / "nope.jsonl")
        assert events == []

    def test_unicode_in_jsonl(self, tmp_path: Path) -> None:
        """JSONL with unicode content roundtrips correctly."""
        path = tmp_path / "unicode.jsonl"
        path.write_text('{"msg": "🚀 日本語"}\n{"msg": "中文测试"}\n')
        events = read_jsonl(path)
        assert events[0]["msg"] == "🚀 日本語"
        assert events[1]["msg"] == "中文测试"

    def test_oserror_returns_empty(self, tmp_path: Path) -> None:
        """read_jsonl returns [] when file triggers OSError (e.g., permission denied)."""
        path = tmp_path / "journal.jsonl"
        path.write_text('{"event":"a"}\n')
        with patch("builtins.open", side_effect=OSError("permission denied")):
            events = read_jsonl(path)
        assert events == []


class TestAtomicWriteText:
    """Tests for atomic_write_text helper."""

    def test_basic_write(self, tmp_path: Path) -> None:
        path = tmp_path / "test.txt"
        atomic_write_text(path, "hello world")
        assert path.read_text() == "hello world"

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "a" / "b" / "c.txt"
        atomic_write_text(path, "deep")
        assert path.read_text() == "deep"

    def test_overwrites_existing_atomically(self, tmp_path: Path) -> None:
        path = tmp_path / "overwrite.txt"
        path.write_text("old")
        atomic_write_text(path, "new")
        assert path.read_text() == "new"

    def test_no_tmp_files_left(self, tmp_path: Path) -> None:
        path = tmp_path / "clean.txt"
        atomic_write_text(path, "content")
        tmp_files = [f for f in tmp_path.iterdir() if ".tmp" in f.name]
        assert len(tmp_files) == 0

    def test_oserror_cleans_tmp(self, tmp_path: Path) -> None:
        """OSError during write cleans up tmp file."""
        path = tmp_path / "fail.txt"
        path.mkdir()  # Make it a directory so rename fails
        with pytest.raises(OSError):
            atomic_write_text(path, "will fail")
        tmp_files = [f for f in tmp_path.iterdir() if ".tmp" in f.name]
        assert len(tmp_files) == 0

    def test_unicode_content(self, tmp_path: Path) -> None:
        path = tmp_path / "unicode.txt"
        content = "🚀 日本語 中文 한국어"
        atomic_write_text(path, content)
        assert path.read_text() == content

    def test_atomic_write_text_parent_dir_missing(self) -> None:
        path = Path("/dev/null/a/b.txt")
        with pytest.raises(OSError):
            atomic_write_text(path, "should fail")

    def test_atomic_write_text_permission_denied(self, tmp_path: Path) -> None:
        path = tmp_path / "denied.txt"
        with patch("builtins.open", side_effect=PermissionError("denied")):
            with pytest.raises(PermissionError):
                atomic_write_text(path, "no access")
        tmp_files = [f for f in tmp_path.iterdir() if ".tmp" in f.name]
        assert len(tmp_files) == 0

    def test_atomic_write_text_unicode_content(self, tmp_path: Path) -> None:
        path = tmp_path / "full_unicode.txt"
        content = "👨‍👩‍👧‍👦 مرحبا \u0000 𠀀 ñ café \U0001f600"
        atomic_write_text(path, content)
        assert path.read_text() == content

    def test_atomic_write_text_very_long_content(self, tmp_path: Path) -> None:
        path = tmp_path / "large.txt"
        content = "x" * (1024 * 1024)
        atomic_write_text(path, content)
        assert path.read_text() == content

    def test_atomic_write_text_concurrent_writes(self, tmp_path: Path) -> None:
        path = tmp_path / "sequential.txt"
        atomic_write_text(path, "first")
        atomic_write_text(path, "second")
        assert path.read_text() == "second"

    def test_dir_fsync_failure_does_not_raise(self, tmp_path: Path) -> None:
        """Dir fsync failure after rename is non-fatal — file is still written."""
        path = tmp_path / "fsync_fail.txt"
        original_os_open = os.open

        def mock_os_open(p: str, flags: int) -> int:
            if flags == os.O_RDONLY and str(tmp_path) in str(p):
                raise OSError("dir open failed")
            return original_os_open(p, flags)

        with patch("duo.protocol.os.open", side_effect=mock_os_open):
            atomic_write_text(path, "survived")
        assert path.read_text() == "survived"


# ---------------------------------------------------------------------------
# Task isolation
# ---------------------------------------------------------------------------


class TestTaskIsolation:
    """Tests verifying tasks are isolated from each other at the protocol level."""

    def test_two_tasks_have_separate_directories(self) -> None:
        """Each task gets its own directory under TASKS_DIR."""
        t1 = create_task("iso-alpha", "A", "/wa", "b1", "c1", [_make_subtask()])
        t2 = create_task("iso-beta", "B", "/wb", "b2", "c2", [_make_subtask()])
        assert t1.dir != t2.dir
        assert t1.dir.is_dir()
        assert t2.dir.is_dir()
        assert (t1.dir / "task.json").exists()
        assert (t2.dir / "task.json").exists()

    def test_task_journal_isolation(self) -> None:
        """Events appended to one task's journal do not appear in another's."""
        t1 = create_task("jrnl-a", "A", "/wa", "b1", "c1", [_make_subtask()])
        t2 = create_task("jrnl-b", "B", "/wb", "b2", "c2", [_make_subtask()])

        append_event(t1, "ping", {"src": "a"})
        append_event(t1, "pong", {"src": "a"})

        events_a = [
            e for e in read_jsonl(t1.journal_path) if e.get("event") in ("ping", "pong")
        ]
        events_b = [
            e for e in read_jsonl(t2.journal_path) if e.get("event") in ("ping", "pong")
        ]
        assert len(events_a) == 2
        assert len(events_b) == 0

    def test_load_task_returns_correct_worktree(self) -> None:
        """Loading a task preserves the worktree it was created with."""
        create_task("wt-check", "W", "/path/a", "b", "c", [_make_subtask()])
        loaded = load_task("wt-check")
        assert loaded is not None
        assert loaded.worktree == "/path/a"

    def test_task_id_uniqueness(self) -> None:
        """Creating a second task with the same ID raises ValueError."""
        create_task("dup-id", "First", "/w1", "b1", "c1", [_make_subtask()])
        with pytest.raises(ValueError, match="already exists"):
            create_task("dup-id", "Second", "/w2", "b2", "c2", [_make_subtask()])


# ---------------------------------------------------------------------------
# Round BJ: Rubber-duck audit regression tests
# ---------------------------------------------------------------------------


class TestTransitionReturnValue:
    """transition() now returns bool: True on success, False on invalid."""

    def test_valid_transition_returns_true(self, tmp_path: Path):
        task = create_task("ret-ok", "d", "/w", "b", "c", [_make_subtask()])
        assert transition(task, TaskStatus.SESSION_STARTING) is True

    def test_invalid_transition_returns_false(self, tmp_path: Path):
        task = create_task("ret-bad", "d", "/w", "b", "c", [_make_subtask()])
        assert transition(task, TaskStatus.COMPLETED) is False

    def test_return_false_does_not_mutate_status(self, tmp_path: Path):
        task = create_task("ret-nomut", "d", "/w", "b", "c", [_make_subtask()])
        original = task.status
        transition(task, TaskStatus.COMPLETED)
        assert task.status == original


class TestIncarnationLength:
    """new_incarnation() now returns 16-char hex (64-bit)."""

    def test_length_is_16(self):
        inc = new_incarnation()
        assert len(inc) == 16

    def test_is_hex(self):
        inc = new_incarnation()
        int(inc, 16)  # Raises ValueError if not valid hex


class TestTransitionSaveBeforeJournal:
    """transition() must persist task.json before appending to journal."""

    def test_save_failure_prevents_journal_entry(self, tmp_path: Path):
        """If save_task raises, no journal entry should be written."""
        task = create_task("sav-fail", "d", "/w", "b", "c", [_make_subtask()])
        with patch("duo.protocol.save_task", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                transition(task, TaskStatus.SESSION_STARTING)
        # Journal should NOT contain a status_changed event
        events = read_jsonl(task.journal_path)
        status_events = [e for e in events if e.get("event") == "status_changed"]
        assert len(status_events) == 0

    def test_successful_transition_has_both(self, tmp_path: Path):
        """Normal transition persists both task.json and journal."""
        task = create_task("sav-ok", "d", "/w", "b", "c", [_make_subtask()])
        transition(task, TaskStatus.SESSION_STARTING)
        # Journal has the event
        events = read_jsonl(task.journal_path)
        status_events = [e for e in events if e.get("event") == "status_changed"]
        assert len(status_events) >= 1
        # task.json is persisted
        loaded = load_task(task.id)
        assert loaded is not None
        assert loaded.status == TaskStatus.SESSION_STARTING


class TestReadJsonlMalformed:
    """read_jsonl() logs warnings for malformed lines instead of crashing."""

    def test_malformed_line_skipped(self, tmp_path: Path):
        journal = tmp_path / "journal.jsonl"
        journal.write_text(
            '{"event":"ok","data":{}}\nnot valid json\n{"event":"also_ok","data":{}}\n'
        )
        entries = read_jsonl(journal)
        assert len(entries) == 2
        assert entries[0]["event"] == "ok"
        assert entries[1]["event"] == "also_ok"

    def test_empty_line_skipped(self, tmp_path: Path):
        journal = tmp_path / "journal.jsonl"
        journal.write_text('{"event":"a","data":{}}\n\n{"event":"b","data":{}}\n')
        entries = read_jsonl(journal)
        assert len(entries) == 2


class TestAppendEventFlock:
    """append_event() uses flock for concurrent-safe journal writes."""

    def test_append_event_writes_with_flock(self, tmp_path: Path):
        """Verify flock is called during append_event."""
        import fcntl

        task = create_task("flock-test", "d", "/w", "b", "c", [_make_subtask()])
        flock_calls: list[tuple[int, int]] = []
        original_flock = fcntl.flock

        def tracking_flock(fd: int, op: int) -> None:
            flock_calls.append((fd, op))
            return original_flock(fd, op)

        with patch("duo.protocol.fcntl.flock", side_effect=tracking_flock):
            append_event(task, "test_event", {"key": "value"})

        # Should have LOCK_EX and LOCK_UN
        ops = [op for _, op in flock_calls]
        assert fcntl.LOCK_EX in ops
        assert fcntl.LOCK_UN in ops

        # Verify event was written
        entries = read_jsonl(task.journal_path)
        test_entries = [e for e in entries if e["event"] == "test_event"]
        assert len(test_entries) == 1
        assert test_entries[0]["data"]["key"] == "value"


class TestAtomicWriteTextUUID:
    """atomic_write_text() uses full UUID for tmp filename uniqueness."""

    def test_write_succeeds(self, tmp_path: Path):
        target = tmp_path / "test.txt"
        atomic_write_text(target, "hello world")
        assert target.read_text() == "hello world"

    def test_no_leftover_tmp_files(self, tmp_path: Path):
        target = tmp_path / "test.txt"
        atomic_write_text(target, "content")
        # No tmp files should remain
        files = list(tmp_path.iterdir())
        assert files == [target]
