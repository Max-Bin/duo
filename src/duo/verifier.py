"""Quality gate — verifies each step before the task advances.

Runs security scope, secret-leak, untracked-file, and acceptance-test
checks against the worktree.  Returns Pass or Correction.
"""

from __future__ import annotations

__all__ = [
    "Correction",
    "Pass",
    "git_diff",
    "git_diff_names",
    "git_untracked",
    "run_in_worktree",
    "verify_step",
]

import fnmatch
import logging
import os
import re
import shlex
import subprocess
import unicodedata
from dataclasses import dataclass

logger = logging.getLogger(__name__)

from duo.protocol import StepResult, Subtask, Task, append_event

_GIT_TIMEOUT = 30  # seconds for git diff/ls-files
_TEST_SUITE_TIMEOUT = 300  # seconds for acceptance test commands


def _match_writable(rel_path: str, pattern: str) -> bool:
    """Root-anchored path matching for writable_paths patterns.

    Uses fnmatch where ``*`` matches any character including ``/``,
    so ``src/*`` matches all files in the ``src/`` tree (including
    nested subdirectories).  Both path and pattern are NFC-normalized.
    """
    norm_path = unicodedata.normalize("NFC", rel_path)
    norm_pat = unicodedata.normalize("NFC", pattern)
    return fnmatch.fnmatch(norm_path, norm_pat)


def _validate_writable_patterns(patterns: list[str]) -> list[str]:
    """Filter and warn about invalid writable_paths patterns.

    Rejects empty strings, whitespace-only, and absolute paths which
    would never match repo-relative paths.
    """
    valid: list[str] = []
    for pat in patterns:
        if not pat or not pat.strip():
            logger.warning("Ignoring empty writable_paths pattern")
            continue
        if pat.startswith("/"):
            logger.warning(
                "Ignoring absolute writable_paths pattern %r — use repo-relative paths",
                pat,
            )
            continue
        valid.append(pat)
    return valid


# === Result types ===


@dataclass(frozen=True, slots=True)
class Pass:
    """All checks passed."""


@dataclass(frozen=True, slots=True)
class Correction:
    """One or more checks failed; includes the reason for the caller."""

    reason: str


VerifyResult = Pass | Correction


# === Git / subprocess helpers ===

_MAX_ERROR_CHARS = 500


def _run_git(cmd: list[str], worktree: str) -> subprocess.CompletedProcess[str]:
    """Run a git command in *worktree*, raising on non-zero exit.

    All subprocess exceptions (timeout, missing executable, OS errors) are
    normalized to ``RuntimeError`` so callers only need one except clause.
    """
    real = os.path.realpath(worktree)
    try:
        proc = subprocess.run(
            cmd,
            cwd=real,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"{' '.join(cmd)} timed out after {_GIT_TIMEOUT}s in {real}"
        ) from None
    except FileNotFoundError:
        raise RuntimeError(f"{cmd[0]!r} not found — is git installed?") from None
    except OSError as exc:
        raise RuntimeError(f"{' '.join(cmd)} OS error in {real}: {exc}") from None
    if proc.returncode != 0:
        raise RuntimeError(
            f"{' '.join(cmd)} failed in {real}: {proc.stderr.strip()[:_MAX_ERROR_CHARS]}"
        )
    return proc


def git_diff_names(worktree: str) -> set[str]:
    """Return the set of file paths changed relative to HEAD."""
    proc = _run_git(["git", "diff", "--name-only", "HEAD"], worktree)
    return {line for line in proc.stdout.strip().splitlines() if line}


_MAX_DIFF_BYTES = 10 * 1024 * 1024  # 10 MB — reject diffs larger than this


def git_diff(worktree: str) -> str:
    """Return the full unified diff relative to HEAD.

    Limits output to ``_MAX_DIFF_BYTES`` (measured in UTF-8 bytes, not
    characters) to prevent OOM from large binary files checked into the
    worktree.
    """
    proc = _run_git(["git", "diff", "HEAD"], worktree)
    byte_len = len(proc.stdout.encode("utf-8"))
    if byte_len > _MAX_DIFF_BYTES:
        raise RuntimeError(
            f"Diff too large ({byte_len} bytes > {_MAX_DIFF_BYTES} limit). "
            "Binary files or very large changes should be reviewed manually."
        )
    return proc.stdout


def git_untracked(worktree: str) -> list[str]:
    """Return untracked files not covered by .gitignore."""
    proc = _run_git(["git", "ls-files", "--others", "--exclude-standard"], worktree)
    return [line for line in proc.stdout.strip().splitlines() if line]


