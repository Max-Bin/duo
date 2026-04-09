"""Edge case tests for duo.cli — unicode names, zero ages, corrupted tasks, budget, dispatch."""

from __future__ import annotations

import json
import string
from pathlib import Path
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner
from hypothesis import given
from hypothesis import strategies as st

import duo.ceo_state
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
    create_task,
    save_task,
)
from duo.transport import DialogKind

# ---------------------------------------------------------------------------
# Fixtures (same as test_cli.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Redirect TASKS_DIR and DUO_DIR to a temporary directory."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    monkeypatch.setattr(duo.protocol, "TASKS_DIR", tasks_dir)
    monkeypatch.setattr(duo.protocol, "_CORRUPTED_DIR", tasks_dir / "_corrupted")
    monkeypatch.setattr(duo.protocol, "DUO_DIR", tmp_path)
    monkeypatch.setattr(duo.cli, "TASKS_DIR", tasks_dir)
    monkeypatch.setattr(duo.cli, "DUO_DIR", tmp_path)
    monkeypatch.setattr(duo.ceo_state, "CEO_STATE_PATH", tmp_path / "ceo-state.json")
    return tasks_dir


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _make_task(task_id: str = "test-task", description: str = "Test task"):  # type: ignore[no-untyped-def]
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


@pytest.fixture()
def make_task():  # type: ignore[no-untyped-def]
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

    def test_corrupted_status_raises(self, make_task) -> None:  # type: ignore[no-untyped-def]
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
        self, runner: CliRunner, make_task  # type: ignore[no-untyped-def]
    ) -> None:
        """cost --budget 0 with task but 0 PR events should exit 0."""
        task = make_task("budget-zero-clean")
        save_task(task)
        result = runner.invoke(main, ["cost", "--budget", "0"])
        assert result.exit_code == 0


class TestCeoDispatchTimeoutZero:
    """Edge case tests for ceo-dispatch with --timeout 0."""

    def test_timeout_zero_dialog_present(
        self, runner: CliRunner, make_task  # type: ignore[no-untyped-def]
    ) -> None:
        """ceo-dispatch --timeout 0 with dialog already present processes it."""
        task = make_task("dispatch-instant")
        pane = "  1. Continue\n  2. Cancel"
        with (
            patch("duo.transport.is_in_dialog", return_value=True),
            patch(
                "duo.transport.get_dialog_kind", return_value=DialogKind.OPTION
            ),
            patch("duo.transport.read_pane", return_value=pane),
            patch("duo.transport.is_permission_dialog", return_value=False),
            patch("duo.transport.select_dialog_option") as mock_sel,
        ):
            result = runner.invoke(
                main, ["ceo-dispatch", task.id, "--timeout", "0"]
            )
        assert result.exit_code == 0
        assert "selected" in result.output.lower()
        mock_sel.assert_called_once()


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
