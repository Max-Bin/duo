"""Thinking sessions — brainstorm with Claude Code before starting a task.

Spawns a Claude Code instance in a tmux pane for structured thinking.
No Copilot pane, no Premium Request consumed.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

from duo.protocol import DUO_DIR, atomic_write_text, now_iso

__all__ = [
    "THINKING_DIR",
    "append_session_log",
    "close_pane",
    "ensure_pane",
    "extract_response",
    "list_sessions",
    "thinking_dir",
    "wait_for_response_stable",
    "write_plan_template",
    "write_thinking_claude_md",
]

logger = logging.getLogger(__name__)

THINKING_DIR = DUO_DIR / "thinking"

# Timing constants
_SPLIT_WAIT = 0.5
_CD_WAIT = 0.3
_STABLE_THRESHOLD = 2.0
_POLL_INTERVAL = 0.5


def thinking_dir(name: str) -> Path:
    """Return ``~/.duo/thinking/{name}/``."""
    return THINKING_DIR / name


_THINKING_CLAUDE_MD = """\
# Thinking Partner — {name}

You are a software-engineering thinking partner. Your job is to help
the user refine a software task idea from vague to actionable.

## Your Conversational Style

- Ask clarifying questions when scope is ambiguous
- Propose 2-3 concrete approaches with trade-offs
- Surface hidden assumptions and risks
- Challenge over-engineering
- Push for minimum viable scope

## Rules

- Do NOT write code into the repository
- Do NOT modify any files outside ~/.duo/thinking/{name}/
- You CAN read files, grep code, and search the web to inform your thinking
- Your output is thinking and analysis, not implementation

## Finalize

When the user says "/done", asks to "finalize", or sends a finalize
instruction, produce a plan document:

1. Read the template at ~/.duo/thinking/{name}/plan-template.md
2. Distill the entire conversation into that format
3. Write the result to ~/.duo/thinking/{name}/plan.md
4. Confirm: "Plan written to ~/.duo/thinking/{name}/plan.md"

Be concrete and specific — this plan will be sent as the initial
prompt to a Copilot code-generating agent.
"""

_PLAN_TEMPLATE = """\
# Plan: {name}

## Goal

