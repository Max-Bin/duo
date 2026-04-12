"""CLI tests for think commands."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import duo.cli
import duo.protocol
from duo.cli import main


@pytest.fixture(autouse=True)
def isolated_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect TASKS_DIR and DUO_DIR to a temporary directory."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    monkeypatch.setattr(duo.protocol, "TASKS_DIR", tasks_dir)
    monkeypatch.setattr(duo.protocol, "_CORRUPTED_DIR", tasks_dir / "_corrupted")
    monkeypatch.setattr(duo.protocol, "DUO_DIR", tmp_path)
    monkeypatch.setattr(duo.cli, "TASKS_DIR", tasks_dir)
    monkeypatch.setattr(duo.cli, "DUO_DIR", tmp_path)
    return tasks_dir


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestThinkInfo:
    """Tests for ``duo think <name>`` (info mode)."""

    def test_info_mode(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        tdir = fake_thinking / "my-app"
        tdir.mkdir(parents=True)
        with (
            patch("duo.thinking.ensure_pane", return_value="think-my-app"),
            patch("duo.thinking.thinking_dir", return_value=tdir),
        ):
            result = runner.invoke(main, ["think", "my-app"])
        assert result.exit_code == 0
        assert "Thinking session: my-app" in result.output
        assert "think-my-app" in result.output

    def test_no_name_error(self, runner: CliRunner, isolated_tasks: Path) -> None:
        result = runner.invoke(main, ["think"])
        assert result.exit_code != 0


class TestThinkAsk:
    """Tests for ``duo think <name> --ask``."""

    def test_ask_happy_path(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        with (
            patch("duo.thinking.ensure_pane", return_value="think-x"),
            patch(
                "duo.transport.read_pane",
                side_effect=["before", "after\nClaude says hello"],
            ),
            patch("duo.transport.type_text"),
            patch("duo.transport.send_keys"),
            patch("duo.thinking.wait_for_response_stable", return_value="idle"),
            patch("duo.thinking.extract_response", return_value="Claude says hello"),
            patch("duo.thinking.append_session_log"),
        ):
            result = runner.invoke(main, ["think", "x", "--ask", "hello"])
        assert result.exit_code == 0
        assert "Claude says hello" in result.output

    def test_ask_dialog_response(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        with (
            patch("duo.thinking.ensure_pane", return_value="think-x"),
            patch("duo.transport.read_pane", return_value="before"),
            patch("duo.transport.type_text"),
            patch("duo.transport.send_keys"),
            patch("duo.thinking.wait_for_response_stable", return_value="dialog"),
        ):
            result = runner.invoke(main, ["think", "x", "--ask", "q"])
        assert result.exit_code != 0
        assert "dialog" in result.output.lower() or "question" in result.output.lower()

    def test_ask_timeout(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        with (
            patch("duo.thinking.ensure_pane", return_value="think-x"),
            patch("duo.transport.read_pane", return_value="before"),
            patch("duo.transport.type_text"),
            patch("duo.transport.send_keys"),
            patch("duo.thinking.wait_for_response_stable", return_value="timeout"),
        ):
            result = runner.invoke(main, ["think", "x", "--ask", "q"])
        assert result.exit_code != 0
        assert (
            "not responding" in result.output.lower()
            or "timeout" in result.output.lower()
        )


class TestThinkFinalize:
    """Tests for ``duo think <name> --finalize``."""

    def test_finalize_success(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        tdir = fake_thinking / "my-app"
        tdir.mkdir(parents=True)
        plan_path = tdir / "plan.md"

        # Pre-write plan.md so the poll finds it immediately
        plan_path.write_text("# Plan: my-app\n## Goal\nBuild it.", encoding="utf-8")

        counter = {"n": 0}

        def fake_time() -> float:
            counter["n"] += 1
            return counter["n"]

        with (
            patch("duo.thinking.ensure_pane", return_value="think-my-app"),
            patch("duo.thinking.thinking_dir", return_value=tdir),
            patch("duo.thinking.wait_for_response_stable", return_value="idle"),
            patch("duo.transport.type_text"),
            patch("duo.transport.send_keys"),
            patch("time.sleep"),
            patch("time.time", side_effect=fake_time),
        ):
            result = runner.invoke(main, ["think", "my-app", "--finalize"])
        assert result.exit_code == 0
        assert "Plan written" in result.output

    def test_finalize_pane_in_dialog(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        with (
            patch("duo.thinking.ensure_pane", return_value="think-x"),
            patch("duo.thinking.thinking_dir", return_value=fake_thinking / "x"),
            patch("duo.thinking.wait_for_response_stable", return_value="dialog"),
        ):
            result = runner.invoke(main, ["think", "x", "--finalize"])
        assert result.exit_code != 0
        assert "dialog" in result.output.lower()

    def test_finalize_pane_unresponsive(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        with (
            patch("duo.thinking.ensure_pane", return_value="think-x"),
            patch("duo.thinking.thinking_dir", return_value=fake_thinking / "x"),
            patch("duo.thinking.wait_for_response_stable", return_value="timeout"),
        ):
            result = runner.invoke(main, ["think", "x", "--finalize"])
        assert result.exit_code != 0
        assert "unresponsive" in result.output.lower() or "Pane" in result.output

    def test_finalize_timeout_no_plan(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Finalize times out when Claude doesn't produce plan.md."""
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        tdir = fake_thinking / "my-app"
        tdir.mkdir(parents=True)
        # No plan.md written — deadline exceeded immediately

        counter = {"n": 0}

        def fake_time() -> float:
            counter["n"] += 1
            return counter["n"] * 100  # jumps past deadline on first loop check

        with (
            patch("duo.thinking.ensure_pane", return_value="think-my-app"),
            patch("duo.thinking.thinking_dir", return_value=tdir),
            patch("duo.thinking.wait_for_response_stable", return_value="idle"),
            patch("duo.transport.type_text"),
            patch("duo.transport.send_keys"),
            patch("time.sleep"),
            patch("time.time", side_effect=fake_time),
        ):
            result = runner.invoke(main, ["think", "my-app", "--finalize"])
        assert result.exit_code != 0
        assert "plan.md" in result.output


