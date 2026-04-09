"""File protocol and FSM — the core state system.

State truth lives in task directories and append-only journals.
Sessions are disposable executors; this module is the durable state.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

__all__ = [
    "DUO_DIR",
    "TRANSITIONS",
    "SecurityPolicy",
    "StepResult",
    "Subtask",
    "Task",
    "TaskStatus",
    "append_event",
    "atomic_write_text",
    "create_task",
    "list_corrupted",
    "list_tasks",
    "load_task",
    "new_incarnation",
    "now_iso",
    "prompt_hash",
    "quarantine_task",
    "read_ack_for_step",
    "read_heartbeat",
    "read_json",
    "read_jsonl",
    "read_result_for_step",
    "replay_state",
    "save_task",
    "transition",
    "write_json",
]

logger = logging.getLogger(__name__)

# === Constants ===

DUO_DIR = Path(os.path.expanduser("~/.duo"))
TASKS_DIR = DUO_DIR / "tasks"
_CORRUPTED_DIR = TASKS_DIR / "_corrupted"


# === FSM State Enum ===


class TaskStatus(StrEnum):
    """FSM states for a task's lifecycle."""

    CREATED = "created"
    QUEUED = "queued"
    SESSION_STARTING = "session_starting"
    PROMPT_SENT = "prompt_sent"
    ACKED = "acked"
    RUNNING = "running"
    RESULT_REPORTED = "result_reported"
    VERIFYING = "verifying"
    CORRECTING = "correcting"
    BLOCKED = "blocked"
    FAILED = "failed"
    COMPLETED = "completed"
    ESCALATED = "escalated"


# Legal state transitions
TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.CREATED: {TaskStatus.SESSION_STARTING, TaskStatus.QUEUED},
    TaskStatus.QUEUED: {TaskStatus.SESSION_STARTING, TaskStatus.FAILED},
    TaskStatus.SESSION_STARTING: {TaskStatus.PROMPT_SENT, TaskStatus.FAILED},
    TaskStatus.PROMPT_SENT: {
        TaskStatus.ACKED,
        TaskStatus.PROMPT_SENT,
        TaskStatus.FAILED,
        TaskStatus.VERIFYING,
        TaskStatus.RUNNING,
        TaskStatus.BLOCKED,
    },
    TaskStatus.ACKED: {
        TaskStatus.RUNNING,
        TaskStatus.RESULT_REPORTED,
        TaskStatus.FAILED,
    },
    TaskStatus.RUNNING: {
        TaskStatus.RESULT_REPORTED,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
    },
    TaskStatus.RESULT_REPORTED: {TaskStatus.VERIFYING, TaskStatus.FAILED},
    TaskStatus.VERIFYING: {
        TaskStatus.PROMPT_SENT,
        TaskStatus.CORRECTING,
        TaskStatus.COMPLETED,
        TaskStatus.ESCALATED,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
    },
    TaskStatus.CORRECTING: {
        TaskStatus.ACKED,
        TaskStatus.ESCALATED,
        TaskStatus.PROMPT_SENT,
        TaskStatus.FAILED,
    },
    TaskStatus.BLOCKED: {
        TaskStatus.SESSION_STARTING,
        TaskStatus.PROMPT_SENT,
        TaskStatus.ESCALATED,
        TaskStatus.FAILED,
    },
    TaskStatus.ESCALATED: {TaskStatus.PROMPT_SENT, TaskStatus.FAILED},
    TaskStatus.FAILED: {TaskStatus.SESSION_STARTING},
    TaskStatus.COMPLETED: set(),
}
# FSM topology:
#   Entry:      CREATED → QUEUED (capacity wait) or SESSION_STARTING (immediate)
#   Happy path: SESSION_STARTING → PROMPT_SENT → ACKED → RUNNING
#               → RESULT_REPORTED → VERIFYING → COMPLETED
#   Correction: VERIFYING → CORRECTING → PROMPT_SENT (loop)
#   Recovery:   FAILED → SESSION_STARTING (restart)
#   Terminal:   COMPLETED (no outgoing transitions)


# === Data models ===


@dataclass
class Subtask:
    """A single step within a task, with its scope and acceptance criteria."""

    step_id: int
    description: str
    target_files: list[str]
    writable_paths: list[str]
    acceptance: str = ""
    forbidden_commands: list[str] = field(default_factory=list)


@dataclass
class SecurityPolicy:
    """Security constraints enforced during verification."""

    writable_paths: list[str] = field(default_factory=list)
    secret_patterns: list[str] = field(
        default_factory=lambda: [
            "API_KEY=",
            "api_key=",
            "apikey=",
            "PASSWORD=",
            "password=",
            "TOKEN=",
            "token=",
            "SECRET=",
            "secret=",
            "PRIVATE_KEY",
            "private_key",
            "Authorization: Bearer",
        ]
    )
    forbidden_commands: list[str] = field(default_factory=list)
    allow_network: bool = False
    require_human_approval: list[str] = field(
        default_factory=lambda: ["delete_file", "modify_config", "change_dependency"]
    )


