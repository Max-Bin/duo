"""Transport layer — wraps tmux-bridge CLI for all pane communication.

tmux-bridge is the sole interface to tmux. We never call raw tmux commands.
list_panes() is diagnostic only — scheduling truth comes from the file protocol.
"""

from __future__ import annotations

import contextlib
import enum
import fcntl
import functools
import logging
import os
import random
import re
import shutil
import signal
import subprocess
import threading
import time as _time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class DialogKind(enum.Enum):
    """Kind of dialog detected in a Copilot pane."""

    NONE = "none"
    OPTION = "option"  # Numbered options (1. 2. 3.)
    TEXT = "text"  # Free-text input ("Type your answer...")
    BULLET = "bullet"  # Arrow-navigated bullets (❯ item / item / ...)


class TmuxServerDownError(RuntimeError):
    """Raised when the tmux server is not running."""


__all__ = [
    "MINIMUM_PANE_COLS",
    "MINIMUM_PANE_ROWS",
    "DialogDetector",
    "DialogKind",
    "PaneInfo",
    "PaneReader",
    "TmuxServerDownError",
    "TransportBridge",
    "approve_permission",
    "bridge",
    "cancel_current",
    "cleanup_pane_state",
    "clear_bootstrap_done",
    "count_bullet_items",
    "detect_copilot_api_error",
    "detect_dialog_kind",
    "diagnose_pane",
    "doctor",
    "ensure_minimum_pane_size",
    "extract_last_box_lines",
    "get_dialog_kind",
    "get_pane_id",
    "get_pane_pid",
    "get_pane_size",
    "get_pr_log",
    "get_tmux_session_target",
    "is_at_main_prompt",
    "is_capi_context_error",
    "is_in_dialog",
    "is_in_dialog_stable",
    "is_likely_stuck",
    "is_pane_alive",
    "is_pane_process_alive",
    "is_permission_dialog",
    "is_process_alive",
    "is_tmux_server_alive",
    "kill_pane",
    "list_panes",
    "name_pane",
    "pane_lock",
    "read_pane",
    "resolve_label",
    "safe_enter",
    "select_bullet_option",
    "select_dialog_option",
    "select_other_option",
    "send_bootstrap",
    "send_eof",
    "send_keys",
    "send_keys_verified",
    "send_message",
    "send_option_other_message",
    "send_prompt",
    "send_shell_command",
    "send_text_dialog_message",
    "set_pr_callback",
    "split_window_horizontal",
    "strip_ansi",
    "type_text",
    "wait_for_dialog",
    "wait_for_idle",
]

logger = logging.getLogger(__name__)

_SAFE_LABEL = re.compile(r"^[a-zA-Z0-9_.-]+\Z")

_BRIDGE_TIMEOUT = 30  # seconds for tmux-bridge subprocess calls

# Advisory lock directory for cross-process dialog operation locking.
_LOCKS_DIR = Path.home() / ".duo" / "locks"

# In-process reentrant lock per label (prevents deadlock when
# approve_permission calls select_dialog_option in the same thread).
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_MAX_CACHED_LOCKS = 256

# Track which thread holds the flock for each label so nested
# pane_lock calls skip the (non-reentrant) fcntl.flock.
_FLOCK_OWNERS: dict[str, int] = {}  # label → thread ident

_F = TypeVar("_F", bound=Callable[..., Any])


class TransportBridge(Protocol):
    """Protocol for tmux-bridge command execution."""

    def __call__(
        self, cmd: list[str], *, check: bool = True
    ) -> str: ...  # pragma: no cover — Protocol abstract stub


class PaneReader(Protocol):
    """Protocol for reading tmux pane content."""

    def __call__(
        self, label: str, lines: int = 50
    ) -> str: ...  # pragma: no cover — Protocol abstract stub


class DialogDetector(Protocol):
    """Protocol for detecting dialog state in a pane."""

    def __call__(
        self, label: str
    ) -> bool: ...  # pragma: no cover — Protocol abstract stub


def _find_bridge() -> str:
    """Locate tmux-bridge binary."""
    path = shutil.which("tmux-bridge")
    if path:
        return path
    fallback = os.path.expanduser("~/.smux/bin/tmux-bridge")
    if os.path.isfile(fallback):
        return fallback
    raise FileNotFoundError(
        "tmux-bridge not found. Install smux: bash ~/smux/install.sh"
    )


_LOCK = threading.Lock()

_BRIDGE: str | None = None


def _bridge_bin() -> str:
    """Return the cached tmux-bridge binary path, locating it on first call."""
    global _BRIDGE
    with _LOCK:
        if _BRIDGE is None:
            _BRIDGE = _find_bridge()
        return _BRIDGE


def _retry(
    max_attempts: int = 3, delay: float = 0.5, backoff: float = 2.0
) -> Callable[[_F], _F]:
    """Retry decorator with exponential backoff and jitter."""

    def decorator(func: _F) -> _F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_error: Exception = RuntimeError("all retries exhausted")
            wait = delay
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except TmuxServerDownError:
                    raise  # no point retrying a dead server
                except (RuntimeError, OSError) as e:
                    last_error = e
                    if attempt < max_attempts - 1:
                        # ±20% jitter to avoid thundering herd
                        jitter = wait * 0.2 * (2 * random.random() - 1)
                        _time.sleep(wait + jitter)
                        wait *= backoff
            raise last_error

        return wrapper  # type: ignore[return-value]

    return decorator


