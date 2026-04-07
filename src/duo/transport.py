"""Transport layer — wraps tmux-bridge CLI for all pane communication.

tmux-bridge is the sole interface to tmux. We never call raw tmux commands.
list_panes() is diagnostic only — scheduling truth comes from the file protocol.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass


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


# === Composite operations ===


def send_prompt(label: str, prompt: str) -> None:
    """Full read→type→read→Enter cycle (smux core pattern)."""
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