@dataclass
class Task:
    """Core task model holding FSM state, subtasks, and file-protocol paths."""

    id: str
    description: str
    worktree: str
    branch: str
    base_commit: str
    pane_label: str
    incarnation_id: str
    status: TaskStatus
    current_step: int
    current_attempt: int
    subtasks: list[Subtask]
    created_at: str
    security_policy: SecurityPolicy = field(default_factory=SecurityPolicy)
    last_prompt_sent_at: str | None = None

    @property
    def dir(self) -> Path:
        """Return the task's root directory under ~/.duo/tasks/."""
        return TASKS_DIR / self.id

    @property
    def journal_path(self) -> Path:
        """Return the path to this task's append-only event journal."""
        return self.dir / "journal.jsonl"

    @property
    def heartbeat_path(self) -> Path:
        """Return the path to this task's heartbeat file."""
        return self.dir / "heartbeat.json"

    def step_dir(self, step: int) -> Path:
        """Return the directory for a given step number."""
        return self.dir / "steps" / f"step-{step:04d}"

    def ack_path(self, step: int, attempt: int) -> Path:
        """Return the path to the ack file for a step and attempt."""
        return self.step_dir(step) / f"ack-attempt-{attempt:02d}.json"

    def result_path(self, step: int, attempt: int) -> Path:
        """Return the path to the result file for a step and attempt."""
        return self.step_dir(step) / f"result-attempt-{attempt:02d}.json"

    def prompt_path(self, step: int, attempt: int) -> Path:
        """Return the path to the saved prompt file for a step and attempt."""
        return self.step_dir(step) / f"prompt-attempt-{attempt:02d}.txt"


# === Helper functions ===


def now_iso() -> str:
    """Return current UTC time as ISO 8601 timestamp."""
    return datetime.now(UTC).isoformat()


def new_incarnation() -> str:
    """Generate a unique 8-character hex session incarnation ID."""
    return uuid.uuid4().hex[:8]


def prompt_hash(prompt: str) -> str:
    """Return first 8 characters of SHA-256 hash of prompt text."""
    return hashlib.sha256(prompt.encode()).hexdigest()[:8]


# === File I/O ===


def read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON file, return None if missing or invalid."""
    try:
        data: dict[str, Any] = json.loads(path.read_text())
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return None


_MAX_JSON_BYTES = 10 * 1024 * 1024  # 10 MB safety limit


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically (write tmp, fsync, then rename)."""
    if ".." in path.parts:
        raise ValueError(f"Path traversal detected: {path}")
    if path.is_symlink():
        raise ValueError(f"Refusing to write through symlink: {path}")

    # Serialize first to validate encoding and check size
    try:
        json_str = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    except (TypeError, ValueError) as e:
        raise ValueError(f"Data not JSON-serializable: {e}") from e
    if len(json_str.encode()) > _MAX_JSON_BYTES:
        raise ValueError(
            f"JSON payload too large ({len(json_str.encode())} bytes, "
            f"max {_MAX_JSON_BYTES})"
        )

    atomic_write_text(path, json_str)