def run_in_worktree(worktree: str, command: str) -> int:
    """Run a command inside *worktree* and return its exit code.

    Returns 127 if the command string cannot be parsed (e.g. unbalanced quotes)
    or the executable is not found.
    """
    worktree = os.path.realpath(worktree)
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        logger.warning("Failed to parse command %r: %s", command, exc)
        return 127
    try:
        proc = subprocess.run(
            argv, shell=False, cwd=worktree, timeout=_TEST_SUITE_TIMEOUT
        )
    except FileNotFoundError:
        return 127
    except subprocess.TimeoutExpired:
        logger.warning(
            "Acceptance test timed out after %ds: %s", _TEST_SUITE_TIMEOUT, command
        )
        return 124  # standard timeout exit code
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
    worktree: str,
) -> VerifyResult | None:
    """HARD reject if any changed file falls outside writable_paths.

    Defense-in-depth:
    1. Validate and filter writable_paths patterns
    2. Reject paths containing null bytes
    3. Normalize paths and reject ``../`` traversals
    4. Resolve symlinks and reject files that escape the worktree
    5. Reject hardlinked files (could alias files outside worktree)
    6. Match the *resolved* relative path against writable_paths
    """
    valid_paths = _validate_writable_patterns(writable_paths)
    real_worktree = os.path.realpath(worktree)
    for path in sorted(changed):
        # Null byte injection defense
        if "\x00" in path:
            reason = f"Security violation: null byte in path {path!r}"
            append_event(
                task, "security_violation", {"file": path, "reason": "null_byte"}
            )
            return Correction(reason)

        # Normalize and reject traversals
        normalized = os.path.normpath(path)
        if normalized.startswith("..") or os.path.isabs(normalized):
            reason = f"Security violation: path traversal in '{path}'"
            append_event(
                task, "security_violation", {"file": path, "reason": "path_traversal"}
            )
            return Correction(reason)

        # Resolve symlinks and verify containment within worktree
        full_path = os.path.join(real_worktree, normalized)
        real_path = os.path.realpath(full_path)
        if (
            not real_path.startswith(real_worktree + os.sep)
            and real_path != real_worktree
        ):
            reason = (
                f"Security violation: '{path}' resolves to '{real_path}' "
                f"which is outside worktree '{real_worktree}'"
            )
            append_event(
                task,
                "security_violation",
                {"file": path, "resolved": real_path, "reason": "symlink_escape"},
            )
            return Correction(reason)

        # Hardlink detection: a file with nlink > 1 could alias a file
        # outside the worktree on the same filesystem
        try:
            st = os.lstat(real_path)
            if st.st_nlink > 1:
                reason = (
                    f"Security violation: '{path}' is a hardlink "
                    f"(nlink={st.st_nlink}) — could alias external file"
                )
                append_event(
                    task,
                    "security_violation",
                    {"file": path, "nlink": st.st_nlink, "reason": "hardlink"},
                )
                return Correction(reason)
        except OSError:
            pass  # File may not exist on disk (staged deletion)

        # Match resolved relative path against writable_paths (root-anchored)
        rel_real = os.path.relpath(real_path, real_worktree)
        if not any(_match_writable(rel_real, pat) for pat in valid_paths):
            reason = (
                f"Security violation: '{path}' is outside writable paths {valid_paths}"
            )
            append_event(
                task,
                "security_violation",
                {
                    "file": path,
                    "writable_paths": valid_paths,
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
        if not any(_match_writable(path, pat) for pat in target_files):
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
    """HARD reject if added lines in the diff contain any secret pattern.

    Patterns are treated as literal substrings (not regex).
    Matching is case-insensitive.

    For patterns ending with ``=``, whitespace around the ``=`` is also
    matched (e.g., ``API_KEY=`` also catches ``API_KEY = "value"``).
    """
    if not secret_patterns:
        logger.warning(
            "No secret patterns configured for task %s — secret detection disabled",
            task.id,
        )
    # Only check newly added lines (start with '+' but not '+++' header)
    added_lines = "\n".join(
        line
        for line in diff_content.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    if not added_lines:
        return None
    for pattern in secret_patterns:
        # For key=value patterns, tolerate whitespace around '='
        if pattern.endswith("="):
            key = re.escape(pattern[:-1])
            regex = key + r"\s*="
        else:
            regex = re.escape(pattern)
        if re.search(regex, added_lines, re.IGNORECASE):
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
    # Enforce BOTH task-level and subtask-level writable_paths (intersection)
    policy_paths = task.security_policy.writable_paths
    subtask_paths = subtask.writable_paths
    if policy_paths:
        # Task policy is the outer boundary; subtask narrows further
        err = _check_security_scope(task, changed, policy_paths, worktree)
        if err is not None:
            return err
    err = _check_security_scope(task, changed, subtask_paths, worktree)
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
    try:
        err = _check_untracked(task, worktree)
    except RuntimeError as exc:
        return Correction(f"Git operation failed: {exc}")
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
