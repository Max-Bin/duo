"""CLI tests for config cmd commands."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from duo.cli import main


class TestConfigSubcommands:
    def test_config_get_known_key(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "get", "copilot_model"])
        assert result.exit_code == 0
        assert "copilot_model" in result.output

    def test_config_get_unknown_key(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "get", "nonexistent_key_xyz"])
        assert result.exit_code != 0
        assert "Unknown config key" in result.output

    def test_config_set_known(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "set", "max_corrections", "5"])
        assert result.exit_code == 0
        assert "max_corrections = 5" in result.output

    def test_config_set_unknown_warns(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "set", "unknown_key", "val"])
        assert "not a known config key" in result.output

    def test_config_set_invalid_bool_error(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "set", "auto_allow_all", "maybe"])
        assert result.exit_code != 0
        assert "Cannot convert" in result.output

    def test_config_list(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "list"])
        assert result.exit_code == 0
        assert "copilot_model" in result.output
        assert "max_corrections" in result.output

    def test_config_list_shows_modified(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        # Set a value first
        runner.invoke(main, ["config", "set", "max_corrections", "99"])
        result = runner.invoke(main, ["config", "list"])
        assert "(modified)" in result.output

    def test_config_reset_single_key(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        # Set then reset
        runner.invoke(main, ["config", "set", "max_corrections", "99"])
        result = runner.invoke(main, ["config", "reset", "max_corrections"])
        assert result.exit_code == 0
        assert "Reset max_corrections" in result.output

    def test_config_reset_all(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "reset"])
        assert result.exit_code == 0
        assert "All config reset" in result.output

    def test_config_get_json(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        """config get --json-output returns JSON."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(
            main, ["config", "get", "copilot_model", "--json-output"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "copilot_model" in data

    def test_config_get_quiet(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        """config get -q prints only the raw value."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "get", "copilot_model", "-q"])
        assert result.exit_code == 0
        val = result.output.strip()
        assert "=" not in val
        assert val  # not empty

    def test_config_list_json(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        """config list --json-output returns all config as JSON with descriptions."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "list", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "copilot_model" in data
        assert "max_corrections" in data
        entry = data["copilot_model"]
        assert "value" in entry
        assert "default" in entry
        assert "modified" in entry
        assert "description" in entry
        assert isinstance(entry["description"], str)

    def test_config_reset_unknown_key(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config reset with unknown key raises DuoUserError."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "reset", "totally_bogus_key"])
        assert result.exit_code != 0
        assert "Unknown config key" in result.output

    def test_config_set_json(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        """config set --json-output returns JSON."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(
            main, ["config", "set", "max_corrections", "5", "--json-output"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["max_corrections"] == 5

    def test_config_reset_json_single(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config reset KEY --json-output returns JSON."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(
            main, ["config", "reset", "max_corrections", "--json-output"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["reset"] == "max_corrections"

    def test_config_reset_json_all(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config reset --json-output returns JSON."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "reset", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["reset"] == "all"

    def test_config_path(self, runner: CliRunner, tmp_path: Path, monkeypatch):
        """config path shows the config file location."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "path"])
        assert result.exit_code == 0
        assert str(fake_config) in result.output

    def test_config_edit_creates_file(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config edit creates config file if missing."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        monkeypatch.setenv("EDITOR", "true")
        result = runner.invoke(main, ["config", "edit"])
        assert result.exit_code == 0
        assert fake_config.exists()

    def test_config_edit_opens_existing(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config edit opens existing config file."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        fake_config.write_text('{"copilot_model": "test"}', encoding="utf-8")
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        monkeypatch.setenv("EDITOR", "true")
        result = runner.invoke(main, ["config", "edit"])
        assert result.exit_code == 0

    def test_config_edit_visual_fallback(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config edit falls back to $VISUAL when $EDITOR not set."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        fake_config.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        monkeypatch.delenv("EDITOR", raising=False)
        monkeypatch.setenv("VISUAL", "true")
        result = runner.invoke(main, ["config", "edit"])
        assert result.exit_code == 0

    def test_config_edit_default_editor(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config edit defaults to vi when no EDITOR or VISUAL set."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        fake_config.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        monkeypatch.delenv("EDITOR", raising=False)
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.setattr("click.edit", lambda **kw: None)
        result = runner.invoke(main, ["config", "edit"])
        assert result.exit_code == 0

    def test_config_validate_valid(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config validate reports valid when config is correct."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        fake_config.write_text('{"max_corrections": 5}', encoding="utf-8")
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "validate"])
        assert result.exit_code == 0
        assert "valid" in result.output

    def test_config_validate_no_file(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config validate with no config file reports valid (defaults used)."""
        import duo.config as config_mod

        fake_config = tmp_path / "no-such-config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "validate"])
        assert result.exit_code == 0
        assert "valid" in result.output

    def test_config_validate_invalid_json(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config validate detects invalid JSON."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        fake_config.write_text("{bad json", encoding="utf-8")
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "validate"])
        assert result.exit_code == 1
        assert "Invalid JSON" in result.output

    def test_config_validate_wrong_type(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config validate detects type mismatches."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        fake_config.write_text('{"max_corrections": "not_a_number"}', encoding="utf-8")
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "validate"])
        assert result.exit_code == 1
        assert "expected number" in result.output

    def test_config_validate_unknown_key(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config validate detects unknown keys."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        fake_config.write_text('{"mystery_key": 42}', encoding="utf-8")
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "validate"])
        assert result.exit_code == 1
        assert "Unknown key" in result.output

    def test_config_validate_json_output(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config validate --json-output returns structured result."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        fake_config.write_text('{"max_corrections": "bad"}', encoding="utf-8")
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "validate", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["valid"] is False
        assert len(data["issues"]) > 0

    def test_config_validate_cross_key_invariant(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config validate detects poll_max < poll_base."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        fake_config.write_text(
            '{"poll_base_interval": 100.0, "poll_max_interval": 10.0}',
            encoding="utf-8",
        )
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "validate"])
        assert result.exit_code == 1
        assert "poll_max_interval" in result.output


class TestConfigListEdgeCases:
    def test_config_list_shows_all_keys(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config list output contains all default keys."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "list"])
        assert result.exit_code == 0
        for key in config_mod.DEFAULTS:
            assert key in result.output

    def test_config_list_shows_descriptions(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ):
        """config list text output includes inline descriptions."""
        import duo.config as config_mod

        fake_config = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
        result = runner.invoke(main, ["config", "list"])
        assert result.exit_code == 0
        assert "# " in result.output
        assert "AI model" in result.output

    def test_config_descriptions_cover_all_keys(self):
        """Every DEFAULTS key has a CONFIG_DESCRIPTIONS entry."""
        import duo.config as config_mod

        for key in config_mod.DEFAULTS:
            assert key in config_mod.CONFIG_DESCRIPTIONS, (
                f"Missing description for config key: {key}"
            )

    def test_config_key_completion(self):
        """_complete_config_keys returns matching keys with help text."""
        from duo.cli import _complete_config_keys

        items = _complete_config_keys(None, None, "poll_")  # type: ignore[arg-type]
        names = [i.value for i in items]
        assert "poll_base_interval" in names
        assert "poll_max_interval" in names
        assert "copilot_model" not in names
        assert all(i.help for i in items)

    def test_config_key_completion_empty_prefix(self):
        """Empty prefix returns all keys."""
        from duo.cli import _complete_config_keys

        items = _complete_config_keys(None, None, "")  # type: ignore[arg-type]
        assert len(items) == 12
