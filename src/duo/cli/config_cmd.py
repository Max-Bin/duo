"""Config subcommands — ``duo config {get,set,list,reset,path,edit,validate}``."""

from __future__ import annotations

import json
import os

import click

from duo.errors import DuoUserError


def _complete_config_keys(
    ctx: click.Context,  # noqa: ARG001
    param: click.Parameter,  # noqa: ARG001
    incomplete: str,
) -> list[click.shell_completion.CompletionItem]:
    from click.shell_completion import CompletionItem

    from duo.config import CONFIG_DESCRIPTIONS, DEFAULTS

    return [
        CompletionItem(k, help=CONFIG_DESCRIPTIONS.get(k, ""))
        for k in sorted(DEFAULTS)
        if k.startswith(incomplete)
    ]


@click.group()
def config() -> None:
    """Manage Duo configuration."""


@config.command("get")
@click.argument("key", shell_complete=_complete_config_keys)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option("-q", "--quiet", is_flag=True, help="Print only the raw value")
def config_get(key: str, *, as_json: bool = False, quiet: bool = False) -> None:
    """Get a config value."""
    from duo.config import get_config

    value = get_config(key)
    if value is None:
        raise DuoUserError(
            f"Unknown config key: {key}",
            fix="Run 'duo config list' to see available keys.",
        )
    if quiet:
        click.echo(str(value))
        return
    if as_json:
        click.echo(json.dumps({key: value}))
    else:
        click.echo(f"{key} = {value}")


@config.command("set")
@click.argument("key", shell_complete=_complete_config_keys)
@click.argument("value")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def config_set(key: str, value: str, *, as_json: bool = False) -> None:
    """Set a config value."""
    from duo.config import DEFAULTS, set_config

    if key not in DEFAULTS:
        click.echo(f"Warning: '{key}' is not a known config key", err=True)
    try:
        result = set_config(key, value)
    except ValueError as exc:
        raise click.ClickException(
            f"Invalid value for '{key}': {exc}. Run 'duo config list' to see valid keys."
        ) from None
    if as_json:
        click.echo(json.dumps({key: result}))
    else:
        click.echo(f"{key} = {result}")


@config.command("list")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def config_list(*, as_json: bool = False) -> None:
    """List all config values."""
    from duo.config import CONFIG_DESCRIPTIONS, DEFAULTS, load_config

    cfg = load_config()
    if as_json:
        output: dict[str, object] = {}
        for key in sorted(DEFAULTS):
            value = cfg.get(key, DEFAULTS[key])
            output[key] = {
                "value": value,
                "default": DEFAULTS[key],
                "modified": value != DEFAULTS[key],
                "description": CONFIG_DESCRIPTIONS.get(key, ""),
            }
        click.echo(json.dumps(output, indent=2))
        return
    for key in sorted(DEFAULTS):
        value = cfg.get(key, DEFAULTS[key])
        default = DEFAULTS[key]
        marker = "" if value == default else " (modified)"
        desc = CONFIG_DESCRIPTIONS.get(key, "")
        desc_suffix = f"  # {desc}" if desc else ""
        click.echo(f"  {key} = {value}{marker}{desc_suffix}")


@config.command("reset")
@click.argument("key", required=False, shell_complete=_complete_config_keys)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def config_reset(key: str | None = None, *, as_json: bool = False) -> None:
    """Reset config to defaults (or reset a single key)."""
    from duo.config import DEFAULTS, reset_config

    if key and key not in DEFAULTS:
        raise DuoUserError(
            f"Unknown config key: {key}",
            fix="Run 'duo config list' to see available keys.",
        )
    reset_config(key)
    if as_json:
        click.echo(json.dumps({"reset": key or "all"}))
    elif key:
        click.echo(f"Reset {key} to default.")
    else:
        click.echo("All config reset to defaults.")


@config.command("path")
def config_path() -> None:
    """Show the config file path."""
    from duo.config import CONFIG_PATH

    click.echo(CONFIG_PATH)


@config.command("edit")
def config_edit() -> None:
    """Open config file in $EDITOR."""
    from duo.config import CONFIG_PATH

    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text("{}\n", encoding="utf-8")
    click.edit(filename=str(CONFIG_PATH), editor=editor)


@config.command("validate")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def config_validate(*, as_json: bool = False) -> None:
    """Validate config file and report issues."""
    from duo.config import validate_config

    issues = validate_config()
    if as_json:
        click.echo(json.dumps({"valid": len(issues) == 0, "issues": issues}))
    elif issues:
        click.echo("Config issues found:")
        for issue in issues:
            click.echo(f"  ⚠ {issue}")
        raise SystemExit(1)
    else:
        click.echo("✓ Config is valid.")
