"""CLI tests for events commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from duo.cli import main


class TestEventsCommand:
    """Tests for duo events subcommands."""

    def test_events_list_empty(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "duo.cli.events_cmd._WATCH_EVENTS_DIR", tmp_path / "no-events"
        )
        result = runner.invoke(main, ["events", "list"])
        assert result.exit_code == 0
        assert "No events" in result.output

    def test_events_list_with_files(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        from duo.protocol import write_json

        write_json(
            edir / "task1-2026-04-08.json",
            {"task_id": "task1", "detected_at": "2026-04-08T10:00:00Z"},
        )
        result = runner.invoke(main, ["events", "list"])
        assert result.exit_code == 0
        assert "task1" in result.output
        # Regression: event line should appear exactly once (not duplicated)
        lines = [ln for ln in result.output.strip().splitlines() if ln.strip()]
        assert len(lines) == 1

    def test_events_list_quiet(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        from duo.protocol import write_json

        write_json(
            edir / "e1.json",
            {"task_id": "t1", "detected_at": "2026-04-08T10:00:00Z"},
        )
        result = runner.invoke(main, ["events", "list", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "e1.json"

    def test_events_list_quiet_empty(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "duo.cli.events_cmd._WATCH_EVENTS_DIR", tmp_path / "no-events"
        )
        result = runner.invoke(main, ["events", "list", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_events_list_quiet_dir_no_files(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        result = runner.invoke(main, ["events", "list", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_events_list_count(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        from duo.protocol import write_json

        write_json(edir / "e1.json", {"task_id": "t1", "detected_at": "x"})
        write_json(edir / "e2.json", {"task_id": "t2", "detected_at": "x"})
        result = runner.invoke(main, ["events", "list", "-c"])
        assert result.exit_code == 0
        assert result.output.strip() == "2"

    def test_events_list_count_empty(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "duo.cli.events_cmd._WATCH_EVENTS_DIR", tmp_path / "no-events"
        )
        result = runner.invoke(main, ["events", "list", "-c"])
        assert result.exit_code == 0
        assert result.output.strip() == "0"

    def test_events_list_count_dir_no_files(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        result = runner.invoke(main, ["events", "list", "-c"])
        assert result.exit_code == 0
        assert result.output.strip() == "0"

    def test_events_show_latest(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        from duo.protocol import write_json

        write_json(
            edir / "a-ev.json", {"task_id": "t1", "detected_at": "2026-04-08T09:00:00Z"}
        )
        write_json(
            edir / "b-ev.json", {"task_id": "t2", "detected_at": "2026-04-08T10:00:00Z"}
        )
        result = runner.invoke(main, ["events", "show"])
        assert result.exit_code == 0
        assert "t2" in result.output

    def test_events_show_by_name(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        from duo.protocol import write_json

        write_json(
            edir / "my-event.json",
            {"task_id": "x", "detected_at": "2026-04-08T10:00:00Z"},
        )
        result = runner.invoke(main, ["events", "show", "my-event"])
        assert result.exit_code == 0
        assert '"task_id"' in result.output

    def test_events_show_exact_name_match(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Event file that matches the exact name (no .json fallback needed)."""
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        (edir / "exact-event").write_text(
            json.dumps({"task_id": "exact", "detected_at": "2026-04-08T10:00:00Z"})
        )
        result = runner.invoke(main, ["events", "show", "exact-event"])
        assert result.exit_code == 0
        assert "exact" in result.output

    def test_events_show_not_found(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        result = runner.invoke(main, ["events", "show", "nonexistent"])
        assert result.exit_code != 0

    def test_events_show_no_dir(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", tmp_path / "nope")
        result = runner.invoke(main, ["events", "show"])
        assert result.exit_code != 0

    def test_events_show_path_traversal(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Path traversal in event name is rejected."""
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        result = runner.invoke(main, ["events", "show", "../../../etc/passwd"])
        assert result.exit_code != 0

    def test_events_show_corrupt_file(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Corrupt event file shows error."""
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        (edir / "bad-event.json").write_text("{corrupt json!!!")
        result = runner.invoke(main, ["events", "show", "bad-event"])
        assert result.exit_code != 0
        assert "Invalid event file" in result.output or "corrupted" in result.output

    def test_events_clear_empty(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "duo.cli.events_cmd._WATCH_EVENTS_DIR", tmp_path / "no-events"
        )
        result = runner.invoke(main, ["events", "clear"])
        assert result.exit_code == 0
        assert "No events" in result.output

    def test_events_clear_force(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        from duo.protocol import write_json

        write_json(edir / "ev1.json", {"task_id": "t"})
        write_json(edir / "ev2.json", {"task_id": "t"})
        result = runner.invoke(main, ["events", "clear", "--force"])
        assert result.exit_code == 0
        assert "Cleared 2" in result.output
        assert not list(edir.glob("*.json"))

    def test_events_clear_confirm_abort(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        from duo.protocol import write_json

        write_json(edir / "ev.json", {"task_id": "t"})
        result = runner.invoke(main, ["events", "clear"], input="n\n")
        assert result.exit_code != 0  # aborted

    def test_events_tail_initial(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Tail shows initial events then gets interrupted."""
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        from duo.protocol import write_json

        write_json(
            edir / "ev.json",
            {"task_id": "tail-test", "detected_at": "2026-04-08T10:00:00Z"},
        )
        # Simulate KeyboardInterrupt on first sleep
        import time

        orig_sleep = time.sleep
        call_count = 0

        def fake_sleep(secs: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                raise KeyboardInterrupt
            orig_sleep(secs)

        monkeypatch.setattr("time.sleep", fake_sleep)
        result = runner.invoke(main, ["events", "tail"])
        assert "tail-test" in result.output
        assert "following" in result.output

    def test_events_list_dir_exists_no_files(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        result = runner.invoke(main, ["events", "list"])
        assert result.exit_code == 0
        assert "No events" in result.output

    def test_events_list_bad_json_skipped(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        (edir / "bad.json").write_text("{{{invalid")
        result = runner.invoke(main, ["events", "list"])
        assert result.exit_code == 0

    def test_events_show_latest_empty_dir(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        result = runner.invoke(main, ["events", "show"])
        assert result.exit_code != 0
        assert "No events" in result.output

    def test_events_show_invalid_json(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        (edir / "bad.json").write_text("{{{nope")
        result = runner.invoke(main, ["events", "show", "bad.json"])
        assert result.exit_code != 0
        assert "Invalid" in result.output

    def test_events_clear_dir_exists_no_files(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        result = runner.invoke(main, ["events", "clear"])
        assert result.exit_code == 0
        assert "No events" in result.output

    def test_events_tail_new_event_in_loop(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Tail picks up a new event added during the loop."""
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        from duo.protocol import write_json

        call_count = 0

        def fake_sleep(secs: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Simulate new event appearing during poll
                write_json(
                    edir / "new-ev.json",
                    {"task_id": "new-task", "detected_at": "2026-04-08T11:00:00Z"},
                )
            elif call_count >= 2:
                raise KeyboardInterrupt

        monkeypatch.setattr("time.sleep", fake_sleep)
        result = runner.invoke(main, ["events", "tail"])
        assert "new-task" in result.output

    def test_events_list_json_empty(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--json-output returns empty list when no events dir."""
        monkeypatch.setattr(
            "duo.cli.events_cmd._WATCH_EVENTS_DIR", tmp_path / "no-events"
        )
        result = runner.invoke(main, ["events", "list", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["events"] == []
        assert data["total"] == 0

    def test_events_list_json_with_files(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--json-output returns event data."""
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        from duo.protocol import write_json

        write_json(
            edir / "t1-2026.json",
            {"task_id": "t1", "detected_at": "2026-04-08T10:00:00Z"},
        )
        result = runner.invoke(main, ["events", "list", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["events"]) == 1
        assert data["events"][0]["task_id"] == "t1"
        assert data["total"] == 1

    def test_events_list_json_dir_no_files(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--json-output returns empty when dir exists but no .json files."""
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        result = runner.invoke(main, ["events", "list", "--json-output"])
        data = json.loads(result.output)
        assert data["events"] == []

    def test_events_clear_json_empty(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--json-output clear returns zero when no events."""
        monkeypatch.setattr(
            "duo.cli.events_cmd._WATCH_EVENTS_DIR", tmp_path / "no-events"
        )
        result = runner.invoke(main, ["events", "clear", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["cleared"] == 0

    def test_events_clear_json_with_files(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--json-output clear returns count of cleared files."""
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        from duo.protocol import write_json

        write_json(edir / "e1.json", {"task_id": "t1"})
        write_json(edir / "e2.json", {"task_id": "t2"})
        result = runner.invoke(main, ["events", "clear", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["cleared"] == 2

    def test_events_clear_json_dir_no_files(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--json-output clear when dir exists but empty."""
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        result = runner.invoke(main, ["events", "clear", "--json-output"])
        data = json.loads(result.output)
        assert data["cleared"] == 0

    def test_events_clear_quiet_no_dir(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "duo.cli.events_cmd._WATCH_EVENTS_DIR", tmp_path / "no-events"
        )
        result = runner.invoke(main, ["events", "clear", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "0"

    def test_events_clear_quiet_empty_dir(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        result = runner.invoke(main, ["events", "clear", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "0"

    def test_events_clear_quiet_with_files(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        edir = tmp_path / "watch-events"
        edir.mkdir()
        monkeypatch.setattr("duo.cli.events_cmd._WATCH_EVENTS_DIR", edir)
        from duo.protocol import write_json

        write_json(edir / "ev1.json", {"task_id": "t1"})
        write_json(edir / "ev2.json", {"task_id": "t2"})
        result = runner.invoke(main, ["events", "clear", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "2"
        assert not list(edir.glob("*.json"))


# ---------------------------------------------------------------------------
# CEO Workflow command tests
# ---------------------------------------------------------------------------
