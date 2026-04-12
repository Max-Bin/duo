"""CLI entry point — thin interface to commander."""

from __future__ import annotations

import json
import logging
import subprocess  # noqa: F401 — test patch target: duo.cli.subprocess.run

import click

from duo.cli._helpers import (  # noqa: F401 — re-export shared helpers
    _ALIASES as _ALIASES,
)
from duo.cli._helpers import (
    _COMMAND_SECTIONS as _COMMAND_SECTIONS,
)
from duo.cli._helpers import (
    _GIT_TIMEOUT as _GIT_TIMEOUT,
)
from duo.cli._helpers import (
    _MAX_AGE_SECONDS as _MAX_AGE_SECONDS,
)
from duo.cli._helpers import (
    _TERMINAL_STATES as _TERMINAL_STATES,  # noqa: F401 — re-export
)
from duo.cli._helpers import (
    _TMUX_TIMEOUT as _TMUX_TIMEOUT,
)
from duo.cli._helpers import (
    _complete_status_values as _complete_status_values,
)
from duo.cli._helpers import (
    _complete_task_names as _complete_task_names,  # noqa: F401 — re-export for tests
)
from duo.cli._helpers import (
    _complete_thinking_names as _complete_thinking_names,
)
from duo.cli._helpers import (
    _create_worktree as _create_worktree,  # noqa: F401 — re-export for tests
)
from duo.cli._helpers import (
    _fmt_age as _fmt_age,
)
from duo.cli._helpers import (
    _fmt_ts as _fmt_ts,  # noqa: F401 — re-export for tests
)
from duo.cli._helpers import (
    _load_task_or_fail as _load_task_or_fail,  # noqa: F401 — re-export for tests
)
from duo.cli._helpers import (
    _OrderedGroup,
)
from duo.cli._helpers import (
    _run_git as _run_git,  # noqa: F401 — re-export for test patching
)
from duo.cli._helpers import (
    _safe_join as _safe_join,
)
from duo.cli._helpers import (
    _validate_task_name as _validate_task_name,
)
from duo.cli.doctor import (
    CheckResult as CheckResult,  # noqa: F401 — re-export
)
from duo.cli.doctor import (
    _doctor_auto_fix as _doctor_auto_fix,  # noqa: F401 — re-export
)
from duo.cli.doctor import (
    _doctor_check_orphan_worktrees as _doctor_check_orphan_worktrees,  # noqa: F401 — re-export
)
from duo.cli.doctor import (
    _doctor_check_stale_locks as _doctor_check_stale_locks,  # noqa: F401 — re-export
)
from duo.cli.doctor import (
    _emit_restart_signal as _emit_restart_signal,  # noqa: F401 — re-export
)
from duo.cli.doctor import (
    _get_pid_child_count as _get_pid_child_count,  # noqa: F401 — re-export
)
from duo.cli.doctor import (
    _get_pid_fd_count as _get_pid_fd_count,  # noqa: F401 — re-export
)
from duo.cli.doctor import (
    _get_pid_kqueue_count as _get_pid_kqueue_count,  # noqa: F401 — re-export
)
from duo.cli.doctor import (
    doctor as doctor_command,
)
from duo.errors import DuoUserError
from duo.protocol import (
    DUO_DIR as DUO_DIR,  # noqa: F401 — re-export for test monkeypatching
)
from duo.protocol import (
    TASKS_DIR,
)

__all__ = ["main"]


