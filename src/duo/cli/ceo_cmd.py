"""CEO workflow commands — ``duo ceo-{wait,select,approve,status}``."""

from __future__ import annotations

import json
import os
import re

import click

from duo.errors import DuoUserError


def _log_pr_budget_warning(label: str, flag: str) -> None:
    """Append a warning line to ~/.duo/pr-budget.log when safety is bypassed."""
    from duo.protocol import DUO_DIR, now_iso

    log_path = DUO_DIR / "pr-budget.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"{now_iso()} WARNING {flag} used on pane '{label}'\n")
        f.flush()
        os.fsync(f.fileno())


def _enforce_not_at_main_prompt(label: str, force_new_session: bool) -> None:
    """Check main prompt guard; bypass only with force_new_session (+ log)."""
    if force_new_session:
        _log_pr_budget_warning(label, "--force-new-session")
    else:
        assert_not_at_main_prompt(label)


def assert_not_at_main_prompt(label: str) -> None:
    """Raise ClickException if the pane is at Copilot's main ❯ prompt.

    ANY input at the main prompt creates a new Premium Request.  This is a
    hard safety gate — callers must abort or require ``--force-new-session``.
    """
    from duo.transport import is_at_main_prompt, read_pane

    content = read_pane(label, 20)
    if is_at_main_prompt(content):
        raise DuoUserError(
            f"REFUSED: '{label}' is at Copilot main ❯ prompt. "
            "Sending any input here would create a NEW Premium Request "
            "and burn budget.",
            fix="Wait for a new dialog or use --force-new-session.",
        )


@click.command("ceo-wait")
@click.argument(
    "task",
    shell_complete=lambda ctx, param, incomplete: __import__(
        "duo.cli", fromlist=["_complete_task_names"]
    )._complete_task_names(ctx, param, incomplete),
)
@click.option(
    "--timeout", default=300, type=float, help="Max seconds to wait (default: 300)."
)
@click.option(
    "--interval", default=5, type=float, help="Poll interval in seconds (default: 5)."
)
def ceo_wait(task: str, timeout: float, interval: float) -> None:
    """Wait for a dialog to appear in a task's pane.

    Blocks until the pane shows a stable dialog box, then prints the
    dialog content to stdout, writes a watch-event signal file, and
    exits 0. On timeout, exits 1.
    """
    from duo.cli import _load_task_or_fail
    from duo.commander import _write_watch_event
    from duo.transport import is_process_alive, read_pane, wait_for_dialog

    t = _load_task_or_fail(task)
    if not is_process_alive(t.pane_label):
        raise DuoUserError(
            f"Pane '{t.pane_label}' is not alive",
            fix=f"Run 'duo status {task}' to check task state, or 'duo resume {task}' to restart.",
        )
    found = wait_for_dialog(t.pane_label, timeout=timeout, interval=interval)
    if not found:
        raise DuoUserError(
            f"Timeout after {timeout}s: no dialog detected in '{task}'",
            fix=f"Check pane manually or increase --timeout. Run 'duo status {task}' for current state.",
        )
    content = read_pane(t.pane_label, 40)
    click.echo(content)
    _write_watch_event(t, content)
    _session_id = os.environ.get("DUO_CEO_SESSION")
    if _session_id:
        from duo.ceo_log import log_dialog_detected

        log_dialog_detected(_session_id, task, content, "dialog")


