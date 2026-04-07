"""Tests for duo.protocol — helpers, file I/O, FSM, and task CRUD."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from duo.protocol import (
    TASKS_DIR,
    TRANSITIONS,
    AckResult,
    Heartbeat,
    StepResult,
    Subtask,
    TaskStatus,
    _clear_task_cache,
    append_event,
    create_task,
    list_tasks,
    load_task,
    new_incarnation,
    now_iso,
    prompt_hash,
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
        assert dt.tzinfo == UTC


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
        assert elapsed2 <= elapsed, "Cached call should not be slower"


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
