"""Configuration management — persistent settings in ~/.duo/config.json."""

from __future__ import annotations

import json
import sys
from typing import Any

from duo.protocol import DUO_DIR

CONFIG_PATH = DUO_DIR / "config.json"

# Default values for all config keys
DEFAULTS: dict[str, Any] = {
    "copilot_model": "claude-opus-4.6",
    "max_corrections": 3,
    "heartbeat_timeout": 90,
    "poll_base_interval": 5.0,
    "poll_max_interval": 120.0,
    "auto_allow_all": True,
    "max_parallel": 3,
    "pr_budget": 0,  # 0 = unlimited, >0 = max PR per task
    "worktree_base_path": "/tmp/duo-worktrees",
}


def load_config() -> dict[str, Any]:
    """Load config, falling back to defaults for missing keys."""
    config = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            stored = json.loads(CONFIG_PATH.read_text())
            config.update(stored)
        except (json.JSONDecodeError, OSError):
            pass
    return config


def save_config(config: dict[str, Any]) -> None:
    """Save config to disk."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")


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
        }
        _FLOAT_MINIMUMS: dict[str, float] = {
            "poll_base_interval": 0,
            "poll_max_interval": 0,
        }
        if key in _INT_MINIMUMS:
            assert isinstance(coerced, int)
            minimum = _INT_MINIMUMS[key]
            if coerced < minimum:
                raise ValueError(
                    f"'{key}' must be >= {minimum}, got {coerced}"
                )
        if key in _FLOAT_MINIMUMS:
            assert isinstance(coerced, float)
            if coerced <= 0:
                raise ValueError(
                    f"'{key}' must be > 0, got {coerced}"
                )
    if key not in DEFAULTS:
        print(f"[duo] Warning: '{key}' is not a known config key", file=sys.stderr)
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
