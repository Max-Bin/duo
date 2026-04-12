"""CLI tests for batch commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from duo.cli import (
    _load_batch_file,
    main,
)


class TestBatchEdgeCases:
    def test_batch_invalid_json_file(self, runner: CliRunner, tmp_path: Path):
        """batch with a malformed JSON file shows an error."""
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json!!!}")
        result = runner.invoke(main, ["batch", str(bad)])
        assert result.exit_code != 0

    def test_batch_invalid_yaml_file(self, runner: CliRunner, tmp_path: Path):
        """batch with non-mapping YAML shows error (if pyyaml available)."""
        try:
            import yaml  # noqa: F401
        except ImportError:
            pytest.skip("PyYAML not installed")
        bad = tmp_path / "bad.yaml"
        # Write content that parses as a plain string, not a dict or list
        bad.write_text("just a plain string\n")
        result = runner.invoke(main, ["batch", str(bad)])
        assert result.exit_code != 0
        assert "tasks" in result.output.lower()


# ---------------------------------------------------------------------------
# export edge cases
# ---------------------------------------------------------------------------

# TestExportEdgeCases — deleted: duplicate of TestExport.test_task_not_found
# TestAuditEdgeCases — deleted: duplicate of TestAudit.test_audit_no_tasks

# ---------------------------------------------------------------------------
# cleanup edge cases
# ---------------------------------------------------------------------------


class TestLoadBatchFile:
    def test_load_json(self, tmp_path: Path):
        """Valid JSON batch file is parsed correctly."""
        f = tmp_path / "tasks.json"
        f.write_text(json.dumps({"tasks": [{"name": "a"}, {"name": "b"}]}))
        result = _load_batch_file(str(f))
        assert len(result) == 2
        assert result[0]["name"] == "a"

    def test_load_yaml(self, tmp_path: Path):
        """Valid YAML batch file is parsed correctly."""
        try:
            import yaml  # noqa: F401
        except ImportError:
            pytest.skip("PyYAML not installed")
        f = tmp_path / "tasks.yaml"
        f.write_text("tasks:\n  - name: y1\n  - name: y2\n")
        result = _load_batch_file(str(f))
        assert len(result) == 2
        assert result[0]["name"] == "y1"

    def test_yaml_no_pyyaml(self, tmp_path: Path):
        """YAML file without pyyaml installed exits with error."""
        f = tmp_path / "tasks.yml"
        f.write_text("tasks:\n  - name: t1\n")
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("no yaml")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            with pytest.raises(click.UsageError):
                _load_batch_file(str(f))

    def test_missing_tasks_key(self, tmp_path: Path):
        """JSON file without 'tasks' key raises UsageError."""
        f = tmp_path / "bad.json"
        f.write_text('{"items": []}')
        with pytest.raises(click.UsageError):
            _load_batch_file(str(f))

    def test_empty_tasks_list(self, tmp_path: Path):
        """JSON file with empty tasks list raises UsageError."""
        f = tmp_path / "empty.json"
        f.write_text('{"tasks": []}')
        with pytest.raises(click.UsageError):
            _load_batch_file(str(f))

    def test_load_yaml_safe_load_path(self, tmp_path: Path):
        """YAML safe_load path is exercised with mocked yaml module (line 439)."""
        import sys

        f = tmp_path / "tasks.yaml"
        f.write_text("tasks:\n  - name: y1\n")
        mock_yaml = MagicMock()
        mock_yaml.safe_load.return_value = {"tasks": [{"name": "y1"}]}

        with patch.dict(sys.modules, {"yaml": mock_yaml}):
            result = _load_batch_file(str(f))
            assert len(result) == 1
            assert result[0]["name"] == "y1"
            mock_yaml.safe_load.assert_called_once()

    def test_load_batch_file_read_error(self):
        """_load_batch_file raises UsageError when the file cannot be read."""
        with pytest.raises(click.UsageError):
            _load_batch_file("/no/such/path/batch.json")

    def test_load_batch_yaml_parse_error(self, tmp_path: Path):
        """_load_batch_file exits on yaml.YAMLError."""
        import sys

        f = tmp_path / "bad.yaml"
        f.write_text("{: bad yaml:}")
        mock_yaml = MagicMock()
        yaml_error = type("YAMLError", (Exception,), {})
        mock_yaml.YAMLError = yaml_error
        mock_yaml.safe_load.side_effect = yaml_error("parse error")

        with patch.dict(sys.modules, {"yaml": mock_yaml}):
            with pytest.raises(click.UsageError):
                _load_batch_file(str(f))

    def test_tasks_not_list(self, tmp_path: Path):
        """tasks key that is not a list raises UsageError."""
        f = tmp_path / "bad.json"
        f.write_text('{"tasks": "not-a-list"}')
        with pytest.raises(click.UsageError, match="must be a list"):
            _load_batch_file(str(f))

    def test_tasks_items_not_dicts(self, tmp_path: Path):
        """tasks list containing non-dict items raises UsageError."""
        f = tmp_path / "bad.json"
        f.write_text('{"tasks": ["string-item"]}')
        with pytest.raises(click.UsageError, match="must be a JSON object"):
            _load_batch_file(str(f))


# ---------------------------------------------------------------------------
# _create_single_task helper
# ---------------------------------------------------------------------------


class TestCreateTaskFromBatchDef:
    def test_success_started(self, runner: CliRunner, tmp_path: Path):
        """Task batch def that starts immediately."""
        from duo.cli import _create_single_task

        defn = {"name": "batch-a", "description": "Batch A", "target_files": ["a.py"]}
        with (
            patch("duo.cli.subprocess.run") as mock_run,
            patch("duo.config.get_config", return_value=str(tmp_path / "wt")),
            patch("duo.commander.start_session"),
            patch("duo.scheduler.enqueue_or_start", return_value="started"),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # rev-parse
                MagicMock(returncode=0, stdout="", stderr=""),  # worktree add
            ]
            result = _create_single_task(defn, str(tmp_path))
            assert result == "batch-a"

    def test_success_queued(self, runner: CliRunner, tmp_path: Path):
        """Task batch def that gets queued."""
        from duo.cli import _create_single_task

        defn = {"name": "batch-q", "description": "Queued"}
        with (
            patch("duo.cli.subprocess.run") as mock_run,
            patch("duo.config.get_config", return_value=str(tmp_path / "wt")),
            patch("duo.commander.start_session") as mock_start,
            patch("duo.scheduler.enqueue_or_start", return_value="queued"),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            result = _create_single_task(defn, str(tmp_path))
            assert result == "batch-q"
            mock_start.assert_not_called()

    def test_worktree_failure(self, runner: CliRunner, tmp_path: Path):
        """Task batch def fails when worktree creation fails."""
        from duo.cli import _create_single_task

        defn = {"name": "batch-fail", "description": "Fail"}
        with (
            patch("duo.cli.subprocess.run") as mock_run,
            patch("duo.config.get_config", return_value=str(tmp_path / "wt")),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\n", stderr=""),
                MagicMock(returncode=1, stdout="", stderr="error"),
            ]
            result = _create_single_task(defn, str(tmp_path))
            assert result is None

    def test_revparse_fails(self, tmp_path: Path):
        """_create_single_task exits when rev-parse fails."""
        from duo.cli import _create_single_task

        defn = {"name": "batch-rp", "description": "RP Fail"}
        with (
            patch("duo.cli.subprocess.run") as mock_run,
            patch("duo.config.get_config", return_value=str(tmp_path / "wt")),
        ):
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="not a git repo"
            )
            with pytest.raises(click.ClickException):
                _create_single_task(defn, str(tmp_path))


# ---------------------------------------------------------------------------
# batch command full flow
# ---------------------------------------------------------------------------


class TestBatchCommand:
    def test_batch_full_flow(self, runner: CliRunner, tmp_path: Path):
        """batch creates multiple tasks from a JSON file."""
        f = tmp_path / "tasks.json"
        f.write_text(
            json.dumps(
                {
                    "tasks": [
                        {"name": "b1", "description": "Task 1"},
                        {"name": "b2", "description": "Task 2"},
                    ]
                }
            )
        )

        with (
            patch("duo.cli.batch_cmd._create_single_task") as mock_create,
            patch(
                "duo.scheduler.queue_status",
                return_value={
                    "active_count": 2,
                    "queued_count": 0,
                    "max_parallel": 3,
                    "active_tasks": ["b1", "b2"],
                    "queued_tasks": [],
                },
            ),
        ):
            mock_create.side_effect = ["b1", "b2"]
            result = runner.invoke(main, ["batch", str(f), "--repo", str(tmp_path)])
            assert result.exit_code == 0
            assert "2 tasks created" in result.output
            assert "Active: 2/3" in result.output

    def test_batch_partial_failure(self, runner: CliRunner, tmp_path: Path):
        """batch: some tasks fail, count reflects only successes."""
        f = tmp_path / "tasks.json"
        f.write_text(
            json.dumps(
                {
                    "tasks": [
                        {"name": "ok1"},
                        {"name": "fail1"},
                    ]
                }
            )
        )

        with (
            patch("duo.cli.batch_cmd._create_single_task") as mock_create,
            patch(
                "duo.scheduler.queue_status",
                return_value={
                    "active_count": 1,
                    "queued_count": 0,
                    "max_parallel": 3,
                    "active_tasks": ["ok1"],
                    "queued_tasks": [],
                },
            ),
        ):
            mock_create.side_effect = ["ok1", None]  # second fails
            result = runner.invoke(main, ["batch", str(f), "--repo", str(tmp_path)])
            assert result.exit_code == 0
            assert "1 tasks created" in result.output

    def test_batch_with_queued_tasks(self, runner: CliRunner, tmp_path: Path):
        """batch shows queue message when tasks are queued (line 559)."""
        f = tmp_path / "tasks.json"
        f.write_text(json.dumps({"tasks": [{"name": "q1"}]}))

        with (
            patch("duo.cli.batch_cmd._create_single_task") as mock_create,
            patch(
                "duo.scheduler.queue_status",
                return_value={
                    "active_count": 2,
                    "queued_count": 3,
                    "max_parallel": 2,
                    "active_tasks": ["a1", "a2"],
                    "queued_tasks": ["q1", "q2", "q3"],
                },
            ),
        ):
            mock_create.return_value = "q1"
            result = runner.invoke(main, ["batch", str(f), "--repo", str(tmp_path)])
            assert result.exit_code == 0
            assert "Queued: 3" in result.output
            assert "duo monitor" in result.output

    def test_batch_queue_flag(self, runner: CliRunner, tmp_path: Path):
        """batch --queue creates tasks in QUEUED state."""
        f = tmp_path / "tasks.json"
        f.write_text(
            json.dumps(
                {
                    "tasks": [
                        {"name": "q1", "description": "Task 1"},
                        {"name": "q2", "description": "Task 2"},
                    ]
                }
            )
        )

        with (
            patch("duo.cli.batch_cmd._create_single_task") as mock_create,
            patch(
                "duo.scheduler.queue_status",
                return_value={
                    "active_count": 0,
                    "queued_count": 2,
                    "max_parallel": 3,
                    "active_tasks": [],
                    "queued_tasks": ["q1", "q2"],
                },
            ),
        ):
            mock_create.side_effect = ["q1", "q2"]
            result = runner.invoke(
                main, ["batch", str(f), "--queue", "--repo", str(tmp_path)]
            )
            assert result.exit_code == 0
            assert "2 tasks created" in result.output
            assert mock_create.call_count == 2

    def test_batch_json_output(self, runner: CliRunner, tmp_path: Path):
        """batch --json-output returns structured creation result."""
        f = tmp_path / "tasks.json"
        f.write_text(json.dumps({"tasks": [{"name": "j1"}, {"name": "j2"}]}))

        with (
            patch("duo.cli.batch_cmd._create_single_task") as mock_create,
            patch(
                "duo.scheduler.queue_status",
                return_value={
                    "active_count": 2,
                    "queued_count": 0,
                    "max_parallel": 3,
                    "active_tasks": ["j1", "j2"],
                    "queued_tasks": [],
                },
            ),
        ):
            mock_create.side_effect = ["j1", "j2"]
            result = runner.invoke(
                main, ["batch", str(f), "--repo", str(tmp_path), "--json-output"]
            )
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["created"] == 2
            assert data["tasks"] == ["j1", "j2"]
            assert data["active"] == 2

    def test_batch_json_dry_run(self, runner: CliRunner, tmp_path: Path):
        """batch --dry-run --json-output returns task preview."""
        f = tmp_path / "tasks.json"
        f.write_text(json.dumps({"tasks": [{"name": "d1", "description": "Desc 1"}]}))
        result = runner.invoke(main, ["batch", str(f), "--dry-run", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["dry_run"] is True
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["name"] == "d1"

    def test_batch_quiet_dry_run(self, runner: CliRunner, tmp_path: Path):
        """batch --dry-run -q prints only the task count."""
        f = tmp_path / "tasks.json"
        f.write_text(
            json.dumps(
                {
                    "tasks": [
                        {"name": "q1", "description": "A"},
                        {"name": "q2", "description": "B"},
                    ]
                }
            )
        )
        result = runner.invoke(main, ["batch", str(f), "--dry-run", "-q"])
        assert result.exit_code == 0
        assert result.output.strip() == "2"

    def test_batch_quiet(self, runner: CliRunner, tmp_path: Path):
        """batch -q prints only the created count."""
        f = tmp_path / "tasks.json"
        f.write_text(json.dumps({"tasks": [{"name": "bq1", "description": "D"}]}))
        with (
            patch("duo.cli.batch_cmd._create_single_task", return_value="bq1"),
            patch(
                "duo.scheduler.queue_status",
                return_value={
                    "active_count": 1,
                    "max_parallel": 3,
                    "queued_count": 0,
                    "active_tasks": ["bq1"],
                    "queued_tasks": [],
                },
            ),
        ):
            result = runner.invoke(
                main, ["batch", str(f), "--repo", str(tmp_path), "-q"]
            )
        assert result.exit_code == 0
        assert result.output.strip() == "1"


# ---------------------------------------------------------------------------
# queue command
# ---------------------------------------------------------------------------


class TestQueueCommand:
    def test_queue_with_active_and_queued(self, runner: CliRunner):
        """queue shows active and queued tasks."""
        with patch(
            "duo.scheduler.queue_status",
            return_value={
                "active_count": 2,
                "queued_count": 1,
                "max_parallel": 3,
                "active_tasks": ["task-a", "task-b"],
                "queued_tasks": ["task-c"],
            },
        ):
            result = runner.invoke(main, ["queue"])
            assert result.exit_code == 0
            assert "2" in result.output
            assert "3" in result.output
            assert "task-c" in result.output

    def test_queue_empty(self, runner: CliRunner):
        """queue with no tasks shows empty."""
        with patch(
            "duo.scheduler.queue_status",
            return_value={
                "active_count": 0,
                "queued_count": 0,
                "max_parallel": 3,
                "active_tasks": [],
                "queued_tasks": [],
            },
        ):
            result = runner.invoke(main, ["queue"])
            assert result.exit_code == 0
            assert "0" in result.output
            assert "empty" in result.output.lower()

    def test_queue_positions(self, runner: CliRunner):
        """queue shows numbered positions for queued tasks."""
        with patch(
            "duo.scheduler.queue_status",
            return_value={
                "active_count": 2,
                "queued_count": 3,
                "max_parallel": 4,
                "active_tasks": ["run-a", "run-b"],
                "queued_tasks": ["task-a", "task-b", "task-c"],
            },
        ):
            result = runner.invoke(main, ["queue"])
            assert result.exit_code == 0
            assert "Queue (3 tasks):" in result.output
            assert "1. task-a" in result.output
            assert "2. task-b" in result.output
            assert "3. task-c" in result.output
            assert "Active: 2 / max_parallel: 4" in result.output

    def test_queue_json_output(self, runner: CliRunner):
        """queue --json-output returns JSON."""
        mock_qs = {
            "active_count": 1,
            "queued_count": 2,
            "max_parallel": 3,
            "active_tasks": ["run-a"],
            "queued_tasks": ["q-a", "q-b"],
        }
        with patch("duo.scheduler.queue_status", return_value=mock_qs):
            result = runner.invoke(main, ["queue", "--json-output"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["active_count"] == 1
            assert data["queued_count"] == 2
            assert data["queued_tasks"] == ["q-a", "q-b"]

    def test_queue_quiet(self, runner: CliRunner):
        """queue -q prints only the queue length."""
        mock_qs = {
            "active_count": 1,
            "queued_count": 2,
            "max_parallel": 3,
            "active_tasks": ["run-a"],
            "queued_tasks": ["q-a", "q-b"],
        }
        with patch("duo.scheduler.queue_status", return_value=mock_qs):
            result = runner.invoke(main, ["queue", "-q"])
            assert result.exit_code == 0
            assert result.output.strip() == "2"

    def test_queue_quiet_empty(self, runner: CliRunner):
        """queue -q with empty queue prints 0."""
        mock_qs = {
            "active_count": 0,
            "queued_count": 0,
            "max_parallel": 3,
            "active_tasks": [],
            "queued_tasks": [],
        }
        with patch("duo.scheduler.queue_status", return_value=mock_qs):
            result = runner.invoke(main, ["queue", "-q"])
            assert result.exit_code == 0
            assert result.output.strip() == "0"


# ---------------------------------------------------------------------------
# audit command — session log path
# ---------------------------------------------------------------------------


class TestBatchValidation:
    """Additional edge cases for batch file validation."""

    def test_batch_tasks_null(self, tmp_path: Path):
        """_load_batch_file raises UsageError when 'tasks' value is null."""
        f = tmp_path / "bad.json"
        f.write_text(json.dumps({"tasks": None}))
        with pytest.raises(click.UsageError):
            _load_batch_file(str(f))

    def test_batch_missing_name_in_task_def(self, runner: CliRunner, tmp_path: Path):
        """batch task missing 'name' key prints error and skips that task."""
        f = tmp_path / "noname.json"
        f.write_text(json.dumps({"tasks": [{"description": "no name field"}]}))
        result = runner.invoke(main, ["batch", str(f), "--repo", str(tmp_path)])
        assert "(unnamed)" in result.output
        assert "missing 'name'" in result.output

    def test_batch_nonexistent_file(self, runner: CliRunner):
        """batch with a path that doesn't exist fails."""
        result = runner.invoke(main, ["batch", "/no/such/file.json"])
        assert result.exit_code != 0

    def test_batch_target_files_not_list_rejected(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """target_files as string instead of list is rejected."""
        f = tmp_path / "bad_tf.json"
        f.write_text(
            json.dumps({"tasks": [{"name": "t1", "target_files": "not-a-list"}]})
        )
        result = runner.invoke(main, ["batch", str(f), "--repo", str(tmp_path)])
        assert "'target_files' must be a list" in result.output

    def test_batch_writable_paths_not_list_rejected(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """writable_paths as dict instead of list is rejected."""
        f = tmp_path / "bad_wp.json"
        f.write_text(
            json.dumps({"tasks": [{"name": "t1", "writable_paths": {"a": 1}}]})
        )
        result = runner.invoke(main, ["batch", str(f), "--repo", str(tmp_path)])
        assert "'writable_paths' must be a list" in result.output


# ---------------------------------------------------------------------------
# --dry-run flags
# ---------------------------------------------------------------------------


class TestBatchInvalidName:
    """Batch rejects task definitions with invalid names."""

    def test_batch_invalid_name_traversal(self, runner: CliRunner, tmp_path: Path):
        """Batch file with path-traversal task name is rejected."""
        batch_file = tmp_path / "bad-names.json"
        batch_file.write_text(
            json.dumps({"tasks": [{"name": "../evil", "description": "bad"}]})
        )
        result = runner.invoke(
            main, ["batch", str(batch_file), "--repo", str(tmp_path)]
        )
        assert "invalid task name" in result.output.lower() or result.exit_code != 0


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------

# TestBatchCorruptedJson — deleted: duplicate of TestBatchEdgeCases.test_batch_invalid_json_file


class TestBatchCorruptedYaml:
    def test_batch_corrupted_yaml(self, runner: CliRunner, tmp_path: Path):
        """batch with corrupted YAML content shows a friendly error."""
        pytest.importorskip("yaml")
        bad = tmp_path / "tasks.yaml"
        bad.write_text("{: bad yaml:}")
        result = runner.invoke(main, ["batch", str(bad), "--repo", "."])
        assert result.exit_code != 0
        out = result.output + (result.stderr or "")
        assert "Error" in out or "invalid" in out.lower() or "error" in out.lower()


# TestBatchFileNotFound — deleted: duplicate of TestBatchValidation.test_batch_nonexistent_file


class TestBatchBareArray:
    def test_batch_bare_array_auto_wrapped(self, runner: CliRunner, tmp_path: Path):
        """Batch file with bare JSON array (not wrapped in {tasks:...}) is auto-wrapped."""
        batch_file = tmp_path / "bare-array.json"
        batch_file.write_text(
            json.dumps(
                [
                    {"name": "task-a", "description": "first task"},
                    {"name": "task-b", "description": "second task"},
                ]
            )
        )
        with (
            patch("duo.cli.batch_cmd._create_single_task") as mock_create,
            patch(
                "duo.scheduler.queue_status",
                return_value={
                    "active_count": 2,
                    "queued_count": 0,
                    "max_parallel": 3,
                    "active_tasks": ["task-a", "task-b"],
                    "queued_tasks": [],
                },
            ),
        ):
            mock_create.side_effect = ["task-a", "task-b"]
            result = runner.invoke(
                main, ["batch", str(batch_file), "--repo", str(tmp_path)]
            )
        # Should succeed, not error about missing 'tasks' key
        assert result.exit_code == 0
        assert "2 tasks created" in result.output


class TestBatchDuplicateNames:
    def test_batch_duplicate_names_rejected(self, runner: CliRunner, tmp_path: Path):
        """Batch file with duplicate task names is rejected."""
        batch_file = tmp_path / "dupes.json"
        batch_file.write_text(
            json.dumps(
                {
                    "tasks": [
                        {"name": "task-a", "description": "first"},
                        {"name": "task-b", "description": "second"},
                        {"name": "task-a", "description": "duplicate"},
                    ]
                }
            )
        )
        result = runner.invoke(
            main, ["batch", str(batch_file), "--repo", str(tmp_path)]
        )
        assert result.exit_code != 0
        assert "duplicate" in result.output.lower()
        assert "task-a" in result.output

    def test_batch_file_too_large_rejected(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Batch file exceeding 10 MB is rejected."""
        batch_file = tmp_path / "huge.json"
        batch_file.write_text("x" * (10_000_001))
        result = runner.invoke(
            main, ["batch", str(batch_file), "--repo", str(tmp_path)]
        )
        assert result.exit_code != 0
        assert "too large" in result.output.lower()


# ---------------------------------------------------------------------------
# Events command tests
# ---------------------------------------------------------------------------
