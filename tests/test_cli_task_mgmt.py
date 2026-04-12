"""CLI tests for task mgmt commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import duo.cli
import duo.protocol
from duo.cli import (
    main,
)
from duo.protocol import (
    Subtask,
    TaskStatus,
    create_task,
    load_task,
    read_jsonl,
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


class TestStop:
    def test_stop_not_found(self, runner: CliRunner):
        result = runner.invoke(main, ["stop", "ghost"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_stop_running_task(self, runner: CliRunner):
        """stop transitions task to BLOCKED and preserves worktree."""
        task = _make_task("stop-running")
        task.status = TaskStatus.RUNNING
        save_task(task)

        with patch("duo.cli.subprocess.run"):
            result = runner.invoke(main, ["stop", "stop-running"])
            assert result.exit_code == 0
            assert "Stopped" in result.output
            assert "resume" in result.output

        reloaded = load_task("stop-running")
        assert reloaded is not None
        assert reloaded.status == TaskStatus.BLOCKED

    def test_stop_already_completed(self, runner: CliRunner):
        """stop on completed task shows message."""
        task = _make_task("stop-done")
        task.status = TaskStatus.COMPLETED
        save_task(task)

        result = runner.invoke(main, ["stop", "stop-done"])
        assert result.exit_code == 0
        assert "terminal state" in result.output

    def test_stop_already_blocked(self, runner: CliRunner):
        """stop on already blocked task shows message."""
        task = _make_task("stop-blocked")
        task.status = TaskStatus.BLOCKED
        save_task(task)

        result = runner.invoke(main, ["stop", "stop-blocked"])
        assert result.exit_code == 0
        assert "already stopped" in result.output

    def test_stop_pane_kill_failure_warns(self, runner: CliRunner):
        """stop shows warning when kill_pane returns False."""
        task = _make_task("stop-pane-fail")
        task.status = TaskStatus.RUNNING
        save_task(task)

        with (
            patch("duo.transport.kill_pane", return_value=False),
            patch("duo.transport.cleanup_pane_state"),
        ):
            result = runner.invoke(main, ["stop", "stop-pane-fail"])
            assert result.exit_code == 0
            assert "Warning" in result.output
            assert "Stopped" in result.output

    def test_stop_records_event(self, runner: CliRunner):
        """stop logs task_stopped event with previous status."""
        task = _make_task("stop-event")
        task.status = TaskStatus.RUNNING
        save_task(task)

        with patch("duo.cli.subprocess.run"):
            runner.invoke(main, ["stop", "stop-event"])

        events = read_jsonl(task.journal_path)
        stopped_events = [e for e in events if e.get("event") == "task_stopped"]
        assert len(stopped_events) == 1
        assert stopped_events[0]["data"]["previous_status"] == "running"

    def test_stop_json_output(self, runner: CliRunner):
        """stop --json-output returns structured JSON."""
        task = _make_task("stop-json")
        task.status = TaskStatus.RUNNING
        save_task(task)

        with (
            patch("duo.transport.kill_pane", return_value=True),
            patch("duo.transport.cleanup_pane_state"),
        ):
            result = runner.invoke(main, ["stop", "stop-json", "--json-output"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["stopped"] is True
            assert data["previous_status"] == "running"
            assert "worktree" in data

    def test_stop_json_pane_kill_failure_silent(self, runner: CliRunner):
        """stop --json-output with kill_pane failure doesn't print warning text."""
        task = _make_task("stop-json-fail")
        task.status = TaskStatus.RUNNING
        save_task(task)

        with (
            patch("duo.transport.kill_pane", return_value=False),
            patch("duo.transport.cleanup_pane_state"),
        ):
            result = runner.invoke(main, ["stop", "stop-json-fail", "--json-output"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["stopped"] is True
            # No text warning in JSON mode
            assert "Warning" not in result.output.split("\n")[0]

    def test_stop_json_already_terminal(self, runner: CliRunner):
        """stop --json-output on terminal task returns reason."""
        task = _make_task("stop-json-term")
        task.status = TaskStatus.COMPLETED
        save_task(task)

        result = runner.invoke(main, ["stop", "stop-json-term", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["stopped"] is False
        assert data["reason"] == "already_terminal"

    def test_stop_json_already_blocked(self, runner: CliRunner):
        """stop --json-output on blocked task returns reason."""
        task = _make_task("stop-json-blk")
        task.status = TaskStatus.BLOCKED
        save_task(task)

        result = runner.invoke(main, ["stop", "stop-json-blk", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["stopped"] is False
        assert data["reason"] == "already_stopped"

    def test_stop_transition_failure_warns(self, runner: CliRunner):
        """stop warns when BLOCKED transition fails (text mode)."""
        task = _make_task("stop-trans-fail")
        task.status = TaskStatus.RUNNING
        save_task(task)

        with (
            patch("duo.cli.subprocess.run"),
            patch("duo.protocol.transition", return_value=False),
        ):
            result = runner.invoke(main, ["stop", "stop-trans-fail"])
            assert "Warning" in result.output
            assert "could not transition" in result.output

    def test_stop_transition_failure_json(self, runner: CliRunner):
        """stop --json-output reports stopped=False when transition fails."""
        task = _make_task("stop-trans-fail-j")
        task.status = TaskStatus.RUNNING
        save_task(task)

        with (
            patch("duo.cli.subprocess.run"),
            patch("duo.protocol.transition", return_value=False),
        ):
            result = runner.invoke(main, ["stop", "stop-trans-fail-j", "--json-output"])
            data = json.loads(result.output)
            assert data["stopped"] is False

    def test_stop_from_all_non_terminal_states(self, runner: CliRunner):
        """stop successfully transitions to BLOCKED from every non-terminal state."""
        non_terminal = [
            TaskStatus.CREATED,
            TaskStatus.QUEUED,
            TaskStatus.SESSION_STARTING,
            TaskStatus.PROMPT_SENT,
            TaskStatus.ACKED,
            TaskStatus.RUNNING,
            TaskStatus.RESULT_REPORTED,
            TaskStatus.VERIFYING,
            TaskStatus.CORRECTING,
            TaskStatus.ESCALATED,
        ]
        for state in non_terminal:
            tid = f"stop-{state.value}"
            task = _make_task(tid)
            task.status = state
            save_task(task)

            with patch("duo.cli.subprocess.run"):
                result = runner.invoke(main, ["stop", tid])
                assert result.exit_code == 0, f"stop failed for {state.value}"
                assert "Stopped" in result.output

            reloaded = load_task(tid)
            assert reloaded is not None
            assert reloaded.status == TaskStatus.BLOCKED

    def test_stop_all_stops_active_tasks(self, runner: CliRunner):
        """stop --all stops all non-terminal tasks."""
        t1 = _make_task("stop-all-1")
        t1.status = TaskStatus.RUNNING
        save_task(t1)
        t2 = _make_task("stop-all-2")
        t2.status = TaskStatus.ACKED
        save_task(t2)
        # completed task should be skipped
        t3 = _make_task("stop-all-3")
        t3.status = TaskStatus.COMPLETED
        save_task(t3)

        with patch("duo.cli.subprocess.run"):
            result = runner.invoke(main, ["stop", "--all"])
            assert result.exit_code == 0
            assert "stop-all-1" in result.output
            assert "stop-all-2" in result.output
            assert "stop-all-3" not in result.output
            assert "2 task(s) stopped" in result.output

    def test_stop_all_no_active(self, runner: CliRunner):
        """stop --all with no active tasks prints message."""
        result = runner.invoke(main, ["stop", "--all"])
        assert result.exit_code == 0
        assert "No active tasks" in result.output

    def test_stop_all_json(self, runner: CliRunner):
        """stop --all --json-output returns structured JSON."""
        t = _make_task("stop-all-j")
        t.status = TaskStatus.RUNNING
        save_task(t)

        with patch("duo.cli.subprocess.run"):
            result = runner.invoke(main, ["stop", "--all", "--json-output"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["stopped_count"] == 1
            assert len(data["tasks"]) == 1

    def test_stop_no_name_no_all(self, runner: CliRunner):
        """stop without name or --all shows error."""
        result = runner.invoke(main, ["stop"])
        assert result.exit_code != 0
        assert "Provide a task NAME or use --all" in result.output

    def test_stop_all_transition_failure(self, runner: CliRunner):
        """stop --all handles tasks where transition fails."""
        t = _make_task("stop-all-tf")
        t.status = TaskStatus.RUNNING
        save_task(t)

        with (
            patch("duo.cli.subprocess.run"),
            patch("duo.protocol.transition", return_value=False),
        ):
            result = runner.invoke(main, ["stop", "--all"])
            assert result.exit_code == 0
            assert "stop-all-tf" in result.output

    def test_stop_quiet_success(self, runner: CliRunner):
        """stop -q prints 'stopped' on success."""
        t = _make_task("stop-q-ok")
        t.status = TaskStatus.RUNNING
        save_task(t)
        with patch("duo.cli.subprocess.run"):
            result = runner.invoke(main, ["stop", "stop-q-ok", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "stopped"

    def test_stop_quiet_already_terminal(self, runner: CliRunner):
        """stop -q on completed task prints status value."""
        t = _make_task("stop-q-done")
        t.status = TaskStatus.COMPLETED
        save_task(t)
        result = runner.invoke(main, ["stop", "stop-q-done", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "completed"

    def test_stop_quiet_already_blocked(self, runner: CliRunner):
        """stop -q on blocked task prints 'blocked'."""
        t = _make_task("stop-q-blk")
        t.status = TaskStatus.BLOCKED
        save_task(t)
        result = runner.invoke(main, ["stop", "stop-q-blk", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "blocked"


# ---------------------------------------------------------------------------


class TestKillSuccess:
    def test_kill_existing_task(self, runner: CliRunner, tmp_path: Path):
        """kill terminates pane, removes worktree, and marks task failed."""
        task = _make_task("kill-task")
        # Create fake worktree dir so os.path.exists returns True
        wt_dir = tmp_path / "fake_worktree"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        save_task(task)

        with patch("duo.cli.subprocess.run") as mock_run:
            # tmux kill-pane, git worktree list, git worktree remove, git branch -D
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="worktree /main\n  branch refs/heads/main\n\n",
                stderr="",
            )
            result = runner.invoke(main, ["kill", "kill-task"])
            assert result.exit_code == 0
            assert "Killed kill-task" in result.output
            assert mock_run.call_count >= 3  # tmux, worktree list, remove, branch

    def test_kill_nonexistent(self, runner: CliRunner):
        result = runner.invoke(main, ["kill", "nope"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_kill_pane_kill_failure_warns(self, runner: CliRunner, tmp_path: Path):
        """kill shows warning when kill_pane returns False."""
        task = _make_task("kill-pane-fail")
        wt_dir = tmp_path / "kill_pane_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        save_task(task)

        def mock_subprocess_run(args, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            if args[:3] == ["git", "worktree", "list"]:
                m.stdout = "worktree /main\n  branch refs/heads/main\n\n"
            return m

        with (
            patch("duo.transport.kill_pane", return_value=False),
            patch("duo.transport.cleanup_pane_state"),
            patch("duo.cli.subprocess.run", side_effect=mock_subprocess_run),
        ):
            result = runner.invoke(main, ["kill", "kill-pane-fail"])
            assert result.exit_code == 0
            assert "Warning" in result.output

    def test_kill_cleanup_warnings(self, runner: CliRunner, tmp_path: Path):
        """kill shows warnings when git cleanup fails."""
        task = _make_task("kill-warn")
        wt_dir = tmp_path / "kill_warn_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        task.branch = "duo/kill-warn"
        save_task(task)

        def mock_subprocess_run(args, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            if args[:3] == ["git", "worktree", "list"]:
                m.stdout = "worktree /main/repo\n\n"
            if args[:3] == ["git", "worktree", "remove"]:
                m.returncode = 1
                m.stderr = "is dirty"
            if args[:3] == ["git", "branch", "-D"]:
                m.returncode = 1
                m.stderr = "not found"
            return m

        with patch("duo.cli.subprocess.run", side_effect=mock_subprocess_run):
            result = runner.invoke(main, ["kill", "kill-warn"])
            assert result.exit_code == 0
            assert "Warning: worktree removal failed" in result.output
            assert "Warning: branch deletion failed" in result.output

    def test_kill_json_output(self, runner: CliRunner, tmp_path: Path):
        """kill --json-output returns structured JSON."""
        task = _make_task("kill-json")
        wt_dir = tmp_path / "kill_json_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        save_task(task)

        with patch("duo.cli.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="worktree /main\n  branch refs/heads/main\n\n",
                stderr="",
            )
            result = runner.invoke(main, ["kill", "kill-json", "--json-output"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["killed"] is True
            assert "pane_killed" in data
            assert "worktree_removed" in data
            assert "branch_deleted" in data

    def test_kill_emits_status_changed_event(self, runner: CliRunner, tmp_path: Path):
        """kill uses transition() to emit status_changed for journal replay."""
        from duo.protocol import TaskStatus, read_jsonl, transition

        task = _make_task("kill-trans")
        transition(task, TaskStatus.SESSION_STARTING)
        wt_dir = tmp_path / "kill_trans_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        save_task(task)

        with patch("duo.cli.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="worktree /main\n  branch refs/heads/main\n\n",
                stderr="",
            )
            result = runner.invoke(main, ["kill", "kill-trans"])
            assert result.exit_code == 0
        events = read_jsonl(task.journal_path)
        status_events = [e for e in events if e.get("event") == "status_changed"]
        assert any(e["data"]["to"] == "failed" for e in status_events)

    def test_kill_completed_task_uses_fallback(self, runner: CliRunner, tmp_path: Path):
        """kill from COMPLETED falls back to direct save (no FAILED transition)."""
        from duo.protocol import TaskStatus, transition

        task = _make_task("kill-comp")
        transition(task, TaskStatus.SESSION_STARTING)
        transition(task, TaskStatus.PROMPT_SENT)
        transition(task, TaskStatus.ACKED)
        transition(task, TaskStatus.RUNNING)
        transition(task, TaskStatus.RESULT_REPORTED)
        transition(task, TaskStatus.VERIFYING)
        transition(task, TaskStatus.COMPLETED)
        wt_dir = tmp_path / "kill_comp_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        save_task(task)

        with patch("duo.cli.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="worktree /main\n  branch refs/heads/main\n\n",
                stderr="",
            )
            result = runner.invoke(main, ["kill", "kill-comp"])
            assert result.exit_code == 0
        task_reloaded = load_task("kill-comp")
        assert task_reloaded.status == TaskStatus.FAILED


class TestKill:
    def test_missing_task(self, runner: CliRunner):
        result = runner.invoke(main, ["kill", "nope"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_kill_invalid_name(self, runner: CliRunner):
        """kill rejects task names containing path traversal characters."""
        result = runner.invoke(main, ["kill", "../bad"])
        assert result.exit_code != 0

    def test_kill_invalid_name_slash(self, runner: CliRunner):
        """kill rejects task names with slashes."""
        result = runner.invoke(main, ["kill", "foo/bar"])
        assert result.exit_code != 0

    def test_kill_empty_worktree_list(self, runner: CliRunner):
        """kill handles empty git worktree list gracefully."""
        task = _make_task("kill-empty-wt")
        task.status = TaskStatus.RUNNING
        save_task(task)

        proc = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("duo.cli.subprocess.run", return_value=proc),
            patch("duo.transport.kill_pane", return_value=True),
            patch("duo.transport.cleanup_pane_state"),
        ):
            result = runner.invoke(main, ["kill", "kill-empty-wt"])
            assert result.exit_code == 0

    def test_kill_worktree_list_no_match(self, runner: CliRunner):
        """kill handles worktree list where no line matches worktree pattern."""
        task = _make_task("kill-no-match")
        task.status = TaskStatus.RUNNING
        save_task(task)

        # All lines contain worktree_base_path or don't start with 'worktree '
        wt_output = "branch refs/heads/main\nbare\n"
        proc = MagicMock(returncode=0, stdout=wt_output, stderr="")
        with (
            patch("duo.cli.subprocess.run", return_value=proc),
            patch("duo.transport.kill_pane", return_value=True),
            patch("duo.transport.cleanup_pane_state"),
        ):
            result = runner.invoke(main, ["kill", "kill-no-match"])
            assert result.exit_code == 0


# ---------------------------------------------------------------------------
# logs command
# ---------------------------------------------------------------------------


class TestKillAll:
    def test_kill_all(self, runner: CliRunner):
        """kill --all kills all tasks."""
        _make_task("kill-all-1")
        _make_task("kill-all-2")

        proc = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("duo.cli.subprocess.run", return_value=proc),
            patch("duo.transport.kill_pane", return_value=True),
            patch("duo.transport.cleanup_pane_state"),
        ):
            result = runner.invoke(main, ["kill", "--all"])
            assert result.exit_code == 0
            assert "kill-all-1" in result.output
            assert "kill-all-2" in result.output
            assert "2 task(s) killed" in result.output

    def test_kill_all_empty(self, runner: CliRunner):
        """kill --all with no tasks prints message."""
        result = runner.invoke(main, ["kill", "--all"])
        assert result.exit_code == 0
        assert "No tasks to kill" in result.output

    def test_kill_all_json(self, runner: CliRunner):
        """kill --all --json-output returns structured JSON."""
        _make_task("kill-all-j")

        proc = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("duo.cli.subprocess.run", return_value=proc),
            patch("duo.transport.kill_pane", return_value=True),
            patch("duo.transport.cleanup_pane_state"),
        ):
            result = runner.invoke(main, ["kill", "--all", "--json-output"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["killed_count"] == 1

    def test_kill_no_name_no_all(self, runner: CliRunner):
        """kill without name or --all shows error."""
        result = runner.invoke(main, ["kill"])
        assert result.exit_code != 0
        assert "Provide a task NAME or use --all" in result.output

    def test_kill_all_with_worktree(self, runner: CliRunner, tmp_path: Path):
        """kill --all removes worktrees that exist on disk."""
        task = _make_task("kill-all-wt")
        wt_dir = tmp_path / "fake_worktree"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        task.status = TaskStatus.RUNNING
        save_task(task)

        proc = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("duo.cli.subprocess.run", return_value=proc),
            patch("duo.transport.kill_pane", return_value=True),
            patch("duo.transport.cleanup_pane_state"),
        ):
            result = runner.invoke(main, ["kill", "--all"])
            assert result.exit_code == 0
            assert "kill-all-wt" in result.output

    def test_kill_all_transition_fallback(self, runner: CliRunner):
        """kill --all falls back to direct save when transition fails."""
        task = _make_task("kill-all-fb")
        task.status = TaskStatus.CREATED
        save_task(task)

        proc = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("duo.cli.subprocess.run", return_value=proc),
            patch("duo.transport.kill_pane", return_value=True),
            patch("duo.transport.cleanup_pane_state"),
        ):
            result = runner.invoke(main, ["kill", "--all"])
            assert result.exit_code == 0
        reloaded = load_task("kill-all-fb")
        assert reloaded.status == TaskStatus.FAILED

    def test_kill_quiet(self, runner: CliRunner, tmp_path: Path):
        """kill -q prints only the task name."""
        task = _make_task("kill-q")
        wt_dir = tmp_path / "kq_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        task.status = TaskStatus.RUNNING
        save_task(task)

        proc = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("duo.cli.subprocess.run", return_value=proc),
            patch("duo.transport.kill_pane", return_value=True),
            patch("duo.transport.cleanup_pane_state"),
        ):
            result = runner.invoke(main, ["kill", "kill-q", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "kill-q"


class TestMergeCommand:
    def test_merge_not_completed(self, runner: CliRunner):
        """merge refuses non-completed tasks."""
        task = _make_task("merge-nc")
        task.status = TaskStatus.RUNNING
        save_task(task)
        result = runner.invoke(main, ["merge", "merge-nc"])
        assert result.exit_code != 0
        assert "not 'completed'" in result.output

    def test_merge_not_found(self, runner: CliRunner):
        result = runner.invoke(main, ["merge", "ghost"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_merge_invalid_name(self, runner: CliRunner):
        """merge rejects task names containing path traversal characters."""
        result = runner.invoke(main, ["merge", "../bad"])
        assert result.exit_code != 0

    def test_merge_invalid_name_slash(self, runner: CliRunner):
        """merge rejects task names with slashes."""
        result = runner.invoke(main, ["merge", "foo/bar"])
        assert result.exit_code != 0

    def test_merge_completed_task(self, runner: CliRunner, tmp_path: Path):
        """merge: happy path — fetch, rebase, ff-merge, cleanup."""
        task = _make_task("merge-ok")
        task.status = TaskStatus.COMPLETED
        wt_dir = tmp_path / "merge_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        task.branch = "duo/merge-ok"
        save_task(task)

        worktree_base = "/some/worktree/base"

        call_count = [0]

        def mock_subprocess_run(args, **kwargs):
            call_count[0] += 1
            m = MagicMock(returncode=0, stdout="", stderr="")
            # git worktree list --porcelain: return main + task worktree
            if args[:3] == ["git", "worktree", "list"]:
                m.stdout = (
                    f"worktree /main/repo\n\nworktree {worktree_base}/merge-ok\n\n"
                )
            return m

        with (
            patch("duo.cli.subprocess.run", side_effect=mock_subprocess_run),
            patch("duo.cli._helpers.get_config", return_value=worktree_base),
        ):
            result = runner.invoke(main, ["merge", "merge-ok"])
            assert result.exit_code == 0
            assert "Merged merge-ok" in result.output
            assert "git push" in result.output

    def test_merge_rebase_conflict(self, runner: CliRunner, tmp_path: Path):
        """merge exits on rebase conflict."""
        task = _make_task("merge-conflict")
        task.status = TaskStatus.COMPLETED
        wt_dir = tmp_path / "merge_conflict_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        save_task(task)

        call_idx = [0]

        def mock_subprocess_run(args, **kwargs):
            call_idx[0] += 1
            m = MagicMock(returncode=0, stdout="", stderr="")
            # fetch succeeds
            if args[:3] == ["git", "fetch", "origin"]:
                return m
            # rebase fails
            if args[:2] == ["git", "rebase"] and "--abort" not in args:
                m.returncode = 1
                m.stderr = "CONFLICT"
                return m
            # rebase --abort succeeds
            if args == ["git", "rebase", "--abort"]:
                return m
            return m

        with patch("duo.cli.subprocess.run", side_effect=mock_subprocess_run):
            result = runner.invoke(main, ["merge", "merge-conflict"])
            assert result.exit_code != 0
            assert "Rebase conflict" in result.output

    def test_merge_fetch_fails_continues(self, runner: CliRunner, tmp_path: Path):
        """merge continues when git fetch fails (line 311)."""
        task = _make_task("merge-fetch")
        task.status = TaskStatus.COMPLETED
        wt_dir = tmp_path / "merge_fetch_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        task.branch = "duo/merge-fetch"
        save_task(task)

        worktree_base = "/some/worktree/base"

        def mock_subprocess_run(args, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            if args[:3] == ["git", "fetch", "origin"]:
                m.returncode = 1
                m.stderr = "could not resolve host"
                return m
            if args[:3] == ["git", "worktree", "list"]:
                m.stdout = (
                    f"worktree /main/repo\n\nworktree {worktree_base}/merge-fetch\n\n"
                )
            return m

        with (
            patch("duo.cli.subprocess.run", side_effect=mock_subprocess_run),
            patch("duo.cli._helpers.get_config", return_value=worktree_base),
        ):
            result = runner.invoke(main, ["merge", "merge-fetch"])
            assert result.exit_code == 0
            assert "Warning: fetch failed" in result.output

    def test_merge_rebase_abort_fails(self, runner: CliRunner, tmp_path: Path):
        """merge warns when rebase --abort also fails (line 322)."""
        task = _make_task("merge-abortfail")
        task.status = TaskStatus.COMPLETED
        wt_dir = tmp_path / "merge_abortfail_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        save_task(task)

        def mock_subprocess_run(args, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            if args[:3] == ["git", "fetch", "origin"]:
                return m
            if args[:2] == ["git", "rebase"] and "--abort" not in args:
                m.returncode = 1
                m.stderr = "CONFLICT"
                return m
            if args == ["git", "rebase", "--abort"]:
                m.returncode = 1
                m.stderr = "abort failed"
                return m
            return m

        with patch("duo.cli.subprocess.run", side_effect=mock_subprocess_run):
            result = runner.invoke(main, ["merge", "merge-abortfail"])
            assert result.exit_code != 0
            assert "Rebase conflict" in result.output
            assert "could not abort rebase" in result.output

    def test_merge_no_main_worktree(self, runner: CliRunner, tmp_path: Path):
        """merge errors when no main worktree found (lines 342-347 unreachable
        due to uninitialized main_worktree; verifies the error path)."""
        task = _make_task("merge-nomain")
        task.status = TaskStatus.COMPLETED
        wt_dir = tmp_path / "merge_nomain_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        save_task(task)

        worktree_base = "/some/worktree/base"

        def mock_subprocess_run(args, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            if args[:3] == ["git", "worktree", "list"]:
                # All worktrees are under worktree_base — no main worktree
                m.stdout = f"worktree {worktree_base}/merge-nomain\n\n"
            return m

        with (
            patch("duo.cli.subprocess.run", side_effect=mock_subprocess_run),
            patch("duo.cli._helpers.get_config", return_value=worktree_base),
        ):
            result = runner.invoke(main, ["merge", "merge-nomain"])
            assert result.exit_code != 0

    def test_merge_cleanup_warnings(self, runner: CliRunner, tmp_path: Path):
        """merge shows warnings when worktree/branch cleanup fails."""
        task = _make_task("merge-warn")
        task.status = TaskStatus.COMPLETED
        wt_dir = tmp_path / "merge_warn_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        task.branch = "duo/merge-warn"
        save_task(task)

        worktree_base = "/some/worktree/base"

        def mock_subprocess_run(args, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            if args[:3] == ["git", "worktree", "list"]:
                m.stdout = (
                    f"worktree /main/repo\n\nworktree {worktree_base}/merge-warn\n\n"
                )
            # Cleanup fails
            if args[:3] == ["git", "worktree", "remove"]:
                m.returncode = 1
                m.stderr = "dirty worktree"
            if args[:3] == ["git", "branch", "-d"]:
                m.returncode = 1
                m.stderr = "branch not found"
            return m

        with (
            patch("duo.cli.subprocess.run", side_effect=mock_subprocess_run),
            patch("duo.cli._helpers.get_config", return_value=worktree_base),
        ):
            result = runner.invoke(main, ["merge", "merge-warn"])
            assert result.exit_code == 0
            assert "Warning: worktree removal failed" in result.output
            assert "Warning: branch deletion failed" in result.output

    def test_merge_ff_only_fails(self, runner: CliRunner, tmp_path: Path):
        """merge exits when ff-only merge fails (lines 358-359)."""
        task = _make_task("merge-ff")
        task.status = TaskStatus.COMPLETED
        wt_dir = tmp_path / "merge_ff_wt"
        wt_dir.mkdir()
        task.worktree = str(wt_dir)
        task.branch = "duo/merge-ff"
        save_task(task)

        worktree_base = "/some/worktree/base"

        def mock_subprocess_run(args, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            if args[:3] == ["git", "worktree", "list"]:
                m.stdout = (
                    f"worktree /main/repo\n\nworktree {worktree_base}/merge-ff\n\n"
                )
            if args[:2] == ["git", "merge"]:
                m.returncode = 1
                m.stderr = "not possible to fast-forward"
            return m

        with (
            patch("duo.cli.subprocess.run", side_effect=mock_subprocess_run),
            patch("duo.cli._helpers.get_config", return_value=worktree_base),
        ):
            result = runner.invoke(main, ["merge", "merge-ff"])
            assert result.exit_code != 0
            assert "Merge failed" in result.output


# ---------------------------------------------------------------------------
# _load_batch_file helper
# ---------------------------------------------------------------------------