def is_tmux_server_alive() -> bool:
    """Check if the tmux server is running."""
    try:
        result = subprocess.run(
            ["tmux", "list-sessions"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    else:
        return result.returncode == 0


_TMUX_DOWN_INDICATORS = (
    "no server running",
    "server not found",
    "error connecting",
    "lost server",
    "session not found",
)


_READ_ONLY_BRIDGE_CMDS = frozenset({"read", "resolve", "id", "list", "doctor"})


@_retry()
def _bridge_with_retry(cmd: list[str], *, check: bool = True) -> str:
    """Execute a read-only tmux-bridge command with automatic retry."""
    return _bridge_once(cmd, check=check)


def _bridge_once(cmd: list[str], *, check: bool = True) -> str:
    """Execute a tmux-bridge sub-command (single attempt, no retry)."""
    try:
        result = subprocess.run(
            [_bridge_bin(), *cmd],
            capture_output=True,
            text=True,
            timeout=_BRIDGE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"tmux-bridge {cmd[0]} timed out after {_BRIDGE_TIMEOUT}s"
        ) from exc
    if check and result.returncode != 0:
        stderr = result.stderr.strip()
        if any(ind in stderr.lower() for ind in _TMUX_DOWN_INDICATORS):
            raise TmuxServerDownError(
                f"tmux server is down. Start a new session: tmux new -s duo\n"
                f"  Detail: {stderr}"
            )
        raise RuntimeError(f"tmux-bridge {cmd[0]} failed: {stderr}")
    return result.stdout


def bridge(cmd: list[str], *, check: bool = True) -> str:
    """Execute a tmux-bridge sub-command and return its stdout.

    Read-only commands (read, resolve, id, list, doctor) are retried
    automatically.  Mutating commands (type, keys, name, message) are
    executed once to prevent duplicate side effects.

    Args:
        cmd: Arguments passed to the tmux-bridge binary (e.g. ``["read", "my-label", "50"]``).
        check: If *True* (default), raise ``RuntimeError`` on non-zero exit.
    """
    if cmd and cmd[0] in _READ_ONLY_BRIDGE_CMDS:
        return _bridge_with_retry(cmd, check=check)
    return _bridge_once(cmd, check=check)


# === Atomic operations (map 1:1 to tmux-bridge commands) ===


def read_pane(label: str, lines: int = 50) -> str:
    """Read pane output (also satisfies read guard). ANSI codes are stripped."""
    return strip_ansi(bridge(["read", label, str(lines)]))


def type_text(label: str, text: str) -> None:
    """Type text into pane (no Enter). Requires prior read_pane."""
    bridge(["type", label, text])


_KEY_TO_HEX = {
    "Enter": "0d",
    "Return": "0d",
    "C-m": "0d",
    "Tab": "09",
    "C-i": "09",
    "Escape": "1b",
    "Esc": "1b",
    "Space": "20",
    "BSpace": "7f",
    "Backspace": "7f",
    "C-c": "03",
    "C-d": "04",
    "C-u": "15",
    "C-l": "0c",
    # ANSI arrow key escape sequences
    "Up": "1b 5b 41",
    "Down": "1b 5b 42",
    "Right": "1b 5b 43",
    "Left": "1b 5b 44",
    "Home": "1b 5b 48",
    "End": "1b 5b 46",
}


def _tmux_send_hex(target: str, hex_code: str) -> None:
    """Send raw hex bytes to a pane, bypassing tmux key-name translation."""
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "-H", *hex_code.split()],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"tmux send-keys -H timed out after 10s for {target}"
        ) from exc
    except FileNotFoundError as exc:
        raise TmuxServerDownError(
            "tmux binary not found. Is tmux installed and on PATH?"
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        if any(ind in stderr.lower() for ind in _TMUX_DOWN_INDICATORS):
            raise TmuxServerDownError(
                f"tmux server is down. Start a new session: tmux new -s duo\n"
                f"  Detail: {stderr}"
            ) from exc
        raise RuntimeError(f"tmux send-keys -H failed for {target}: {stderr}") from exc


def send_keys(label: str, *keys: str) -> None:
    """Send special keys. Requires prior read_pane.

    For keys with a known raw byte representation (Enter, arrows, Tab, Esc,
    Ctrl-*, etc.) we use ``tmux send-keys -H <hex>`` to write raw bytes
    directly to the PTY, bypassing tmux's key-name translation layer.
    This is critical for Copilot's Ink TUI, which reliably consumes raw
    stdin bytes but can silently drop key-name events when its stdin
    listener is in a reduced polling state (e.g. while background tasks
    are running).

    Unknown keys fall through to tmux-bridge ``keys`` (name-based) as a
    best-effort fallback.

    A ``tmux select-pane`` call precedes key delivery to ensure input
    reaches non-focused panes in multi-pane layouts.
    """
    target = resolve_label(label)
    # Ensure the target pane receives focus before sending keys
    with contextlib.suppress(
        OSError, subprocess.TimeoutExpired, subprocess.SubprocessError
    ):
        subprocess.run(
            ["tmux", "select-pane", "-t", target],
            capture_output=True,
            text=True,
            timeout=5,
        )
    for key in keys:
        hex_code = _KEY_TO_HEX.get(key)
        if hex_code is not None:
            _tmux_send_hex(target, hex_code)
        else:
            bridge(["keys", label, key])


def get_pane_pid(label: str) -> int | None:
    """Return the PID of the foreground process in the pane, or None."""
    try:
        pane_id = resolve_label(label)
        result = subprocess.run(
            ["tmux", "display-message", "-t", pane_id, "-p", "#{pane_pid}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip())
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
        pass
    return None


def is_pane_process_alive(label: str) -> bool:
    """Check if the foreground process in the pane is alive and running.

    Returns False if the process is dead, stopped (SIGTSTP), or the
    PID cannot be determined.  Logs a warning for stopped processes.
    """
    pid = get_pane_pid(label)
    if pid is None:
        return False
    try:
        result = subprocess.run(
            ["ps", "-o", "state=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return False
        state = result.stdout.strip()
        if state.startswith("T"):
            logger.warning(
                "Pane %s process (PID %d) is stopped — run `fg` in the pane",
                label,
                pid,
            )
            return False
    except (OSError, subprocess.TimeoutExpired):
        return False
    else:
        return True


def _normalize_pane_content(content: str) -> str:
    """Normalize pane content for comparison.

    Strips trailing whitespace from each line and collapses trailing
    blank lines.  This prevents false positives in content comparison
    caused by CJK double-width characters, tmux rendering
    inconsistencies, or trailing-space fluctuations.
    """
    lines = [line.rstrip() for line in content.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def send_keys_verified(
    label: str,
    key: str,
    *,
    settle: float = 0.5,
    retries: int = 3,
    auto_resize: bool = True,
) -> bool:
    """Send a single key and verify the pane state changed.

    Captures pane content before and after, with a brief settle period for
    Ink TUIs to re-render.  Retries on no-change with small backoff.

    When *auto_resize* is True and all retries fail, attempts to resize
    the pane to minimum dimensions (see :data:`MINIMUM_PANE_COLS` /
    :data:`MINIMUM_PANE_ROWS`) and retries once.  This mitigates the
    observed input failures after ``tmux resize-pane`` events.

    Returns True if the pane content changed (key was consumed), False if
    the key appears to have been silently buffered (Copilot Ink event loop
    likely blocked on background tasks — physical keypress may be needed).

    Raises RuntimeError if the pane process is dead or stopped.
    """
    if not is_pane_process_alive(label):
        pid = get_pane_pid(label)
        if pid is None:
            raise RuntimeError(f"Pane '{label}' process is dead — cannot send keys")
        raise RuntimeError(
            f"Pane '{label}' process (PID {pid}) is stopped — "
            f"run `fg` in the pane first"
        )

    before = _normalize_pane_content(read_pane(label, 20))
    for attempt in range(retries):
        send_keys(label, key)
        _time.sleep(settle)
        after = _normalize_pane_content(read_pane(label, 20))
        if after != before:
            return True
        _time.sleep(settle * (attempt + 1))

    # All retries failed — try auto-resize as last resort
    if auto_resize:
        try:
            resized = ensure_minimum_pane_size(label)
            if resized:
                logger.info(
                    "Pane %s was undersized; resized and retrying key send", label
                )
                _time.sleep(settle * 2)  # extra settle after SIGWINCH
                send_keys(label, key)
                _time.sleep(settle)
                after = _normalize_pane_content(read_pane(label, 20))
                if after != before:
                    return True
        except (subprocess.SubprocessError, OSError, RuntimeError):
            logger.debug("Auto-resize failed for %s, ignoring", label)

    return False


def is_likely_stuck(label: str, *, poll_ms: int = 500) -> bool:
    """Heuristic: True if the pane appears frozen (content unchanged across
    two reads with a brief gap).  Useful for fail-fast error messages when
    Copilot's Ink TUI event loop is blocked on hung background tasks.
    """
    first = read_pane(label, 20)
    _time.sleep(poll_ms / 1000.0)
    second = read_pane(label, 20)
    return first == second


def name_pane(target: str, label: str) -> None:
    """Label a pane (visible in tmux border via smux .tmux.conf).

    Raises:
        ValueError: If *label* contains unsafe characters.
    """
    _validate_label(label)
    bridge(["name", target, label])


def kill_pane(target: str) -> bool:
    """Kill a tmux pane by ID or label.

    *target* may be a validated label (alphanumeric) or a raw tmux pane
    ID such as ``%42``.  Returns ``True`` if the pane was killed (or
    was already gone), ``False`` if the kill command failed unexpectedly.
    """
    if not re.match(r"^[%a-zA-Z0-9_.-]+$", target):
        raise ValueError(f"Unsafe pane target: {target!r}")
    try:
        result = subprocess.run(
            ["tmux", "kill-pane", "-t", target],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("kill_pane(%s) failed: %s", target, exc)
        return False
    if result.returncode != 0:
        raw_stderr = result.stderr
        msg = (
            raw_stderr.decode(errors="replace")
            if isinstance(raw_stderr, bytes)
            else raw_stderr
        )
        msg_lower = msg.lower().strip()
        logger.debug(
            "kill_pane(%s) exited %d: %s", target, result.returncode, msg.strip()
        )
        # Known "pane already gone" patterns — treat as success
        gone_patterns = ("can't find", "not found", "no pane")
        return any(p in msg_lower for p in gone_patterns)
    return True


def is_pane_alive(target: str) -> bool:
    """Check if a tmux pane exists and is reachable.

    *target* may be a pane ID (``%42``) or a validated label.
    Returns ``True`` if the pane responds, ``False`` otherwise.
    """
    if not re.match(r"^[%a-zA-Z0-9_.-]+$", target):
        raise ValueError(f"Unsafe pane target: {target!r}")
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-t", target, "-p", ""],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def split_window_horizontal() -> str:
    """Create a new tmux pane via horizontal split and return its pane ID.

    Uses :func:`get_tmux_session_target` to ensure the split happens in the
    **caller's** tmux session, not whichever session happens to be "active"
    on the tmux server.  Without ``-t``, ``tmux split-window`` targets the
    server's most-recently-active pane, which may be in a completely different
    session — causing cross-session pollution.

    Returns the new pane ID (e.g. ``%42``).
    Raises ``RuntimeError`` if the split fails.
    """
    session_target = get_tmux_session_target()
    try:
        result = subprocess.run(
            [
                "tmux", "split-window", "-h",
                "-t", session_target,
                "-P", "-F", "#{pane_id}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("tmux split-window timed out") from None
    if result.returncode != 0:
        raise RuntimeError(f"tmux split-window failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _validate_label(label: str) -> None:
    """Ensure pane label is safe for shell use."""
    if not _SAFE_LABEL.match(label):
        raise ValueError(f"Unsafe pane label: {label!r}")


def _get_thread_lock(label: str) -> threading.RLock:
    """Get or create a per-label reentrant thread lock.

    When the cache exceeds ``_MAX_CACHED_LOCKS``, idle entries (labels
    not currently held via flock) are evicted to prevent unbounded growth.
    """
    with _THREAD_LOCKS_GUARD:
        if label not in _THREAD_LOCKS:
            if len(_THREAD_LOCKS) >= _MAX_CACHED_LOCKS:
                _evict_idle_locks()
            _THREAD_LOCKS[label] = threading.RLock()
        return _THREAD_LOCKS[label]


def _evict_idle_locks() -> None:
    """Remove cached locks for labels with no active flock owner.

    Must be called while holding ``_THREAD_LOCKS_GUARD``.
    """
    idle = [lbl for lbl in _THREAD_LOCKS if lbl not in _FLOCK_OWNERS]
    for lbl in idle:
        del _THREAD_LOCKS[lbl]


@contextlib.contextmanager
def pane_lock(label: str, *, timeout: float = 30.0) -> Generator[None, None, None]:
    """Acquire an advisory lock for a pane label.

    Two-level locking:
    1. In-process: ``threading.RLock`` per label (reentrant, so
       ``approve_permission`` → ``select_dialog_option`` in the same
       thread doesn't deadlock).
    2. Cross-process: ``fcntl.flock(LOCK_EX)`` on a per-label file
       (prevents interleaving from separate ``duo`` CLI invocations).
       Skipped on reentrant calls (same thread already holds flock).

    The cross-process lock uses non-blocking polls with a timeout.
    If the lock cannot be acquired within *timeout* seconds,
    ``TimeoutError`` is raised.

    The lock is released when the outermost context exits (or the
    process dies, which auto-releases flock locks).
    """
    _validate_label(label)
    thread_lock = _get_thread_lock(label)

    if not thread_lock.acquire(timeout=timeout):
        raise TimeoutError(
            f"Could not acquire in-process pane lock for {label!r} within {timeout}s"
        )

    tid = threading.get_ident()
    with _THREAD_LOCKS_GUARD:
        already_owned = _FLOCK_OWNERS.get(label) == tid

    try:
        if already_owned:
            # Reentrant — flock already held by this thread.
            yield
        else:
            _LOCKS_DIR.mkdir(parents=True, exist_ok=True)
            lock_path = _LOCKS_DIR / f"{label}.lock"
            fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY)
            try:
                deadline = _time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if _time.monotonic() >= deadline:
                            raise TimeoutError(
                                f"Could not acquire cross-process pane lock "
                                f"for {label!r} within {timeout}s — another "
                                f"dialog operation may be in progress"
                            ) from None
                        _time.sleep(0.1)
                with _THREAD_LOCKS_GUARD:
                    _FLOCK_OWNERS[label] = tid
                try:
                    yield
                finally:
                    with _THREAD_LOCKS_GUARD:
                        _FLOCK_OWNERS.pop(label, None)
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
    finally:
        thread_lock.release()


def resolve_label(label: str) -> str:
    """Resolve a human-readable pane label to its tmux pane ID.

    Labels are assigned via :func:`name_pane` and stored in the tmux
    environment.  This function queries tmux-bridge to translate a label
    (e.g. ``"task-fix-auth"``) into the underlying pane target
    (e.g. ``"%42"``).

    Raises:
        ValueError: If *label* contains unsafe characters.
        RuntimeError: If the label cannot be resolved.
    """
    _validate_label(label)
    return bridge(["resolve", label]).strip()


def get_pane_id() -> str:
    """Return the tmux pane ID of the *current* pane (the one running this process).

    Useful for self-identification when the orchestrator needs to know
    which pane it is executing in (e.g. to avoid sending commands to itself).
    """
    return bridge(["id"]).strip()


def get_tmux_session_target() -> str:
    """Return a tmux session target (``$N``) for the caller's session.

    Reads the ``$TMUX`` environment variable (format:
    ``<socket_path>,<server_pid>,<session_id>``) and extracts the session
    ID.  When ``$TMUX`` is not set, falls back to using the sole active
    session (raises if 0 or 2+ sessions exist).

    The returned value (e.g. ``"$0"``) can be passed as ``-t`` target to
    ``tmux split-window`` and ``tmux select-layout`` to guarantee pane
    creation happens in the caller's session, preventing cross-session
    pollution.

    Raises:
        RuntimeError: If the tmux session cannot be determined.
    """
    tmux_env = os.environ.get("TMUX", "")
    if tmux_env:
        # Parse from right — socket path may theoretically contain commas
        parts = tmux_env.rsplit(",", 2)
        if len(parts) >= 3 and parts[2].strip().isdigit():
            return f"${parts[2].strip()}"
        # Malformed $TMUX — fall through to list-sessions

    # Fallback: if exactly one session exists, use it
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_id}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "tmux is not running. Start a session first:\n  tmux new -s duo"
        ) from exc

    if result.returncode == 0:
        sessions = [s.strip() for s in result.stdout.strip().splitlines() if s.strip()]
        if len(sessions) == 1:
            # tmux list-sessions -F '#{session_id}' returns "$N" with prefix
            return sessions[0]
        if len(sessions) == 0:
            raise RuntimeError(
                "No tmux sessions found. Start one first:\n  tmux new -s duo"
            )
        raise RuntimeError(
            f"Multiple tmux sessions found ({len(sessions)}). "
            "Run duo inside a tmux session so $TMUX is set.\n"
            "  Example: tmux attach -t duo && duo start my-task"
        )

    raise RuntimeError(
        "Cannot determine tmux session. Run duo inside a tmux session.\n"
        "  Start one with: tmux new -s duo"
    )


# Minimum pane dimensions for reliable Copilot Ink input handling.
# Smaller panes may clip dialog boxes, change Ink component focus,
# or cause SIGWINCH-related stdin listener detachment during re-render.
MINIMUM_PANE_COLS = 100
MINIMUM_PANE_ROWS = 24


def get_pane_size(label: str) -> tuple[int, int]:
    """Return (columns, rows) for the pane identified by *label*.

    Uses ``tmux display-message`` to query the pane dimensions.
    Raises RuntimeError if the pane cannot be found or the output
    is unparseable.
    """
    target = resolve_label(label)
    result = subprocess.run(
        ["tmux", "display-message", "-t", target, "-p", "#{pane_width} #{pane_height}"],
        capture_output=True,
        text=True,
        timeout=_BRIDGE_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Cannot query pane size for {label}: {result.stderr.strip()}"
        )
    parts = result.stdout.strip().split()
    if len(parts) != 2:
        raise RuntimeError(f"Unexpected pane size output: {result.stdout!r}")
    return int(parts[0]), int(parts[1])


def _get_client_size() -> tuple[int, int]:
    """Return (width, height) of the largest attached tmux client.

    Falls back to (200, 50) if the query fails (no attached client,
    tmux not running, etc.).
    """
    try:
        result = subprocess.run(
            [
                "tmux",
                "display-message",
                "-p",
                "#{client_width} #{client_height}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split()
            if len(parts) == 2:
                return int(parts[0]), int(parts[1])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return 200, 50


def ensure_minimum_pane_size(
    label: str,
    *,
    min_cols: int = MINIMUM_PANE_COLS,
    min_rows: int = MINIMUM_PANE_ROWS,
) -> bool:
    """Ensure a pane meets minimum dimensions, resizing if needed.

    Returns True if the pane was resized, False if it already met minimums.
    Logs a warning when resizing.

    If the tmux client is smaller than the requested minimum, the
    resize target is capped to ``client_size - 1`` and a warning is
    logged.  This prevents resize failures on small monitors or
    laptops with large fonts.

    This is a defensive measure against the observed tmux input failures
    that correlate with pane resize events (see docs/known-issues.md).
    After a resize, Copilot's Ink TUI receives SIGWINCH and re-renders;
    a brief settle period may be needed before sending input.
    """
    cols, rows = get_pane_size(label)
    client_w, client_h = _get_client_size()

    # Cap to client dimensions (leave 1 col/row for tmux borders)
    effective_cols = min(min_cols, max(client_w - 1, 40))
    effective_rows = min(min_rows, max(client_h - 1, 10))

    if effective_cols < min_cols or effective_rows < min_rows:
        logger.warning(
            "Client size %dx%d smaller than minimum %dx%d — capping resize to %dx%d",
            client_w,
            client_h,
            min_cols,
            min_rows,
            effective_cols,
            effective_rows,
        )

    resized = False
    target = resolve_label(label)

    if cols < effective_cols:
        logger.warning(
            "Pane %s width %d < minimum %d, resizing",
            label,
            cols,
            effective_cols,
        )
        subprocess.run(
            ["tmux", "resize-pane", "-t", target, "-x", str(effective_cols)],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        resized = True

    if rows < effective_rows:
        logger.warning(
            "Pane %s height %d < minimum %d, resizing",
            label,
            rows,
            effective_rows,
        )
        subprocess.run(
            ["tmux", "resize-pane", "-t", target, "-y", str(effective_rows)],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        resized = True

    return resized


@dataclass(slots=True)
class PaneInfo:
    """Parsed row from ``tmux-bridge list`` output."""

    target: str
    session_win: str
    size: str
    process: str
    label: str
    cwd: str


def list_panes() -> list[PaneInfo]:
    """List all panes.

    DIAGNOSTIC ONLY — not scheduling truth.
    Parses tmux-bridge list text output; format may change with smux versions.
    Scheduling truth comes from the file protocol (heartbeat/result/ack).
    """
    output = bridge(["list"], check=False)
    panes: list[PaneInfo] = []
    for line in output.strip().split("\n")[1:]:  # skip header
        parts = line.split()
        if len(parts) >= 6:
            panes.append(PaneInfo(*parts[:6]))
        else:
            logger.debug("Skipping unparseable tmux line: %s", line)
    return panes


# === PREMIUM REQUEST PROTECTION (HARD ENFORCEMENT) ===
# - send_bootstrap(): locked after 1st use per pane
# - send_prompt(): BANNED, always raises
# - safe_enter(): blocks Enter if at ❯ prompt
# - select_dialog_option(): double-checks dialog stability

_BOOTSTRAP_DONE: set[str] = set()

# PR consumption audit — every PR-consuming action is recorded
_PR_LOG: list[dict[str, str]] = []
_pr_callback: Callable[[str, str, str], None] | None = None


def set_pr_callback(callback: Callable[[str, str, str], None] | None) -> None:
    """Register a callback for PR consumption events.

    Callback receives (label, action, context).
    """
    global _pr_callback
    with _LOCK:
        _pr_callback = callback


def _record_pr(label: str, action: str, context: str = "") -> None:
    """Record a Premium Request consumption."""
    import datetime as _dt

    entry = {
        "ts": _dt.datetime.now(_dt.UTC).isoformat(),
        "label": label,
        "action": action,
        "context": context,
    }
    with _LOCK:
        _PR_LOG.append(entry)
        if len(_PR_LOG) > 10000:
            del _PR_LOG[: len(_PR_LOG) - 10000]
        callback = _pr_callback
    if callback is not None:
        try:
            callback(label, action, context)
        except Exception:  # user-supplied callback can raise anything
            logger.warning("PR callback failed for %s/%s", label, action, exc_info=True)


def get_pr_log() -> list[dict[str, str]]:
    """Return the full PR consumption audit log."""
    with _LOCK:
        return list(_PR_LOG)


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return _ANSI_RE.sub("", text)


def is_at_main_prompt(content: str) -> bool:
    """True if pane is IDLE at Copilot ❯ prompt. ANY input here = PR consumed.

    The ❯ prompt is always rendered at the bottom of Copilot CLI, even while
    processing or inside a dialog — so presence of ❯ alone is not enough.
    We must also confirm no spinner (active processing) and no dialog box
    is visible in the pane.
    """
    content = strip_ansi(content)
    # If a spinner is present, Copilot is actively processing — not idle.
    if any(marker in content for marker in ("◉ ", "◎ ", "○ ")):
        return False
    # If a dialog box is present, we're in a dialog — not at main prompt.
    if "╰─" in content or "╭─" in content:
        return False
    for line in reversed(content.strip().split("\n")):
        s = line.strip()
        if s.startswith("❯") and ("Type @" in s or s == "❯" or "mention files" in s):
            return True
        if "Remaining reqs" in s or "shift+tab" in s:
            continue
        if s and not s.startswith("─"):
            break
    return False


def extract_last_box_lines(content: str) -> list[str] | None:
    """Return lines inside the last ╭─…╰─ dialog box, or None if not found.

    For tall dialogs where ╭─ scrolled off-screen, if we see ╰─ without ╭─
    we treat all lines above ╰─ as box content (partial box).
    """
    lines = content.split("\n")
    box_start = -1
    box_end = -1
    for i, line in enumerate(lines):
        if "╭─" in line:
            box_start = i
        if "╰─" in line and box_start >= 0:
            box_end = i
    if box_start >= 0 and box_end > box_start:
        return lines[box_start + 1 : box_end]
    # Partial box: ╰─ visible but ╭─ scrolled off — treat everything above as box
    for i, line in enumerate(lines):
        if "╰─" in line:
            box_end = i
            break
    if box_end > 0:
        return lines[:box_end]
    return None


def detect_dialog_kind(content: str) -> DialogKind:
    """Classify dialog kind from pane content (no I/O).

    Only examines content within the last dialog box (╭─ … ╰─).
    Numbered lists outside the box are ignored.

    A box is considered an **active dialog** only if:
    1. It contains an interactive marker (numbered options, ❯ cursor,
       or a footer like "Enter accept", "↑↓ select", etc.), AND
    2. The box is near the bottom of the pane (≤ 5 non-empty lines
       after the closing ╰─ border).
    This prevents false positives from Copilot narration text that
    contains box-drawing characters.
    """
    content = strip_ansi(content)
    if is_at_main_prompt(content):
        return DialogKind.NONE

    # --- Bottom-anchor check ---
    # Find the last ╰─ line and reject boxes with too much trailing content
    all_lines = content.split("\n")
    last_box_end_idx = -1
    for i in range(len(all_lines) - 1, -1, -1):
        if "╰─" in all_lines[i]:
            last_box_end_idx = i
            break
    if last_box_end_idx >= 0:
        trailing = [ln for ln in all_lines[last_box_end_idx + 1 :] if ln.strip()]
        if len(trailing) > 5:
            return DialogKind.NONE

    box_lines = extract_last_box_lines(content)
    if box_lines is None:
        return DialogKind.NONE

    box_content = "\n".join(box_lines)

    has_opt = any(
        any(l.strip().startswith(f"{n}.") or f"❯ {n}." in l for n in range(1, 7))
        for l in box_lines
    )
    if has_opt:
        return DialogKind.OPTION

    # Bullet dialog: ❯ cursor without numbered options, or footer markers
    bullet_footer = ("↑↓ select", "Enter accept", "ctrl+d decline")
    if any(ind in box_content for ind in bullet_footer):
        return DialogKind.BULLET

    # ❯ prefix on non-numbered lines inside the box
    has_bullet = any(
        re.match(r"\s*[│]?\s*❯\s+\S", l) and not re.match(r"\s*[│]?\s*❯\s+\d+\.", l)
        for l in box_lines
    )
    if has_bullet:
        return DialogKind.BULLET

    text_indicators = ("Type your answer", "Enter to submit", "type your response")
    if any(ind in box_content for ind in text_indicators):
        return DialogKind.TEXT
    return DialogKind.NONE


def get_dialog_kind(label: str) -> DialogKind:
    """Read pane and classify the dialog kind."""
    content = read_pane(label, 100)
    return detect_dialog_kind(content)


def is_in_dialog(label: str) -> bool:
    """True if Copilot shows any dialog (option, text, or bullet)."""
    return get_dialog_kind(label) != DialogKind.NONE


def is_in_dialog_stable(label: str) -> bool:
    """Double-check: read twice with 1s gap. Both must show dialog."""
    if not is_in_dialog(label):
        return False
    _time.sleep(1.0)
    return is_in_dialog(label)


def wait_for_idle(
    label: str, timeout: float = 30.0, poll_interval: float = 1.0
) -> bool:
    """Wait until pane output stabilizes (two consecutive reads are identical).

    Returns *True* if output stabilised within *timeout* seconds, *False* otherwise.
    """
    previous = ""
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        current = read_pane(label, 20)
        if current == previous and current.strip():
            return True
        previous = current
        _time.sleep(poll_interval)
    return False


def wait_for_dialog(
    label: str,
    timeout: float = 300,
    interval: float = 5,
    stop_event: threading.Event | None = None,
) -> bool:
    """Wait for STABLE dialog (double-checked).

    If *stop_event* is provided, the wait aborts early when the event is set,
    returning False.  This allows callers (e.g. watch_tasks) to interrupt
    long waits without waiting for the full timeout.
    """
    if interval <= 0:
        raise ValueError(f"interval must be positive, got {interval}")
    if timeout <= 0:
        raise ValueError(f"timeout must be positive, got {timeout}")
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            return False
        if is_in_dialog_stable(label):
            return True
        if stop_event is not None:
            stop_event.wait(timeout=interval)
        else:
            _time.sleep(interval)
    return False


def safe_enter(label: str) -> None:
    """Press Enter ONLY if NOT at ❯ prompt. Raises otherwise.

    After sending Enter, re-reads the pane to detect if a TOCTOU race
    caused the key to land on the main prompt (PR consumed).  Logs a
    critical warning if this is detected.
    """
    content = read_pane(label, 20)
    if is_at_main_prompt(content):
        raise RuntimeError(
            f"BLOCKED: '{label}' at ❯ prompt. Enter = PR consumed. REFUSED."
        )
    send_keys(label, "Enter")
    # Post-send TOCTOU detection: if the pane is now at the main prompt
    # AND no dialog appeared, a PR may have been consumed by the race.
    _time.sleep(0.15)
    post = read_pane(label, 20)
    if is_at_main_prompt(post):
        logger.critical(
            "TOCTOU: Enter sent to '%s' but pane is now at ❯ prompt — "
            "a Premium Request may have been consumed by a race condition",
            label,
        )
        _record_pr(label, "toctou_enter", "safe_enter race detected")


def is_permission_dialog(label: str) -> bool:
    """True if dialog is a permission/approval prompt."""
    content = read_pane(label, 20)
    perm_indicators = [
        "Do you want to run",
        "Do you want to edit",
        "Allow directory",
        "Do you want to allow",
    ]
    return any(ind in content for ind in perm_indicators)


def _preemptive_dialog_resize(label: str, content: str) -> None:
    """Resize pane if a dialog box is taller than the pane.

    Counts the number of lines between ╭─ and ╰─ markers and compares
    with the pane height.  If the dialog needs more rows, preemptively
    resizes before interacting with the dialog.
    """
    lines = content.strip().split("\n")
    box_start: int | None = None
    box_end: int | None = None
    for i, line in enumerate(lines):
        if "╭─" in line and box_start is None:
            box_start = i
        if "╰─" in line:
            box_end = i

    if box_start is None or box_end is None:
        return

    dialog_height = box_end - box_start + 1
    try:
        _cols, rows = get_pane_size(label)
    except (subprocess.SubprocessError, OSError, RuntimeError):
        return

    # Need dialog_height + margin for prompt above and status below
    needed = dialog_height + 6
    if needed > rows:
        try:
            ensure_minimum_pane_size(label, min_rows=needed)
            logger.info(
                "Preemptive resize: dialog %d lines, pane %d rows → %d rows",
                dialog_height,
                rows,
                needed,
            )
        except (subprocess.SubprocessError, OSError, RuntimeError):
            logger.debug("Preemptive dialog resize failed for %s", label)


def approve_permission(label: str) -> None:
    """Approve a permission dialog by reading options and picking the right one.

    Different permission dialogs have different layouts:
    - 3 options: 1=Yes, 2=Yes+approve for session, 3=No → pick 2
    - 2 options: 1=Yes, 2=No → pick 1

    MUST read the actual option text to decide. Never blindly pick a number.
    """
    with pane_lock(label):
        if not is_in_dialog_stable(label):
            raise RuntimeError(f"SAFETY: '{label}' not in stable dialog. REFUSED.")
        content = read_pane(label, 20)
        _preemptive_dialog_resize(label, content)

        options, _cursor = _parse_dialog_options(content)

        # Strategy: find the best "yes" option
        # Prefer "Yes + approve for session" over plain "Yes"
        _AFFIRMATIVE = {
            "yes",
            "ok",
            "continue",
            "proceed",
            "allow",
            "accept",
            "confirm",
        }
        best = None
        for num, text in options.items():
            text_lower = text.lower()
            # Skip any "No" or "tell differently" options
            if "no" in text_lower and (
                "tell" in text_lower
                or "esc" in text_lower
                or "differently" in text_lower
            ):
                continue
            if text_lower.startswith("no") or text_lower == "cancel":
                continue
            # Prefer "approve for session" / "approve all" / "add to allowed"
            if "approve" in text_lower or (
                "add" in text_lower and "allowed" in text_lower
            ):
                best = num
                break
            # Otherwise any affirmative keyword
            if any(kw in text_lower for kw in _AFFIRMATIVE) and best is None:
                best = num

        if best is None:
            raise RuntimeError(
                f"SAFETY: '{label}' permission dialog has no recognizable "
                f"'yes' option. Options found: {options}. REFUSED."
            )

        select_dialog_option(label, best)


def _parse_dialog_options(content: str) -> tuple[dict[str, str], int]:
    """Extract numbered options and cursor position from the last dialog box.

    Returns (options, cursor_pos) where options maps number-string to text,
    and cursor_pos is the option number where ❯ is placed (0 if none).
    """
    lines = content.strip().split("\n")
    in_box = False
    options: dict[str, str] = {}
    cursor_pos = 0
    opt_re = re.compile(r"[❯\s]+(\d+)\.\s+(.*)")
    cursor_re = re.compile(r"\s*[│]?\s*❯\s*(\d+)\.")
    for line in lines:
        if "╭─" in line:
            in_box = True
            options = {}
            cursor_pos = 0
            continue
        if "╰─" in line:
            in_box = False
            continue
        if not in_box:
            continue
        m = opt_re.search(line)
        if m:
            options[m.group(1)] = m.group(2).strip()
        cm = cursor_re.match(line)
        if cm:
            cursor_pos = int(cm.group(1))
    return options, cursor_pos


def _retry_enter_until_dismissed(label: str, max_retries: int = 2) -> bool:
    """Send Enter and verify dialog dismissal, retrying up to max_retries times.

    Returns True if the dialog was dismissed, False if retries exhausted.
    """
    for _retry in range(max_retries):
        content = read_pane(label, 20)
        if detect_dialog_kind(content) == DialogKind.NONE:
            return True
        send_keys(label, "Enter")
        _time.sleep(0.5)
    # Final check
    content = read_pane(label, 20)
    return detect_dialog_kind(content) == DialogKind.NONE


def select_dialog_option(label: str, option: str) -> None:
    """Select dialog option with triple safety.

    After typing the option number, Copilot may immediately dismiss the
    dialog (some permission dialogs accept on keypress without Enter).
    If the dialog is already gone, we skip safe_enter to avoid a
    spurious "at ❯ prompt" error.
    """
    with pane_lock(label):
        if not is_in_dialog_stable(label):
            raise RuntimeError(f"SAFETY: '{label}' not in stable dialog. REFUSED.")
        type_text(label, option)
        _time.sleep(0.3)
        # Dialog may have been dismissed by the keypress alone
        if is_in_dialog(label):
            safe_enter(label)
        _record_pr(label, "dialog_option", option[:80])


def count_bullet_items(content: str) -> tuple[int, int]:
    """Count bullet items and find current cursor position in a BULLET dialog.

    Returns (total_items, current_position) where position is 1-based.
    The cursor position is the item with ❯ prefix.
    """
    box_lines = extract_last_box_lines(content)
    if box_lines is None:
        return 0, 0

    total = 0
    cursor_pos = 0
    for line in box_lines:
        stripped = line.strip().lstrip("│").strip()
        if not stripped:
            continue
        if "↑↓" in stripped or "ctrl+" in stripped:
            continue
        total += 1
        if stripped.startswith("❯"):
            cursor_pos = total
    return total, cursor_pos


def select_bullet_option(label: str, position: int) -> None:
    """Select a bullet dialog option by 1-based position.

    Uses Up/Down arrow keys to navigate to the target position,
    then sends Enter to confirm the selection.
    """
    with pane_lock(label):
        if not is_in_dialog_stable(label):
            raise RuntimeError(f"SAFETY: '{label}' not in stable dialog. REFUSED.")

        content = read_pane(label, 40)
        content = strip_ansi(content)
        total, current = count_bullet_items(content)

        if total == 0:
            raise RuntimeError(f"SAFETY: '{label}' no bullet items found.")
        if position < 1 or position > total:
            raise RuntimeError(f"SAFETY: position {position} out of range (1–{total}).")

        # Navigate to target
        diff = position - current
        key = "Down" if diff > 0 else "Up"
        for _ in range(abs(diff)):
            send_keys(label, key)
            _time.sleep(0.2)

        # Confirm selection
        _time.sleep(0.3)
        if is_in_dialog(label):
            safe_enter(label)
        _record_pr(label, "bullet_option", str(position))


def send_option_other_message(label: str, text: str) -> bool:
    """Navigate to 'Other' option, type text, and submit with reliable Enter.

    Like send_text_dialog_message but handles the OPTION dialog navigate-to-last step.
    Returns True if dialog was dismissed, False if retries exhausted.
    """
    with pane_lock(label):
        content = read_pane(label, 20)
        if is_at_main_prompt(content):
            raise RuntimeError(f"BLOCKED: '{label}' at ❯ prompt. REFUSED.")
        if not is_in_dialog(label):
            raise RuntimeError(f"SAFETY: '{label}' not in dialog. REFUSED.")
        _preemptive_dialog_resize(label, content)

        # Count options within the LAST dialog box (╭─ … ╰─)
        options, current_pos = _parse_dialog_options(content)
        option_count = max((int(k) for k in options), default=0)

        if option_count < 2:
            raise RuntimeError(
                f"SAFETY: '{label}' dialog has {option_count} options, need ≥2."
            )

        # Navigate down to last option (Other)
        downs_needed = option_count - current_pos
        for _ in range(downs_needed):
            send_keys(label, "Down")
            _time.sleep(0.2)
            read_pane(label, 5)

        # Type text
        type_text(label, text)
        _time.sleep(0.3)

        # Unconditional Enter (NOT safe_enter)
        send_keys(label, "Enter")
        _time.sleep(0.5)

        # Verify dialog dismissed; retry Enter up to 2 times
        dismissed = _retry_enter_until_dismissed(label)
        if dismissed:
            _record_pr(label, "dialog_other", text[:80])
        return dismissed


def select_other_option(label: str, text: str) -> None:
    """Navigate to 'Other' (last option) in dialog, type text, and submit."""
    success = send_option_other_message(label, text)
    if not success:
        logger.warning(
            "select_other_option: dialog may still be active for '%s'", label
        )


def send_text_dialog_message(label: str, text: str) -> bool:
    """Type text into a dialog input field and submit with reliable Enter.

    After typing, verifies the text is visible in the pane before sending
    Enter. After Enter, verifies the dialog was dismissed. Retries Enter
    up to 2 times if the dialog persists.

    Returns True if the dialog was successfully dismissed, False if
    retries were exhausted and the dialog is still showing.
    """
    with pane_lock(label):
        type_text(label, text)
        _time.sleep(0.3)

        # Verify text is visible before sending Enter
        text_confirmed = False
        for _attempt in range(3):
            content = read_pane(label, 20)
            if text in content:
                text_confirmed = True
                break
            # Text not visible — send SIGWINCH to force Ink TUI refresh
            try:
                pane_id = resolve_label(label)
                pid_result = subprocess.run(
                    ["tmux", "display-message", "-t", pane_id, "-p", "#{pane_pid}"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if pid_result.returncode == 0 and pid_result.stdout.strip():
                    os.kill(int(pid_result.stdout.strip()), signal.SIGWINCH)
            except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
                pass
            _time.sleep(0.3)

        if not text_confirmed:
            logger.warning(
                "Text not confirmed visible in pane '%s' after 3 attempts; "
                "aborting Enter to avoid submitting empty/wrong answer",
                label,
            )
            return False

        # Send Enter
        send_keys(label, "Enter")
        _time.sleep(0.5)

        # Verify dialog was dismissed; retry Enter up to 2 times
        return _retry_enter_until_dismissed(label)


# === Composite operations ===


def send_shell_command(label: str, command: str) -> None:
    """Send to SHELL (before Copilot starts). No PR cost.

    Safety: rejects if pane is at Copilot's main ❯ prompt.
    Holds pane_lock to prevent interleaving with concurrent callers.
    """
    with pane_lock(label):
        content = read_pane(label, 5)
        if is_at_main_prompt(content):
            raise RuntimeError(
                f"BLOCKED: '{label}' at ❯ prompt. "
                "send_shell_command is for shell-only. Use select_dialog_option."
            )
        type_text(label, command)
        read_pane(label, 5)
        send_keys(label, "Enter")


def send_bootstrap(label: str, prompt: str) -> None:
    """THE ONE bootstrap prompt. 1 PR. PERMANENTLY LOCKED after use.

    Rollback logic: if pane I/O fails BEFORE text is typed, the lock is
    rolled back so the caller can retry.  Once ``type_text`` succeeds the
    lock is committed — rollback would risk duplicate/garbled input.

    Holds pane_lock (cross-process) outside the thread-level _LOCK to
    prevent interleaving.  Note: _BOOTSTRAP_DONE is in-process only;
    cross-process bootstrap state is not persisted (see known-issues.md).
    """
    with pane_lock(label):
        with _LOCK:
            if label in _BOOTSTRAP_DONE:
                raise RuntimeError(
                    f"BLOCKED: Bootstrap done for '{label}'. PERMANENT LOCK."
                )
            _BOOTSTRAP_DONE.add(label)
        try:
            read_pane(label, 5)
            type_text(label, prompt)
        except Exception:  # re-raised; rollback bootstrap flag on any failure
            with _LOCK:
                _BOOTSTRAP_DONE.discard(label)
            raise
        read_pane(label, 5)
        send_keys(label, "Enter")
        _record_pr(label, "bootstrap", prompt[:80])


def clear_bootstrap_done(label: str) -> None:
    """Remove *label* from the bootstrap-done set (thread-safe)."""
    with _LOCK:
        _BOOTSTRAP_DONE.discard(label)


def cleanup_pane_state(label: str) -> None:
    """Clean up all module-level state associated with a pane label.

    Call this when a pane is destroyed or recycled to prevent
    unbounded growth of ``_THREAD_LOCKS`` and stale ``_BOOTSTRAP_DONE``
    entries.
    """
    clear_bootstrap_done(label)
    with _THREAD_LOCKS_GUARD:
        _THREAD_LOCKS.pop(label, None)
        _FLOCK_OWNERS.pop(label, None)


def send_prompt(label: str, prompt: str) -> None:
    """BANNED. Always raises."""
    raise RuntimeError(
        "send_prompt() BANNED. Use send_shell_command/send_bootstrap/select_dialog_option."
    )


def send_message(label: str, text: str) -> None:
    """Send a message to a pane using the smux message protocol.

    Adds an automatic sender header so the receiving pane can identify
    the origin.  Unlike :func:`send_bootstrap`, this does not consume a
    Premium Request.  Holds pane_lock to prevent interleaving.
    """
    with pane_lock(label):
        read_pane(label, 5)
        bridge(["message", label, text])
        read_pane(label, 5)
        send_keys(label, "Enter")


def cancel_current(label: str) -> None:
    """Send Ctrl+C."""
    read_pane(label, 5)
    send_keys(label, "C-c")


def send_eof(label: str) -> None:
    """Send Ctrl+D (EOF)."""
    read_pane(label, 5)
    send_keys(label, "C-d")


# === Diagnostics (leveraging smux doctor) ===


def doctor() -> str:
    """Run tmux-bridge doctor to diagnose connection issues."""
    return bridge(["doctor"], check=False)


def diagnose_pane(label: str) -> str:
    """Diagnostic fallback: read terminal when heartbeat times out."""
    return read_pane(label, 200)


_CAPI_ERROR_PATTERNS = ("CAPIError", "rate limit")


def detect_copilot_api_error(content: str) -> bool:
    """Detect Copilot CLI backend errors in pane content.

    Uses narrow patterns to avoid false positives from user code output.
    Matches CAPIError (backend context/request limit) and rate limit messages.
    """
    lower = content.lower()
    return any(p.lower() in lower for p in _CAPI_ERROR_PATTERNS)


def is_capi_context_error(content: str) -> bool:
    """Detect CAPIError specifically (backend context limit exhausted).

    More severe than a rate limit — the session typically cannot recover
    and should be restarted.
    """
    return "CAPIError" in content


def is_process_alive(label: str) -> bool:
    """Check if the pane's process is alive (via tmux-bridge list).

    DIAGNOSTIC ONLY. If process is a shell (zsh/bash), copilot has exited.
    """
    shells = {"zsh", "bash", "fish", "sh", "-zsh", "-bash"}
    for pane in list_panes():
        if pane.label == label:
            return pane.process not in shells
    return False
