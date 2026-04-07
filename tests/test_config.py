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


class TestResetConfig:
    def test_reset_single_key(self):
        config_mod.set_config("max_corrections", "10")
        assert config_mod.get_config("max_corrections") == 10
        config_mod.reset_config("max_corrections")
        assert config_mod.get_config("max_corrections") == config_mod.DEFAULTS["max_corrections"]

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
