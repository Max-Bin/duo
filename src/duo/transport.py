"""Transport layer — wraps tmux-bridge CLI for all pane communication.

tmux-bridge is the sole interface to tmux. We never call raw tmux commands.
list_panes() is diagnostic only — scheduling truth comes from the file protocol.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import time as _time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

_F = TypeVar("_F", bound=Callable[..., Any])


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


_BRIDGE: str | None = None


def _bridge_bin() -> str:
    global _BRIDGE
    if _BRIDGE is None:
        _BRIDGE = _find_bridge()
    return _BRIDGE


def _retry(max_attempts: int = 3, delay: float = 0.5, backoff: float = 2.0) -> Callable[[_F], _F]:
    """Retry decorator with exponential backoff for transient failures."""
    def decorator(func: _F) -> _F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_error = None
            wait = delay
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except RuntimeError as e:
                    last_error = e
                    if attempt < max_attempts - 1:
                        _time.sleep(wait)
                        wait *= backoff
            raise last_error  # type: ignore[misc]
        return wrapper  # type: ignore[return-value]
    return decorator


@_retry()
def bridge(cmd: list[str], *, check: bool = True) -> str:
    """Call tmux-bridge, return stdout."""
    result = subprocess.run(
        [_bridge_bin(), *cmd],
        capture_output=True,
        text=True,
    )
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
    """Label a pane (visible in tmux border via smux .tmux.conf)."""
    bridge(["name", target, label])


def resolve_label(label: str) -> str:
    """Resolve label to pane ID."""
    return bridge(["resolve", label]).strip()


def get_pane_id() -> str:
    """Get current pane's ID."""
    return bridge(["id"]).strip()


@dataclass
class PaneInfo:
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
    return panes


# === Safety: dialog detection ===


def is_in_dialog(label: str) -> bool:
    """Check if a pane is showing a dialog/prompt requiring input.

    Detects common Copilot CLI dialogs by reading terminal content.
    """
    content = read_pane(label, 10)
    dialog_indicators = [
        "? ",          # Copilot question prompt
        "(y/n)",       # Yes/No dialog
        "[Y/n]",       # Default-yes dialog
        "[y/N]",       # Default-no dialog
        "Press Enter",
        "Continue?",
    ]
    return any(indicator in content for indicator in dialog_indicators)


def wait_for_idle(label: str, timeout: float = 30.0, poll_interval: float = 1.0) -> bool:
    """Wait until a pane appears idle (no new output for poll_interval).

    Returns True if idle detected, False if timeout reached.
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
    """Wait until Copilot shows an ask_user dialog.

    Returns True if dialog appeared, False if timeout.
    NEVER interact with Copilot until this returns True.
    """
    import time
    elapsed = 0.0
    while elapsed < timeout:
        if is_in_dialog(label):
            return True
        time.sleep(interval)
        elapsed += interval
    return False


def select_dialog_option(label: str, option: str) -> None:
    """Safely select an option in a Copilot ask_user dialog.

    ONLY call this when is_in_dialog() returns True.
    Raises RuntimeError if Copilot is not in dialog mode.
    """
    if not is_in_dialog(label):
        raise RuntimeError(
            f"SAFETY: Copilot pane '{label}' is NOT in dialog mode. "
            "Sending input now would consume a Premium Request. "
            "Wait for ask_user dialog to appear."
        )
    type_text(label, option)
    read_pane(label, 5)
    send_keys(label, "Enter")


# === Composite operations ===


def send_prompt(label: str, prompt: str) -> None:
    """Full read→type→read→Enter cycle (smux core pattern).

    WARNING: This sends to the main ❯ prompt and consumes a Premium Request.
    Only use for the FIRST message (bootstrap) or when explicitly intended.
    For continuation, use select_dialog_option() instead.
    """
    read_pane(label, 5)          # 1. satisfy read guard
    type_text(label, prompt)     # 2. type text (clears guard)
    read_pane(label, 5)          # 3. verify text landed (re-satisfy guard)
    send_keys(label, "Enter")    # 4. submit


def send_message(label: str, text: str) -> None:
    """Send via smux message protocol (auto sender header)."""
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