class TestThinkClose:
    """Tests for ``duo think <name> --close``."""

    def test_close_existing(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        tdir = fake_thinking / "my-app"
        tdir.mkdir(parents=True)
        (tdir / "CLAUDE.md").write_text("test", encoding="utf-8")
        with (
            patch("duo.thinking.close_pane", return_value=True),
            patch("duo.thinking.thinking_dir", return_value=tdir),
        ):
            result = runner.invoke(main, ["think", "my-app", "--close"])
        assert result.exit_code == 0
        assert "Closed" in result.output

    def test_close_no_active_pane(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Close when session dir exists but pane is not active."""
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        tdir = fake_thinking / "my-app"
        tdir.mkdir(parents=True)
        (tdir / "CLAUDE.md").write_text("test", encoding="utf-8")
        with (
            patch("duo.thinking.close_pane", return_value=False),
            patch("duo.thinking.thinking_dir", return_value=tdir),
        ):
            result = runner.invoke(main, ["think", "my-app", "--close"])
        assert result.exit_code == 0
        assert "No active pane" in result.output

    def test_close_no_session(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        with patch("duo.thinking.thinking_dir", return_value=fake_thinking / "nope"):
            result = runner.invoke(main, ["think", "nope", "--close"])
        assert result.exit_code != 0


class TestThinkDelete:
    """Tests for ``duo think <name> --delete``."""

    def test_delete_confirmed(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        tdir = fake_thinking / "my-app"
        tdir.mkdir(parents=True)
        (tdir / "CLAUDE.md").write_text("test", encoding="utf-8")
        with (
            patch("duo.thinking.close_pane", return_value=True),
            patch("duo.thinking.thinking_dir", return_value=tdir),
        ):
            result = runner.invoke(main, ["think", "my-app", "--delete"], input="y\n")
        assert result.exit_code == 0
        assert "Deleted" in result.output
        assert not tdir.exists()

    def test_delete_aborted(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        tdir = fake_thinking / "my-app"
        tdir.mkdir(parents=True)
        (tdir / "CLAUDE.md").write_text("test", encoding="utf-8")
        with patch("duo.thinking.thinking_dir", return_value=tdir):
            result = runner.invoke(main, ["think", "my-app", "--delete"], input="n\n")
        assert result.exit_code == 0
        assert "Aborted" in result.output
        assert tdir.exists()

    def test_delete_no_session(
        self, runner: CliRunner, isolated_tasks: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_thinking = isolated_tasks.parent / "thinking"
        monkeypatch.setattr("duo.thinking.THINKING_DIR", fake_thinking)
        with patch("duo.thinking.thinking_dir", return_value=fake_thinking / "nope"):
            result = runner.invoke(main, ["think", "nope", "--delete"], input="y\n")
        assert result.exit_code != 0


class TestThinkList:
    """Tests for ``duo think list``."""

    def test_empty_list(self, runner: CliRunner, isolated_tasks: Path) -> None:
        with patch("duo.thinking.list_sessions", return_value=[]):
            result = runner.invoke(main, ["think", "list"])
        assert result.exit_code == 0
        assert "No thinking sessions" in result.output

    def test_with_sessions(self, runner: CliRunner, isolated_tasks: Path) -> None:
        sessions = [
            {
                "name": "alpha",
                "pane": "alive",
                "status": "active",
                "files": "CLAUDE.md",
            },
            {
                "name": "beta",
                "pane": "none",
                "status": "finalized",
                "files": "CLAUDE.md plan.md",
            },
        ]
        with patch("duo.thinking.list_sessions", return_value=sessions):
            result = runner.invoke(main, ["think", "list"])
        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "beta" in result.output
        assert "finalized" in result.output


# ── Bench command tests ───────────────────────────────────────────────
