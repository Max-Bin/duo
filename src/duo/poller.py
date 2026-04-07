"""Adaptive poller — monitors Codex sessions via file protocol.

Checks heartbeats and results with exponential backoff.
Resets to fast polling when state changes or timeouts occur.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from duo.protocol import Task, read_heartbeat, read_result_for_step
from duo.transport import diagnose_pane, is_process_alive


# === Constants ===

BASE_INTERVAL = 5.0       # seconds, right after sending a prompt
MAX_INTERVAL = 120.0      # seconds, stable cruising period
RAMP_FACTOR = 1.5         # multiply interval each cycle when heartbeat is active
HEARTBEAT_TIMEOUT = 90.0  # seconds without heartbeat before declaring timeout


# === Poll result enum ===


class PollResult(str, Enum):
    RESULT_READY = "result_ready"
    WORKING = "working"
    HEARTBEAT_TIMEOUT = "heartbeat_timeout"
    UNKNOWN = "unknown"


# === Helpers ===


def age(iso_ts: str) -> float:
    """Return seconds elapsed since an ISO-8601 timestamp."""
    try:
        dt = datetime.fromisoformat(iso_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except (ValueError, TypeError):
        return float("inf")


# === Adaptive Poller ===


class AdaptivePoller:
    """Heartbeat-aware poller with exponential backoff.

    Usage::

        poller = AdaptivePoller()
        result = poller.poll(task)
        await asyncio.sleep(poller.interval)
    """

    def __init__(
        self,
        base_interval: float = BASE_INTERVAL,
        max_interval: float = MAX_INTERVAL,
    ) -> None:
        self.base_interval = base_interval
        self.max_interval = max_interval
        self.interval = base_interval

    def _ramp(self) -> None:
        """Increase interval toward max."""
        self.interval = min(self.interval * RAMP_FACTOR, self.max_interval)

    def _reset(self) -> None:
        """Reset interval to base (something changed, poll fast)."""
        self.interval = self.base_interval

    def poll(self, task: Task) -> PollResult:
        """Check task status via file protocol. Returns a PollResult.

        All checks verify incarnation match to reject stale data.
        """
        inc = task.incarnation_id
        step = task.current_step
        attempt = task.current_attempt

        # 1. Check for a result file (highest priority).
        result = read_result_for_step(task, step, attempt)
        if result is not None and result.incarnation == inc:
            self._reset()
            return PollResult.RESULT_READY

        # 2. Check heartbeat.
        hb = read_heartbeat(task)

        if hb is not None and hb.incarnation == inc:
            hb_age = age(hb.ts)
            if hb_age > HEARTBEAT_TIMEOUT:
                self._reset()
                return PollResult.HEARTBEAT_TIMEOUT
            # Active heartbeat — ramp up (slow down) interval.
            self._ramp()
            return PollResult.WORKING

        # 3. No matching heartbeat at all.
        # If we sent a prompt recently, give it time to start.
        if task.last_prompt_sent_at is not None:
            prompt_age = age(task.last_prompt_sent_at)
            if prompt_age < HEARTBEAT_TIMEOUT:
                # Still within grace period, stay at current interval.
                return PollResult.WORKING

            # Prompt sent long ago, no heartbeat — timeout.
            self._reset()
            return PollResult.HEARTBEAT_TIMEOUT

        # 4. No prompt sent, no heartbeat — unknown state.
        self._reset()
        return PollResult.UNKNOWN
