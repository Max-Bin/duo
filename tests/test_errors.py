"""Tests for duo.errors — unified error hierarchy."""

from __future__ import annotations

import click

from duo.errors import (
    DuoDataError,
    DuoError,
    DuoSystemError,
    DuoUserError,
    TaskLockedError,
)


class TestDuoError:
    def test_base_class(self) -> None:
        err = DuoError("something broke")
        assert str(err) == "something broke"
        assert isinstance(err, Exception)

    def test_subclass_hierarchy(self) -> None:
        assert issubclass(DuoSystemError, DuoError)
        assert issubclass(DuoDataError, DuoError)

    def test_error_inheritance(self) -> None:
        assert isinstance(DuoUserError("x"), click.ClickException)
        assert not isinstance(DuoUserError("x"), DuoError)
        assert isinstance(DuoSystemError("x"), DuoError)
        assert isinstance(DuoDataError("x"), DuoError)


class TestDuoUserError:
    def test_is_click_exception(self) -> None:
        err = DuoUserError("bad input")
        assert isinstance(err, click.ClickException)

    def test_message_attribute(self) -> None:
        err = DuoUserError("something went wrong")
        assert err.message == "something went wrong"

    def test_format_message_without_fix(self) -> None:
        err = DuoUserError("task not found")
        assert err.format_message() == "task not found"
        assert err.fix == ""

    def test_format_message_with_fix(self) -> None:
        err = DuoUserError(
            "task 'foo' not found", fix="Run 'duo list' to see available tasks."
        )
        result = err.format_message()
        assert "task 'foo' not found" in result
        assert "Fix: Run 'duo list'" in result
        assert (
            result
            == "task 'foo' not found\n  Fix: Run 'duo list' to see available tasks."
        )

    def test_fix_default_empty(self) -> None:
        err = DuoUserError("oops")
        assert err.fix == ""

    def test_exit_code(self) -> None:
        err = DuoUserError("bad")
        assert err.exit_code == 1

    def test_duo_user_error_no_fix(self) -> None:
        err = DuoUserError("missing file")
        assert err.fix == ""
        result = err.format_message()
        assert result == "missing file"
        assert "\n" not in result

    def test_duo_user_error_with_fix(self) -> None:
        err = DuoUserError("config invalid", fix="Check ~/.duo/config.toml")
        result = err.format_message()
        assert "config invalid" in result
        assert result.endswith("Check ~/.duo/config.toml")
        assert "  Fix:" in result

    def test_duo_user_error_empty_fix(self) -> None:
        err = DuoUserError("unknown command", fix="")
        result = err.format_message()
        assert "Fix:" not in result
        assert result == "unknown command"


class TestDuoSystemError:
    def test_basic(self) -> None:
        err = DuoSystemError("tmux crashed")
        assert str(err) == "tmux crashed"
        assert isinstance(err, DuoError)
        assert isinstance(err, Exception)

    def test_duo_system_error_basic(self) -> None:
        err = DuoSystemError("subprocess failed")
        assert str(err) == "subprocess failed"
        assert isinstance(err, DuoError)
        assert isinstance(err, Exception)


class TestDuoDataError:
    def test_basic(self) -> None:
        err = DuoDataError("corrupt JSON")
        assert str(err) == "corrupt JSON"
        assert err.path == ""

    def test_with_path(self) -> None:
        err = DuoDataError(
            "schema mismatch", path="/home/user/.duo/tasks/foo/task.json"
        )
        assert str(err) == "schema mismatch"
        assert err.path == "/home/user/.duo/tasks/foo/task.json"
        assert isinstance(err, DuoError)

    def test_path_default_empty(self) -> None:
        err = DuoDataError("bad data")
        assert err.path == ""

    def test_duo_data_error_with_path(self) -> None:
        err = DuoDataError("parse error", path="/etc/duo/data.json")
        assert err.path == "/etc/duo/data.json"
        assert isinstance(err.path, str)

    def test_duo_data_error_no_path(self) -> None:
        err = DuoDataError("invalid format")
        assert err.path == ""


class TestErrorHierarchyParametrized:
    """Parametrized error hierarchy checks."""

    def test_inheritance(self) -> None:
        for cls, base in [
            (DuoError, Exception),
            (DuoSystemError, DuoError),
            (DuoDataError, DuoError),
            (DuoUserError, click.ClickException),
        ]:
            assert issubclass(cls, base), (
                f"{cls.__name__} should subclass {base.__name__}"
            )

    def test_user_error_format(self) -> None:
        for msg, fix, expected_has_fix in [
            ("simple error", "", False),
            ("with fix", "try this", True),
            ("empty fix", "", False),
            ("long msg " * 10, "short fix", True),
        ]:
            err = DuoUserError(msg, fix=fix)
            result = err.format_message()
            assert msg in result, f"msg={msg!r}"
            if expected_has_fix:
                assert "Fix:" in result
                assert fix in result
            else:
                assert "Fix:" not in result

    def test_data_error_path(self) -> None:
        for path in [
            "",
            "/tmp/test.json",
            "/home/user/.duo/tasks/x/task.json",
            "relative/path",
        ]:
            err = DuoDataError("test error", path=path)
            assert err.path == path, f"path={path!r}"


class TestErrorEdgeCases:
    """Edge case value coverage for error module."""

    def test_duo_user_error_no_fix(self) -> None:
        """DuoUserError with no fix has no 'Fix:' in output."""
        err = DuoUserError("something failed")
        assert "Fix:" not in err.format_message()

    def test_duo_user_error_empty_fix(self) -> None:
        """DuoUserError with empty fix string has no 'Fix:' in output."""
        err = DuoUserError("something failed", fix="")
        assert "Fix:" not in err.format_message()

    def test_duo_user_error_with_fix(self) -> None:
        """DuoUserError with fix appends it."""
        err = DuoUserError("failed", fix="try again")
        msg = err.format_message()
        assert "Fix: try again" in msg

    def test_duo_data_error_empty_path(self) -> None:
        """DuoDataError with empty path stores empty string."""
        err = DuoDataError("corruption", path="")
        assert err.path == ""

    def test_task_locked_error_is_duo_error(self) -> None:
        """TaskLockedError is a DuoError subclass."""
        err = TaskLockedError("locked")
        assert isinstance(err, DuoError)

    def test_duo_system_error_is_duo_error(self) -> None:
        """DuoSystemError is a DuoError subclass."""
        err = DuoSystemError("system failure")
        assert isinstance(err, DuoError)
        assert str(err) == "system failure"
