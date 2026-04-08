"""Tests for duo.config — persistent configuration management."""

from __future__ import annotations

import json

import pytest

import duo.config as config_mod


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Redirect CONFIG_PATH to a temp directory so tests never touch real config."""
    fake_config = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
    return fake_config


class TestLoadConfig:
    def test_returns_defaults_when_no_file(self):
        cfg = config_mod.load_config()
        assert cfg == config_mod.DEFAULTS

    def test_merges_stored_with_defaults(self, isolated_config):
        isolated_config.write_text(json.dumps({"copilot_model": "gpt-4o"}))
        cfg = config_mod.load_config()
        assert cfg["copilot_model"] == "gpt-4o"
        # Other defaults still present
        assert cfg["max_corrections"] == config_mod.DEFAULTS["max_corrections"]

    def test_corrupt_file_falls_back_to_defaults(self, isolated_config):
        isolated_config.write_text("not valid json {{{")
        cfg = config_mod.load_config()
        assert cfg == config_mod.DEFAULTS

    def test_pr_budget_in_defaults(self):
        """Verify pr_budget is present in DEFAULTS."""
        assert "pr_budget" in config_mod.DEFAULTS
        assert config_mod.DEFAULTS["pr_budget"] == 0


class TestSaveConfig:
    def test_round_trip(self, isolated_config):
        original = dict(config_mod.DEFAULTS)
        original["copilot_model"] = "test-model"
        config_mod.save_config(original)

        loaded = config_mod.load_config()
        assert loaded["copilot_model"] == "test-model"

    def test_creates_parent_dirs(self, tmp_path, monkeypatch):
        nested = tmp_path / "a" / "b" / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", nested)
        config_mod.save_config({"key": "val"})
        assert nested.exists()


class TestGetConfig:
    def test_returns_default_value(self):
        assert config_mod.get_config("max_corrections") == 3

    def test_returns_stored_value(self, isolated_config):
        isolated_config.write_text(json.dumps({"max_corrections": 5}))
        assert config_mod.get_config("max_corrections") == 5

    def test_returns_none_for_unknown_key(self):
        assert config_mod.get_config("nonexistent_key") is None


class TestSetConfig:
    def test_sets_string_value(self):
        result = config_mod.set_config("copilot_model", "gpt-4o")
        assert result == "gpt-4o"
        assert config_mod.get_config("copilot_model") == "gpt-4o"

    def test_coerces_bool_true(self):
        for truthy in ("true", "True", "1", "yes"):
            result = config_mod.set_config("auto_allow_all", truthy)
            assert result is True

    def test_coerces_bool_false(self):
        for falsy in ("false", "0", "no"):
            result = config_mod.set_config("auto_allow_all", falsy)
            assert result is False

    def test_coerces_int(self):
        result = config_mod.set_config("max_corrections", "7")
        assert result == 7
        assert isinstance(result, int)

    def test_coerces_float(self):
        result = config_mod.set_config("poll_base_interval", "2.5")
        assert result == 2.5
        assert isinstance(result, float)

    def test_unknown_key_stored_as_string(self):
        result = config_mod.set_config("custom_key", "hello")
        assert result == "hello"
        assert config_mod.get_config("custom_key") == "hello"

    def test_coerces_pr_budget_int(self):
        result = config_mod.set_config("pr_budget", "10")
        assert result == 10
        assert isinstance(result, int)

    def test_invalid_int_raises_value_error(self):
        with pytest.raises(ValueError, match="Cannot convert 'abc' to int"):
            config_mod.set_config("max_corrections", "abc")

    def test_invalid_float_raises_value_error(self):
        with pytest.raises(ValueError, match="Cannot convert 'xyz' to float"):
            config_mod.set_config("poll_base_interval", "xyz")

    def test_set_unknown_key_warns(self, caplog):
        """Setting an unknown key should still work but emit a warning."""
        import logging

        with caplog.at_level(logging.WARNING):
            result = config_mod.set_config("totally_unknown", "val")
        assert result == "val"
        assert config_mod.get_config("totally_unknown") == "val"
        assert "Unknown config key: 'totally_unknown'" in caplog.text

    def test_set_unknown_key_warns_via_logging(self, caplog):
        """Unknown config key produces a warning (caplog-based detection)."""
        import logging

        with caplog.at_level(logging.WARNING, logger="duo.config"):
            config_mod.set_config("nonexistent_key", "value")
        assert "Unknown config key" in caplog.text


class TestResetConfig:
    def test_reset_single_key(self):
        config_mod.set_config("max_corrections", "10")
        assert config_mod.get_config("max_corrections") == 10
        config_mod.reset_config("max_corrections")
        assert (
            config_mod.get_config("max_corrections")
            == config_mod.DEFAULTS["max_corrections"]
        )

    def test_reset_all_keys(self):
        config_mod.set_config("max_corrections", "10")
        config_mod.set_config("copilot_model", "custom")
        config_mod.reset_config()
        cfg = config_mod.load_config()
        assert cfg == config_mod.DEFAULTS

    def test_reset_unknown_key_removes_it(self):
        config_mod.set_config("custom_key", "value")
        assert config_mod.get_config("custom_key") == "value"
        config_mod.reset_config("custom_key")
        assert config_mod.get_config("custom_key") is None


# ---------------------------------------------------------------------------
# Config edge cases
# ---------------------------------------------------------------------------


class TestConfigEdgeCases:
    """Additional edge-case coverage for config operations."""

    def test_set_config_preserves_other_keys(self, isolated_config):
        """Setting one key doesn't affect other keys."""
        isolated_config.write_text(
            json.dumps({"copilot_model": "custom-model", "max_corrections": 10})
        )
        config_mod.set_config("max_corrections", "7")
        assert config_mod.get_config("max_corrections") == 7
        assert config_mod.get_config("copilot_model") == "custom-model"

    def test_load_config_with_extra_keys(self, isolated_config):
        """Config with unknown keys are preserved (forward compatibility)."""
        isolated_config.write_text(
            json.dumps({"future_feature": "enabled", "copilot_model": "gpt-4o"})
        )
        cfg = config_mod.load_config()
        assert cfg["future_feature"] == "enabled"
        assert cfg["copilot_model"] == "gpt-4o"
        # Defaults still merged in
        assert cfg["max_corrections"] == config_mod.DEFAULTS["max_corrections"]

    def test_config_empty_file(self, isolated_config):
        """Empty config.json falls back to defaults."""
        isolated_config.write_text("")
        cfg = config_mod.load_config()
        assert cfg == config_mod.DEFAULTS


