"""Tests for duo.errors — unified error hierarchy."""

from __future__ import annotations

import click

from duo.errors import DuoDataError, DuoError, DuoSystemError, DuoUserError


class TestDuoError:
    def test_base_class(self) -> None:
        err = DuoError("something broke")
        assert str(err) == "something broke"
        assert isinstance(err, Exception)

    def test_subclass_hierarchy(self) -> None:
        assert issubclass(DuoSystemError, DuoError)
        assert issubclass(DuoDataError, DuoError)


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


class TestDuoSystemError:
    def test_basic(self) -> None:
        err = DuoSystemError("tmux crashed")
        assert str(err) == "tmux crashed"
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
