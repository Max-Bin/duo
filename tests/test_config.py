"""Tests for duo.config — persistent configuration management."""

from __future__ import annotations

import json
from pathlib import Path

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

    def test_rejects_invalid_bool(self):
        for invalid in ("maybe", "yep", "nah", "2", "tru"):
            with pytest.raises(ValueError, match="Cannot convert"):
                config_mod.set_config("auto_allow_all", invalid)

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


class TestUnknownConfigKeys:
    def test_unknown_keys_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Unknown config keys are preserved for forward compatibility."""
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"unknown_key": 42, "max_parallel": 5}))
        monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
        config = config_mod.load_config()
        assert config["unknown_key"] == 42
        assert config["max_parallel"] == 5

    def test_unknown_keys_logged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
    ):
        """Unknown keys produce a warning log."""
        import logging

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"typo_key": "oops"}))
        monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
        with caplog.at_level(logging.WARNING, logger="duo.config"):
            config_mod.load_config()
        assert "unknown config keys" in caplog.text.lower()
        assert "typo_key" in caplog.text


# ---------------------------------------------------------------------------
# Round BO: load_config validation + worktree default
# ---------------------------------------------------------------------------


class TestLoadConfigValidation:
    """load_config() validates types and cross-key constraints."""

    def test_wrong_type_bool_falls_back(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"auto_allow_all": "not-a-bool"}))
        monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
        import logging

        with caplog.at_level(logging.WARNING, logger="duo.config"):
            cfg = config_mod.load_config()
        assert cfg["auto_allow_all"] is True  # default
        assert "expected bool" in caplog.text.lower()

    def test_wrong_type_number_falls_back(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"heartbeat_timeout": "bad"}))
        monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
        import logging

        with caplog.at_level(logging.WARNING, logger="duo.config"):
            cfg = config_mod.load_config()
        assert cfg["heartbeat_timeout"] == 90  # default
        assert "expected number" in caplog.text.lower()

    def test_wrong_type_str_falls_back(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"copilot_model": 42}))
        monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
        import logging

        with caplog.at_level(logging.WARNING, logger="duo.config"):
            cfg = config_mod.load_config()
        assert cfg["copilot_model"] == "claude-opus-4.6"  # default
        assert "expected str" in caplog.text.lower()

    def test_poll_max_lt_base_resets(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps({"poll_base_interval": 10.0, "poll_max_interval": 5.0})
        )
        monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
        import logging

        with caplog.at_level(logging.WARNING, logger="duo.config"):
            cfg = config_mod.load_config()
        assert cfg["poll_max_interval"] >= cfg["poll_base_interval"]
        assert "resetting max to base" in caplog.text.lower()

    def test_valid_values_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps({"heartbeat_timeout": 30, "copilot_model": "gpt-4o"})
        )
        monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
        cfg = config_mod.load_config()
        assert cfg["heartbeat_timeout"] == 30
        assert cfg["copilot_model"] == "gpt-4o"

    def test_float_as_int_coerced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """JSON may store 90.0 for an int field; load_config coerces to int."""
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"heartbeat_timeout": 90.0}))
        monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
        cfg = config_mod.load_config()
        assert cfg["heartbeat_timeout"] == 90
        assert isinstance(cfg["heartbeat_timeout"], int)


class TestWorktreeDefault:
    """worktree_base_path defaults to persistent ~/.duo/worktrees."""

    def test_default_is_persistent(self) -> None:
        path = config_mod.DEFAULTS["worktree_base_path"]
        assert "/tmp" not in path
        assert ".duo/worktrees" in path


class TestConfigBranchEdgeCases:
    """Cover rare branches in load_config and reset_config."""

    def test_reset_nonexistent_key_is_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resetting a key that's not in DEFAULTS or config does nothing."""
        cfg_path = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
        config_mod.save_config({"copilot_model": "test"})
        config_mod.reset_config("totally_nonexistent_key")
        cfg = config_mod.load_config()
        assert cfg["copilot_model"] == "test"

    def test_load_config_unknown_type_passthrough(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Config value with a type not in bool/int/float/str is stored as-is."""
        cfg_path = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
        # Add a list-type default to trigger the else branch
        monkeypatch.setitem(config_mod.DEFAULTS, "tags", ["default"])
        cfg_path.write_text(json.dumps({"tags": ["a", "b"]}))
        cfg = config_mod.load_config()
        assert cfg["tags"] == ["a", "b"]


class TestConfigRubberDuckHardening:
    """Tests from rubber-duck audit: non-finite, non-dict, int overflow."""

    def test_load_non_dict_json_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Non-dict JSON (array, string, null) falls back to defaults."""
        cfg_path = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
        for content in ["[]", '"hello"', "null", "42"]:
            cfg_path.write_text(content, encoding="utf-8")
            cfg = config_mod.load_config()
            assert cfg == dict(config_mod.DEFAULTS)

    def test_load_nan_in_numeric_field_uses_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """NaN in a numeric field is rejected."""
        cfg_path = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
        cfg_path.write_text('{"poll_base_interval": NaN}', encoding="utf-8")
        cfg = config_mod.load_config()
        assert cfg["poll_base_interval"] == config_mod.DEFAULTS["poll_base_interval"]

    def test_load_infinity_in_numeric_field_uses_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Infinity in a numeric field is rejected."""
        cfg_path = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
        cfg_path.write_text('{"poll_base_interval": Infinity}', encoding="utf-8")
        cfg = config_mod.load_config()
        assert cfg["poll_base_interval"] == config_mod.DEFAULTS["poll_base_interval"]

    def test_load_int_overflow_uses_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Float that overflows int conversion uses default."""
        cfg_path = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
        cfg_path.write_text('{"max_corrections": 1e999}', encoding="utf-8")
        cfg = config_mod.load_config()
        assert cfg["max_corrections"] == config_mod.DEFAULTS["max_corrections"]

    def test_load_int_conversion_value_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Numeric value that passes isfinite but fails int() uses default."""
        from unittest.mock import patch

        cfg_path = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
        cfg_path.write_text(json.dumps({"max_corrections": 3}), encoding="utf-8")

        orig_json_loads = json.loads

        def _inject_bad_value(s, **kw):
            result = orig_json_loads(s, **kw)
            if isinstance(result, dict) and "max_corrections" in result:
                # Replace with a mock object that passes isinstance and isfinite
                # but fails int()
                result["max_corrections"] = float("inf")
            return result

        with patch("duo.config.json.loads", side_effect=_inject_bad_value):
            with patch("duo.config.math.isfinite", return_value=True):
                cfg = config_mod.load_config()
        assert cfg["max_corrections"] == config_mod.DEFAULTS["max_corrections"]

    def test_load_unicode_error_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Non-UTF-8 file falls back to defaults."""
        cfg_path = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
        cfg_path.write_bytes(b"\xff\xfe invalid utf-8")
        cfg = config_mod.load_config()
        assert cfg == dict(config_mod.DEFAULTS)

    def test_set_nan_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """set_config rejects NaN for float fields."""
        cfg_path = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
        with pytest.raises(ValueError, match="finite"):
            config_mod.set_config("poll_base_interval", "nan")

    def test_set_infinity_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """set_config rejects Infinity for float fields."""
        cfg_path = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
        with pytest.raises(ValueError, match="finite"):
            config_mod.set_config("poll_base_interval", "inf")

    def test_set_neg_infinity_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """set_config rejects -Infinity for float fields."""
        cfg_path = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
        with pytest.raises(ValueError, match="finite"):
            config_mod.set_config("poll_base_interval", "-inf")


class TestConfigDocAccuracy:
    """Guard that docs mention all config keys and no phantom ones."""

    def test_getting_started_lists_all_config_keys(self) -> None:
        """docs/getting-started.md should reference every config key."""
        docs_path = (
            Path(__file__).resolve().parent.parent / "docs" / "getting-started.md"
        )
        text = docs_path.read_text(encoding="utf-8")
        for key in config_mod.DEFAULTS:
            assert key in text, (
                f"Config key '{key}' not mentioned in getting-started.md"
            )

    def test_architecture_lists_all_config_keys(self) -> None:
        """docs/architecture.md should reference every config key."""
        docs_path = Path(__file__).resolve().parent.parent / "docs" / "architecture.md"
        text = docs_path.read_text(encoding="utf-8")
        for key in config_mod.DEFAULTS:
            assert key in text, f"Config key '{key}' not mentioned in architecture.md"