(One sentence describing what we're building and why.)

## Scope

### In scope
- Item 1
- Item 2

### Out of scope
- Item 1
- Item 2

## Approach

(Concrete technical plan in 3-5 paragraphs. Architecture, key design
decisions, data flow, dependencies.)

## Acceptance Criteria

- [ ] Verifiable condition 1
- [ ] Verifiable condition 2
- [ ] Verifiable condition 3

## Risks / Unknowns

- Risk 1
- Risk 2
"""


def _ensure_thinking_dir(name: str) -> Path:
    """Return the thinking session directory, creating it if needed."""
    tdir = thinking_dir(name)
    tdir.mkdir(parents=True, exist_ok=True)
    return tdir


def write_thinking_claude_md(name: str) -> Path:
    """Write CLAUDE.md into the thinking session directory.

    Creates ``~/.duo/thinking/{name}/`` if it does not exist.
    Returns the path to the written CLAUDE.md.
    """
    tdir = _ensure_thinking_dir(name)
    claude_md = tdir / "CLAUDE.md"
    atomic_write_text(claude_md, _THINKING_CLAUDE_MD.format(name=name))
    logger.info("Wrote %s", claude_md)
    return claude_md


def write_plan_template(name: str) -> Path:
    """Write plan-template.md into the thinking session directory.

    Creates ``~/.duo/thinking/{name}/`` if it does not exist.
    Returns the path to the written plan-template.md.
    """
    tdir = _ensure_thinking_dir(name)
    tmpl = tdir / "plan-template.md"
    atomic_write_text(tmpl, _PLAN_TEMPLATE.format(name=name))
    logger.info("Wrote %s", tmpl)
    return tmpl


def _pane_label(name: str) -> str:
    """Return the tmux pane label for a thinking session."""
    return f"think-{name}"


def _pane_exists(label: str) -> bool:
    """Check if a pane with *label* exists (resolve succeeds)."""
    from duo.transport import resolve_label

    try:
        resolve_label(label)
    except (RuntimeError, ValueError):
        return False
    else:
        return True


def _pane_alive(label: str) -> bool:
    """Check if the pane is alive (process is Claude Code, not a shell)."""
    from duo.transport import is_process_alive

    return is_process_alive(label)


def _spawn_claude_pane(label: str, working_dir: str) -> str:
    """Spawn a new tmux pane, cd to *working_dir*, and start ``claude``.

    Returns the raw tmux pane ID (e.g. ``%42``).
    Raises ``RuntimeError`` on failure.
    """
    from duo.transport import (
        get_tmux_session_target,
        name_pane,
        send_shell_command,
        wait_for_idle,
    )

    # Target the caller's session to prevent cross-session pollution
    session_target = get_tmux_session_target()

    result = subprocess.run(
        ["tmux", "split-window", "-v", "-P", "-F", "#{pane_id}", "-t", session_target],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to create thinking pane. Is tmux running? ({result.stderr.strip()})"
        )

    pane_id = result.stdout.strip()
    name_pane(pane_id, label)

    # Tile layout — target the new pane to resolve correct window
    subprocess.run(
        ["tmux", "select-layout", "-t", pane_id, "tiled"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    time.sleep(_SPLIT_WAIT)
    try:
        send_shell_command(label, f"cd {working_dir}")
        time.sleep(_CD_WAIT)
        send_shell_command(label, "claude")
    except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
        # Clean up orphaned pane
        from duo.transport import kill_pane

        kill_pane(pane_id)
        raise RuntimeError(f"Failed to start Claude Code in pane: {exc}") from exc

    wait_for_idle(label, timeout=30)
    logger.info("Claude Code started in pane %s (label=%s)", pane_id, label)
    return pane_id


def ensure_pane(name: str) -> str:
    """Ensure a thinking pane exists for *name*.

    Creates the directory, writes CLAUDE.md + plan-template, and spawns
    the pane if it doesn't already exist.  Recovers dead panes.

    Returns the pane label.
    """
    from duo.transport import send_shell_command, wait_for_idle

    label = _pane_label(name)

    # Write scaffold files if needed
    tdir = thinking_dir(name)
    if not (tdir / "CLAUDE.md").exists():
        write_thinking_claude_md(name)
    if not (tdir / "plan-template.md").exists():
        write_plan_template(name)

    if _pane_exists(label):
        if not _pane_alive(label):
            # Pane exists but Claude Code exited — restart
            logger.info("Recovering dead thinking pane %s", label)
            send_shell_command(label, "claude")
            wait_for_idle(label, timeout=30)
        return label

    # No pane — create one
    _spawn_claude_pane(label, str(tdir))
    return label


def wait_for_response_stable(
    label: str,
    timeout: float = 120.0,
    stable_threshold: float = _STABLE_THRESHOLD,
) -> str:
    """Wait until the thinking pane reaches a stable state.

    Returns:
        ``"idle"``    — at main prompt, output stable for *stable_threshold* seconds
        ``"dialog"``  — Claude Code is asking a question (ask_user dialog)
        ``"timeout"`` — neither idle nor dialog within timeout
    """
    from duo.transport import (
        DialogKind,
        detect_dialog_kind,
        is_at_main_prompt,
        read_pane,
    )

    deadline = time.time() + timeout
    last_hash: int | None = None
    stable_since: float | None = None

    while time.time() < deadline:
        content = read_pane(label, 50)

        # Active spinner means still working
        has_spinner = any(
            marker in content for marker in ("\u25c9 ", "\u25ce ", "\u25cb ")
        )

        # Dialog detection
        dialog_kind = detect_dialog_kind(content)
        if dialog_kind != DialogKind.NONE:
            return "dialog"

        # Main prompt = idle
        at_prompt = is_at_main_prompt(content)
        if at_prompt and not has_spinner:
            current_hash = hash(content)
            if current_hash == last_hash:
                if (
                    stable_since is not None
                    and time.time() - stable_since > stable_threshold
                ):
                    return "idle"
            else:
                last_hash = current_hash
                stable_since = time.time()
        else:
            last_hash = None
            stable_since = None

        time.sleep(_POLL_INTERVAL)

    return "timeout"


def extract_response(content_before: str, content_after: str, user_message: str) -> str:
    """Extract the assistant response from pane content delta.

    Compares *content_before* (snapshot before sending) with
    *content_after* (snapshot after response is stable) and returns
    the cleaned assistant text.
    """
    before_lines = content_before.splitlines()
    after_lines = content_after.splitlines()

    # Longest common prefix
    common = 0
    for i, (a, b) in enumerate(zip(before_lines, after_lines, strict=False)):
        if a == b:
            common = i + 1
        else:
            break

    delta_lines = after_lines[common:]

    # Filter noise
    user_stripped = user_message.strip()
    filtered: list[str] = []
    for line in delta_lines:
        stripped = line.strip()
        if stripped == user_stripped:
            continue
        if stripped in ("", ">", "\u276f"):
            continue
        if stripped.startswith(
            ("\u25cf Edit", "\u25cf Read", "\u25cf Bash", "\u25cf Grep")
        ):
            continue
        filtered.append(line)

    return "\n".join(filtered)


def append_session_log(name: str, user_message: str, response: str) -> None:
    """Append a timestamped ask entry to session.log."""
    tdir = thinking_dir(name)
    log_file = tdir / "session.log"
    entry = (
        f"--- ask at {now_iso()} ---\n[user] {user_message}\n[response]\n{response}\n\n"
    )
    with log_file.open("a", encoding="utf-8") as f:
        f.write(entry)
        f.flush()
        os.fsync(f.fileno())


def list_sessions() -> list[dict[str, str]]:
    """List all thinking sessions with their status.

    Returns a list of dicts: ``{"name", "pane", "status", "files"}``.
    """
    if not THINKING_DIR.exists():
        return []

    sessions: list[dict[str, str]] = []
    for d in sorted(THINKING_DIR.iterdir()):
        if not d.is_dir():
            continue
        name = d.name
        label = _pane_label(name)
        exists = _pane_exists(label)
        alive = _pane_alive(label) if exists else False

        pane = "alive" if alive else ("dead" if exists else "none")
        has_plan = (d / "plan.md").exists()
        status = "finalized" if has_plan else "active"
        files = " ".join(f.name for f in sorted(d.iterdir()) if f.is_file())

        sessions.append({"name": name, "pane": pane, "status": status, "files": files})
    return sessions


def close_pane(name: str) -> bool:
    """Close the thinking pane (keep files). Returns True if closed."""
    from duo.transport import kill_pane, resolve_label

    label = _pane_label(name)
    try:
        pane_id = resolve_label(label)
    except (RuntimeError, ValueError):
        return False

    return kill_pane(pane_id)
