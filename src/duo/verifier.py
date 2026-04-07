"""Quality gate — verifies each step before the task advances.

Runs security scope, secret-leak, untracked-file, and acceptance-test
checks against the worktree.  Returns Pass or Correction.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import PurePosixPath

from duo.protocol import StepResult, Subtask, Task, append_event

# === Result types ===


@dataclass(frozen=True)
class Pass:
    """All checks passed."""


@dataclass(frozen=True)
class Correction:
    """One or more checks failed; includes the reason for the caller."""

    reason: str


VerifyResult = Pass | Correction


# === Git / subprocess helpers ===


def git_diff_names(worktree: str) -> set[str]:
    """Return the set of file paths changed relative to HEAD."""
    proc = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git diff --name-only failed in {worktree}: {proc.stderr.strip()}"
        )
    return {line for line in proc.stdout.strip().splitlines() if line}


def git_diff(worktree: str) -> str:
    """Return the full unified diff relative to HEAD."""
    proc = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git diff failed in {worktree}: {proc.stderr.strip()}")
    return proc.stdout


def git_untracked(worktree: str) -> list[str]:
    """Return untracked files not covered by .gitignore."""
    proc = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-files failed in {worktree}: {proc.stderr.strip()}")
    return [line for line in proc.stdout.strip().splitlines() if line]


def run_in_worktree(worktree: str, command: str) -> int:
    """Run a command inside *worktree* and return its exit code."""
    proc = subprocess.run(
        shlex.split(command),
        shell=False,
        cwd=worktree,
    )
    return proc.returncode


# === Verification checks ===


def _current_subtask(task: Task) -> Subtask | None:
    """Look up the subtask matching *task.current_step*."""
    for st in task.subtasks:
        if st.step_id == task.current_step:
            return st
    return None


def _check_security_scope(
    task: Task,
    changed: set[str],
    writable_paths: list[str],
) -> VerifyResult | None:
    """HARD reject if any changed file falls outside writable_paths."""
    for path in sorted(changed):
        if not any(PurePosixPath(path).match(pat) for pat in writable_paths):
            reason = f"Security violation: '{path}' is outside writable paths {writable_paths}"
            append_event(
                task,
                "security_violation",
                {
                    "file": path,
                    "writable_paths": writable_paths,
                },
            )
            return Correction(reason)
    return None


def _check_task_scope(
    task: Task,
    changed: set[str],
    target_files: list[str],
) -> None:
    """SOFT warning when changes go beyond target_files (never rejects)."""
    for path in sorted(changed):
        if not any(PurePosixPath(path).match(pat) for pat in target_files):
            append_event(
                task,
                "task_scope_warning",
                {
                    "file": path,
                    "target_files": target_files,
                },
            )


def _check_secret_leak(
    task: Task,
    diff_content: str,
    secret_patterns: list[str],
) -> VerifyResult | None:
    """HARD reject if added lines in the diff contain any secret pattern."""
    # Only check newly added lines (start with '+' but not '+++' header)
    added_lines = "\n".join(
        line
        for line in diff_content.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    if not added_lines:
        return None
    for pattern in secret_patterns:
        if re.search(re.escape(pattern), added_lines, re.IGNORECASE):
            reason = f"Secret leak detected: pattern '{pattern}' found in added lines"
            append_event(task, "secret_leak", {"pattern": pattern})
            return Correction(reason)
    return None


def _check_untracked(
    task: Task,
    worktree: str,
) -> VerifyResult | None:
    """HARD reject if there are untracked files in the worktree."""
    untracked = git_untracked(worktree)
    if untracked:
        reason = f"Untracked files found: {untracked}"
        append_event(task, "untracked_files", {"files": untracked})
        return Correction(reason)
    return None


def _check_acceptance(
    task: Task,
    worktree: str,
    acceptance: str,
) -> VerifyResult | None:
    """HARD reject if the acceptance command exits non-zero."""
    if not acceptance:
        return None
    exit_code = run_in_worktree(worktree, acceptance)
    if exit_code != 0:
        reason = f"Acceptance test failed (exit {exit_code}): {acceptance}"
        append_event(
            task,
            "acceptance_failed",
            {
                "command": acceptance,
                "exit_code": exit_code,
            },
        )
        return Correction(reason)
    return None


# === Main entry point ===


def verify_step(task: Task, result: StepResult) -> VerifyResult:
    """Run all quality-gate checks for the current step.

    Checks are executed in order; the first HARD failure short-circuits.
    """
    subtask = _current_subtask(task)
    if subtask is None:
        return Correction(f"No subtask found for step {task.current_step}")

    worktree = task.worktree
    try:
        changed = git_diff_names(worktree)
        diff_content = git_diff(worktree)
    except RuntimeError as exc:
        return Correction(f"Git operation failed: {exc}")

    # (a) Security scope — HARD
    err = _check_security_scope(task, changed, subtask.writable_paths)
    if err is not None:
        return err

    # (b) Task scope — SOFT (log only)
    _check_task_scope(task, changed, subtask.target_files)

    # (c) Secret leak — HARD
    err = _check_secret_leak(
        task,
        diff_content,
        task.security_policy.secret_patterns,
    )
    if err is not None:
        return err

    # (d) Untracked files — HARD
    err = _check_untracked(task, worktree)
    if err is not None:
        return err

    # (e) Acceptance test — HARD
    err = _check_acceptance(task, worktree, subtask.acceptance)
    if err is not None:
        return err

    # All checks passed
    append_event(
        task,
        "review_passed",
        {
            "step": task.current_step,
            "attempt": task.current_attempt,
            "files_checked": sorted(changed),
        },
    )
    return Pass()
