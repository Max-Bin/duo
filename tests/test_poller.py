"""Tests for duo.poller — age(), AdaptivePoller, and PollResult."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from duo import protocol
from duo.poller import (
    BASE_INTERVAL,
    HEARTBEAT_TIMEOUT,
    MAX_INTERVAL,
    RAMP_FACTOR,
    AdaptivePoller,
    PollResult,
    age,
)
from duo.protocol import (
    Subtask,
    create_task,
    write_json,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_subtask(step_id: int = 1) -> Subtask:
    return Subtask(
        step_id=step_id,
        description=f"step-{step_id}",
        target_files=["main.py"],
        writable_paths=["src/"],
    )


def _make_task(tmp_path: Path, **overrides) -> protocol.Task:
    """Create a real Task rooted under *tmp_path*."""
    return create_task(
        task_id=overrides.pop("task_id", "test-task"),
        description=overrides.pop("description", "unit test task"),
        worktree=str(tmp_path),
        branch="main",
        base_commit="abc1234",
        subtasks=[_make_subtask()],
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _ago_iso(seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


# ===================================================================
# age() tests
# ===================================================================


class TestAge:
    def test_recent_timestamp(self):
        ts = _now_iso()
        result = age(ts)
        assert 0 <= result < 5

    def test_old_timestamp(self):
        ts = _ago_iso(3600)
        result = age(ts)
        assert 3595 < result < 3605

    def test_invalid_string_returns_inf(self):
        assert age("not-a-timestamp") == float("inf")

    def test_none_returns_inf(self):
        assert age(None) == float("inf")

    def test_future_timestamp_returns_zero(self):
        """Future timestamps return 0, not negative."""
        future_ts = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        assert age(future_ts) == 0.0

    def test_timezone_naive_treated_as_utc(self):
        naive = datetime.now(UTC).replace(tzinfo=None).isoformat()
        result = age(naive)
        assert 0 <= result < 5


# ===================================================================
# AdaptivePoller._ramp / _reset tests
# ===================================================================


class TestAdaptivePollerRampReset:
    def test_ramp_increases_interval(self):
        p = AdaptivePoller()
        old = p.interval
        p._ramp()
        assert p.interval == old * RAMP_FACTOR

    def test_ramp_caps_at_max(self):
        p = AdaptivePoller()
        for _ in range(100):
            p._ramp()
        assert p.interval == MAX_INTERVAL

    def test_reset_restores_base(self):
        p = AdaptivePoller()
        p._ramp()
        p._ramp()
        p._reset()
        assert p.interval == BASE_INTERVAL


# ===================================================================
# AdaptivePoller.poll() tests
# ===================================================================


class TestPoll:
    """Integration-style tests using real Task objects and on-disk files."""

    def test_result_ready(self, tmp_path: Path):
        task = _make_task(tmp_path)
        write_json(
            task.result_path(task.current_step, task.current_attempt),
            {
                "step": task.current_step,
                "attempt": task.current_attempt,
                "incarnation": task.incarnation_id,
                "status": "done",
                "files_changed": [],
                "summary": "ok",
            },
        )
        poller = AdaptivePoller()
        assert poller.poll(task) == PollResult.RESULT_READY

    def test_result_ready_resets_interval(self, tmp_path: Path):
        task = _make_task(tmp_path)
        write_json(
            task.result_path(task.current_step, task.current_attempt),
            {
                "step": task.current_step,
                "attempt": task.current_attempt,
                "incarnation": task.incarnation_id,
                "status": "done",
                "files_changed": [],
                "summary": "ok",
            },
        )
        poller = AdaptivePoller()
        poller._ramp()
        poller.poll(task)
        assert poller.interval == BASE_INTERVAL

    def test_working_fresh_heartbeat(self, tmp_path: Path):
        task = _make_task(tmp_path)
        write_json(
            task.heartbeat_path,
            {
                "ts": _now_iso(),
                "incarnation": task.incarnation_id,
                "step": 1,
                "status": "working",
                "current_file": "test.py",
            },
        )
        poller = AdaptivePoller()
        assert poller.poll(task) == PollResult.WORKING

    def test_working_ramps_interval(self, tmp_path: Path):
        task = _make_task(tmp_path)
        write_json(
            task.heartbeat_path,
            {
                "ts": _now_iso(),
                "incarnation": task.incarnation_id,
                "step": 1,
                "status": "working",
                "current_file": "test.py",
            },
        )
        poller = AdaptivePoller()
        old = poller.interval
        poller.poll(task)
        assert poller.interval == old * RAMP_FACTOR

    def test_heartbeat_timeout_stale(self, tmp_path: Path):
        task = _make_task(tmp_path)
        write_json(
            task.heartbeat_path,
            {
                "ts": _ago_iso(HEARTBEAT_TIMEOUT + 30),
                "incarnation": task.incarnation_id,
                "step": 1,
                "status": "working",
                "current_file": "test.py",
            },
        )
        poller = AdaptivePoller()
        assert poller.poll(task) == PollResult.HEARTBEAT_TIMEOUT

    def test_grace_period_working(self, tmp_path: Path):
        """Prompt sent recently, no heartbeat yet → WORKING."""
        task = _make_task(tmp_path)
        task.last_prompt_sent_at = _now_iso()
        poller = AdaptivePoller()
        assert poller.poll(task) == PollResult.WORKING

    def test_prompt_sent_long_ago_timeout(self, tmp_path: Path):
        """Prompt sent long ago, no heartbeat → HEARTBEAT_TIMEOUT."""
        task = _make_task(tmp_path)
        task.last_prompt_sent_at = _ago_iso(HEARTBEAT_TIMEOUT + 30)
        poller = AdaptivePoller()
        assert poller.poll(task) == PollResult.HEARTBEAT_TIMEOUT

    def test_no_heartbeat_no_prompt_unknown(self, tmp_path: Path):
        """No heartbeat, no prompt sent → UNKNOWN."""
        task = _make_task(tmp_path)
        task.last_prompt_sent_at = None
        poller = AdaptivePoller()
        assert poller.poll(task) == PollResult.UNKNOWN

    def test_result_wrong_incarnation_ignored(self, tmp_path: Path):
        """Result with wrong incarnation is skipped."""
        task = _make_task(tmp_path)
        task.last_prompt_sent_at = None
        write_json(
            task.result_path(task.current_step, task.current_attempt),
            {
                "step": task.current_step,
                "attempt": task.current_attempt,
                "incarnation": "deadbeef",
                "status": "done",
                "files_changed": [],
                "summary": "wrong",
            },
        )
        poller = AdaptivePoller()
        # No matching result → falls through to heartbeat / unknown path
        assert poller.poll(task) == PollResult.UNKNOWN

    def test_heartbeat_wrong_incarnation_ignored(self, tmp_path: Path):
        """Heartbeat with wrong incarnation is skipped."""
        task = _make_task(tmp_path)
        task.last_prompt_sent_at = None
        write_json(
            task.heartbeat_path,
            {
                "ts": _now_iso(),
                "incarnation": "deadbeef",
                "step": 1,
                "status": "working",
                "current_file": "test.py",
            },
        )
        poller = AdaptivePoller()
        assert poller.poll(task) == PollResult.UNKNOWN

    def test_result_takes_precedence_over_heartbeat(self, tmp_path: Path):
        """When both result and heartbeat exist, result is used."""
        task = _make_task(tmp_path)
        # Write a valid heartbeat
        write_json(
            task.heartbeat_path,
            {
                "ts": _now_iso(),
                "incarnation": task.incarnation_id,
                "step": 1,
                "status": "working",
                "current_file": "test.py",
            },
        )
        # Write a valid result
        write_json(
            task.result_path(task.current_step, task.current_attempt),
            {
                "step": task.current_step,
                "attempt": task.current_attempt,
                "incarnation": task.incarnation_id,
                "status": "done",
                "files_changed": [],
                "summary": "ok",
            },
        )
        poller = AdaptivePoller()
        assert poller.poll(task) == PollResult.RESULT_READY

    def test_heartbeat_wrong_incarnation_with_prompt_sent(self, tmp_path: Path):
        """Heartbeat with wrong incarnation_id is ignored; falls back to prompt timing."""
        task = _make_task(tmp_path)
        task.last_prompt_sent_at = _ago_iso(HEARTBEAT_TIMEOUT + 30)
        write_json(
            task.heartbeat_path,
            {
                "ts": _now_iso(),
                "incarnation": "wronginc1",
                "step": 1,
                "status": "working",
                "current_file": "test.py",
            },
        )
        poller = AdaptivePoller()
        # Wrong incarnation heartbeat is ignored, and prompt is stale → timeout
        assert poller.poll(task) == PollResult.HEARTBEAT_TIMEOUT


# ---------------------------------------------------------------------------
# Property-based tests (hypothesis)
# ---------------------------------------------------------------------------


class TestAgePropertyBased:
    """Property-based tests for age() using hypothesis."""

    @pytest.mark.parametrize("offset_seconds", [0, 1, 60, 3600, 86400])
    def test_age_monotonically_nonnegative(self, offset_seconds):
        """age() always returns >= 0 for past timestamps."""
        ts = (datetime.now(UTC) - timedelta(seconds=offset_seconds)).isoformat()
        result = age(ts)
        assert result >= 0.0

    @pytest.mark.parametrize(
        "garbage",
        ["not-a-date", "", "12345", "T", "2025-99-99T00:00:00", "abc def", "null"],
    )
    def test_age_with_garbage_returns_inf(self, garbage: str):
        """age() returns inf for unparseable strings."""
        assert age(garbage) == float("inf")

    def test_age_with_none_returns_inf(self):
        """age(None) returns inf."""
        assert age(None) == float("inf")

    def test_age_future_timestamp_clamped_to_zero(self):
        """age() returns 0.0 for timestamps in the future."""
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        assert age(future) == 0.0


class TestPollerHardening:
    """Tests for rubber-duck audit findings."""

    def test_max_interval_below_heartbeat_timeout(self) -> None:
        """MAX_INTERVAL must be < HEARTBEAT_TIMEOUT for timely detection."""
        assert MAX_INTERVAL < HEARTBEAT_TIMEOUT

    def test_heartbeat_at_exact_timeout_is_timeout(self, tmp_path: Path) -> None:
        """Heartbeat age exactly at timeout threshold triggers timeout."""
        task = _make_task(tmp_path)
        write_json(
            task.heartbeat_path,
            {
                "ts": _ago_iso(HEARTBEAT_TIMEOUT),
                "incarnation": task.incarnation_id,
                "step": 1,
                "status": "working",
                "current_file": "test.py",
            },
        )
        poller = AdaptivePoller()
        result = poller.poll(task)
        assert result == PollResult.HEARTBEAT_TIMEOUT

    def test_grace_period_resets_interval(self, tmp_path: Path) -> None:
        """Grace period (no heartbeat, recent prompt) resets to fast polling."""
        task = _make_task(tmp_path)
        task.last_prompt_sent_at = _now_iso()
        protocol.save_task(task)

        poller = AdaptivePoller()
        poller.interval = 50.0
        result = poller.poll(task)
        assert result == PollResult.WORKING
        assert poller.interval == BASE_INTERVAL

    def test_custom_max_interval_respected_by_ramp(self) -> None:
        """Ramp never exceeds the configured max_interval."""
        poller = AdaptivePoller(max_interval=10.0)
        for _ in range(100):
            poller._ramp()
        assert poller.interval == 10.0