@click.command("ceo-select")
@click.argument(
    "task",
    shell_complete=lambda ctx, param, incomplete: __import__(
        "duo.cli", fromlist=["_complete_task_names"]
    )._complete_task_names(ctx, param, incomplete),
)
@click.argument("option", required=False, default=None)
@click.option(
    "--other",
    "other_text",
    default=None,
    help="Navigate to the 'Other' option and type this text instead",
)
@click.option(
    "--force-new-session",
    is_flag=True,
    default=False,
    help="Bypass main-prompt safety check (WARNING: creates a new PR)",
)
def ceo_select(
    task: str,
    option: str | None,
    other_text: str | None,
    force_new_session: bool,
) -> None:
    """Select a dialog option in a task's pane.

    OPTION is a number (1-9) to pick that option directly.
    Use --other TEXT instead to navigate to the last option
    ("Other"/"type your answer") and type custom text.
    OPTION and --other are mutually exclusive.

    Safety: refuses to act if the pane is at the main ❯ prompt (would
    create a new Premium Request). Override with --force-new-session.
    """
    from duo.cli import _load_task_or_fail
    from duo.transport import (
        DialogKind,
        get_dialog_kind,
        is_in_dialog_stable,
        select_dialog_option,
        send_option_other_message,
        send_text_dialog_message,
    )

    if option is not None and other_text is not None:
        raise click.UsageError("Cannot specify both OPTION and --other. Pick one.")
    if option is None and other_text is None:
        raise click.UsageError("Must specify OPTION or --other TEXT.")

    if option is not None and not option.isdigit():
        raise DuoUserError(
            f"OPTION must be a number (1-9), got '{option}'",
            fix="Run 'duo ceo-select TASK 1' to select the first option.",
        )

    t = _load_task_or_fail(task)
    _enforce_not_at_main_prompt(t.pane_label, force_new_session)
    if not is_in_dialog_stable(t.pane_label):
        raise DuoUserError(
            f"Pane '{t.pane_label}' is not in a stable dialog",
            fix=f"Wait for the dialog to appear, then retry. Run 'duo ceo-wait {task}' to wait.",
        )
    kind = get_dialog_kind(t.pane_label)
    if kind == DialogKind.TEXT:
        # Text-input dialog: no numbered options
        if option is not None:
            raise DuoUserError(
                "This is a text-input dialog with no numbered options",
                fix="Use --other TEXT to type a response.",
            )
        if other_text is None:  # pragma: no cover — guarded by mutual-exclusion above
            raise click.ClickException(
                "Internal error: expected --other TEXT for text dialog."
            )
        success = send_text_dialog_message(t.pane_label, other_text)
        if success:
            click.echo(f"Typed text: {other_text}")
        else:
            click.echo(
                f"Typed text: {other_text} (dialog may still be active — check manually)"
            )
    elif kind == DialogKind.BULLET:
        from duo.transport import select_bullet_option

        if option is not None:
            select_bullet_option(t.pane_label, int(option))
            click.echo(f"Selected bullet option {option}")
        elif other_text is not None:
            # BULLET last item is usually "Type your answer..."
            send_text_dialog_message(t.pane_label, other_text)
            click.echo(f"Typed text in bullet dialog: {other_text}")
        else:  # pragma: no cover — unreachable: Click mutual-exclusion ensures option or other_text is set
            raise click.ClickException("Internal error: expected OPTION or --other.")
    elif other_text is not None:
        success = send_option_other_message(t.pane_label, other_text)
        if success:
            click.echo(f"Selected 'Other' with text: {other_text}")
        else:
            click.echo(
                f"Selected 'Other' with text: {other_text} (dialog may still be active)"
            )
    else:
        if option is None:  # pragma: no cover — guarded by mutual-exclusion above
            raise click.ClickException("Internal error: expected OPTION number.")
        select_dialog_option(t.pane_label, option)
        click.echo(f"Selected option {option}")
    _session_id = os.environ.get("DUO_CEO_SESSION")
    if _session_id:
        from duo.ceo_log import log_decision

        chosen = other_text if other_text is not None else (option or "?")
        log_decision(_session_id, task, "select", f"selected {chosen}", elapsed_ms=0)


