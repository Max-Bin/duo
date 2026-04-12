"""Lifecycle commands — ``duo start``, ``duo init``, ``duo go``."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import click

import duo.protocol as _protocol
from duo.cli._helpers import _create_worktree, _validate_task_name
from duo.errors import DuoUserError
from duo.protocol import Subtask, TaskStatus, atomic_write_text, create_task, load_task


@click.command()
@click.argument("name")
@click.option("--repo", default=".", help="Git repo path to create worktree from")
@click.option("--desc", default="", help="Task description")
@click.option("--model", default=None, help="Override copilot model for this task")
@click.option(
    "--queue", "start_queued", is_flag=True, help="Create task in queued state"
)
@click.option(
    "--from-thinking",
    "from_thinking",
    is_flag=True,
    help="Use plan.md from a thinking session as the task description",
)
@click.option(
    "--immediate",
    is_flag=True,
    help="Send bootstrap prompt immediately (default: defer until 'duo send')",
)
@click.option(
    "--reuse-pane",
    "reuse_pane",
    default="",
    help="Reuse existing tmux pane ID instead of creating new one",
)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option(
    "-q", "--quiet", is_flag=True, help="Print only the task name for scripting"
)
def start(
    name: str,
    repo: str,
    desc: str,
    model: str | None,
    start_queued: bool,
    from_thinking: bool,
    immediate: bool,
    reuse_pane: str,
    *,
    as_json: bool = False,
    quiet: bool = False,
) -> None:
    """Create a task with worktree + Copilot session."""
    from duo.commander import start_session

    _validate_task_name(name)

    # Check for duplicate task early (before expensive repo validation)
    existing = load_task(name)
    if existing is not None:
        raise DuoUserError(
            f"task '{name}' already exists (status: {existing.status.value}, worktree: {existing.worktree})",
            fix=f"Use 'duo kill {name}' first, or choose a different task name.",
        )

    repo = os.path.abspath(repo)

    if from_thinking:
        from duo.thinking import thinking_dir

        plan_path = thinking_dir(name) / "plan.md"
        if not plan_path.exists():
            raise DuoUserError(
                f"No plan.md found for thinking session '{name}'",
                fix=f"Run 'duo think {name} --finalize' first.",
            )
        plan_content = plan_path.read_text(encoding="utf-8").strip()
        if not plan_content:
            raise DuoUserError(
                f"plan.md for '{name}' is empty",
                fix=f"Run 'duo think {name} --finalize' again.",
            )
        desc = plan_content

    if model:
        os.environ["DUO_COPILOT_MODEL"] = model

    # Acquire lockfile to prevent concurrent duplicate creation (TOCTOU)
    lock_path = _protocol.TASKS_DIR / f".{name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd: Any = None
    try:
        lock_fd = open(lock_path, "w", encoding="utf-8")  # noqa: SIM115 — kept open for flock
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        if lock_fd is not None:
            lock_fd.close()
        raise DuoUserError(
            f"task '{name}' is being created by another process",
            fix="Wait and retry, or run 'duo cleanup' if stuck.",
        ) from None

    try:
        # Re-check after acquiring lock
        existing = load_task(name)
        if existing is not None:
            raise DuoUserError(
                f"task '{name}' already exists (status: {existing.status.value}, worktree: {existing.worktree})",
                fix=f"Use 'duo kill {name}' first, or choose a different task name.",
            )

        worktree, base_commit = _create_worktree(name, repo)
        branch = f"duo/{name}"

        # Create task with a placeholder subtask (user will send actual tasks)
        task = create_task(
            task_id=name,
            description=desc or f"Task {name}",
            worktree=worktree,
            branch=branch,
            base_commit=base_commit,
            subtasks=[
                Subtask(
                    step_id=1,
                    description=desc or "Awaiting instructions",
                    target_files=[],
                    writable_paths=["*"],  # permissive by default
                )
            ],
        )
    finally:
        lock_fd.close()
        lock_path.unlink(missing_ok=True)

    if not as_json and not quiet:
        click.echo(f"Created task: {name}")
        click.echo(f"  Worktree: {worktree}")
        click.echo(f"  Branch: {branch}")
        click.echo(f"  Incarnation: {task.incarnation_id}")
        if from_thinking:
            click.echo("  Plan: loaded from thinking session")

    if start_queued:
        from duo.protocol import transition

        if not transition(task, TaskStatus.QUEUED):
            if quiet:
                click.echo(name)
                return
            if as_json:
                click.echo(
                    json.dumps(
                        {
                            "created": True,
                            "task": name,
                            "status": "error",
                            "error": "transition to QUEUED failed",
                        }
                    )
                )
            else:
                click.echo(
                    f"Warning: task '{name}' created but could not transition to QUEUED.",
                    err=True,
                )
            return
        if quiet:
            click.echo(name)
            return
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "created": True,
                        "task": name,
                        "worktree": worktree,
                        "branch": branch,
                        "incarnation": task.incarnation_id,
                        "status": "queued",
                    }
                )
            )
        else:
            click.echo(f"Task '{name}' queued.")
        return

    # Check if we should queue or start
    from duo.scheduler import enqueue_or_start, queue_status

    action = enqueue_or_start(task)

    if action == "queued":
        qs = queue_status()
        if quiet:
            click.echo(name)
            return
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "created": True,
                        "task": name,
                        "worktree": worktree,
                        "branch": branch,
                        "incarnation": task.incarnation_id,
                        "status": "queued",
                        "queue_position": qs["queued_count"],
                    }
                )
            )
        else:
            click.echo(
                f"  Queued ({qs['queued_count']} in queue). {qs['active_count']}/{qs['max_parallel']} slots in use."
            )
            click.echo("  Task will start automatically when a slot opens.")
            click.echo("  Run 'duo monitor' to manage the queue.")
        return

    # Start Copilot session
    defer = not immediate
    if not as_json and not quiet:
        click.echo("Starting Copilot session...")
    start_session(task, defer=defer, reuse_pane=reuse_pane)
    if quiet:
        click.echo(name)
        return
    if as_json:
        click.echo(
            json.dumps(
                {
                    "created": True,
                    "task": name,
                    "worktree": worktree,
                    "branch": branch,
                    "incarnation": task.incarnation_id,
                    "pane_label": task.pane_label,
                    "status": "deferred" if defer else "started",
                }
            )
        )
    else:
        if defer:
            click.echo(f"Session ready (deferred). Pane: {task.pane_label}")
            click.echo("  Copilot is idle — no PR consumed yet.")
            click.echo(f"  Send first prompt: duo send {name} 'your instruction'")
        else:
            click.echo(f"Session started. Pane label: {task.pane_label}")


@click.command()
@click.option("--repo", default=".", help="Git repository path to initialize")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option(
    "-q", "--quiet", is_flag=True, help="Print only the number of items created"
)
def init(repo: str, *, as_json: bool = False, quiet: bool = False) -> None:
    """Initialize a project for Duo (creates .duo config and instructions)."""
    from duo.config import load_config, save_config

    repo_path = Path(repo).resolve()
    project_duo = repo_path / ".duo"

    # Already initialized?
    if project_duo.exists():
        if quiet:
            click.echo("0")
            return
        if as_json:
            click.echo(json.dumps({"status": "already_initialized", "created": []}))
        else:
            click.echo("Already initialized.")
        return

    # Must be a git repo
    if not (repo_path / ".git").exists():
        raise DuoUserError("not a git repository", fix="Run 'git init' first.")

    created: list[str] = []

    # 1. ~/.duo/
    _protocol.DUO_DIR.mkdir(parents=True, exist_ok=True)
    created.append(str(_protocol.DUO_DIR))

    # 2. ~/.duo/config.json (only if missing)
    config_path = _protocol.DUO_DIR / "config.json"
    if not config_path.exists():
        save_config(load_config())
        created.append(str(config_path))

    # 3. ~/.duo/tasks/
    _protocol.TASKS_DIR.mkdir(parents=True, exist_ok=True)
    created.append(str(_protocol.TASKS_DIR))

    # 3b. Worktree base directory (persistent, not /tmp)
    from duo.config import get_config as _gc

    worktree_base = Path(_gc("worktree_base_path"))
    worktree_base.mkdir(parents=True, exist_ok=True)
    created.append(str(worktree_base))

    # 4. .duo/ inside repo
    project_duo.mkdir(parents=True, exist_ok=True)
    created.append(str(project_duo))

    # 5. .duo/instructions.md
    instructions = project_duo / "instructions.md"
    atomic_write_text(
        instructions,
        "# Duo Project Instructions\n"
        "\n"
        "## Project Overview\n"
        "<!-- Describe your project here -->\n"
        "\n"
        "## Coding Conventions\n"
        "<!-- List your coding standards -->\n"
        "\n"
        "## Testing\n"
        "<!-- How to run tests -->\n"
        "\n"
        "## Important Notes\n"
        "<!-- Anything the executor should know -->\n",
    )
    created.append(str(instructions))

    # 6. Add .duo/ to .gitignore
    gitignore = repo_path / ".gitignore"
    needs_entry = True
    content = ""
    if gitignore.exists():
        content = gitignore.read_text()
        for line in content.splitlines():
            stripped = line.strip()
            if stripped in (".duo/", ".duo"):
                needs_entry = False
                break
    if needs_entry:
        with open(gitignore, "a", encoding="utf-8") as f:
            if content and not content.endswith("\n"):
                f.write("\n")
            f.write(".duo/\n")
            f.flush()
            os.fsync(f.fileno())
        created.append(str(gitignore) + " (updated)")

    if quiet:
        click.echo(str(len(created)))
        return
    if as_json:
        click.echo(json.dumps({"status": "initialized", "created": created}))
    else:
        click.echo("Initialized Duo project:")
        for item in created:
            click.echo(f"  ✓ {item}")


@click.command()
@click.option("--repo", default=".", help="Repository path (default: current dir)")
def go(repo: str) -> None:
    """One-command setup: CEO (Claude Code) + Executor (Copilot) side by side.

    Sets up everything needed to start working with Duo:
    1. Checks tmux is running
    2. Initializes git + duo if needed
    3. Writes project CLAUDE.md with CEO operating manual
    4. Splits a Copilot standby pane (0 PR cost)
    5. Execs Claude Code in the current pane

    After `duo go`, just chat with Claude Code about what you want to build.
    """
    import shlex

    from duo.commander import write_project_claude_md
    from duo.config import get_config
    from duo.protocol import (
        load_go_session,
        save_go_session,
    )
    from duo.transport import (
        is_at_main_prompt,
        is_pane_alive,
        kill_pane,
        name_pane,
        read_pane,
        send_shell_command,
        send_slash_command,
        split_window_horizontal,
        wait_for_idle,
    )

    repo_path = Path(repo).resolve()

    # 1. Check tmux
    if not os.environ.get("TMUX"):
        raise DuoUserError(
            "not inside a tmux session",
            fix="Start tmux first: tmux new -s work",
        )

    # 2. Check/init git
    if not (repo_path / ".git").exists():
        click.echo("No git repo found. Initializing...")
        result = subprocess.run(
            ["git", "init", str(repo_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise DuoUserError(
                f"git init failed: {result.stderr.strip()}",
                fix="Initialize a git repository manually.",
            )
        click.echo(f"  ✓ git init {repo_path}")

    # 3. duo init (idempotent)
    duo_project = repo_path / ".duo"
    if not duo_project.exists():
        from click.testing import CliRunner as _InternalRunner

        from duo.cli import main as _main

        _InternalRunner().invoke(_main, ["init", "--repo", str(repo_path)])
        click.echo("  ✓ duo init")
    else:
        # Ensure global directories exist even if .duo/ already exists
        _protocol.DUO_DIR.mkdir(parents=True, exist_ok=True)
        _protocol.TASKS_DIR.mkdir(parents=True, exist_ok=True)

    # 4. Write project CLAUDE.md
    write_project_claude_md(str(repo_path))
    click.echo("  ✓ project CLAUDE.md")

    # 5. Split pane for standby Copilot (or reuse existing)
    standby_label = "duo-copilot-standby"
    pane_id = ""

    # Check for existing go-session — only reuse if SAME repo AND same tmux session
    existing = load_go_session()
    current_tmux = os.environ.get("TMUX", "")
    if (
        existing
        and existing.get("copilot_pane")
        and existing.get("repo_root") == str(repo_path)
        and existing.get("tmux_env", "") == current_tmux
    ):
        # Verify pane is alive via transport layer
        if is_pane_alive(existing["copilot_pane"]):
            pane_id = existing["copilot_pane"]
            click.echo(f"  ✓ reusing standby pane {pane_id}")

    if not pane_id:
        # Create new pane via transport layer
        try:
            pane_id = split_window_horizontal()
        except RuntimeError as exc:
            raise DuoUserError(
                str(exc),
                fix="Check tmux is responding: tmux list-panes",
            ) from None

        # Name the pane (split_window_horizontal diff is reliable)
        try:
            name_pane(pane_id, standby_label)
        except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
            kill_pane(pane_id)
            raise DuoUserError(
                f"failed to name standby pane: {exc}",
                fix="Retry duo go, or check tmux panes.",
            ) from None

        # Start Copilot in the standby pane via transport layer
        copilot_model = get_config("copilot_model")
        copilot_cmd = f"copilot --model {shlex.quote(str(copilot_model))}"
        if get_config("bypass_permissions"):
            copilot_cmd += " --yolo"

        time.sleep(1.5)  # Wait for shell to fully start in new pane
        try:
            send_shell_command(standby_label, f"cd {shlex.quote(str(repo_path))}")
            time.sleep(0.5)
            send_shell_command(standby_label, copilot_cmd)
        except (RuntimeError, OSError) as exc:
            click.echo(f"  ⚠ Failed to start Copilot in pane: {exc}")
            click.echo("    Right pane may need manual: cd <project> && copilot")
            # Don't abort — Claude Code can still work without Copilot

        click.echo("  Waiting for Copilot to start...")
        if wait_for_idle(standby_label, timeout=45, poll_interval=2.0):
            pane_content = read_pane(standby_label)
            if is_at_main_prompt(pane_content):
                # Send /allow-all (slash command at ❯ prompt)
                if get_config("auto_allow_all"):
                    send_slash_command(standby_label, "/allow-all")
                    wait_for_idle(standby_label, timeout=15, poll_interval=1.0)
                click.echo(f"  ✓ Copilot standby ready ({pane_id})")
            else:
                click.echo("  ⚠ Copilot pane not at prompt (continuing anyway)")
        else:
            click.echo(
                "  ⚠ Copilot startup slow (continuing — it may still be loading)"
            )

    # 6. Save go-session state
    save_go_session(
        pane_label=standby_label,
        repo_root=str(repo_path),
        copilot_pane=pane_id,
        tmux_env=current_tmux,
    )
    click.echo("  ✓ go-session saved")

    # 7. Exec Claude Code (replaces this process)
    click.echo("\nLaunching Claude Code as CEO...")
    click.echo("Just tell Claude what you want to build. It knows how to use Duo.\n")

    claude_args = ["claude"]
    if get_config("bypass_permissions"):
        claude_args.append("--dangerously-skip-permissions")

    # Change to repo directory before exec
    os.chdir(repo_path)

    # os.execvp replaces this process — no return
    os.execvp("claude", claude_args)