# ---------------------------------------------------------------------------
# Numeric range validation
# ---------------------------------------------------------------------------


class TestSetConfigValidation:
    """Verify numeric range checks on set_config()."""

    def test_set_config_negative_max_parallel(self):
        with pytest.raises(ValueError, match="max_parallel.*>= 1"):
            config_mod.set_config("max_parallel", "-1")

    def test_set_config_zero_max_parallel(self):
        with pytest.raises(ValueError, match="max_parallel.*>= 1"):
            config_mod.set_config("max_parallel", "0")

    def test_set_config_negative_pr_budget(self):
        with pytest.raises(ValueError, match="pr_budget.*>= 0"):
            config_mod.set_config("pr_budget", "-1")

    def test_set_config_zero_heartbeat_timeout(self):
        with pytest.raises(ValueError, match="heartbeat_timeout.*>= 1"):
            config_mod.set_config("heartbeat_timeout", "0")

    def test_set_config_negative_poll_interval(self):
        with pytest.raises(ValueError, match="poll_base_interval.*> 0"):
            config_mod.set_config("poll_base_interval", "-1.0")

    def test_set_float_key_zero_rejected(self):
        """Float config keys reject zero."""
        with pytest.raises(ValueError, match="must be a number > 0"):
            config_mod.set_config("poll_base_interval", "0")

    def test_set_float_key_negative_rejected(self):
        """Float config keys reject negative values."""
        with pytest.raises(ValueError, match="must be a number > 0"):
            config_mod.set_config("poll_base_interval", "-5.0")

    def test_set_config_negative_task_timeout(self):
        with pytest.raises(ValueError, match="task_timeout.*>= 0"):
            config_mod.set_config("task_timeout", "-1")

    def test_set_config_zero_task_timeout(self):
        result = config_mod.set_config("task_timeout", "0")
        assert result == 0

    def test_set_config_positive_task_timeout(self):
        result = config_mod.set_config("task_timeout", "300")
        assert result == 300

    def test_set_config_max_parallel_upper_bound(self):
        """max_parallel rejects values > 100."""
        with pytest.raises(ValueError, match="max_parallel.*<= 100"):
            config_mod.set_config("max_parallel", "101")

    def test_set_config_heartbeat_upper_bound(self):
        """heartbeat_timeout rejects values > 3600."""
        with pytest.raises(ValueError, match="heartbeat_timeout.*<= 3600"):
            config_mod.set_config("heartbeat_timeout", "3601")

    def test_set_config_poll_interval_upper_bound(self):
        """poll_base_interval rejects values > 300."""
        with pytest.raises(ValueError, match="poll_base_interval.*<= 300"):
            config_mod.set_config("poll_base_interval", "301")

    def test_set_config_poll_max_upper_bound(self):
        """poll_max_interval rejects values > 3600."""
        with pytest.raises(ValueError, match="poll_max_interval.*<= 3600"):
            config_mod.set_config("poll_max_interval", "3601")


class TestTaskTimeoutDefault:
    def test_task_timeout_in_defaults(self):
        assert "task_timeout" in config_mod.DEFAULTS
        assert config_mod.DEFAULTS["task_timeout"] == 0

    def test_task_timeout_default_value(self):
        cfg = config_mod.load_config()
        assert cfg["task_timeout"] == 0