@click.command("ceo-approve")
@click.argument(
    "task",
    shell_complete=lambda ctx, param, incomplete: __import__(
        "duo.cli", fromlist=["_complete_task_names"]
    )._complete_task_names(ctx, param, incomplete),
)
@click.option(
    "--force-new-session",
    is_flag=True,
    default=False,
    help="Bypass main-prompt safety check (WARNING: creates a new PR)",
)
def ceo_approve(task: str, force_new_session: bool) -> None:
    """Auto-approve a permission dialog in a task's pane.

    Only works on permission dialogs (e.g. "Do you want to run this
    command?"). For ask-user dialogs, use ceo-select instead.

    Reads the dialog options and picks the "most positive" yes option:
    prefers "Yes + approve for session" over plain "Yes", skips "No".

    Safety: refuses to act if the pane is at the main ❯ prompt (would
    create a new Premium Request). Override with --force-new-session.
    """
    from duo.cli import _load_task_or_fail
    from duo.transport import approve_permission, is_permission_dialog

    t = _load_task_or_fail(task)
    _enforce_not_at_main_prompt(t.pane_label, force_new_session)
    if not is_permission_dialog(t.pane_label):
        raise DuoUserError(
            f"'{task}' is not showing a permission dialog",
            fix="Use 'duo ceo-select' for other dialog types, or 'duo ceo-wait' to wait for a dialog.",
        )
    approve_permission(t.pane_label)
    click.echo(f"Approved dialog in '{task}'")
    _session_id = os.environ.get("DUO_CEO_SESSION")
    if _session_id:
        from duo.ceo_log import log_decision

        log_decision(_session_id, task, "approve", "permission approved", elapsed_ms=0)


@click.command("ceo-status")
@click.argument(
    "task",
    shell_complete=lambda ctx, param, incomplete: __import__(
        "duo.cli", fromlist=["_complete_task_names"]
    )._complete_task_names(ctx, param, incomplete),
)
@click.option(
    "--assert-in-dialog",
    is_flag=True,
    default=False,
    help="Exit non-zero if pane is NOT in a dialog (for scripting)",
)
def ceo_status(task: str, assert_in_dialog: bool) -> None:
    """Print the current pane state as a single JSON line.

    States: idle, processing, dialog, text_dialog, dead.

    \b
    Output examples:
      {"task":"e2e-test","state":"dialog","options":5}
      {"task":"e2e-test","state":"text_dialog"}

    Use --assert-in-dialog in scripts:
      duo ceo-status my-task --assert-in-dialog || handle_no_dialog
    """
    from duo.cli import _load_task_or_fail
    from duo.transport import (
        DialogKind,
        get_dialog_kind,
        is_process_alive,
        read_pane,
        strip_ansi,
    )

    t = _load_task_or_fail(task)
    label = t.pane_label

    if not is_process_alive(label):
        click.echo(json.dumps({"task": task, "state": "dead"}))
        if assert_in_dialog:
            raise SystemExit(1)
        return

    content = read_pane(label, 30)
    kind = get_dialog_kind(label)

    # Check dialog first (most specific)
    if kind == DialogKind.OPTION:
        # Count options only within the dialog box boundaries (╭─ … ╰─)
        lines = content.split("\n")
        in_box = False
        opt_count = 0
        for line in lines:  # pragma: no cover — split("\n") always yields ≥1 element
            if "╭─" in line:
                in_box = True
                continue
            if "╰─" in line:
                break
            if in_box and re.match(r"\s*[│]?\s*(❯\s*)?\d+\.\s", line):
                opt_count += 1
        click.echo(json.dumps({"task": task, "state": "dialog", "options": opt_count}))
        return

    if kind == DialogKind.TEXT:
        click.echo(json.dumps({"task": task, "state": "text_dialog"}))
        return

    if kind == DialogKind.BULLET:
        from duo.transport import count_bullet_items

        total, cursor = count_bullet_items(strip_ansi(content))
        click.echo(
            json.dumps(
                {
                    "task": task,
                    "state": "bullet_dialog",
                    "items": total,
                    "cursor": cursor,
                }
            )
        )
        return

    # Check spinner (processing)
    if any(m in content for m in ("◉ ", "◎ ", "○ ")):
        click.echo(json.dumps({"task": task, "state": "processing"}))
        if assert_in_dialog:
            raise SystemExit(1)
        return

    # Otherwise idle
    click.echo(json.dumps({"task": task, "state": "idle"}))
    if assert_in_dialog:
        raise SystemExit(1)
