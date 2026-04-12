"""Edge case tests for duo.cli — unicode names, zero ages, corrupted tasks, budget, dispatch."""

from __future__ import annotations

import json
import string
from collections.abc import Callable
from pathlib import Path

import click
import pytest
from click.testing import CliRunner
from hypothesis import given
from hypothesis import strategies as st

import duo.cli
import duo.protocol
from duo.cli import (
    _parse_age,
    _safe_join,
    _validate_task_name,
    main,
)
from duo.errors import DuoUserError
from duo.protocol import (
    Subtask,
    Task,
    create_task,
    save_task,
)

# ---------------------------------------------------------------------------
# Fixtures (same as test_cli.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def _make_task(task_id: str = "test-task", description: str = "Test task") -> Task:
    """Create a task in the isolated TASKS_DIR and return it."""
    return create_task(
        task_id=task_id,
        description=description,
        worktree="/fake/worktree",
        branch=f"duo/{task_id}",
        base_commit="abc123",
        subtasks=[
            Subtask(
                step_id=1,
                description=description,
                target_files=[],
                writable_paths=["*"],
            )
        ],
    )


@pytest.fixture
def make_task() -> Callable[..., Task]:
    """Fixture wrapper around _make_task for use in test classes."""
    return _make_task


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestValidateTaskNameUnicode:
    """Edge case tests for _validate_task_name with unicode characters."""

    def test_emoji_rejected(self) -> None:
        """Unicode emoji in task name is rejected."""
        with pytest.raises(click.BadParameter):
            _validate_task_name("task-\U0001f680")

    def test_cjk_characters_rejected(self) -> None:
        """CJK characters in task name are rejected."""
        with pytest.raises(click.BadParameter):
            _validate_task_name("\u4efb\u52a1\u540d")

    def test_accented_characters_rejected(self) -> None:
        """Accented characters in task name are rejected."""
        with pytest.raises(click.BadParameter):
            _validate_task_name("caf\u00e9")

    def test_mixed_ascii_and_unicode_rejected(self) -> None:
        """Mixed ASCII + unicode in task name is rejected."""
        with pytest.raises(click.BadParameter):
            _validate_task_name("task-\u00fc-name")


class TestParseAgeZeroValues:
    """Edge case tests for _parse_age with zero values."""

    def test_zero_hours_rejected(self) -> None:
        """_parse_age rejects 0h (zero hours)."""
        with pytest.raises(click.UsageError, match="must be > 0"):
            _parse_age("0h")

    def test_zero_days_rejected(self) -> None:
        """_parse_age rejects 0d (zero days)."""
        with pytest.raises(click.UsageError, match="must be > 0"):
            _parse_age("0d")

    def test_zero_minutes_rejected(self) -> None:
        """_parse_age rejects 0m (zero minutes)."""
        with pytest.raises(click.UsageError, match="must be > 0"):
            _parse_age("0m")

    def test_zero_seconds_rejected(self) -> None:
        """_parse_age rejects 0s (zero seconds)."""
        with pytest.raises(click.UsageError, match="must be > 0"):
            _parse_age("0s")


class TestLoadTaskOrFailCorrupted:
    """Edge case tests for _load_task_or_fail with corrupted tasks."""

    def test_corrupted_status_raises(self, make_task: Callable[..., Task]) -> None:
        """_load_task_or_fail raises when task has invalid status in JSON."""
        from duo.cli import _load_task_or_fail

        task = make_task("corrupt-status")
        data = duo.protocol.read_json(task.dir / "task.json")
        assert data is not None
        data["status"] = "bogus_status"
        (task.dir / "task.json").write_text(json.dumps(data, indent=2))
        with pytest.raises(DuoUserError, match="not found"):
            _load_task_or_fail("corrupt-status")


class TestCostBudgetZeroEdgeCases:
    """Edge case tests for cost command with --budget 0."""

    def test_cost_budget_zero_no_tasks(self, runner: CliRunner) -> None:
        """cost --budget 0 with no tasks should exit 0 (0 <= 0)."""
        result = runner.invoke(main, ["cost", "--budget", "0"])
        assert result.exit_code == 0

    def test_cost_budget_zero_no_pr_events(
        self,
        runner: CliRunner,
        make_task: Callable[..., Task],
    ) -> None:
        """cost --budget 0 with task but 0 PR events should exit 0."""
        task = make_task("budget-zero-clean")
        save_task(task)
        result = runner.invoke(main, ["cost", "--budget", "0"])
        assert result.exit_code == 0


class TestSafeJoinPropertyBased:
    """Hypothesis property test for _safe_join with random path components."""

    @given(
        st.text(
            alphabet=string.ascii_letters + string.digits + "_-",
            min_size=1,
            max_size=20,
        )
    )
    def test_safe_join_random_components_no_traversal(self, name: str) -> None:
        """_safe_join with safe characters never raises."""
        result = _safe_join("/base/path", name)
        assert "/base/path" in result

    @given(
        st.text(
            alphabet=string.ascii_letters + string.digits + "_-",
            min_size=1,
            max_size=20,
        )
    )
    def test_safe_join_result_is_under_base(self, name: str) -> None:
        """_safe_join result always stays under the base directory."""
        result = _safe_join("/base/path", name)
        resolved_base = str(Path("/base/path").resolve())
        assert result.startswith(resolved_base)


class TestCommandSectionsComplete:
    """Every Click command in 'main' must be listed in _COMMAND_SECTIONS."""

    def test_all_commands_in_sections(self) -> None:
        """No command should fall through to the 'Other' catch-all section."""
        from duo.cli import _COMMAND_SECTIONS

        sectioned = {name for names in _COMMAND_SECTIONS.values() for name in names}
        ctx = click.Context(main)
        registered = set(main.list_commands(ctx))
        orphaned = registered - sectioned
        assert orphaned == set(), f"Commands not in _COMMAND_SECTIONS: {orphaned}"

    def test_no_phantom_section_entries(self) -> None:
        """Every name in _COMMAND_SECTIONS must be a real registered command."""
        from duo.cli import _COMMAND_SECTIONS

        ctx = click.Context(main)
        registered = set(main.list_commands(ctx))
        sectioned = {name for names in _COMMAND_SECTIONS.values() for name in names}
        phantom = sectioned - registered
        assert phantom == set(), f"Phantom entries in _COMMAND_SECTIONS: {phantom}"

    def test_command_count_matches_docs(self) -> None:
        """CLAUDE.md says '35 commands' — verify this matches reality."""
        ctx = click.Context(main)
        registered = main.list_commands(ctx)
        assert len(registered) == 35, (
            f"CLAUDE.md says 35 commands but found {len(registered)}. "
            "Update CLAUDE.md if commands were added/removed."
        )


class TestCommandHelpSmoke:
    """Smoke test: every command's --help exits 0."""

    def test_all_commands_help_exits_zero(self) -> None:
        """'duo <cmd> --help' must exit 0 for every registered command."""
        runner = CliRunner()
        ctx = click.Context(main)
        for cmd_name in main.list_commands(ctx):
            result = runner.invoke(main, [cmd_name, "--help"])
            assert result.exit_code == 0, (
                f"duo {cmd_name} --help exited {result.exit_code}: "
                f"{result.output[:200]}"
            )
