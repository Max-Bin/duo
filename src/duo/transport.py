"""Transport layer — wraps tmux-bridge CLI for all pane communication.

tmux-bridge is the sole interface to tmux. We never call raw tmux commands.
list_panes() is diagnostic only — scheduling truth comes from the file protocol.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import shutil
import subprocess
import threading
import time as _time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

logger = logging.getLogger(__name__)

_SAFE_LABEL = re.compile(r"^[a-zA-Z0-9_.-]+$")

_F = TypeVar("_F", bound=Callable[..., Any])


class TransportBridge(Protocol):
    """Protocol for tmux-bridge command execution."""

    def __call__(self, cmd: list[str], *, check: bool = True) -> str: ...


class PaneReader(Protocol):
    """Protocol for reading tmux pane content."""

    def __call__(self, label: str, lines: int = 50) -> str: ...


class DialogDetector(Protocol):
    """Protocol for detecting dialog state in a pane."""

    def __call__(self, label: str) -> bool: ...


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
    """Retry decorator with exponential backoff for transient failures."""

    def decorator(func: _F) -> _F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_error: Exception = RuntimeError("all retries exhausted")
            wait = delay
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except (RuntimeError, OSError) as e:
                    last_error = e
                    if attempt < max_attempts - 1:
                        _time.sleep(wait)
                        wait *= backoff
            raise last_error

        return wrapper  # type: ignore[return-value]

    return decorator


@_retry()
def bridge(cmd: list[str], *, check: bool = True) -> str:
    """Execute a tmux-bridge sub-command and return its stdout.

    All pane communication flows through this function.  Failures are
    retried automatically by the ``@_retry`` decorator.

    Args:
        cmd: Arguments passed to the tmux-bridge binary (e.g. ``["read", "my-label", "50"]``).
        check: If *True* (default), raise ``RuntimeError`` on non-zero exit.
    """
    try:
        result = subprocess.run(
            [_bridge_bin(), *cmd],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"tmux-bridge {cmd[0]} timed out after 30s"
        ) from exc
    if check and result.returncode != 0:
        raise RuntimeError(f"tmux-bridge {cmd[0]} failed: {result.stderr.strip()}")
    return result.stdout


# === Atomic operations (map 1:1 to tmux-bridge commands) ===


def read_pane(label: str, lines: int = 50) -> str:
    """Read pane output (also satisfies read guard)."""
    return bridge(["read", label, str(lines)])


def type_text(label: str, text: str) -> None:
    """Type text into pane (no Enter). Requires prior read_pane."""
    bridge(["type", label, text])


def send_keys(label: str, *keys: str) -> None:
    """Send special keys. Requires prior read_pane."""
    bridge(["keys", label, *keys])


def name_pane(target: str, label: str) -> None:
    """Label a pane (visible in tmux border via smux .tmux.conf).

    Raises:
        ValueError: If *label* contains unsafe characters.
    """
    _validate_label(label)
    bridge(["name", target, label])


def _validate_label(label: str) -> None:
    """Ensure pane label is safe for shell use."""
    if not _SAFE_LABEL.match(label):
        raise ValueError(f"Unsafe pane label: {label!r}")


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


@dataclass
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
    if _pr_callback is not None:
        _pr_callback(label, action, context)


def get_pr_log() -> list[dict[str, str]]:
    """Return the full PR consumption audit log."""
    with _LOCK:
        return list(_PR_LOG)


def _is_at_main_prompt(content: str) -> bool:
    """True if pane shows Copilot ❯ prompt. ANY input here = PR consumed."""
    for line in reversed(content.strip().split("\n")):
        s = line.strip()
        if s.startswith("❯") and ("Type @" in s or s == "❯" or "mention files" in s):
            return True
        if "Remaining reqs" in s or "shift+tab" in s:
            continue
        if s and not s.startswith("─"):
            break
    return False


def is_in_dialog(label: str) -> bool:
    """True if Copilot shows a ╭╰ dialog box with numbered options."""
    content = read_pane(label, 20)
    if _is_at_main_prompt(content):
        return False
    has_box = any("╰─" in l or "╭─" in l for l in content.split("\n"))
    has_opt = any(
        any(l.strip().startswith(f"{n}.") or f"❯ {n}." in l for n in range(1, 7))
        for l in content.split("\n")
    )
    return has_box and has_opt


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
    elapsed = 0.0
    while elapsed < timeout:
        current = read_pane(label, 20)
        if current == previous and current.strip():
            return True
        previous = current
        _time.sleep(poll_interval)
        elapsed += poll_interval
    return False


def wait_for_dialog(label: str, timeout: float = 300, interval: float = 5) -> bool:
    """Wait for STABLE dialog (double-checked)."""
    elapsed = 0.0
    while elapsed < timeout:
        if is_in_dialog_stable(label):
            return True
        _time.sleep(interval)
        elapsed += interval
    return False


def safe_enter(label: str) -> None:
    """Press Enter ONLY if NOT at ❯ prompt. Raises otherwise."""
    content = read_pane(label, 20)
    if _is_at_main_prompt(content):
        raise RuntimeError(
            f"BLOCKED: '{label}' at ❯ prompt. Enter = PR consumed. REFUSED."
        )
    send_keys(label, "Enter")


def is_permission_dialog(label: str) -> bool:
    """True if dialog is a permission/approval prompt (safe to auto-approve).

    Permission dialogs contain phrases like 'Do you want to run',
    'Do you want to edit', 'Allow directory', 'approve'.
    These are SAFE to auto-approve with option 2.

    ask_user dialogs (feature choices, continuation prompts) are NOT
    permission dialogs and MUST be reviewed by the commander before selecting.
    """
    content = read_pane(label, 20)
    perm_indicators = [
        "Do you want to run",
        "Do you want to edit",
        "Allow directory",
        "approve",
        "Do you want to allow",
    ]
    return any(ind in content for ind in perm_indicators)


def select_dialog_option(label: str, option: str) -> None:
    """Select dialog option with triple safety."""
    if not is_in_dialog_stable(label):
        raise RuntimeError(f"SAFETY: '{label}' not in stable dialog. REFUSED.")
    type_text(label, option)
    safe_enter(label)
    _record_pr(label, "dialog_option", option[:80])


# === Composite operations ===


def send_shell_command(label: str, command: str) -> None:
    """Send to SHELL (before Copilot starts). No PR cost.

    Safety: rejects if pane is at Copilot's main ❯ prompt.
    """
    content = read_pane(label, 5)
    if _is_at_main_prompt(content):
        raise RuntimeError(
            f"BLOCKED: '{label}' at ❯ prompt. "
            "send_shell_command is for shell-only. Use select_dialog_option."
        )
    type_text(label, command)
    read_pane(label, 5)
    send_keys(label, "Enter")


def send_bootstrap(label: str, prompt: str) -> None:
    """THE ONE bootstrap prompt. 1 PR. PERMANENTLY LOCKED after use."""
    with _LOCK:
        if label in _BOOTSTRAP_DONE:
            raise RuntimeError(f"BLOCKED: Bootstrap done for '{label}'. PERMANENT LOCK.")
        _BOOTSTRAP_DONE.add(label)
    read_pane(label, 5)
    type_text(label, prompt)
    read_pane(label, 5)
    send_keys(label, "Enter")
    _record_pr(label, "bootstrap", prompt[:80])


def clear_bootstrap_done(label: str) -> None:
    """Remove *label* from the bootstrap-done set (thread-safe)."""
    with _LOCK:
        _BOOTSTRAP_DONE.discard(label)


def send_prompt(label: str, prompt: str) -> None:
    """BANNED. Always raises."""
    raise RuntimeError(
        "send_prompt() BANNED. Use send_shell_command/send_bootstrap/select_dialog_option."
    )


def send_message(label: str, text: str) -> None:
    """Send a message to a pane using the smux message protocol.

    Adds an automatic sender header so the receiving pane can identify
    the origin.  Unlike :func:`send_bootstrap`, this does not consume a
    Premium Request.
    """
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


def is_process_alive(label: str) -> bool:
    """Check if the pane's process is alive (via tmux-bridge list).

    DIAGNOSTIC ONLY. If process is a shell (zsh/bash), copilot has exited.
    """
    shells = {"zsh", "bash", "fish", "sh", "-zsh", "-bash"}
    for pane in list_panes():
        if pane.label == label:
            return pane.process not in shells
    return False
