"""CLI tests for doctor commands."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

import duo.cli
import duo.cli.doctor
import duo.protocol
from duo.cli import main
from duo.cli.doctor import (
    CheckResult,
    _doctor_check_capi_error,
    _doctor_check_claude_cli,
    _doctor_check_config,
    _doctor_check_copilot_cli,
    _doctor_check_copilot_health,
    _doctor_check_corrupted,
    _doctor_check_duo_dir,
    _doctor_check_git,
    _doctor_check_python,
    _doctor_check_task_timeout,
    _doctor_check_tmux,
    _doctor_check_tmux_bridge,
    _doctor_check_tmux_session,
    _emit_restart_signal,
    _get_pid_child_count,
    _get_pid_fd_count,
    _get_pid_kqueue_count,
)
from duo.protocol import (
    Subtask,
    TaskStatus,
    append_event,
    create_task,
    save_task,
)


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
    monkeypatch.setattr(duo.cli.doctor, "TASKS_DIR", tasks_dir)
    monkeypatch.setattr(duo.cli.doctor, "DUO_DIR", tmp_path)
    return tasks_dir


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _make_task(task_id: str = "test-task", description: str = "Test task"):
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
def make_task():
    """Fixture wrapper around _make_task for use in test classes."""
    return _make_task


class TestDoctor:
    """Tests for the overhauled doctor command and individual check functions."""

    # ── Individual check function tests ──────────────────────────────

    def test_check_python_pass(self):
        """Python check passes on current interpreter (>= 3.12)."""
        r = _doctor_check_python()
        assert r.status == "pass"
        assert r.name == "Python"

    def test_check_python_fail(self, monkeypatch: pytest.MonkeyPatch):
        """Python check fails when version < 3.12."""
        from collections import namedtuple

        FakeVI = namedtuple(
            "version_info", ["major", "minor", "micro", "releaselevel", "serial"]
        )
        fake_vi = FakeVI(3, 11, 0, "final", 0)
        monkeypatch.setattr("duo.cli.doctor.sys.version_info", fake_vi)
        r = _doctor_check_python()
        assert r.status == "fail"
        assert "3.11" in r.message

    def test_check_tmux_pass(self, monkeypatch: pytest.MonkeyPatch):
        """tmux check passes with version >= 3.0."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: "/usr/bin/tmux" if n == "tmux" else None,
        )
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(stdout="tmux 3.4\n", returncode=0),
        )
        r = _doctor_check_tmux()
        assert r.status == "pass"
        assert "3.4" in r.message

    def test_check_tmux_warn_old_version(self, monkeypatch: pytest.MonkeyPatch):
        """tmux check warns when version < 3.0."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: "/usr/bin/tmux" if n == "tmux" else None,
        )
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(stdout="tmux 2.9\n", returncode=0),
        )
        r = _doctor_check_tmux()
        assert r.status == "warn"
        assert "2.9" in r.message

    def test_check_tmux_fail_missing(self, monkeypatch: pytest.MonkeyPatch):
        """tmux check fails when not installed."""
        monkeypatch.setattr("duo.cli.doctor.shutil.which", lambda n: None)
        r = _doctor_check_tmux()
        assert r.status == "fail"
        assert "not found" in r.message

    def test_check_tmux_timeout(self, monkeypatch: pytest.MonkeyPatch):
        """tmux check passes (graceful) on subprocess timeout."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: "/usr/bin/tmux" if n == "tmux" else None,
        )

        def _timeout(*a: object, **kw: object) -> None:
            raise subprocess.TimeoutExpired("tmux", 10)

        monkeypatch.setattr("duo.cli.doctor.subprocess.run", _timeout)
        r = _doctor_check_tmux()
        assert r.status == "pass"
        assert r.message == "installed"

    def test_check_tmux_unparseable_version(self, monkeypatch: pytest.MonkeyPatch):
        """tmux check passes (graceful) when version string cannot be parsed."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: "/usr/bin/tmux" if n == "tmux" else None,
        )
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(stdout="tmux next-server\n", returncode=0),
        )
        r = _doctor_check_tmux()
        assert r.status == "pass"
        assert r.message == "installed"

    def test_check_tmux_bridge_in_path(self, monkeypatch: pytest.MonkeyPatch):
        """tmux-bridge found in PATH."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: "/usr/bin/tmux-bridge" if n == "tmux-bridge" else None,
        )
        r = _doctor_check_tmux_bridge()
        assert r.status == "pass"

    def test_check_tmux_bridge_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """tmux-bridge found at ~/.smux/bin/tmux-bridge fallback."""
        monkeypatch.setattr("duo.cli.doctor.shutil.which", lambda n: None)
        smux_bin = tmp_path / ".smux" / "bin"
        smux_bin.mkdir(parents=True)
        bridge = smux_bin / "tmux-bridge"
        bridge.touch()
        bridge.chmod(0o755)
        monkeypatch.setattr("duo.cli.doctor.Path.home", lambda: tmp_path)
        r = _doctor_check_tmux_bridge()
        assert r.status == "pass"
        assert "found at" in r.message

    def test_check_tmux_bridge_not_executable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """tmux-bridge found but not executable."""
        monkeypatch.setattr("duo.cli.doctor.shutil.which", lambda n: None)
        smux_bin = tmp_path / ".smux" / "bin"
        smux_bin.mkdir(parents=True)
        bridge = smux_bin / "tmux-bridge"
        bridge.touch()
        bridge.chmod(0o644)
        monkeypatch.setattr("duo.cli.doctor.Path.home", lambda: tmp_path)
        monkeypatch.setattr("duo.cli.doctor.os.access", lambda p, m: False)
        r = _doctor_check_tmux_bridge()
        assert r.status == "fail"
        assert "not executable" in r.message

    def test_check_tmux_bridge_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """tmux-bridge missing everywhere."""
        monkeypatch.setattr("duo.cli.doctor.shutil.which", lambda n: None)
        monkeypatch.setattr("duo.cli.doctor.Path.home", lambda: tmp_path)
        r = _doctor_check_tmux_bridge()
        assert r.status == "fail"
        assert "not found" in r.message

    def test_check_claude_cli_pass(self, monkeypatch: pytest.MonkeyPatch):
        """claude CLI found."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: "/usr/bin/claude" if n == "claude" else None,
        )
        r = _doctor_check_claude_cli()
        assert r.status == "pass"

    def test_check_claude_cli_warn(self, monkeypatch: pytest.MonkeyPatch):
        """claude CLI not found → warn."""
        monkeypatch.setattr("duo.cli.doctor.shutil.which", lambda n: None)
        r = _doctor_check_claude_cli()
        assert r.status == "warn"

    def test_check_copilot_cli_pass_copilot(self, monkeypatch: pytest.MonkeyPatch):
        """Copilot CLI found via 'copilot'."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: "/usr/bin/copilot" if n == "copilot" else None,
        )
        r = _doctor_check_copilot_cli()
        assert r.status == "pass"

    def test_check_copilot_cli_pass_github(self, monkeypatch: pytest.MonkeyPatch):
        """Copilot CLI found via 'github-copilot-cli'."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: (
                "/usr/bin/github-copilot-cli" if n == "github-copilot-cli" else None
            ),
        )
        r = _doctor_check_copilot_cli()
        assert r.status == "pass"

    def test_check_copilot_cli_warn(self, monkeypatch: pytest.MonkeyPatch):
        """Copilot CLI not found → warn."""
        monkeypatch.setattr("duo.cli.doctor.shutil.which", lambda n: None)
        r = _doctor_check_copilot_cli()
        assert r.status == "warn"

    def test_check_duo_dir_pass(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """~/.duo directory exists, writable, with space."""
        monkeypatch.setattr(duo.cli, "DUO_DIR", tmp_path)
        monkeypatch.setattr(duo.cli.doctor, "DUO_DIR", tmp_path)
        monkeypatch.setattr("duo.cli.doctor.os.access", lambda p, m: True)
        usage = MagicMock(free=500 * 1024 * 1024)  # 500MB
        monkeypatch.setattr("duo.cli.doctor.shutil.disk_usage", lambda p: usage)
        r = _doctor_check_duo_dir()
        assert r.status == "pass"
        assert "writable" in r.message

    def test_check_duo_dir_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """~/.duo directory missing → fail."""
        monkeypatch.setattr(duo.cli, "DUO_DIR", tmp_path / "nonexistent")
        monkeypatch.setattr(duo.cli.doctor, "DUO_DIR", tmp_path / "nonexistent")
        r = _doctor_check_duo_dir()
        assert r.status == "fail"
        assert "missing" in r.message

    def test_check_duo_dir_not_writable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """~/.duo directory not writable → fail."""
        monkeypatch.setattr(duo.cli, "DUO_DIR", tmp_path)
        monkeypatch.setattr(duo.cli.doctor, "DUO_DIR", tmp_path)
        monkeypatch.setattr("duo.cli.doctor.os.access", lambda p, m: False)
        r = _doctor_check_duo_dir()
        assert r.status == "fail"
        assert "not writable" in r.message

    def test_check_duo_dir_low_space(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """~/.duo directory low disk space → warn."""
        monkeypatch.setattr(duo.cli, "DUO_DIR", tmp_path)
        monkeypatch.setattr(duo.cli.doctor, "DUO_DIR", tmp_path)
        monkeypatch.setattr("duo.cli.doctor.os.access", lambda p, m: True)
        usage = MagicMock(free=50 * 1024 * 1024)  # 50MB
        monkeypatch.setattr("duo.cli.doctor.shutil.disk_usage", lambda p: usage)
        r = _doctor_check_duo_dir()
        assert r.status == "warn"
        assert "50 MB" in r.message

    def test_check_duo_dir_disk_usage_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """~/.duo disk_usage raises OSError → pass gracefully."""
        monkeypatch.setattr(duo.cli, "DUO_DIR", tmp_path)
        monkeypatch.setattr(duo.cli.doctor, "DUO_DIR", tmp_path)
        monkeypatch.setattr("duo.cli.doctor.os.access", lambda p, m: True)

        def _raise(*a: object) -> None:
            raise OSError("disk error")

        monkeypatch.setattr("duo.cli.doctor.shutil.disk_usage", _raise)
        r = _doctor_check_duo_dir()
        assert r.status == "pass"
        assert r.message == "writable"

    def test_check_config_pass(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Valid config.json → pass."""
        import duo.config as config_mod

        monkeypatch.setattr(duo.cli, "DUO_DIR", tmp_path)
        monkeypatch.setattr(duo.cli.doctor, "DUO_DIR", tmp_path)
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.json")
        (tmp_path / "config.json").write_text('{"max_corrections": 5}')
        r = _doctor_check_config()
        assert r.status == "pass"
        assert r.message == "valid"

    def test_check_config_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Missing config.json → warn."""
        monkeypatch.setattr(duo.cli, "DUO_DIR", tmp_path)
        monkeypatch.setattr(duo.cli.doctor, "DUO_DIR", tmp_path)
        r = _doctor_check_config()
        assert r.status == "warn"
        assert "missing" in r.message

    def test_check_config_invalid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Invalid JSON → warn."""
        monkeypatch.setattr(duo.cli, "DUO_DIR", tmp_path)
        monkeypatch.setattr(duo.cli.doctor, "DUO_DIR", tmp_path)
        (tmp_path / "config.json").write_text("{{{invalid")
        r = _doctor_check_config()
        assert r.status == "warn"
        assert "invalid" in r.message.lower()

    def test_check_config_validation_issues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Config with validation issues → warn with issue count."""
        import duo.config as config_mod

        monkeypatch.setattr(duo.cli, "DUO_DIR", tmp_path)
        monkeypatch.setattr(duo.cli.doctor, "DUO_DIR", tmp_path)
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.json")
        (tmp_path / "config.json").write_text('{"unknown_key": true}')
        r = _doctor_check_config()
        assert r.status == "warn"
        assert "issue" in r.message
        assert "duo config validate" in r.fix

    def test_check_tmux_session_pass(self, monkeypatch: pytest.MonkeyPatch):
        """Active tmux session → pass."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: "/usr/bin/tmux" if n == "tmux" else None,
        )
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0),
        )
        r = _doctor_check_tmux_session()
        assert r.status == "pass"

    def test_check_tmux_session_warn_no_session(self, monkeypatch: pytest.MonkeyPatch):
        """No active tmux session → warn."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: "/usr/bin/tmux" if n == "tmux" else None,
        )
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=1),
        )
        r = _doctor_check_tmux_session()
        assert r.status == "warn"
        assert "no active" in r.message

    def test_check_tmux_session_warn_not_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """tmux not installed → warn for session check."""
        monkeypatch.setattr("duo.cli.doctor.shutil.which", lambda n: None)
        r = _doctor_check_tmux_session()
        assert r.status == "warn"
        assert "not installed" in r.message

    def test_check_tmux_session_timeout(self, monkeypatch: pytest.MonkeyPatch):
        """tmux list-sessions times out → warn."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: "/usr/bin/tmux" if n == "tmux" else None,
        )

        def _timeout(*a: object, **kw: object) -> None:
            raise subprocess.TimeoutExpired("tmux", 10)

        monkeypatch.setattr("duo.cli.doctor.subprocess.run", _timeout)
        r = _doctor_check_tmux_session()
        assert r.status == "warn"

    def test_check_task_timeout_pass(self, monkeypatch: pytest.MonkeyPatch):
        """Valid task_timeout → pass."""
        monkeypatch.setattr("duo.cli.doctor.get_config", lambda k: 300)
        r = _doctor_check_task_timeout()
        assert r.status == "pass"
        assert "300s" in r.message

    def test_check_task_timeout_pass_disabled(self, monkeypatch: pytest.MonkeyPatch):
        """task_timeout = 0 → pass (disabled)."""
        monkeypatch.setattr("duo.cli.doctor.get_config", lambda k: 0)
        r = _doctor_check_task_timeout()
        assert r.status == "pass"
        assert "disabled" in r.message

    def test_check_task_timeout_warn_invalid(self, monkeypatch: pytest.MonkeyPatch):
        """Invalid task_timeout → warn."""
        monkeypatch.setattr("duo.cli.doctor.get_config", lambda k: -1)
        r = _doctor_check_task_timeout()
        assert r.status == "warn"
        assert "invalid" in r.message

    def test_check_task_timeout_warn_none(self, monkeypatch: pytest.MonkeyPatch):
        """task_timeout is None → warn."""
        monkeypatch.setattr("duo.cli.doctor.get_config", lambda k: None)
        r = _doctor_check_task_timeout()
        assert r.status == "warn"

    def test_check_corrupted_pass(self, monkeypatch: pytest.MonkeyPatch):
        """No corrupted tasks → pass."""
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])
        r = _doctor_check_corrupted()
        assert r.status == "pass"
        assert r.message == "0"

    def test_check_corrupted_warn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Corrupted tasks present → warn."""
        monkeypatch.setattr(
            "duo.protocol.list_corrupted", lambda: [tmp_path / "a", tmp_path / "b"]
        )
        r = _doctor_check_corrupted()
        assert r.status == "warn"
        assert r.message == "2"

    def test_check_git_pass(self, monkeypatch: pytest.MonkeyPatch):
        """git found with version → pass."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: "/usr/bin/git" if n == "git" else None,
        )
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(stdout="git version 2.44.0\n", returncode=0),
        )
        r = _doctor_check_git()
        assert r.status == "pass"
        assert "2.44.0" in r.message

    def test_check_git_warn_missing(self, monkeypatch: pytest.MonkeyPatch):
        """git not found → warn."""
        monkeypatch.setattr("duo.cli.doctor.shutil.which", lambda n: None)
        r = _doctor_check_git()
        assert r.status == "warn"
        assert "not found" in r.message

    def test_check_git_timeout(self, monkeypatch: pytest.MonkeyPatch):
        """git --version times out → pass gracefully."""
        monkeypatch.setattr(
            "duo.cli.doctor.shutil.which",
            lambda n: "/usr/bin/git" if n == "git" else None,
        )

        def _timeout(*a: object, **kw: object) -> None:
            raise subprocess.TimeoutExpired("git", 10)

        monkeypatch.setattr("duo.cli.doctor.subprocess.run", _timeout)
        r = _doctor_check_git()
        assert r.status == "pass"
        assert r.message == "installed"

    # ── Integration tests: doctor command ──────────────────────────────

    def _setup_all_pass(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Configure monkeypatches for all checks to pass."""
        monkeypatch.setattr("duo.cli.doctor.DUO_DIR", tmp_path)
        config_path = tmp_path / "config.json"
        config_path.write_text('{"copilot_model": "claude-opus-4.6"}\n')

        def fake_which(name: str) -> str | None:
            return f"/usr/bin/{name}"

        monkeypatch.setattr("duo.cli.doctor.shutil.which", fake_which)
        monkeypatch.setattr("duo.cli.doctor.os.access", lambda p, m: True)
        usage = MagicMock(free=5 * 1024 * 1024 * 1024)
        monkeypatch.setattr("duo.cli.doctor.shutil.disk_usage", lambda p: usage)
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(
                returncode=0,
                stdout="tmux 3.4\n"
                if a and a[0] and a[0][0] == "tmux"
                else "git version 2.44.0\n",
            ),
        )
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])

    def test_doctor_all_pass(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """All checks pass when everything is available."""
        self._setup_all_pass(monkeypatch, tmp_path)
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0
        assert "PASS" in result.output
        assert "13/13 checks passed" in result.output

    def test_doctor_missing_tmux(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Missing tmux shows fix suggestion and exits 1."""
        self._setup_all_pass(monkeypatch, tmp_path)

        def fake_which(name: str) -> str | None:
            if name == "tmux":
                return None
            return f"/usr/bin/{name}"

        monkeypatch.setattr("duo.cli.doctor.shutil.which", fake_which)
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code != 0
        assert "brew install tmux" in result.output

    def test_doctor_summary_count(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Doctor output ends with X/Y checks passed."""
        self._setup_all_pass(monkeypatch, tmp_path)
        result = runner.invoke(main, ["doctor"])
        assert "/13 checks passed" in result.output

    def test_doctor_tmux_bridge_fallback_path(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """tmux-bridge found via ~/.smux/bin/tmux-bridge when not in PATH."""
        self._setup_all_pass(monkeypatch, tmp_path)

        def fake_which(name: str) -> str | None:
            if name == "tmux-bridge":
                return None
            return f"/usr/bin/{name}"

        monkeypatch.setattr("duo.cli.doctor.shutil.which", fake_which)
        smux_bin = tmp_path / "fakehome" / ".smux" / "bin"
        smux_bin.mkdir(parents=True)
        bridge = smux_bin / "tmux-bridge"
        bridge.touch()
        bridge.chmod(0o755)
        monkeypatch.setattr("duo.cli.doctor.Path.home", lambda: tmp_path / "fakehome")
        monkeypatch.setattr("duo.cli.doctor.os.access", lambda p, m: True)
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0
        assert "tmux-bridge" in result.output
        assert "not found" not in result.output

    def test_doctor_invalid_config_json(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Invalid JSON in config.json shows warn."""
        self._setup_all_pass(monkeypatch, tmp_path)
        (tmp_path / "config.json").write_text("not valid json {{{")
        result = runner.invoke(main, ["doctor"])
        assert "invalid" in result.output.lower()

    def test_doctor_json_output(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """--json-output produces valid JSON with checks and summary."""
        self._setup_all_pass(monkeypatch, tmp_path)
        result = runner.invoke(main, ["doctor", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "checks" in data
        assert "summary" in data
        assert len(data["checks"]) == 13
        assert data["summary"]["total"] == 13
        for check in data["checks"]:
            assert "name" in check
            assert "status" in check
            assert "message" in check
            assert "fix" in check

    def test_doctor_strict_with_warnings(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """--strict exits non-zero when warnings exist."""
        self._setup_all_pass(monkeypatch, tmp_path)

        def fake_which(name: str) -> str | None:
            if name == "claude":
                return None
            return f"/usr/bin/{name}"

        monkeypatch.setattr("duo.cli.doctor.shutil.which", fake_which)
        result = runner.invoke(main, ["doctor", "--strict"])
        assert result.exit_code != 0

    def test_doctor_strict_all_pass(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """--strict exits 0 when all checks pass."""
        self._setup_all_pass(monkeypatch, tmp_path)
        result = runner.invoke(main, ["doctor", "--strict"])
        assert result.exit_code == 0

    def test_doctor_exit_0_with_warnings_no_strict(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Warnings without --strict → exit 0."""
        self._setup_all_pass(monkeypatch, tmp_path)

        def fake_which(name: str) -> str | None:
            if name == "claude":
                return None
            return f"/usr/bin/{name}"

        monkeypatch.setattr("duo.cli.doctor.shutil.which", fake_which)
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0
        assert "warning" in result.output.lower()

    def test_doctor_json_with_failure(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """JSON output with a failure includes fail count and exits non-zero."""
        self._setup_all_pass(monkeypatch, tmp_path)

        def fake_which(name: str) -> str | None:
            if name == "tmux":
                return None
            return f"/usr/bin/{name}"

        monkeypatch.setattr("duo.cli.doctor.shutil.which", fake_which)
        result = runner.invoke(main, ["doctor", "--json-output"])
        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["summary"]["fail"] > 0

    def test_doctor_header_present(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Default output includes header."""
        self._setup_all_pass(monkeypatch, tmp_path)
        result = runner.invoke(main, ["doctor"])
        assert "Duo Environment Diagnostics" in result.output

    def test_check_result_dataclass(self):
        """CheckResult dataclass fields are accessible."""
        cr = CheckResult(name="test", status="pass", message="ok", fix="")
        assert cr.name == "test"
        assert cr.status == "pass"
        assert cr.message == "ok"
        assert cr.fix == ""

    def test_doctor_quiet_all_pass(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """--quiet with all pass prints nothing."""
        self._setup_all_pass(monkeypatch, tmp_path)
        result = runner.invoke(main, ["doctor", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_doctor_quiet_with_failure(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """--quiet with failures shows only failure lines."""
        self._setup_all_pass(monkeypatch, tmp_path)

        def fake_which(name: str) -> str | None:
            if name == "tmux":
                return None
            return f"/usr/bin/{name}"

        monkeypatch.setattr("duo.cli.doctor.shutil.which", fake_which)
        result = runner.invoke(main, ["doctor", "-q"])
        assert result.exit_code != 0
        assert "tmux" in result.output.lower()


# ---------------------------------------------------------------------------
# resume command
# ---------------------------------------------------------------------------


class TestDoctorTaskTimeout:
    def test_doctor_shows_task_timeout(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """doctor output includes task_timeout check."""
        config_path = tmp_path / "config.json"
        config_path.write_text('{"copilot_model": "claude-opus-4.6"}\n')

        def fake_which(name: str) -> str | None:
            return f"/usr/bin/{name}"

        monkeypatch.setattr("duo.cli.doctor.shutil.which", fake_which)
        monkeypatch.setattr("duo.cli.doctor.os.access", lambda p, m: True)
        usage = MagicMock(free=5 * 1024 * 1024 * 1024)
        monkeypatch.setattr("duo.cli.doctor.shutil.disk_usage", lambda p: usage)
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="tmux 3.4\n"),
        )
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])
        result = runner.invoke(main, ["doctor"])
        assert "task_timeout" in result.output

    def test_doctor_invalid_task_timeout(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """doctor reports invalid task_timeout value."""
        monkeypatch.setattr("duo.cli.doctor.shutil.which", lambda _: "/usr/bin/fake")
        monkeypatch.setattr("duo.cli.doctor.os.access", lambda p, m: True)
        usage = MagicMock(free=5 * 1024 * 1024 * 1024)
        monkeypatch.setattr("duo.cli.doctor.shutil.disk_usage", lambda p: usage)
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="tmux 3.4\n"),
        )
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])
        monkeypatch.setattr(
            "duo.cli.doctor.get_config", lambda k: -1 if k == "task_timeout" else 0
        )
        result = runner.invoke(main, ["doctor"])
        assert "task_timeout" in result.output
        assert "invalid" in result.output.lower()


class TestDoctorStaleLocks:
    """Tests for _doctor_check_stale_locks()."""

    def test_no_stale_locks(self):
        """No lock files → pass."""
        from duo.cli.doctor import _doctor_check_stale_locks

        result = _doctor_check_stale_locks()
        assert result.status == "pass"

    def test_stale_locks_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Lock files present → warn."""
        from duo.cli.doctor import _doctor_check_stale_locks

        monkeypatch.setattr("duo.cli.doctor.TASKS_DIR", tmp_path)
        (tmp_path / ".my-task.lock").touch()
        (tmp_path / ".other.lock").touch()

        result = _doctor_check_stale_locks()
        assert result.status == "warn"
        assert "2 found" in result.message


class TestDoctorOrphanWorktrees:
    """Tests for _doctor_check_orphan_worktrees()."""

    def test_no_orphans(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """No orphan worktrees → pass."""
        from duo.cli.doctor import _doctor_check_orphan_worktrees

        monkeypatch.setattr("duo.cli.doctor.TASKS_DIR", tmp_path)
        (tmp_path / "my-task").mkdir()
        porcelain = "worktree /repo\n\nworktree /repo/duo-my-task\nbranch refs/heads/duo/my-task\n"
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout=porcelain),
        )
        result = _doctor_check_orphan_worktrees()
        assert result.status == "pass"

    def test_orphan_found(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """Worktree exists but task dir does not → warn."""
        from duo.cli.doctor import _doctor_check_orphan_worktrees

        monkeypatch.setattr("duo.cli.doctor.TASKS_DIR", tmp_path)
        porcelain = "worktree /repo\n\nworktree /repo/duo-ghost-task\nbranch refs/heads/duo/ghost-task\n"
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout=porcelain),
        )
        result = _doctor_check_orphan_worktrees()
        assert result.status == "warn"
        assert "duo-ghost-task" in result.message

    def test_git_unavailable(self, monkeypatch: pytest.MonkeyPatch):
        """Git failure → skip gracefully."""
        from duo.cli.doctor import _doctor_check_orphan_worktrees

        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("no git")),
        )
        result = _doctor_check_orphan_worktrees()
        assert result.status == "pass"
        assert "skipped" in result.message

    def test_not_git_repo(self, monkeypatch: pytest.MonkeyPatch):
        """git worktree list fails (not a repo) → skip."""
        from duo.cli.doctor import _doctor_check_orphan_worktrees

        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=128, stdout=""),
        )
        result = _doctor_check_orphan_worktrees()
        assert result.status == "pass"
        assert "skipped" in result.message


class TestDoctorAutoFix:
    """Tests for _doctor_auto_fix() and doctor --fix."""

    def test_fix_removes_stale_locks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """--fix removes stale .lock files (only if older than 1 hour)."""
        import os

        from duo.cli.doctor import _doctor_auto_fix

        monkeypatch.setattr("duo.cli.doctor.TASKS_DIR", tmp_path)
        lock_file = tmp_path / ".my-task.lock"
        lock_file.touch()
        os.utime(lock_file, (0, 0))
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=128, stdout=""),
        )
        fixed = _doctor_auto_fix()
        assert any("stale lock" in f for f in fixed)
        assert not lock_file.exists()

    def test_fix_lock_oserror_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Lock files that raise OSError on stat are silently skipped."""
        from duo.cli.doctor import _doctor_auto_fix

        monkeypatch.setattr("duo.cli.doctor.TASKS_DIR", tmp_path)
        lock_file = tmp_path / ".bad.lock"
        lock_file.touch()
        # Remove the file so stat fails, but glob still finds it via race
        lock_file.unlink()
        # Re-create as a broken symlink so glob finds it but stat fails
        lock_file.symlink_to(tmp_path / "nonexistent-target")
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=128, stdout=""),
        )
        fixed = _doctor_auto_fix()
        assert not any("stale lock" in f for f in fixed)

    def test_fix_fresh_lock_not_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Lock files less than 1 hour old are NOT removed."""
        from duo.cli.doctor import _doctor_auto_fix

        monkeypatch.setattr("duo.cli.doctor.TASKS_DIR", tmp_path)
        lock_file = tmp_path / ".fresh.lock"
        lock_file.touch()  # mtime = now (fresh)
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=128, stdout=""),
        )
        fixed = _doctor_auto_fix()
        assert not any("stale lock" in f for f in fixed)
        assert lock_file.exists()

    def test_fix_purges_quarantined(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """--fix purges quarantined tasks."""
        from duo.cli.doctor import _doctor_auto_fix

        monkeypatch.setattr("duo.cli.doctor.TASKS_DIR", tmp_path)
        qdir = tmp_path / "bad-task"
        qdir.mkdir()
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [qdir])
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=128, stdout=""),
        )
        fixed = _doctor_auto_fix()
        assert any("quarantined" in f for f in fixed)

    def test_fix_nothing_to_fix(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """--fix with nothing broken returns empty list."""
        from duo.cli.doctor import _doctor_auto_fix

        monkeypatch.setattr("duo.cli.doctor.TASKS_DIR", tmp_path)
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=128, stdout=""),
        )
        fixed = _doctor_auto_fix()
        assert fixed == []

    def test_fix_orphan_worktree(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """--fix removes orphan duo-* worktrees."""
        from duo.cli.doctor import _doctor_auto_fix

        monkeypatch.setattr("duo.cli.doctor.TASKS_DIR", tmp_path)
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])
        porcelain = "worktree /repo\n\nworktree /repo/duo-orphan\nbranch refs/heads/duo/orphan\n"
        calls: list[list[str]] = []

        def fake_run(*a: object, **kw: object) -> MagicMock:
            cmd = a[0] if a else kw.get("args", [])
            calls.append(cmd)
            if cmd and "list" in cmd:
                return MagicMock(returncode=0, stdout=porcelain)
            return MagicMock(returncode=0, stdout="")

        monkeypatch.setattr("duo.cli.doctor.subprocess.run", fake_run)
        fixed = _doctor_auto_fix()
        assert any("orphan worktree" in f for f in fixed)

    def test_fix_git_error_skips_orphan_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """--fix handles git errors gracefully during orphan scan."""
        from duo.cli.doctor import _doctor_auto_fix

        monkeypatch.setattr("duo.cli.doctor.TASKS_DIR", tmp_path)
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])

        def raise_os_error(*a: object, **kw: object) -> None:
            raise OSError("no git")

        monkeypatch.setattr("duo.cli.doctor.subprocess.run", raise_os_error)
        fixed = _doctor_auto_fix()
        assert not any("orphan" in f for f in fixed)

    def test_doctor_fix_flag_output(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """doctor --fix shows fixed items in output."""
        monkeypatch.setattr("duo.cli.doctor.TASKS_DIR", tmp_path)
        lock_file = tmp_path / ".stale.lock"
        lock_file.touch()
        import os

        os.utime(lock_file, (0, 0))
        monkeypatch.setattr("duo.cli.doctor.shutil.which", lambda _: "/usr/bin/fake")
        monkeypatch.setattr("duo.cli.doctor.os.access", lambda p, m: True)
        usage = MagicMock(free=5 * 1024 * 1024 * 1024)
        monkeypatch.setattr("duo.cli.doctor.shutil.disk_usage", lambda p: usage)
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="tmux 3.4\n"),
        )
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])
        result = runner.invoke(main, ["doctor", "--fix"])
        assert "Auto-fixed" in result.output
        assert "stale lock" in result.output

    def test_doctor_fix_json(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """doctor --fix --json-output includes fixed list."""
        monkeypatch.setattr("duo.cli.doctor.TASKS_DIR", tmp_path)
        lock_file = tmp_path / ".old.lock"
        lock_file.touch()
        import os

        os.utime(lock_file, (0, 0))
        monkeypatch.setattr("duo.cli.doctor.shutil.which", lambda _: "/usr/bin/fake")
        monkeypatch.setattr("duo.cli.doctor.os.access", lambda p, m: True)
        usage = MagicMock(free=5 * 1024 * 1024 * 1024)
        monkeypatch.setattr("duo.cli.doctor.shutil.disk_usage", lambda p: usage)
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="tmux 3.4\n"),
        )
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])
        result = runner.invoke(main, ["doctor", "--fix", "--json-output"])
        data = json.loads(result.output)
        assert "fixed" in data
        assert len(data["fixed"]) >= 1


# ── Copilot health check (doctor) ────────────────────────────────────


class TestGetPidFdCount:
    """Tests for _get_pid_fd_count()."""

    def test_counts_lines(self, monkeypatch: pytest.MonkeyPatch):
        """Should return line count minus header."""
        header = "COMMAND PID FD TYPE"
        lines = "\n".join([header] + [f"line{i}" for i in range(10)])
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout=lines),
        )
        assert _get_pid_fd_count(1234) == 10

    def test_lsof_fails(self, monkeypatch: pytest.MonkeyPatch):
        """lsof returns non-zero → -1."""
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=1, stdout=""),
        )
        assert _get_pid_fd_count(1234) == -1

    def test_lsof_timeout(self, monkeypatch: pytest.MonkeyPatch):
        """Timeout → -1."""
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: (_ for _ in ()).throw(
                subprocess.TimeoutExpired("lsof", 10)
            ),
        )
        assert _get_pid_fd_count(1234) == -1

    def test_lsof_oserror(self, monkeypatch: pytest.MonkeyPatch):
        """OSError → -1."""
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("no lsof")),
        )
        assert _get_pid_fd_count(1234) == -1

    def test_empty_output(self, monkeypatch: pytest.MonkeyPatch):
        """Empty output → 0."""
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout=""),
        )
        assert _get_pid_fd_count(1234) == 0


class TestGetPidKqueueCount:
    """Tests for _get_pid_kqueue_count()."""

    def test_counts_kqueue_lines(self, monkeypatch: pytest.MonkeyPatch):
        """Should count lines containing KQUEUE."""
        output = "HEADER\nnode 123 KQUEUE\nnode 124 FD\nnode 125 KQUEUE\n"
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout=output),
        )
        assert _get_pid_kqueue_count(1234) == 2

    def test_no_kqueues(self, monkeypatch: pytest.MonkeyPatch):
        """No KQUEUE lines → 0."""
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="HEADER\nfd\nfd\n"),
        )
        assert _get_pid_kqueue_count(1234) == 0

    def test_lsof_fails(self, monkeypatch: pytest.MonkeyPatch):
        """lsof non-zero → -1."""
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=1, stdout=""),
        )
        assert _get_pid_kqueue_count(1234) == -1

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch):
        """Timeout → -1."""
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: (_ for _ in ()).throw(
                subprocess.TimeoutExpired("lsof", 10)
            ),
        )
        assert _get_pid_kqueue_count(1234) == -1


class TestGetPidChildCount:
    """Tests for _get_pid_child_count()."""

    def test_counts_children(self, monkeypatch: pytest.MonkeyPatch):
        """Should count non-empty output lines."""
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="111\n222\n333\n"),
        )
        assert _get_pid_child_count(1234) == 3

    def test_no_children(self, monkeypatch: pytest.MonkeyPatch):
        """pgrep returns non-zero → 0."""
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=1, stdout=""),
        )
        assert _get_pid_child_count(1234) == 0

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch):
        """Timeout → -1."""
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: (_ for _ in ()).throw(
                subprocess.TimeoutExpired("pgrep", 5)
            ),
        )
        assert _get_pid_child_count(1234) == -1

    def test_oserror(self, monkeypatch: pytest.MonkeyPatch):
        """OSError → -1."""
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("no pgrep")),
        )
        assert _get_pid_child_count(1234) == -1

    def test_blank_lines_ignored(self, monkeypatch: pytest.MonkeyPatch):
        """Blank lines in pgrep output should be ignored."""
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="111\n\n222\n\n"),
        )
        assert _get_pid_child_count(1234) == 2


class TestDoctorCheckCopilotHealth:
    """Tests for _doctor_check_copilot_health()."""

    def test_no_active_tasks(self, monkeypatch: pytest.MonkeyPatch):
        """No active tasks → empty list."""
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [])
        assert _doctor_check_copilot_health() == []

    def test_list_tasks_exception(self, monkeypatch: pytest.MonkeyPatch):
        """list_tasks raises → empty list."""
        monkeypatch.setattr(
            "duo.cli.doctor.list_tasks",
            lambda: (_ for _ in ()).throw(OSError("fs error")),
        )
        assert _doctor_check_copilot_health() == []

    def test_terminal_tasks_skipped(self, monkeypatch: pytest.MonkeyPatch):
        """Completed/failed tasks should be skipped."""
        tasks = [
            MagicMock(status=TaskStatus.COMPLETED, pane_label="done-pane"),
            MagicMock(status=TaskStatus.FAILED, pane_label="fail-pane"),
            MagicMock(status=TaskStatus.ESCALATED, pane_label="esc-pane"),
        ]
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: tasks)
        assert _doctor_check_copilot_health() == []

    def test_pid_unavailable(self, monkeypatch: pytest.MonkeyPatch):
        """PID not found → warn."""
        task = MagicMock(status=TaskStatus.RUNNING, pane_label="my-pane")
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda label: None)
        results = _doctor_check_copilot_health()
        assert len(results) == 1
        assert results[0].status == "warn"
        assert "PID unavailable" in results[0].message

    def test_healthy_pane(self, monkeypatch: pytest.MonkeyPatch):
        """Low fd/kqueue/child counts → pass."""
        task = MagicMock(status=TaskStatus.RUNNING, pane_label="healthy-pane")
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda label: 9999)
        monkeypatch.setattr("duo.cli.doctor._get_pid_fd_count", lambda pid: 50)
        monkeypatch.setattr("duo.cli.doctor._get_pid_kqueue_count", lambda pid: 5)
        monkeypatch.setattr("duo.cli.doctor._get_pid_child_count", lambda pid: 2)
        results = _doctor_check_copilot_health()
        assert len(results) == 1
        assert results[0].status == "pass"
        assert "fds=50" in results[0].message

    def test_critical_on_very_high_fds(self, monkeypatch: pytest.MonkeyPatch):
        """fds >= 2000 → fail."""
        task = MagicMock(status=TaskStatus.RUNNING, pane_label="crit-pane")
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda label: 9999)
        monkeypatch.setattr("duo.cli.doctor._get_pid_fd_count", lambda pid: 3000)
        monkeypatch.setattr("duo.cli.doctor._get_pid_kqueue_count", lambda pid: 5)
        monkeypatch.setattr("duo.cli.doctor._get_pid_child_count", lambda pid: 2)
        results = _doctor_check_copilot_health()
        assert results[0].status == "fail"
        assert "restart" in results[0].fix.lower()

    def test_warn_on_high_kqueue(self, monkeypatch: pytest.MonkeyPatch):
        """kqueue >= 50 → warn."""
        task = MagicMock(status=TaskStatus.RUNNING, pane_label="kq-pane")
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda label: 9999)
        monkeypatch.setattr("duo.cli.doctor._get_pid_fd_count", lambda pid: 100)
        monkeypatch.setattr("duo.cli.doctor._get_pid_kqueue_count", lambda pid: 60)
        monkeypatch.setattr("duo.cli.doctor._get_pid_child_count", lambda pid: 2)
        results = _doctor_check_copilot_health()
        assert results[0].status == "warn"

    def test_warn_on_high_children(self, monkeypatch: pytest.MonkeyPatch):
        """children >= 10 → warn."""
        task = MagicMock(status=TaskStatus.RUNNING, pane_label="child-pane")
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda label: 9999)
        monkeypatch.setattr("duo.cli.doctor._get_pid_fd_count", lambda pid: 100)
        monkeypatch.setattr("duo.cli.doctor._get_pid_kqueue_count", lambda pid: 5)
        monkeypatch.setattr("duo.cli.doctor._get_pid_child_count", lambda pid: 15)
        results = _doctor_check_copilot_health()
        assert results[0].status == "warn"

    def test_fail_overrides_warn(self, monkeypatch: pytest.MonkeyPatch):
        """If fds critical AND kqueue high → status is fail (worst wins)."""
        task = MagicMock(status=TaskStatus.RUNNING, pane_label="both-pane")
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda label: 9999)
        monkeypatch.setattr("duo.cli.doctor._get_pid_fd_count", lambda pid: 2500)
        monkeypatch.setattr("duo.cli.doctor._get_pid_kqueue_count", lambda pid: 100)
        monkeypatch.setattr("duo.cli.doctor._get_pid_child_count", lambda pid: 20)
        results = _doctor_check_copilot_health()
        assert results[0].status == "fail"

    def test_negative_counts_ignored(self, monkeypatch: pytest.MonkeyPatch):
        """Negative counts (lsof unavailable) → pass with empty parts."""
        task = MagicMock(status=TaskStatus.RUNNING, pane_label="neg-pane")
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda label: 9999)
        monkeypatch.setattr("duo.cli.doctor._get_pid_fd_count", lambda pid: -1)
        monkeypatch.setattr("duo.cli.doctor._get_pid_kqueue_count", lambda pid: -1)
        monkeypatch.setattr("duo.cli.doctor._get_pid_child_count", lambda pid: -1)
        results = _doctor_check_copilot_health()
        assert results[0].status == "pass"
        assert "healthy" in results[0].message

    def test_multiple_panes(self, monkeypatch: pytest.MonkeyPatch):
        """Multiple active tasks → one result per pane."""
        tasks = [
            MagicMock(status=TaskStatus.RUNNING, pane_label="pane-a"),
            MagicMock(status=TaskStatus.ACKED, pane_label="pane-b"),
        ]
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: tasks)
        pids = {"pane-a": 111, "pane-b": 222}
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda label: pids.get(label))
        monkeypatch.setattr("duo.cli.doctor._get_pid_fd_count", lambda pid: 10)
        monkeypatch.setattr("duo.cli.doctor._get_pid_kqueue_count", lambda pid: 1)
        monkeypatch.setattr("duo.cli.doctor._get_pid_child_count", lambda pid: 0)
        results = _doctor_check_copilot_health()
        assert len(results) == 2
        names = {r.name for r in results}
        assert "pane:pane-a" in names
        assert "pane:pane-b" in names

    def test_empty_pane_label_skipped(self, monkeypatch: pytest.MonkeyPatch):
        """Task with empty pane_label → skipped."""
        task = MagicMock(status=TaskStatus.RUNNING, pane_label="")
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        assert _doctor_check_copilot_health() == []

    def test_doctor_integrates_health_checks(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """doctor command includes copilot health check results."""
        monkeypatch.setattr("duo.cli.doctor.shutil.which", lambda n: f"/usr/bin/{n}")
        monkeypatch.setattr("duo.cli.doctor.os.access", lambda p, m: True)
        usage = MagicMock(free=5 * 1024 * 1024 * 1024)
        monkeypatch.setattr("duo.cli.doctor.shutil.disk_usage", lambda p: usage)
        monkeypatch.setattr(
            "duo.cli.doctor.subprocess.run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="tmux 3.4\n"),
        )
        monkeypatch.setattr("duo.protocol.list_corrupted", lambda: [])
        monkeypatch.setattr(duo.cli, "DUO_DIR", tmp_path)
        monkeypatch.setattr(duo.cli.doctor, "DUO_DIR", tmp_path)
        monkeypatch.setattr(duo.cli, "TASKS_DIR", tmp_path / "tasks")
        monkeypatch.setattr(duo.cli.doctor, "TASKS_DIR", tmp_path / "tasks")
        (tmp_path / "tasks").mkdir(exist_ok=True)

        task = MagicMock(status=TaskStatus.RUNNING, pane_label="test-pane")
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda label: 9999)
        monkeypatch.setattr("duo.cli.doctor._get_pid_fd_count", lambda pid: 600)
        monkeypatch.setattr("duo.cli.doctor._get_pid_kqueue_count", lambda pid: 10)
        monkeypatch.setattr("duo.cli.doctor._get_pid_child_count", lambda pid: 3)

        result = runner.invoke(main, ["doctor"])
        assert "pane:test-pane" in result.output
        assert "fds=600" in result.output


# ── Auto-restart signal ──────────────────────────────────────────────


class TestEmitRestartSignal:
    """Tests for _emit_restart_signal()."""

    def test_writes_signal_file(self, make_task):
        """Should create restart-recommended file in task dir."""
        task = make_task("restart-test")
        _emit_restart_signal(task.id)
        signal_path = task.dir / "restart-recommended"
        assert signal_path.exists()
        content = signal_path.read_text()
        assert "Restart recommended" in content

    def test_nonexistent_task_dir_ignored(self, tmp_path: Path):
        """Missing task dir → no crash (OSError caught)."""
        _emit_restart_signal("nonexistent-task-xyz")

    def test_critical_health_emits_signal(
        self, make_task, monkeypatch: pytest.MonkeyPatch
    ):
        """doctor health check with critical fds should emit restart signal."""
        task = make_task("signal-test")
        task.pane_label = "sig-pane"
        save_task(task)
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda label: 9999)
        monkeypatch.setattr("duo.cli.doctor._get_pid_fd_count", lambda pid: 3000)
        monkeypatch.setattr("duo.cli.doctor._get_pid_kqueue_count", lambda pid: 5)
        monkeypatch.setattr("duo.cli.doctor._get_pid_child_count", lambda pid: 2)
        results = _doctor_check_copilot_health()
        assert results[0].status == "fail"
        signal_path = task.dir / "restart-recommended"
        assert signal_path.exists()

    def test_warn_health_no_signal(self, make_task, monkeypatch: pytest.MonkeyPatch):
        """doctor health check with warn (not critical) should NOT emit signal."""
        task = make_task("no-signal-test")
        task.pane_label = "nosig-pane"
        save_task(task)
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        monkeypatch.setattr("duo.transport.get_pane_pid", lambda label: 9999)
        monkeypatch.setattr("duo.cli.doctor._get_pid_fd_count", lambda pid: 600)
        monkeypatch.setattr("duo.cli.doctor._get_pid_kqueue_count", lambda pid: 5)
        monkeypatch.setattr("duo.cli.doctor._get_pid_child_count", lambda pid: 2)
        results = _doctor_check_copilot_health()
        assert results[0].status == "warn"
        signal_path = task.dir / "restart-recommended"
        assert not signal_path.exists()


class TestDoctorCheckCapiError:
    """Tests for _doctor_check_capi_error()."""

    def test_no_tasks(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [])
        assert _doctor_check_capi_error() == []

    def test_list_tasks_error(self, monkeypatch: pytest.MonkeyPatch):
        def _raise():
            raise FileNotFoundError

        monkeypatch.setattr("duo.cli.doctor.list_tasks", _raise)
        assert _doctor_check_capi_error() == []

    def test_no_active_tasks(self, monkeypatch: pytest.MonkeyPatch):
        task = MagicMock(status=TaskStatus.COMPLETED)
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        assert _doctor_check_capi_error() == []

    def test_no_journal(self, monkeypatch: pytest.MonkeyPatch):
        from pathlib import Path

        task = MagicMock(
            status=TaskStatus.RUNNING,
            journal_path=Path("/nonexistent/journal.jsonl"),
        )
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        assert _doctor_check_capi_error() == []

    def test_no_capi_events(self, monkeypatch: pytest.MonkeyPatch):
        task = _make_task()
        task.status = TaskStatus.RUNNING
        save_task(task)
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        append_event(task, "api_error", {"terminal": "rate limit"})
        results = _doctor_check_capi_error()
        assert results == []

    def test_capi_error_detected(self, monkeypatch: pytest.MonkeyPatch):
        task = _make_task()
        task.status = TaskStatus.RUNNING
        save_task(task)
        monkeypatch.setattr("duo.cli.doctor.list_tasks", lambda: [task])
        append_event(task, "capi_error", {"terminal": "CAPIError: 400"})
        results = _doctor_check_capi_error()
        assert len(results) == 1
        assert results[0].status == "fail"
        assert "CAPIError" in results[0].message
        assert "restart" in results[0].fix.lower()