@click.group(cls=_OrderedGroup)
@click.option("-v", "--verbose", is_flag=True, help="Verbose output")
@click.version_option(package_name="duo", prog_name="duo")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """Duo — Agent Orchestration Runtime."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    if verbose:
        logging.basicConfig(
            level=logging.DEBUG, format="%(name)s %(levelname)s: %(message)s"
        )
    try:
        TASKS_DIR.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError) as e:
        raise DuoUserError(
            f"cannot create tasks directory '{TASKS_DIR}': {e}",
            fix="Check write permissions or run 'duo doctor'.",
        ) from None


main.add_command(doctor_command, "doctor")


@main.command()
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option("-q", "--quiet", is_flag=True, help="Print only the version number")
def version(*, as_json: bool = False, quiet: bool = False) -> None:
    """Show Duo version."""
    import platform
    import sys

    from duo import __version__

    if quiet:
        click.echo(__version__)
        return
    if as_json:
        click.echo(
            json.dumps(
                {
                    "version": __version__,
                    "python": sys.version.split()[0],
                    "platform": platform.system(),
                }
            )
        )
    else:
        click.echo(f"duo {__version__}")


@main.command()
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
def completion(shell: str) -> None:
    """Generate shell completion script.

    \b
    Add to your shell profile:
      # Bash (~/.bashrc)
      eval "$(duo completion bash)"

      # Zsh (~/.zshrc)
      eval "$(duo completion zsh)"

      # Fish (~/.config/fish/config.fish)
      duo completion fish | source
    """
    scripts = {
        "bash": 'eval "$(_DUO_COMPLETE=bash_source duo)"',
        "zsh": 'eval "$(_DUO_COMPLETE=zsh_source duo)"',
        "fish": "set -x _DUO_COMPLETE fish_source\nduo | source\nset -e _DUO_COMPLETE",
    }
    click.echo(scripts[shell])


from duo.cli.lifecycle_cmd import go as go_command  # noqa: E402
from duo.cli.lifecycle_cmd import init as init_command  # noqa: E402
from duo.cli.lifecycle_cmd import start as start_command  # noqa: E402

main.add_command(start_command, "start")
main.add_command(init_command, "init")
main.add_command(go_command, "go")


from duo.cli.task_ops_cmd import recover as recover_command  # noqa: E402
from duo.cli.task_ops_cmd import send as send_command  # noqa: E402

main.add_command(send_command, "send")


from duo.cli.monitoring_cmd import (  # noqa: E402
    _print_task as _print_task,  # noqa: F401 — re-export
)
from duo.cli.monitoring_cmd import (  # noqa: E402
    dashboard as dashboard_command,
)
from duo.cli.monitoring_cmd import (
    list_cmd as list_command,
)
from duo.cli.monitoring_cmd import (
    monitor as monitor_command,
)
from duo.cli.monitoring_cmd import (
    status as status_command,
)
from duo.cli.monitoring_cmd import (
    watch as watch_command,
)

main.add_command(status_command, "status")
main.add_command(list_command, "list")
main.add_command(monitor_command, "monitor")
main.add_command(watch_command, "watch")


main.add_command(recover_command, "recover")


from duo.cli.task_mgmt_cmd import kill as kill_command  # noqa: E402
from duo.cli.task_mgmt_cmd import merge as merge_command  # noqa: E402
from duo.cli.task_mgmt_cmd import stop as stop_command  # noqa: E402

main.add_command(merge_command, "merge")
main.add_command(stop_command, "stop")
main.add_command(kill_command, "kill")


from duo.cli.batch_cmd import (  # noqa: E402
    _create_single_task as _create_single_task,  # noqa: F401 — re-export for tests
)
from duo.cli.batch_cmd import (  # noqa: E402
    _load_batch_file as _load_batch_file,  # noqa: F401 — re-export for tests
)
from duo.cli.batch_cmd import batch as batch_command  # noqa: E402
from duo.cli.batch_cmd import queue as queue_command  # noqa: E402

main.add_command(batch_command, "batch")
main.add_command(queue_command, "queue")


from duo.cli.reporting_cmd import audit as audit_command  # noqa: E402
from duo.cli.reporting_cmd import cost as cost_command  # noqa: E402
from duo.cli.reporting_cmd import diff_cmd as diff_command  # noqa: E402

main.add_command(audit_command, "audit")
main.add_command(cost_command, "cost")
main.add_command(diff_command, "diff")


main.add_command(dashboard_command, "dashboard")


from duo.cli.logs_cmd import (  # noqa: E402
    logs as logs_command,
)

main.add_command(logs_command, "logs")


from duo.cli.inspect_cmd import inspect as inspect_command

main.add_command(inspect_command, "inspect")


from duo.cli.task_ops_cmd import resume as resume_command  # noqa: E402
from duo.cli.task_ops_cmd import retry as retry_command  # noqa: E402

main.add_command(resume_command, "resume")
main.add_command(retry_command, "retry")


# ---------------------------------------------------------------------------
# Config — extracted to duo.cli.config_cmd
# ---------------------------------------------------------------------------
from duo.cli.config_cmd import (
    _complete_config_keys as _complete_config_keys,  # noqa: F401,E402 — re-export
)
from duo.cli.config_cmd import config as config_group  # noqa: E402

main.add_command(config_group, "config")


# ---------------------------------------------------------------------------
# duo events — extracted to duo.cli.events_cmd
# ---------------------------------------------------------------------------
from duo.cli.events_cmd import (  # noqa: E402
    _WATCH_EVENTS_DIR as _WATCH_EVENTS_DIR,  # noqa: F401 — re-export
)
from duo.cli.events_cmd import events as events_group  # noqa: E402

main.add_command(events_group, "events")


# ---------------------------------------------------------------------------
# duo ceo-* — Ergonomic CEO workflow commands
# ---------------------------------------------------------------------------


from duo.cli.ceo_cmd import (  # noqa: E402
    _enforce_not_at_main_prompt as _enforce_not_at_main_prompt,  # noqa: F401 — re-export
)
from duo.cli.ceo_cmd import (  # noqa: E402
    _log_pr_budget_warning as _log_pr_budget_warning,  # noqa: F401 — re-export
)
from duo.cli.ceo_cmd import (  # noqa: E402
    assert_not_at_main_prompt as assert_not_at_main_prompt,  # noqa: F401 — re-export
)
from duo.cli.ceo_cmd import (  # noqa: E402
    ceo_approve,
    ceo_select,
    ceo_status,
    ceo_wait,
)

main.add_command(ceo_wait, "ceo-wait")
main.add_command(ceo_select, "ceo-select")
main.add_command(ceo_approve, "ceo-approve")
main.add_command(ceo_status, "ceo-status")


from duo.cli.cleanup_cmd import (  # noqa: E402
    _parse_age as _parse_age,  # noqa: F401 — re-export
)
from duo.cli.cleanup_cmd import (  # noqa: E402
    cleanup as cleanup_command,
)

main.add_command(cleanup_command, "cleanup")


# ---------------------------------------------------------------------------
# Think — extracted to duo.cli.think_cmd
# ---------------------------------------------------------------------------
from duo.cli.think_cmd import think as think_command  # noqa: E402

main.add_command(think_command, "think")


# ---------------------------------------------------------------------------
# bench
# ---------------------------------------------------------------------------
