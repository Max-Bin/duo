"""End-to-end tests for duo think — full lifecycle with mocked panes."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from duo.cli import main
from duo.protocol import load_task


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def fake_thinking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect THINKING_DIR to a temp dir."""
    fake = tmp_path / "thinking"
    monkeypatch.setattr("duo.thinking.THINKING_DIR", fake)
    return fake


@pytest.fixture(autouse=True)
def isolated_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate TASKS_DIR so tasks don't persist across tests."""
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    monkeypatch.setattr("duo.cli.TASKS_DIR", tasks)
    monkeypatch.setattr("duo.protocol.TASKS_DIR", tasks)
    return tasks


# ---------------------------------------------------------------------------
# Happy path: full lifecycle
# ---------------------------------------------------------------------------


class TestHappyPath:
    """Complete lifecycle: create → ask → ask → finalize → list → start → close → delete."""

    def test_full_lifecycle(
        self,
        runner: CliRunner,
        fake_thinking: Path,
        isolated_tasks: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        name = "my-idea"

        # 1. First call — creates dir + CLAUDE.md + plan-template.md + pane
        with (
            patch("duo.thinking._pane_exists", return_value=False),
            patch("duo.thinking._spawn_claude_pane", return_value="%10"),
            patch("duo.thinking.write_thinking_claude_md") as mock_claude,
            patch("duo.thinking.write_plan_template") as mock_tmpl,
        ):
            result = runner.invoke(main, ["think", name])
        assert result.exit_code == 0
        assert "Thinking session: my-idea" in result.output
        mock_claude.assert_called_once_with(name)
        mock_tmpl.assert_called_once_with(name)

        # 2. --ask first question (pane now exists + alive)
        with (
            patch("duo.thinking._pane_exists", return_value=True),
            patch("duo.thinking._pane_alive", return_value=True),
            patch(
                "duo.transport.read_pane",
                side_effect=["before1", "before1\nClaude thinks X"],
            ),
            patch("duo.transport.type_text"),
            patch("duo.transport.send_keys"),
            patch("duo.thinking.wait_for_response_stable", return_value="idle"),
            patch("duo.thinking.extract_response", return_value="Claude thinks X"),
            patch("duo.thinking.append_session_log"),
        ):
            # Need CLAUDE.md to exist so ensure_pane doesn't re-write
            tdir = fake_thinking / name
            tdir.mkdir(parents=True, exist_ok=True)
            (tdir / "CLAUDE.md").write_text("test", encoding="utf-8")
            (tdir / "plan-template.md").write_text("test", encoding="utf-8")
            result = runner.invoke(main, ["think", name, "--ask", "How to build auth?"])
        assert result.exit_code == 0
        assert "Claude thinks X" in result.output

        # 3. --ask second question
        with (
            patch("duo.thinking._pane_exists", return_value=True),
            patch("duo.thinking._pane_alive", return_value=True),
            patch(
                "duo.transport.read_pane",
                side_effect=["before2", "before2\nRate limit with Redis"],
            ),
            patch("duo.transport.type_text"),
            patch("duo.transport.send_keys"),
            patch("duo.thinking.wait_for_response_stable", return_value="idle"),
            patch(
                "duo.thinking.extract_response", return_value="Rate limit with Redis"
            ),
            patch("duo.thinking.append_session_log"),
        ):
            result = runner.invoke(
                main, ["think", name, "--ask", "What about rate limiting?"]
            )
        assert result.exit_code == 0
        assert "Rate limit with Redis" in result.output

        # 4. --finalize
        plan_content = "# Plan: my-idea\n\n## Goal\nBuild auth with rate limiting."
        (tdir / "plan.md").write_text(plan_content, encoding="utf-8")
        counter = {"n": 0}

        def fake_time() -> float:
            counter["n"] += 1
            return counter["n"]

        with (
            patch("duo.thinking._pane_exists", return_value=True),
            patch("duo.thinking._pane_alive", return_value=True),
            patch("duo.thinking.wait_for_response_stable", return_value="idle"),
            patch("duo.transport.type_text"),
            patch("duo.transport.send_keys"),
            patch("time.sleep"),
            patch("time.time", side_effect=fake_time),
        ):
            result = runner.invoke(main, ["think", name, "--finalize"])
        assert result.exit_code == 0
        assert "Plan written" in result.output
        assert "--from-thinking" in result.output

        # 5. duo think list
        with (
            patch("duo.thinking._pane_exists", return_value=True),
            patch("duo.thinking._pane_alive", return_value=True),
        ):
            result = runner.invoke(main, ["think", "list"])
        assert result.exit_code == 0
        assert "my-idea" in result.output

        # 6. duo start my-idea --from-thinking
        with (
            patch("duo.cli._create_worktree") as mock_wt,
            patch("duo.commander.start_session"),
            patch("duo.scheduler.enqueue_or_start", return_value="started"),
        ):
            mock_wt.return_value = (str(tmp_path / "wt" / name), "abc123")
            result = runner.invoke(
                main,
                ["start", name, "--from-thinking", "--repo", str(tmp_path)],
            )
        assert result.exit_code == 0
        assert "Plan: loaded from thinking session" in result.output
        task = load_task(name)
        assert task is not None
        assert "Plan: my-idea" in task.description

        # 7. --close
        with (
            patch("duo.thinking.close_pane", return_value=True),
        ):
            result = runner.invoke(main, ["think", name, "--close"])
        assert result.exit_code == 0
        assert "Closed" in result.output
        assert tdir.exists()  # files preserved

        # 8. --delete
        with (
            patch("duo.thinking.close_pane", return_value=False),
        ):
            result = runner.invoke(main, ["think", name, "--delete"], input="y\n")
        assert result.exit_code == 0
        assert "Deleted" in result.output
        assert not tdir.exists()


# ---------------------------------------------------------------------------
# Edge cases: --ask
# ---------------------------------------------------------------------------


class TestAskEdgeCases:
    def test_ask_auto_creates_pane(
        self, runner: CliRunner, fake_thinking: Path
    ) -> None:
        """--ask when pane doesn't exist → auto-create."""
        with (
            patch("duo.thinking._pane_exists", return_value=False),
            patch("duo.thinking._spawn_claude_pane", return_value="%10"),
            patch("duo.transport.read_pane", side_effect=["b", "b\nReply"]),
            patch("duo.transport.type_text"),
            patch("duo.transport.send_keys"),
            patch("duo.thinking.wait_for_response_stable", return_value="idle"),
            patch("duo.thinking.extract_response", return_value="Reply"),
            patch("duo.thinking.append_session_log"),
        ):
            result = runner.invoke(main, ["think", "new-idea", "--ask", "hello?"])
        assert result.exit_code == 0
        assert "Reply" in result.output

    def test_ask_recovers_dead_pane(
        self, runner: CliRunner, fake_thinking: Path
    ) -> None:
        """--ask when pane exists but Claude exited → auto re-spawn."""
        tdir = fake_thinking / "dead-idea"
        tdir.mkdir(parents=True)
        (tdir / "CLAUDE.md").write_text("x", encoding="utf-8")
        (tdir / "plan-template.md").write_text("x", encoding="utf-8")

        with (
            patch("duo.thinking._pane_exists", return_value=True),
            patch("duo.thinking._pane_alive", return_value=False),
            patch("duo.transport.send_shell_command"),
            patch("duo.transport.wait_for_idle"),
            patch(
                "duo.transport.read_pane", side_effect=["before", "before\nRecovered"]
            ),
            patch("duo.transport.type_text"),
            patch("duo.transport.send_keys"),
            patch("duo.thinking.wait_for_response_stable", return_value="idle"),
            patch("duo.thinking.extract_response", return_value="Recovered"),
            patch("duo.thinking.append_session_log"),
        ):
            result = runner.invoke(main, ["think", "dead-idea", "--ask", "hi"])
        assert result.exit_code == 0
        assert "Recovered" in result.output

    def test_ask_dialog_response(self, runner: CliRunner, fake_thinking: Path) -> None:
        """--ask triggers a dialog → error with guidance."""
        tdir = fake_thinking / "dg"
        tdir.mkdir(parents=True)
        (tdir / "CLAUDE.md").write_text("x", encoding="utf-8")
        (tdir / "plan-template.md").write_text("x", encoding="utf-8")

        with (
            patch("duo.thinking._pane_exists", return_value=True),
            patch("duo.thinking._pane_alive", return_value=True),
            patch("duo.transport.read_pane", return_value="before"),
            patch("duo.transport.type_text"),
            patch("duo.transport.send_keys"),
            patch("duo.thinking.wait_for_response_stable", return_value="dialog"),
        ):
            result = runner.invoke(main, ["think", "dg", "--ask", "do something"])
        assert result.exit_code != 0
        assert "dialog" in result.output.lower() or "question" in result.output.lower()

    def test_ask_timeout_response(self, runner: CliRunner, fake_thinking: Path) -> None:
        """--ask timeout → error with guidance."""
        tdir = fake_thinking / "to"
        tdir.mkdir(parents=True)
        (tdir / "CLAUDE.md").write_text("x", encoding="utf-8")
        (tdir / "plan-template.md").write_text("x", encoding="utf-8")

        with (
            patch("duo.thinking._pane_exists", return_value=True),
            patch("duo.thinking._pane_alive", return_value=True),
            patch("duo.transport.read_pane", return_value="before"),
            patch("duo.transport.type_text"),
            patch("duo.transport.send_keys"),
            patch("duo.thinking.wait_for_response_stable", return_value="timeout"),
        ):
            result = runner.invoke(main, ["think", "to", "--ask", "hello"])
        assert result.exit_code != 0
        assert (
            "timeout" in result.output.lower()
            or "not responding" in result.output.lower()
        )


# ---------------------------------------------------------------------------
# Edge cases: --finalize
# ---------------------------------------------------------------------------


class TestFinalizeEdgeCases:
    def test_finalize_dialog_abort(
        self, runner: CliRunner, fake_thinking: Path
    ) -> None:
        """--finalize when pane is in dialog → abort with clear error."""
        tdir = fake_thinking / "fda"
        tdir.mkdir(parents=True)
        (tdir / "CLAUDE.md").write_text("x", encoding="utf-8")
        (tdir / "plan-template.md").write_text("x", encoding="utf-8")

        with (
            patch("duo.thinking._pane_exists", return_value=True),
            patch("duo.thinking._pane_alive", return_value=True),
            patch("duo.thinking.wait_for_response_stable", return_value="dialog"),
        ):
            result = runner.invoke(main, ["think", "fda", "--finalize"])
        assert result.exit_code != 0
        assert "dialog" in result.output.lower()

    def test_finalize_pane_unresponsive(
        self, runner: CliRunner, fake_thinking: Path
    ) -> None:
        """--finalize when pane doesn't respond → timeout error."""
        tdir = fake_thinking / "fur"
        tdir.mkdir(parents=True)
        (tdir / "CLAUDE.md").write_text("x", encoding="utf-8")
        (tdir / "plan-template.md").write_text("x", encoding="utf-8")

        with (
            patch("duo.thinking._pane_exists", return_value=True),
            patch("duo.thinking._pane_alive", return_value=True),
            patch("duo.thinking.wait_for_response_stable", return_value="timeout"),
        ):
            result = runner.invoke(main, ["think", "fur", "--finalize"])
        assert result.exit_code != 0
        assert "unresponsive" in result.output.lower() or "Pane" in result.output

    def test_finalize_no_plan_md_timeout(
        self, runner: CliRunner, fake_thinking: Path
    ) -> None:
        """--finalize succeeds to send but plan.md never appears → timeout."""
        tdir = fake_thinking / "npl"
        tdir.mkdir(parents=True)
        (tdir / "CLAUDE.md").write_text("x", encoding="utf-8")
        (tdir / "plan-template.md").write_text("x", encoding="utf-8")

        counter = {"n": 0}

        def fake_time() -> float:
            counter["n"] += 1
            return counter["n"] * 100  # immediately past deadline

        with (
            patch("duo.thinking._pane_exists", return_value=True),
            patch("duo.thinking._pane_alive", return_value=True),
            patch("duo.thinking.wait_for_response_stable", return_value="idle"),
            patch("duo.transport.type_text"),
            patch("duo.transport.send_keys"),
            patch("time.sleep"),
            patch("time.time", side_effect=fake_time),
        ):
            result = runner.invoke(main, ["think", "npl", "--finalize"])
        assert result.exit_code != 0
        assert "plan.md" in result.output


# ---------------------------------------------------------------------------
# wait_for_response_stable state transitions
# ---------------------------------------------------------------------------


class TestWaitForResponseStable:
    """Verify state-machine behavior of wait_for_response_stable."""

    def test_spinner_prevents_idle(self) -> None:
        """Spinner markers mean still working, not idle."""
        from duo.thinking import wait_for_response_stable
        from duo.transport import DialogKind

        call_count = {"n": 0}

        def fake_read_pane(label: str, lines: int) -> str:
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return "◉ Working...\n❯"
            return "Done\n❯"

        with (
            patch("duo.transport.read_pane", side_effect=fake_read_pane),
            patch("duo.transport._detect_dialog_kind", return_value=DialogKind.NONE),
            patch("duo.transport._is_at_main_prompt", return_value=True),
            patch("time.time", side_effect=[0, 0.5, 1, 1.5, 2, 2.5, 3, 5, 10]),
            patch("time.sleep"),
        ):
            result = wait_for_response_stable("test", timeout=30, stable_threshold=2.0)
        assert result == "idle"

    def test_dialog_detected_immediately(self) -> None:
        """Dialog detection returns immediately."""
        from duo.thinking import wait_for_response_stable
        from duo.transport import DialogKind

        with (
            patch(
                "duo.transport.read_pane", return_value="╭─ dialog ─╮\n1. Yes\n2. No"
            ),
            patch("duo.transport._detect_dialog_kind", return_value=DialogKind.OPTION),
            patch("time.time", side_effect=[0, 0.5]),
        ):
            result = wait_for_response_stable("test", timeout=30)
        assert result == "dialog"

    def test_hash_change_resets_stability(self) -> None:
        """Content changing resets the stable_since timer."""
        from duo.thinking import wait_for_response_stable
        from duo.transport import DialogKind

        read_count = {"n": 0}

        def fake_read(label: str, lines: int) -> str:
            read_count["n"] += 1
            if read_count["n"] <= 2:
                return f"content-{read_count['n']}\n❯"
            return "stable-content\n❯"

        with (
            patch("duo.transport.read_pane", side_effect=fake_read),
            patch("duo.transport._detect_dialog_kind", return_value=DialogKind.NONE),
            patch("duo.transport._is_at_main_prompt", return_value=True),
            patch("time.time", side_effect=[0, 0.5, 1, 1.5, 2, 2.5, 3, 5, 10]),
            patch("time.sleep"),
        ):
            result = wait_for_response_stable("test", timeout=30, stable_threshold=2.0)
        assert result == "idle"


# ---------------------------------------------------------------------------
# extract_response with tool calls
# ---------------------------------------------------------------------------


class TestExtractResponseToolCalls:
    """Test extract_response with real-world-like output containing tool calls."""

    def test_response_with_tool_reads(self) -> None:
        from duo.thinking import extract_response

        before = "❯ previous output"
        after = (
            "❯ previous output\n"
            "How to build auth?\n"
            "\n"
            "● Read src/auth.py\n"
            "● Read src/config.py\n"
            "\n"
            "Based on reading the code, I recommend:\n"
            "1. Use JWT tokens\n"
            "2. Add rate limiting middleware\n"
            "\n"
            "❯"
        )
        result = extract_response(before, after, "How to build auth?")
        assert "JWT tokens" in result
        assert "rate limiting" in result
        assert "● Read" not in result
        assert "How to build auth?" not in result

    def test_response_with_edits_and_bash(self) -> None:
        from duo.thinking import extract_response

        before = "❯"
        after = (
            "❯\n"
            "● Edit src/main.py\n"
            "● Bash npm install\n"
            "Here is what I did:\n"
            "- Added the handler\n"
            "- Installed deps\n"
            ">"
        )
        result = extract_response(before, after, "do the thing")
        assert "handler" in result
        assert "● Edit" not in result
        assert "● Bash" not in result

    def test_empty_response(self) -> None:
        from duo.thinking import extract_response

        before = "line1\nline2"
        after = "line1\nline2"
        result = extract_response(before, after, "q")
        assert result == ""

    def test_response_with_grep(self) -> None:
        from duo.thinking import extract_response

        before = "❯"
        after = "❯\n● Grep auth\nFound these patterns in your codebase.\n❯"
        result = extract_response(before, after, "find auth")
        assert "Found these patterns" in result
        assert "● Grep" not in result


# ---------------------------------------------------------------------------
# Concurrency and races
# ---------------------------------------------------------------------------


class TestConcurrencyEdgeCases:
    def test_close_already_dead_pane(
        self, runner: CliRunner, fake_thinking: Path
    ) -> None:
        """--close when pane is already dead → silent success."""
        tdir = fake_thinking / "dead-app"
        tdir.mkdir(parents=True)
        (tdir / "CLAUDE.md").write_text("x", encoding="utf-8")

        with patch("duo.thinking.close_pane", return_value=False):
            result = runner.invoke(main, ["think", "dead-app", "--close"])
        assert result.exit_code == 0
        assert "No active pane" in result.output

    def test_delete_nonexistent_session(
        self, runner: CliRunner, fake_thinking: Path
    ) -> None:
        """--delete for nonexistent session → clear error."""
        with patch("duo.thinking.thinking_dir", return_value=fake_thinking / "ghost"):
            result = runner.invoke(main, ["think", "ghost", "--delete"])
        assert result.exit_code != 0
        assert "No thinking session" in result.output

    def test_close_nonexistent_session(
        self, runner: CliRunner, fake_thinking: Path
    ) -> None:
        """--close for nonexistent session → clear error."""
        with patch("duo.thinking.thinking_dir", return_value=fake_thinking / "noexist"):
            result = runner.invoke(main, ["think", "noexist", "--close"])
        assert result.exit_code != 0
        assert "No thinking session" in result.output


# ---------------------------------------------------------------------------
# duo think list
# ---------------------------------------------------------------------------


class TestThinkList:
    def test_list_empty(self, runner: CliRunner, fake_thinking: Path) -> None:
        result = runner.invoke(main, ["think", "list"])
        assert result.exit_code == 0
        assert "No thinking sessions" in result.output

    def test_list_multiple_sessions(
        self, runner: CliRunner, fake_thinking: Path
    ) -> None:
        """List shows all sessions with correct status."""
        # Create two sessions
        for nm in ("alpha", "beta"):
            d = fake_thinking / nm
            d.mkdir(parents=True)
            (d / "CLAUDE.md").write_text("x", encoding="utf-8")
        # beta has plan
        (fake_thinking / "beta" / "plan.md").write_text("# Plan", encoding="utf-8")

        with patch("duo.thinking._pane_exists", return_value=False):
            result = runner.invoke(main, ["think", "list"])
        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "beta" in result.output


# ---------------------------------------------------------------------------
# --from-thinking edge cases
# ---------------------------------------------------------------------------


class TestFromThinkingEdgeCases:
    def test_from_thinking_no_plan(
        self, runner: CliRunner, fake_thinking: Path, tmp_path: Path
    ) -> None:
        """--from-thinking without plan.md → clear error."""
        result = runner.invoke(
            main,
            ["start", "no-plan", "--from-thinking", "--repo", str(tmp_path)],
        )
        assert result.exit_code != 0
        assert "No plan.md" in result.output

    def test_from_thinking_empty_plan(
        self, runner: CliRunner, fake_thinking: Path, tmp_path: Path
    ) -> None:
        """--from-thinking with empty plan.md → clear error."""
        tdir = fake_thinking / "empty-plan"
        tdir.mkdir(parents=True)
        (tdir / "plan.md").write_text("   \n  \n", encoding="utf-8")

        result = runner.invoke(
            main,
            ["start", "empty-plan", "--from-thinking", "--repo", str(tmp_path)],
        )
        assert result.exit_code != 0
        assert "empty" in result.output.lower()

    def test_from_thinking_with_desc_override(
        self, runner: CliRunner, fake_thinking: Path, tmp_path: Path
    ) -> None:
        """--from-thinking overrides --desc when both given."""
        tdir = fake_thinking / "override-test"
        tdir.mkdir(parents=True)
        (tdir / "plan.md").write_text("# From thinking plan", encoding="utf-8")

        with (
            patch("duo.cli._create_worktree") as mock_wt,
            patch("duo.commander.start_session"),
            patch("duo.scheduler.enqueue_or_start", return_value="started"),
        ):
            mock_wt.return_value = (str(tmp_path / "wt" / "override-test"), "abc123")
            result = runner.invoke(
                main,
                [
                    "start",
                    "override-test",
                    "--from-thinking",
                    "--desc",
                    "manual desc",
                    "--repo",
                    str(tmp_path),
                ],
            )
        assert result.exit_code == 0
        task = load_task("override-test")
        assert task is not None
        # --from-thinking takes priority
        assert "From thinking plan" in task.description


# ---------------------------------------------------------------------------
# Session log
# ---------------------------------------------------------------------------


class TestSessionLogE2E:
    def test_ask_appends_session_log(
        self, runner: CliRunner, fake_thinking: Path
    ) -> None:
        """--ask writes to session.log."""
        tdir = fake_thinking / "log-test"
        tdir.mkdir(parents=True)
        (tdir / "CLAUDE.md").write_text("x", encoding="utf-8")
        (tdir / "plan-template.md").write_text("x", encoding="utf-8")

        with (
            patch("duo.thinking._pane_exists", return_value=True),
            patch("duo.thinking._pane_alive", return_value=True),
            patch("duo.transport.read_pane", side_effect=["before", "before\nAnswer"]),
            patch("duo.transport.type_text"),
            patch("duo.transport.send_keys"),
            patch("duo.thinking.wait_for_response_stable", return_value="idle"),
            patch("duo.thinking.extract_response", return_value="Answer"),
        ):
            # Don't mock append_session_log — let it run
            result = runner.invoke(main, ["think", "log-test", "--ask", "question?"])
        assert result.exit_code == 0

        log_file = tdir / "session.log"
        assert log_file.exists()
        log_content = log_file.read_text(encoding="utf-8")
        assert "[user] question?" in log_content
        assert "Answer" in log_content
