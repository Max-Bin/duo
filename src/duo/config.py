"""Configuration management — persistent settings in ~/.duo/config.json."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from duo.protocol import DUO_DIR, atomic_write_text

__all__ = [
    "CONFIG_PATH",
    "DEFAULTS",
    "get_config",
    "load_config",
    "reset_config",
    "save_config",
    "set_config",
]

CONFIG_PATH = DUO_DIR / "config.json"

logger = logging.getLogger(__name__)

# Default values for all config keys
DEFAULTS: dict[str, Any] = {
    "copilot_model": "claude-opus-4.6",
    "max_corrections": 3,
    "heartbeat_timeout": 90,
    "poll_base_interval": 5.0,
    "poll_max_interval": 120.0,
    "auto_allow_all": True,
    "auto_claude_commander": True,
    "max_parallel": 3,
    "pr_budget": 0,  # 0 = unlimited, >0 = max PR per task
    "task_timeout": 0,  # 0 = disabled, >0 = max seconds per task
    "worktree_base_path": str(Path("~/.duo/worktrees").expanduser()),
}


def load_config() -> dict[str, Any]:
    """Load config, falling back to defaults for missing/invalid keys."""
    config = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            stored = json.loads(CONFIG_PATH.read_text())
            unknown = [k for k in stored if k not in DEFAULTS]
            if unknown:
                logger.warning(
                    "Unknown config keys (ignored for defaults): %s", ", ".join(unknown)
                )
            for key, value in stored.items():
                if key not in DEFAULTS:
                    config[key] = value
                    continue
                expected = type(DEFAULTS[key])
                if expected is bool:
                    if not isinstance(value, bool):
                        logger.warning(
                            "Config key '%s' expected bool, got %s — using default",
                            key,
                            type(value).__name__,
                        )
                        continue
                elif expected in (int, float):
                    if not isinstance(value, (int, float)):
                        logger.warning(
                            "Config key '%s' expected number, got %s — using default",
                            key,
                            type(value).__name__,
                        )
                        continue
                    if expected is int:
                        value = int(value)
                elif expected is str:
                    if not isinstance(value, str):
                        logger.warning(
                            "Config key '%s' expected str, got %s — using default",
                            key,
                            type(value).__name__,
                        )
                        continue
                config[key] = value
            # Cross-key constraint: poll_max must be >= poll_base
            if config["poll_max_interval"] < config["poll_base_interval"]:
                logger.warning(
                    "poll_max_interval (%.1f) < poll_base_interval (%.1f) — "
                    "resetting max to base",
                    config["poll_max_interval"],
                    config["poll_base_interval"],
                )
                config["poll_max_interval"] = config["poll_base_interval"]
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Config file corrupted or empty, using defaults: %s", e)
    return config


def save_config(config: dict[str, Any]) -> None:
    """Save config to disk."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        CONFIG_PATH, json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    )


def get_config(key: str) -> Any:
    """Get a single config value."""
    config = load_config()
    return config.get(key)


def set_config(key: str, value: str) -> bool | int | float | str:
    """Set a config value with type coercion based on defaults."""
    config = load_config()
    coerced: bool | int | float | str = value
    # Type coerce based on default type
    if key in DEFAULTS:
        default_type = type(DEFAULTS[key])
        if default_type is bool:
            coerced = value.lower() in ("true", "1", "yes")
        elif default_type is int:
            try:
                coerced = int(value)
            except ValueError as err:
                raise ValueError(
                    f"Cannot convert '{value}' to {default_type.__name__} for key '{key}'"
                ) from err
        elif default_type is float:
            try:
                coerced = float(value)
            except ValueError as err:
                raise ValueError(
                    f"Cannot convert '{value}' to {default_type.__name__} for key '{key}'"
                ) from err
        # Validate numeric ranges
        _INT_MINIMUMS: dict[str, int] = {
            "max_parallel": 1,
            "max_corrections": 1,
            "heartbeat_timeout": 1,
            "pr_budget": 0,
            "task_timeout": 0,
        }
        _FLOAT_MINIMUMS: dict[str, float] = {
            "poll_base_interval": 0,
            "poll_max_interval": 0,
        }
        _INT_MAXIMUMS: dict[str, int] = {
            "max_parallel": 100,
            "max_corrections": 100,
            "heartbeat_timeout": 3600,
            "pr_budget": 100000,
            "task_timeout": 604800,  # 7 days
        }
        _FLOAT_MAXIMUMS: dict[str, float] = {
            "poll_base_interval": 300.0,
            "poll_max_interval": 3600.0,
        }
        if key in _INT_MINIMUMS:
            minimum = _INT_MINIMUMS[key]
            if not isinstance(coerced, int) or coerced < minimum:
                raise ValueError(
                    f"'{key}' must be an integer >= {minimum}, got {coerced!r}"
                )
        if key in _INT_MAXIMUMS:
            maximum = _INT_MAXIMUMS[key]
            if not isinstance(coerced, int) or coerced > maximum:
                raise ValueError(
                    f"'{key}' must be an integer <= {maximum}, got {coerced!r}"
                )
        if key in _FLOAT_MINIMUMS:
            minimum_f = _FLOAT_MINIMUMS[key]
            if not isinstance(coerced, (int, float)) or coerced <= minimum_f:
                raise ValueError(
                    f"'{key}' must be a number > {minimum_f}, got {coerced!r}"
                )
        if key in _FLOAT_MAXIMUMS:
            maximum_f = _FLOAT_MAXIMUMS[key]
            if not isinstance(coerced, (int, float)) or coerced > maximum_f:
                raise ValueError(
                    f"'{key}' must be a number <= {maximum_f}, got {coerced!r}"
                )
    if key not in DEFAULTS:
        logger.warning("Unknown config key: '%s'", key)
    config[key] = coerced
    save_config(config)
    return coerced


def reset_config(key: str | None = None) -> None:
    """Reset one key or all keys to defaults."""
    if key is None:
        save_config(dict(DEFAULTS))
    else:
        config = load_config()
        if key in DEFAULTS:
            config[key] = DEFAULTS[key]
        elif key in config:
            del config[key]
        save_config(config)
