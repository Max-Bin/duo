"""Think subcommand — ``duo think <name> [--ask|--finalize|--close|--delete]``."""

from __future__ import annotations

import click

from duo.errors import DuoUserError


@click.command("think")
@click.argument(
    "name",
    shell_complete=lambda ctx, param, incomplete: __import__(
        "duo.cli", fromlist=["_complete_thinking_names"]
    )._complete_thinking_names(ctx, param, incomplete),
)
@click.option(
    "--ask", "ask_text", default=None, help="One-shot question (CEO primary path)"
)
@click.option(
    "--finalize", is_flag=True, help="Generate plan.md from the thinking session"
)
@click.option("--close", "do_close", is_flag=True, help="Close pane, keep files")
@click.option("--delete", "do_delete", is_flag=True, help="Close pane + delete files")
def think(
    name: str,
    ask_text: str | None,
    finalize: bool,
    do_close: bool,
    do_delete: bool,
) -> None:
    """Pre-start brainstorming with Claude Code.

    NAME is the thinking session name, or 'list' to show all sessions.
    """
    from duo.cli._helpers import _validate_task_name

    # Special case: "duo think list"
    if name == "list":
        _think_list_all()
        return

    from duo.thinking import (
        ensure_pane,
        thinking_dir,
    )

    _validate_task_name(name)

    if do_delete:
        _think_delete(name)
        return
    if do_close:
        _think_close(name)
        return
    if finalize:
        _think_finalize(name)
        return
    if ask_text is not None:
        _think_ask(name, ask_text)
        return

    # Bare `duo think <name>` — info mode
    label = ensure_pane(name)
    tdir = thinking_dir(name)
    has_plan = (tdir / "plan.md").exists()
    click.echo(f"Thinking session: {name}")
    click.echo(f"  Pane: {label} (alive)")
    click.echo(f"  Dir:  {tdir}")
    click.echo(f"  Plan: {'finalized' if has_plan else 'not yet finalized'}")
    click.echo()
    click.echo(f"To brainstorm directly, switch to pane '{label}'.")
    click.echo(f'To send a one-shot message: duo think {name} --ask "..."')
    click.echo(f"To finalize: duo think {name} --finalize")


def _think_ask(name: str, text: str) -> None:
    """Handle ``duo think <name> --ask "..."``."""

    from duo.thinking import (
        append_session_log,
        ensure_pane,
        extract_response,
        wait_for_response_stable,
    )
    from duo.transport import read_pane, send_keys, type_text

    label = ensure_pane(name)

    content_before = read_pane(label, 200)
    type_text(label, text)
    send_keys(label, "Enter")

    timeout = 120.0
    result = wait_for_response_stable(label, timeout=timeout)

    if result == "dialog":
        raise DuoUserError(
            "Claude Code asked a question in the pane",
            fix=f"Switch to pane '{label}' to answer, then retry.",
        )
    if result == "timeout":
        raise DuoUserError(
            f"Thinking pane not responding after {int(timeout)}s",
            fix=f"Try: duo think {name} --close",
        )

    content_after = read_pane(label, 200)
    response = extract_response(content_before, content_after, text)
    append_session_log(name, text, response)
    click.echo(response)


def _think_finalize(name: str) -> None:
    """Handle ``duo think <name> --finalize``."""
    import time as _time

    from duo.thinking import ensure_pane, thinking_dir, wait_for_response_stable
    from duo.transport import send_keys, type_text

    label = ensure_pane(name)
    tdir = thinking_dir(name)

    # Ensure pane is idle before sending finalize
    result = wait_for_response_stable(label, timeout=30)
    if result == "dialog":
        raise DuoUserError(
            "Pane is in a dialog",
            fix=f"Switch to pane '{label}' to handle it first.",
        )
    if result == "timeout":
        raise DuoUserError(
            "Pane unresponsive",
            fix=f"Try: duo think {name} --close",
        )

    finalize_msg = (
        f"Please distill our entire conversation into a plan document using the "
        f"template at {tdir}/plan-template.md. Write the result to {tdir}/plan.md. "
        f"Be concrete and specific — this plan will be the initial prompt for a "
        f"code-generating agent."
    )
    type_text(label, finalize_msg)
    send_keys(label, "Enter")

    # Poll for plan.md
    plan_path = tdir / "plan.md"
    deadline = _time.time() + 60
    last_size = -1
    stable_since: float | None = None

    while _time.time() < deadline:
        if plan_path.exists():
            size = plan_path.stat().st_size
            if size == last_size and size > 0:
                if stable_since is not None and _time.time() - stable_since > 3.0:
                    break
            else:
                last_size = size
                stable_since = _time.time()
        _time.sleep(1.0)
    else:
        raise DuoUserError(
            "Claude Code didn't produce plan.md within 60s",
            fix="Check the thinking pane manually, or run --finalize again.",
        )

    click.echo(f"Plan written to {plan_path}")
    click.echo("Review it, then run:")
    click.echo(f"  duo start {name} --from-thinking")


def _think_close(name: str) -> None:
    """Handle ``duo think <name> --close``."""
    from duo.thinking import close_pane, thinking_dir

    tdir = thinking_dir(name)
    if not tdir.exists():
        raise DuoUserError(
            f"No thinking session '{name}' found",
            fix="Run 'duo think --list' to see available sessions.",
        )
    if close_pane(name):
        click.echo(f"Closed pane for '{name}'. Files preserved in {tdir}")
    else:
        click.echo(f"No active pane for '{name}'. Files preserved in {tdir}")


def _think_delete(name: str) -> None:
    """Handle ``duo think <name> --delete``."""
    import shutil

    from duo.thinking import close_pane, thinking_dir

    tdir = thinking_dir(name)
    if not tdir.exists():
        raise DuoUserError(
            f"No thinking session '{name}' found",
            fix="Run 'duo think --list' to see available sessions.",
        )

    if not click.confirm(f"Delete thinking session '{name}'? This cannot be undone."):
        click.echo("Aborted.")
        return

    close_pane(name)
    shutil.rmtree(tdir)
    click.echo(f"Deleted thinking session '{name}'.")


def _think_list_all() -> None:
    """Handle ``duo think list``."""
    from duo.thinking import list_sessions

    sessions = list_sessions()
    if not sessions:
        click.echo("No thinking sessions.")
        return

    click.echo(f"{'NAME':<20} {'PANE':<8} {'STATUS':<12} FILES")
    for s in sessions:
        click.echo(f"{s['name']:<20} {s['pane']:<8} {s['status']:<12} {s['files']}")