def atomic_write_text(path: Path, content: str) -> None:
    """Write text atomically: tmp file → fsync → rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        tmp.rename(path)
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def read_jsonl(path: Path, *, tail: int | None = None) -> list[dict[str, Any]]:
    """Read a JSONL file, return list of events.

    Uses streaming I/O to avoid loading the entire file into memory.
    If *tail* is set, return only the last N valid events using a bounded
    deque so memory stays O(tail) even for huge journals.
    """
    if not path.exists():
        return []
    result: deque[dict[str, Any]] | list[dict[str, Any]]
    if tail is not None:
        result = deque(maxlen=tail)
    else:
        result = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            stripped = raw.strip()
            if stripped:
                try:
                    result.append(json.loads(stripped))
                except json.JSONDecodeError:
                    continue
    return list(result)


# === Event Journal ===

MAX_JOURNAL_BYTES = 10 * 1024 * 1024  # 10 MB


def append_event(task: Task, event: str, data: dict[str, Any] | None = None) -> None:
    """Append an event to the task's journal."""
    task.journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal = task.journal_path
    try:
        if journal.exists() and journal.stat().st_size > MAX_JOURNAL_BYTES:
            # Rotate: keep last half
            lines = journal.read_text(encoding="utf-8").splitlines()
            half = len(lines) // 2
            atomic_write_text(journal, "\n".join(lines[half:]) + "\n")
            logger.info(
                "Rotated journal for task %r (%d entries removed)", task.id, half
            )
    except OSError:
        logger.warning("Journal rotation failed for task %r — skipping", task.id)
    entry = {"ts": now_iso(), "event": event, "data": data or {}}
    with open(task.journal_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


# === State transitions ===


def transition(task: Task, new_status: TaskStatus) -> None:
    """Transition task to new status. Logs invalid transitions but doesn't crash."""
    old = task.status
    if new_status not in TRANSITIONS.get(old, set()):
        append_event(
            task,
            "invalid_transition",
            {
                "from": old.value,
                "to": new_status.value,
                "incarnation": task.incarnation_id,
            },
        )
        return
    task.status = new_status
    append_event(
        task,
        "status_changed",
        {
            "from": old.value,
            "to": new_status.value,
            "incarnation": task.incarnation_id,
        },
    )
    save_task(task)


# === Task CRUD ===


def create_task(
    task_id: str,
    description: str,
    worktree: str,
    branch: str,
    base_commit: str,
    subtasks: list[Subtask],
) -> Task:
    """Create a new task with initial state."""
    if not subtasks:
        raise ValueError("Task must have at least one subtask")
    task = Task(
        id=task_id,
        description=description,
        worktree=worktree,
        branch=branch,
        base_commit=base_commit,
        pane_label=task_id,
        incarnation_id=new_incarnation(),
        status=TaskStatus.CREATED,
        current_step=1,
        current_attempt=1,
        subtasks=subtasks,
        created_at=now_iso(),
    )

    # Create directory structure
    task.dir.mkdir(parents=True, exist_ok=True)
    for st in subtasks:
        task.step_dir(st.step_id).mkdir(parents=True, exist_ok=True)

    save_task(task)
    append_event(
        task,
        "task_created",
        {
            "id": task_id,
            "incarnation": task.incarnation_id,
        },
    )
    return task


def save_task(task: Task) -> None:
    """Persist task.json."""
    data = {
        "id": task.id,
        "description": task.description,
        "worktree": task.worktree,
        "branch": task.branch,
        "base_commit": task.base_commit,
        "pane_label": task.pane_label,
        "incarnation_id": task.incarnation_id,
        "status": task.status.value,
        "current_step": task.current_step,
        "current_attempt": task.current_attempt,
        "created_at": task.created_at,
        "last_prompt_sent_at": task.last_prompt_sent_at,
        "subtasks": [
            {
                "step_id": s.step_id,
                "description": s.description,
                "target_files": s.target_files,
                "writable_paths": s.writable_paths,
                "acceptance": s.acceptance,
                "forbidden_commands": s.forbidden_commands,
            }
            for s in task.subtasks
        ],
        "security_policy": {
            "writable_paths": task.security_policy.writable_paths,
            "secret_patterns": task.security_policy.secret_patterns,
            "forbidden_commands": task.security_policy.forbidden_commands,
            "allow_network": task.security_policy.allow_network,
            "require_human_approval": task.security_policy.require_human_approval,
        },
    }
    write_json(task.dir / "task.json", data)


def load_task(task_id: str) -> Task | None:
    """Load a task from its directory."""
    data = read_json(TASKS_DIR / task_id / "task.json")
    if data is None:
        return None

    if not isinstance(data.get("subtasks"), list):
        logger.warning("Task '%s' has invalid subtasks field", task_id)
        return None
    if not isinstance(data.get("current_step"), int):
        logger.warning("Task '%s' has invalid current_step field", task_id)
        return None

    n_subtasks = len(data["subtasks"])
    cs = data["current_step"]
    if n_subtasks > 0 and cs < 1:
        logger.warning(
            "Task '%s' current_step %d out of range (must be >= 1)",
            task_id,
            cs,
        )
        return None

    subtasks = [
        Subtask(
            step_id=s["step_id"],
            description=s["description"],
            target_files=s["target_files"],
            writable_paths=s["writable_paths"],
            acceptance=s.get("acceptance", ""),
            forbidden_commands=s.get("forbidden_commands", []),
        )
        for s in data.get("subtasks", [])
    ]

    sp = data.get("security_policy", {})
    security_policy = SecurityPolicy(
        writable_paths=sp.get("writable_paths", []),
        secret_patterns=sp.get("secret_patterns", []),
        forbidden_commands=sp.get("forbidden_commands", []),
        allow_network=sp.get("allow_network", False),
        require_human_approval=sp.get("require_human_approval", []),
    )

    try:
        return Task(
            id=data["id"],
            description=data["description"],
            worktree=data["worktree"],
            branch=data["branch"],
            base_commit=data["base_commit"],
            pane_label=data["pane_label"],
            incarnation_id=data["incarnation_id"],
            status=TaskStatus(data["status"]),
            current_step=data["current_step"],
            current_attempt=data["current_attempt"],
            subtasks=subtasks,
            created_at=data["created_at"],
            security_policy=security_policy,
            last_prompt_sent_at=data.get("last_prompt_sent_at"),
        )
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("Failed to load task '%s': %s", task_id, exc)
        return None


_task_cache: dict[str, tuple[float, Task]] = {}


def _clear_task_cache() -> None:
    """Clear the internal task-list mtime cache (useful in tests)."""
    _task_cache.clear()


def list_tasks() -> list[Task]:
    """List all tasks, using mtime-based caching to skip re-parsing unchanged files.

    Corrupted tasks (those that fail to load) are auto-quarantined to
    ``_corrupted/`` on first encounter — no repeated warnings.
    """
    if not TASKS_DIR.exists():
        return []
    tasks: list[Task] = []
    seen: set[str] = set()
    for d in sorted(TASKS_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        task_json = d / "task.json"
        try:
            mtime = task_json.stat().st_mtime
        except OSError:
            continue
        seen.add(d.name)
        cached = _task_cache.get(d.name)
        if cached is not None and cached[0] == mtime:
            tasks.append(cached[1])
        else:
            t = load_task(d.name)
            if t is not None:
                _task_cache[d.name] = (mtime, t)
                tasks.append(t)
            else:
                quarantine_task(d.name, "failed to load")
    # Evict entries for deleted tasks
    for stale in set(_task_cache) - seen:
        del _task_cache[stale]
    return tasks


def quarantine_task(task_id: str, reason: str = "") -> Path | None:
    """Move a corrupted task directory to _corrupted/ quarantine.

    Returns the quarantine path, or None if the task dir doesn't exist.
    """
    src = TASKS_DIR / task_id
    if not src.is_dir():
        return None
    _CORRUPTED_DIR.mkdir(parents=True, exist_ok=True)
    ts = now_iso().replace(":", "-")
    dst = _CORRUPTED_DIR / f"{task_id}-{ts}"
    shutil.move(str(src), str(dst))
    logger.info(
        "Quarantined corrupted task '%s' → %s (reason: %s)", task_id, dst, reason
    )
    _task_cache.pop(task_id, None)
    return dst


def list_corrupted() -> list[Path]:
    """List quarantined (corrupted) task directories."""
    if not _CORRUPTED_DIR.exists():
        return []
    return sorted(d for d in _CORRUPTED_DIR.iterdir() if d.is_dir())


# === File protocol readers (step/attempt-aware) ===


@dataclass
class Heartbeat:
    """Parsed heartbeat.json written by the executor session."""

    ts: str
    incarnation: str
    step: int
    status: str
    current_file: str


@dataclass
class AckResult:
    """Parsed acknowledgement written by the executor for a step attempt."""

    step: int
    attempt: int
    incarnation: str
    prompt_hash: str
    acked_at: str


@dataclass
class StepResult:
    """Parsed result written by the executor after completing a step attempt."""

    step: int
    attempt: int
    incarnation: str
    status: str
    files_changed: list[str] = field(default_factory=list)
    summary: str = ""
    reason: str = ""


def read_heartbeat(task: Task) -> Heartbeat | None:
    """Read heartbeat.json."""
    data = read_json(task.heartbeat_path)
    if data is None:
        return None
    return Heartbeat(
        ts=data.get("ts", ""),
        incarnation=data.get("incarnation", ""),
        step=data.get("step", 0),
        status=data.get("status", ""),
        current_file=data.get("current_file", ""),
    )


def read_ack_for_step(task: Task, step: int, attempt: int) -> AckResult | None:
    """Read ack for a specific step+attempt."""
    data = read_json(task.ack_path(step, attempt))
    if data is None:
        return None
    return AckResult(
        step=data.get("step", 0),
        attempt=data.get("attempt", 0),
        incarnation=data.get("incarnation", ""),
        prompt_hash=data.get("prompt_hash", ""),
        acked_at=data.get("acked_at", ""),
    )


def read_result_for_step(task: Task, step: int, attempt: int) -> StepResult | None:
    """Read result for a specific step+attempt."""
    data = read_json(task.result_path(step, attempt))
    if data is None:
        return None
    return StepResult(
        step=data.get("step", 0),
        attempt=data.get("attempt", 0),
        incarnation=data.get("incarnation", ""),
        status=data.get("status", ""),
        files_changed=data.get("files_changed", []),
        summary=data.get("summary", ""),
        reason=data.get("reason", ""),
    )


# === Journal replay ===


def replay_state(task: Task) -> TaskStatus:
    """Replay journal to determine current FSM state.

    Reads the last few events to reconstruct where we are.
    """
    events = read_jsonl(task.journal_path)
    if not events:
        return TaskStatus.CREATED

    status = TaskStatus.CREATED
    for ev in events:
        event_type = ev.get("event", "")
        if event_type == "status_changed":
            try:
                status = TaskStatus(ev["data"]["to"])
            except (KeyError, ValueError):
                pass

    return status
