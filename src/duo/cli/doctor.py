"""Doctor subsystem — environment diagnostics and auto-fix."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from duo.config import get_config
from duo.protocol import DUO_DIR, TASKS_DIR, TaskStatus, list_tasks

_TERMINAL_STATES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ESCALATED}
)

_TMUX_TIMEOUT = 10  # seconds for tmux kill/health operations


@dataclass(slots=True)
class CheckResult:
    """Result of a single doctor diagnostic check."""

    name: str
    status: str  # "pass", "warn", "fail"
    message: str
    fix: str  # suggested fix action


def _doctor_check_python() -> CheckResult:
    """Check Python version >= 3.12."""
    vi = sys.version_info
    ver = f"{vi.major}.{vi.minor}.{vi.micro}"
    if vi >= (3, 12):
        return CheckResult("Python", "pass", ver, "")
    return CheckResult("Python", "fail", ver, "Upgrade to Python >= 3.12")


def _doctor_check_tmux() -> CheckResult:
    """Check tmux installed and version >= 3.0."""
    if shutil.which("tmux") is None:
        return CheckResult(
            "tmux",
            "fail",
            "not found",
            "Install with: brew install tmux (macOS) or apt install tmux (Linux)",
        )
    try:
        proc = subprocess.run(
            ["tmux", "-V"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_TMUX_TIMEOUT,
        )
        raw = proc.stdout.strip()
        match = re.search(r"(\d+(?:\.\d+)?)", raw)
        if match:
            ver_str = match.group(1)
            parts = ver_str.split(".")
            major = int(parts[0])
            minor = int(parts[1]) if len(parts) > 1 else 0
            if (major, minor) >= (3, 0):
                return CheckResult("tmux", "pass", f"{ver_str} (>= 3.0)", "")
            return CheckResult(
                "tmux",
                "warn",
                f"{ver_str} (< 3.0)",
                "Upgrade tmux to >= 3.0",
            )
        return CheckResult("tmux", "pass", "installed", "")
    except (subprocess.TimeoutExpired, OSError):
        return CheckResult("tmux", "pass", "installed", "")


def _doctor_check_tmux_bridge() -> CheckResult:
    """Check tmux-bridge binary exists and is executable."""
    path_loc = shutil.which("tmux-bridge")
    if path_loc is not None:
        return CheckResult("tmux-bridge", "pass", f"found at {path_loc}", "")
    smux_path = Path.home() / ".smux" / "bin" / "tmux-bridge"
    if smux_path.exists() and os.access(str(smux_path), os.X_OK):
        return CheckResult(
            "tmux-bridge",
            "pass",
            f"found at {smux_path}",
            "",
        )
    if smux_path.exists():
        return CheckResult(
            "tmux-bridge",
            "fail",
            f"found at {smux_path} but not executable",
            f"Run: chmod +x {smux_path}",
        )
    return CheckResult(
        "tmux-bridge",
        "fail",
        "not found",
        "Install from: https://github.com/anthropic-ai/tmux-bridge",
    )


def _doctor_check_claude_cli() -> CheckResult:
    """Check claude CLI available."""
    if shutil.which("claude") is not None:
        return CheckResult("claude CLI", "pass", "installed", "")
    return CheckResult(
        "claude CLI",
        "warn",
        "not found",
        "Install for 'duo think': npm i -g @anthropic-ai/claude-cli",
    )


def _doctor_check_copilot_cli() -> CheckResult:
    """Check Copilot CLI available."""
    if (
        shutil.which("github-copilot-cli") is not None
        or shutil.which("copilot") is not None
    ):
        return CheckResult("Copilot CLI", "pass", "installed", "")
    return CheckResult(
        "Copilot CLI",
        "warn",
        "not found",
        "Install from: https://github.com/github/copilot-cli",
    )


def _doctor_check_duo_dir() -> CheckResult:
    """Check ~/.duo directory writable and disk space >= 100MB."""
    if not DUO_DIR.exists():
        return CheckResult(
            "~/.duo",
            "fail",
            "missing",
            "Run: duo init",
        )
    if not os.access(str(DUO_DIR), os.W_OK):
        return CheckResult(
            "~/.duo",
            "fail",
            "not writable",
            f"Run: chmod u+w {DUO_DIR}",
        )
    try:
        usage = shutil.disk_usage(str(DUO_DIR))
        free_mb = usage.free / (1024 * 1024)
        if free_mb < 100:
            return CheckResult(
                "~/.duo",
                "warn",
                f"writable ({free_mb:.0f} MB free)",
                "Free up disk space (< 100 MB remaining)",
            )
        free_gb = free_mb / 1024
        return CheckResult(
            "~/.duo",
            "pass",
            f"writable ({free_gb:.1f} GB free)",
            "",
        )
    except OSError:
        return CheckResult("~/.duo", "pass", "writable", "")


def _doctor_check_config() -> CheckResult:
    """Check config.json exists, is valid JSON, and passes validation."""
    config_path = DUO_DIR / "config.json"
    if not config_path.exists():
        return CheckResult(
            "config.json",
            "warn",
            "missing",
            "Run: duo init",
        )
    try:
        json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError):
        return CheckResult(
            "config.json",
            "warn",
            "invalid JSON",
            "Run: duo config reset",
        )
    from duo.config import validate_config

    issues = validate_config()
    if issues:
        return CheckResult(
            "config.json",
            "warn",
            f"{len(issues)} issue(s)",
            "Run: duo config validate",
        )
    return CheckResult("config.json", "pass", "valid", "")


def _doctor_check_tmux_session() -> CheckResult:
    """Check for active tmux session."""
    if shutil.which("tmux") is None:
        return CheckResult(
            "tmux session",
            "warn",
            "tmux not installed",
            "Install tmux first",
        )
    try:
        proc = subprocess.run(
            ["tmux", "list-sessions"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_TMUX_TIMEOUT,
        )
        if proc.returncode == 0:
            return CheckResult("tmux session", "pass", "active", "")
        return CheckResult(
            "tmux session",
            "warn",
            "no active session",
            "Start: tmux new -s duo",
        )
    except (subprocess.TimeoutExpired, OSError):
        return CheckResult(
            "tmux session",
            "warn",
            "could not query tmux",
            "Check tmux installation",
        )


def _doctor_check_task_timeout() -> CheckResult:
    """Check task_timeout config value."""
    timeout_val = get_config("task_timeout")
    if isinstance(timeout_val, int) and timeout_val >= 0:
        label = f"{timeout_val}s" if timeout_val > 0 else "disabled"
        return CheckResult("task_timeout", "pass", f"configured ({label})", "")
    return CheckResult(
        "task_timeout",
        "warn",
        "invalid value",
        "Set to 0 or positive integer in config.json",
    )


def _doctor_check_corrupted() -> CheckResult:
    """Count corrupted tasks in ~/.duo/corrupted/."""
    from duo.protocol import list_corrupted

    items = list_corrupted()
    count = len(items)
    if count == 0:
        return CheckResult("corrupted tasks", "pass", "0", "")
    return CheckResult(
        "corrupted tasks",
        "warn",
        str(count),
        "Run: duo cleanup --corrupted",
    )


def _doctor_check_git() -> CheckResult:
    """Check git available."""
    if shutil.which("git") is None:
        return CheckResult(
            "git",
            "warn",
            "not found",
            "Install git: https://git-scm.com/downloads",
        )
    try:
        proc = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_TMUX_TIMEOUT,
        )
        raw = proc.stdout.strip()
        match = re.search(r"(\d+\.\d+[\.\d]*)", raw)
        ver = match.group(1) if match else "installed"
        return CheckResult("git", "pass", ver, "")
    except (subprocess.TimeoutExpired, OSError):
        return CheckResult("git", "pass", "installed", "")


_COPILOT_FD_WARN = 500
_COPILOT_FD_CRITICAL = 2000
_COPILOT_CHILD_WARN = 10
_COPILOT_KQUEUE_WARN = 50


def _get_pid_fd_count(pid: int) -> int:
    """Return total open fd count for *pid* using lsof."""
    try:
        proc = subprocess.run(
            ["lsof", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            return -1
        # lsof header is 1 line; each subsequent line = 1 fd entry
        return max(0, len(proc.stdout.strip().splitlines()) - 1)
    except (OSError, subprocess.TimeoutExpired):
        return -1


def _get_pid_kqueue_count(pid: int) -> int:
    """Return kqueue fd count for *pid* using lsof (macOS only)."""
    try:
        proc = subprocess.run(
            ["lsof", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            return -1
        return sum(1 for line in proc.stdout.splitlines() if "KQUEUE" in line)
    except (OSError, subprocess.TimeoutExpired):
        return -1


def _get_pid_child_count(pid: int) -> int:
    """Return count of child processes for *pid*."""
    try:
        proc = subprocess.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            return 0
        lines = [l for l in proc.stdout.strip().splitlines() if l.strip()]
        return len(lines)
    except (OSError, subprocess.TimeoutExpired):
        return -1


def _doctor_check_copilot_health() -> list[CheckResult]:
    """Check Copilot pane process health (fd/kqueue/child counts).

    Returns a list of CheckResult — one per active Copilot pane.
    An empty list is returned when there are no active panes.
    """
    from duo.transport import get_pane_pid

    try:
        tasks = list_tasks()
    except (FileNotFoundError, OSError, ValueError):
        return []

    active = [t for t in tasks if t.status not in _TERMINAL_STATES and t.pane_label]
    if not active:
        return []

    results: list[CheckResult] = []
    for task in active:
        pid = get_pane_pid(task.pane_label)
        if pid is None:
            results.append(
                CheckResult(
                    f"pane:{task.pane_label}",
                    "warn",
                    "PID unavailable",
                    "Pane may be dead — run: duo status",
                )
            )
            continue

        fd_count = _get_pid_fd_count(pid)
        kqueue_count = _get_pid_kqueue_count(pid)
        child_count = _get_pid_child_count(pid)

        parts: list[str] = []
        worst = "pass"

        if fd_count >= 0:
            parts.append(f"fds={fd_count}")
            if fd_count >= _COPILOT_FD_CRITICAL:
                worst = "fail"
            elif fd_count >= _COPILOT_FD_WARN:
                worst = "warn" if worst != "fail" else worst
        if kqueue_count >= 0:
            parts.append(f"kqueue={kqueue_count}")
            if kqueue_count >= _COPILOT_KQUEUE_WARN:
                worst = "warn" if worst != "fail" else worst
        if child_count >= 0:
            parts.append(f"children={child_count}")
            if child_count >= _COPILOT_CHILD_WARN:
                worst = "warn" if worst != "fail" else worst

        msg = f"PID {pid}: {', '.join(parts)}" if parts else f"PID {pid}: healthy"
        fix = ""
        if worst == "fail":
            fix = "Critical — restart session: duo stop + duo start"
            _emit_restart_signal(task.id)
        elif worst == "warn":
            fix = "Run: duo stop <task> && duo start --resume"

        results.append(CheckResult(f"pane:{task.pane_label}", worst, msg, fix))

    return results


def _doctor_check_capi_error() -> list[CheckResult]:
    """Check journals for recent CAPIError events (backend context limit).

    Reads each active task's journal for ``capi_error`` events.  Unlike pane
    content reading, journal checks are race-free and replay-safe.
    """
    from duo.protocol import read_jsonl

    try:
        tasks = list_tasks()
    except (FileNotFoundError, OSError, ValueError):
        return []

    active = [t for t in tasks if t.status not in _TERMINAL_STATES]
    if not active:
        return []

    results: list[CheckResult] = []
    for task in active:
        if not task.journal_path.exists():
            continue
        events = read_jsonl(task.journal_path)
        capi_events = [e for e in events if e.get("event") == "capi_error"]
        if capi_events:
            last = capi_events[-1]
            ts = last.get("ts", "unknown")[:19]
            results.append(
                CheckResult(
                    f"capi:{task.id}",
                    "fail",
                    f"CAPIError detected at {ts}",
                    "Session context limit exhausted — restart: duo stop + duo start",
                )
            )

    return results


def _emit_restart_signal(task_id: str) -> None:
    """Write a restart-recommended signal file for a task.

    The file is placed at ~/.duo/tasks/{id}/restart-recommended.
    CEO automation can check for this file and initiate orderly restart.
    """
    signal_path = TASKS_DIR / task_id / "restart-recommended"
    with contextlib.suppress(OSError):
        signal_path.write_text(
            f"Restart recommended — health check detected critical thresholds.\n"
            f"Time: {time.strftime('%Y-%m-%dT%H:%M:%S')}\n"
        )


def _doctor_check_stale_locks() -> CheckResult:
    """Check for stale .lock files from interrupted duo start commands."""
    lock_files = list(TASKS_DIR.glob(".*.lock")) if TASKS_DIR.exists() else []
    if not lock_files:
        return CheckResult("stale locks", "pass", "none", "")
    names = [f.name for f in lock_files]
    return CheckResult(
        "stale locks",
        "warn",
        f"{len(lock_files)} found: {', '.join(names)}",
        "Remove manually or run: duo cleanup",
    )


def _doctor_check_orphan_worktrees() -> CheckResult:
    """Check for git worktrees with no matching task in TASKS_DIR."""
    try:
        r = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return CheckResult("orphan worktrees", "pass", "skipped (git unavailable)", "")

    if r.returncode != 0:
        return CheckResult("orphan worktrees", "pass", "skipped (not a git repo)", "")

    known_ids = (
        {d.name for d in TASKS_DIR.iterdir() if d.is_dir()}
        if TASKS_DIR.exists()
        else set()
    )

    orphans: list[str] = []
    worktree_base = str(Path(str(get_config("worktree_base_path"))).resolve())
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            wt_path = line[len("worktree ") :]
            wt_resolved = str(Path(wt_path).resolve())
            wt_name = Path(wt_path).name
            is_duo_worktree = wt_resolved.startswith(
                worktree_base + "/"
            ) or wt_name.startswith("duo-")
            task_id = wt_name.removeprefix("duo-")
            if is_duo_worktree and task_id not in known_ids:
                orphans.append(wt_name)

    if not orphans:
        return CheckResult("orphan worktrees", "pass", "none", "")
    return CheckResult(
        "orphan worktrees",
        "warn",
        f"{len(orphans)} found: {', '.join(orphans[:5])}",
        "Remove with: git worktree remove <path>",
    )


_DOCTOR_CHECKS: list[Any] = [
    _doctor_check_python,
    _doctor_check_tmux,
    _doctor_check_tmux_bridge,
    _doctor_check_claude_cli,
    _doctor_check_copilot_cli,
    _doctor_check_duo_dir,
    _doctor_check_config,
    _doctor_check_tmux_session,
    _doctor_check_task_timeout,
    _doctor_check_corrupted,
    _doctor_check_stale_locks,
    _doctor_check_orphan_worktrees,
    _doctor_check_git,
]

_STATUS_ICONS: dict[str, str] = {
    "pass": "✓ PASS",
    "warn": "⚠ WARN",
    "fail": "✗ FAIL",
}

_STATUS_COLORS: dict[str, str] = {
    "pass": "green",
    "warn": "yellow",
    "fail": "red",
}


def _doctor_auto_fix() -> list[str]:
    """Attempt to auto-fix known issues. Returns list of fixed descriptions."""
    import shutil

    fixed: list[str] = []

    # Fix 1: Remove stale .lock files (only if older than 1 hour)
    if TASKS_DIR.exists():
        stale_threshold = time.time() - 3600
        for lf in TASKS_DIR.glob(".*.lock"):
            try:
                if lf.stat().st_mtime < stale_threshold:
                    lf.unlink(missing_ok=True)
                    fixed.append(f"removed stale lock: {lf.name}")
            except OSError:
                pass

    # Fix 2: Purge quarantined (corrupted) tasks
    from duo.protocol import list_corrupted

    for p in list_corrupted():
        shutil.rmtree(p, ignore_errors=True)
        fixed.append(f"purged quarantined task: {p.name}")

    # Fix 3: Remove orphan duo-* worktrees
    try:
        r = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        r = None

    if r and r.returncode == 0:
        known_ids = (
            {d.name for d in TASKS_DIR.iterdir() if d.is_dir()}
            if TASKS_DIR.exists()
            else set()
        )
        worktree_base = str(Path(str(get_config("worktree_base_path"))).resolve())
        for line in r.stdout.splitlines():
            if line.startswith("worktree "):
                wt_path = line[len("worktree ") :]
                wt_resolved = str(Path(wt_path).resolve())
                wt_name = Path(wt_path).name
                is_duo_worktree = wt_resolved.startswith(
                    worktree_base + "/"
                ) or wt_name.startswith("duo-")
                task_id = wt_name.removeprefix("duo-")
                if is_duo_worktree and task_id not in known_ids:
                    rm = subprocess.run(
                        ["git", "worktree", "remove", "--force", wt_path],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if rm.returncode == 0:
                        fixed.append(f"removed orphan worktree: {wt_name}")

    return fixed


@click.command()
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option("--strict", is_flag=True, help="Exit non-zero on warnings too.")
@click.option("--fix", is_flag=True, help="Auto-fix issues that can be resolved.")
@click.option("-q", "--quiet", is_flag=True, help="Print only failures (one per line)")
def doctor(as_json: bool, strict: bool, fix: bool, quiet: bool) -> None:
    """Check environment dependencies and configuration."""
    results: list[CheckResult] = [fn() for fn in _DOCTOR_CHECKS]
    results.extend(_doctor_check_copilot_health())
    results.extend(_doctor_check_capi_error())

    fixed_items: list[str] = []
    if fix:
        fixed_items = _doctor_auto_fix()

    counts = {"pass": 0, "warn": 0, "fail": 0}
    for r in results:
        counts[r.status] += 1
    total = len(results)

    if quiet:
        for r in results:
            if r.status == "fail":
                click.echo(f"{r.name}: {r.message}")
        has_fail = counts["fail"] > 0
        has_warn = counts["warn"] > 0
        if has_fail or (strict and has_warn):
            raise SystemExit(1)
        return

    if as_json:
        payload: dict[str, Any] = {
            "checks": [
                {
                    "name": r.name,
                    "status": r.status,
                    "message": r.message,
                    "fix": r.fix,
                }
                for r in results
            ],
            "summary": {**counts, "total": total},
        }
        if fix:
            payload["fixed"] = fixed_items
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo("Duo Environment Diagnostics")
        click.echo("\u2500" * 28)
        for r in results:
            icon = _STATUS_ICONS[r.status]
            color = _STATUS_COLORS[r.status]
            suffix = ""
            if r.fix:
                suffix = f" — {r.fix}"
            line = f"  {icon}  {r.name:<18}{r.message}{suffix}"
            click.echo(click.style(line, fg=color))
        parts: list[str] = []
        parts.append(f"{counts['pass']}/{total} checks passed")
        if counts["warn"]:
            parts.append(
                f"{counts['warn']} warning{'s' if counts['warn'] != 1 else ''}"
            )
        if counts["fail"]:
            parts.append(
                f"{counts['fail']} failure{'s' if counts['fail'] != 1 else ''}"
            )
        click.echo(f"\n{', '.join(parts)}")
        if fixed_items:
            click.echo(f"\nAuto-fixed {len(fixed_items)} issue(s):")
            for item in fixed_items:
                click.echo(f"  ✓ {item}")

    has_fail = counts["fail"] > 0
    has_warn = counts["warn"] > 0
    if has_fail or (strict and has_warn):
        raise SystemExit(1)
