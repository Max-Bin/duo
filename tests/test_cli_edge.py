"""Edge case tests for duo.cli — unicode names, zero ages, corrupted tasks, budget, dispatch."""

from __future__ import annotations

import json
import string
import subprocess
from collections.abc import Callable
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
    Task,
    create_task,
    save_task,
)
from duo.transport import DialogKind

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
    monkeypatch.setattr(duo.ceo_state, "CEO_STATE_PATH", tmp_path / "ceo-state.json")
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


class TestCeoDispatchTimeoutZero:
    """Edge case tests for ceo-dispatch with --timeout 0."""

    def test_timeout_zero_dialog_present(
        self,
        runner: CliRunner,
        make_task: Callable[..., Task],
    ) -> None:
        """ceo-dispatch --timeout 0 with dialog already present processes it."""
        task = make_task("dispatch-instant")
        pane = "  1. Continue\n  2. Cancel"
        with (
            patch("duo.transport.is_in_dialog", return_value=True),
            patch("duo.transport.get_dialog_kind", return_value=DialogKind.OPTION),
            patch("duo.transport.read_pane", return_value=pane),
            patch("duo.transport.is_permission_dialog", return_value=False),
            patch("duo.transport.select_dialog_option") as mock_sel,
        ):
            result = runner.invoke(main, ["ceo-dispatch", task.id, "--timeout", "0"])
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
        """CLAUDE.md says '52 commands' — verify this matches reality."""
        ctx = click.Context(main)
        registered = main.list_commands(ctx)
        assert len(registered) == 52, (
            f"CLAUDE.md says 52 commands but found {len(registered)}. "
            "Update CLAUDE.md if commands were added/removed."
        )


class TestModuleExports:
    """Guard that all source modules define __all__."""

    def test_all_modules_have_all_exports(self) -> None:
        """Every module in src/duo/ must define __all__ for public API clarity."""
        import importlib

        modules = [
            "ceo_log",
            "ceo_state",
            "cli",
            "commander",
            "config",
            "dashboard",
            "errors",
            "poller",
            "protocol",
            "scheduler",
            "thinking",
            "transport",
            "verifier",
        ]
        missing: list[str] = []
        for name in modules:
            mod = importlib.import_module(f"duo.{name}")
            if not hasattr(mod, "__all__"):
                missing.append(name)
        assert missing == [], f"Modules missing __all__: {missing}"

    def test_all_exports_exist(self) -> None:
        """Every name in __all__ must be a real attribute of the module."""
        import importlib

        modules = [
            "ceo_log",
            "ceo_state",
            "cli",
            "commander",
            "config",
            "dashboard",
            "errors",
            "poller",
            "protocol",
            "scheduler",
            "thinking",
            "transport",
            "verifier",
        ]
        bad: list[str] = []
        for name in modules:
            mod = importlib.import_module(f"duo.{name}")
            all_names = getattr(mod, "__all__", [])
            bad.extend(
                f"duo.{name}.{attr}" for attr in all_names if not hasattr(mod, attr)
            )
        assert bad == [], f"__all__ references missing attributes: {bad}"


class TestDuoUserErrorFixSuggestion:
    """Guard: every DuoUserError must include a fix= suggestion."""

    def test_all_duo_user_errors_have_fix(self) -> None:
        """Every raise DuoUserError(...) in cli.py must have fix= keyword."""
        import ast

        src = Path(duo.cli.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        missing: list[int] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and node.exc:
                call = node.exc
                if isinstance(call, ast.Call):
                    func = call.func
                    if isinstance(func, ast.Name) and func.id == "DuoUserError":
                        has_fix = any(kw.arg == "fix" for kw in call.keywords)
                        if not has_fix:
                            missing.append(node.lineno)
        assert missing == [], f"DuoUserError at lines {missing} missing fix= parameter"


class TestCommandHelpText:
    """Guard: every CLI command must have meaningful help text."""

    def test_all_commands_have_help(self) -> None:
        """Every registered Click command must have help text >= 10 chars."""
        ctx = click.Context(main)
        commands = main.list_commands(ctx)
        short_help: list[str] = []
        for name in commands:
            cmd = main.get_command(ctx, name)
            if not cmd or not cmd.help or len(cmd.help.strip()) < 10:
                short_help.append(name)
        assert short_help == [], f"Commands with missing/short help text: {short_help}"


class TestNoDuplicateTestClasses:
    """Guard against duplicate class names within the same test file."""

    def test_no_shadowed_classes(self) -> None:
        """No test file should have two classes with the same name.

        Python silently redefines the class, causing all tests from the
        first class to be lost. This bit us with TestCeoMetrics (20 tests
        were silently never running).
        """
        import re

        tests_dir = Path(__file__).resolve().parent
        dupes: list[str] = []
        for f in sorted(tests_dir.glob("test_*.py")):
            content = f.read_text(encoding="utf-8")
            classes = re.findall(r"^class (Test\w+)", content, re.MULTILINE)
            seen: set[str] = set()
            for c in classes:
                if c in seen:
                    dupes.append(f"{f.name}::{c}")
                seen.add(c)
        assert dupes == [], (
            f"Duplicate test classes (shadowed, tests silently lost): {dupes}"
        )

    def test_no_shadowed_methods(self) -> None:
        """No test class should have two methods with the same name.

        Python silently redefines the method, causing the first test to
        never run. Found 4 shadowed tests in TestCeoSelect.
        """
        import re

        tests_dir = Path(__file__).resolve().parent
        dupes: list[str] = []
        for f in sorted(tests_dir.glob("test_*.py")):
            content = f.read_text(encoding="utf-8")
            parts = re.split(r"^class (Test\w+)", content, flags=re.MULTILINE)
            for i in range(1, len(parts), 2):
                class_name = parts[i]
                body = parts[i + 1].split("\nclass ")[0]
                methods = re.findall(r"def (test_\w+)", body)
                seen: set[str] = set()
                for m in methods:
                    if m in seen:
                        dupes.append(f"{f.name}::{class_name}::{m}")
                    seen.add(m)
        assert dupes == [], (
            f"Duplicate test methods (shadowed, tests silently lost): {dupes}"
        )


class TestConftestIsolation:
    """Guard: conftest.py must isolate all test files from ~/.duo."""

    def test_tasks_dir_is_tmp(self, tmp_path: Path) -> None:
        """TASKS_DIR must point to a temp directory, not ~/.duo/tasks."""
        assert "tmp" in str(duo.protocol.TASKS_DIR).lower() or str(tmp_path) in str(
            duo.protocol.TASKS_DIR
        )

    def test_config_path_is_tmp(self, tmp_path: Path) -> None:
        """CONFIG_PATH must point to a temp directory."""
        from duo.config import CONFIG_PATH

        assert "tmp" in str(CONFIG_PATH).lower() or str(tmp_path) in str(CONFIG_PATH)

    def test_corrupted_dir_is_tmp(self, tmp_path: Path) -> None:
        """_CORRUPTED_DIR must point to a temp directory."""
        corrupted = duo.protocol._CORRUPTED_DIR
        assert "tmp" in str(corrupted).lower() or str(tmp_path) in str(corrupted)


class TestPragmaNoCoverDocumented:
    """Guard: every # pragma: no cover must have an explanation."""

    def test_all_pragmas_have_rationale(self) -> None:
        """Every pragma: no cover must have a comment explaining why."""
        src_dir = Path(duo.cli.__file__).resolve().parent
        undocumented: list[str] = []
        for f in sorted(src_dir.glob("*.py")):
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if "pragma: no cover" in line:
                    after = line.split("pragma: no cover", 1)[1].strip()
                    if not after or after == "#":
                        undocumented.append(f"{f.name}:{i}")
        assert undocumented == [], f"pragma: no cover without rationale: {undocumented}"


class TestModuleAllExports:
    """Guard: every name in __all__ must exist in its module."""

    MODULES = [
        "duo.ceo_log",
        "duo.ceo_state",
        "duo.cli",
        "duo.commander",
        "duo.config",
        "duo.dashboard",
        "duo.errors",
        "duo.poller",
        "duo.protocol",
        "duo.scheduler",
        "duo.thinking",
        "duo.transport",
        "duo.verifier",
    ]

    def test_all_exports_exist(self) -> None:
        """Every name listed in __all__ must be an actual attribute."""
        import importlib

        missing: list[str] = []
        for mod_name in self.MODULES:
            mod = importlib.import_module(mod_name)
            missing.extend(
                f"{mod_name}.{name}"
                for name in getattr(mod, "__all__", [])
                if not hasattr(mod, name)
            )
        assert missing == [], f"__all__ references non-existent attributes: {missing}"

    def test_no_empty_all(self) -> None:
        """Every module must export at least one name."""
        import importlib

        empty: list[str] = []
        for mod_name in self.MODULES:
            mod = importlib.import_module(mod_name)
            if not getattr(mod, "__all__", []):
                empty.append(mod_name)
        assert empty == [], f"Modules with empty __all__: {empty}"


class TestSecurityGuards:
    """Guard: security-critical patterns must not appear in production code."""

    def test_no_shell_true(self) -> None:
        """No subprocess call should use shell=True."""
        src_dir = Path(duo.cli.__file__).resolve().parent
        violations: list[str] = []
        for f in sorted(src_dir.glob("*.py")):
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if "shell=True" in line and not line.strip().startswith("#"):
                    violations.append(f"{f.name}:{i}")
        assert violations == [], f"shell=True found in production code: {violations}"

    def test_no_eval_exec(self) -> None:
        """No production code should use eval() or exec()."""
        src_dir = Path(duo.cli.__file__).resolve().parent
        violations: list[str] = []
        for f in sorted(src_dir.glob("*.py")):
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "eval(" in stripped or "exec(" in stripped:
                    violations.append(f"{f.name}:{i}: {stripped[:60]}")
        assert violations == [], f"eval/exec found in production code: {violations}"


class TestVersionConsistency:
    """Guard: runtime version must match pyproject.toml."""

    def test_version_matches_pyproject(self) -> None:
        """duo.__version__ must equal pyproject.toml version."""
        import tomllib

        pyproject = (
            Path(duo.cli.__file__).resolve().parent.parent.parent / "pyproject.toml"
        )
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
        expected = data["project"]["version"]
        assert duo.__version__ == expected, (
            f"Version mismatch: duo.__version__={duo.__version__!r}, "
            f"pyproject.toml={expected!r}"
        )


class TestConfigKeysDocumented:
    """Guard: all config keys must appear in getting-started.md."""

    def test_all_config_keys_in_docs(self) -> None:
        """Every key in DEFAULTS must be mentioned in getting-started.md."""
        from duo.config import DEFAULTS

        docs_dir = Path(duo.cli.__file__).resolve().parent.parent.parent / "docs"
        doc = (docs_dir / "getting-started.md").read_text(encoding="utf-8")
        missing = [k for k in sorted(DEFAULTS) if k not in doc]
        assert missing == [], f"Config keys missing from getting-started.md: {missing}"


class TestDocCrossReferences:
    """Guard: all markdown cross-references must resolve to existing files."""

    def test_docs_links_valid(self) -> None:
        """Every [text](file.md) link in docs/ must point to a real file."""
        import re

        docs_dir = Path(duo.cli.__file__).resolve().parent.parent.parent / "docs"
        broken: list[str] = []
        for md_file in sorted(docs_dir.glob("*.md")):
            content = md_file.read_text(encoding="utf-8")
            links = re.findall(r"\[.*?\]\((\./[^)]+|[a-zA-Z0-9_-]+\.md)", content)
            for link in links:
                target = docs_dir / link.lstrip("./")
                if not target.exists():
                    broken.append(f"{md_file.name} -> {link}")
        assert broken == [], f"Broken doc cross-references: {broken}"


class TestFSMDocAccuracy:
    """Guard: architecture.md FSM transition table must match protocol.py."""

    def test_fsm_transitions_match_docs(self) -> None:
        """Every transition in architecture.md must match TRANSITIONS dict."""
        import re

        from duo.protocol import TRANSITIONS, TaskStatus

        docs_dir = Path(duo.cli.__file__).resolve().parent.parent.parent / "docs"
        doc = (docs_dir / "architecture.md").read_text(encoding="utf-8")

        doc_transitions: dict[str, set[str]] = {}
        for line in doc.splitlines():
            m = re.match(r"^(\w+)\s+.+\{(.+)\}", line.strip())
            if m:
                src = m.group(1)
                raw = {t.strip() for t in m.group(2).split(",")}
                doc_transitions[src] = raw - {""}  # filter empty string

        mismatches: list[str] = []
        for src_str, doc_targets in doc_transitions.items():
            src = TaskStatus(src_str.lower())
            code_targets = {t.value.upper() for t in TRANSITIONS.get(src, frozenset())}
            if doc_targets != code_targets:
                mismatches.append(
                    f"{src_str}: doc={sorted(doc_targets)}, code={sorted(code_targets)}"
                )

        assert mismatches == [], (
            f"FSM transition mismatches between docs and code: {mismatches}"
        )


class TestPublicAPIStability:
    """Guard: duo package public API must not accidentally shrink."""

    EXPECTED_EXPORTS = {
        "SecurityPolicy",
        "Subtask",
        "Task",
        "TaskStatus",
        "__version__",
        "create_task",
        "get_config",
        "list_tasks",
        "load_config",
        "load_task",
        "save_task",
        "set_config",
        "transition",
    }

    def test_public_api_complete(self) -> None:
        """duo.__all__ must contain all expected public symbols."""
        missing = self.EXPECTED_EXPORTS - set(duo.__all__)
        assert missing == set(), f"Missing from duo.__all__: {sorted(missing)}"


class TestDeadCodeGuard:
    """Guard: vulture must find zero dead code at 80% confidence."""

    def test_no_dead_code(self) -> None:
        result = subprocess.run(
            ["vulture", "src/duo/", "--min-confidence", "80"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"vulture found dead code:\n{result.stdout}"


class TestImportCycleGuard:
    """Guard: no circular imports among duo modules."""

    def test_no_import_cycles(self) -> None:
        """Import every duo module in a subprocess to catch circular imports."""
        modules = [
            "duo.cli",
            "duo.commander",
            "duo.config",
            "duo.poller",
            "duo.protocol",
            "duo.scheduler",
            "duo.thinking",
            "duo.transport",
            "duo.verifier",
        ]
        for mod in modules:
            result = subprocess.run(
                ["python", "-c", f"import {mod}"],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, f"Failed to import {mod}: {result.stderr}"


class TestDocstringCoverageGuard:
    """Guard: all public functions must have docstrings."""

    # Inner functions of decorators — not truly public
    ALLOWLIST = {
        ("transport.py", "decorator"),
        ("transport.py", "wrapper"),
    }

    def test_public_functions_have_docstrings(self) -> None:
        import ast

        missing = []
        for f in sorted(Path("src/duo").glob("*.py")):
            tree = ast.parse(f.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not node.name.startswith("_") and not ast.get_docstring(node):
                        if (f.name, node.name) not in self.ALLOWLIST:
                            missing.append(f"{f.name}:{node.lineno} {node.name}")
        assert missing == [], "Public functions without docstrings:\n" + "\n".join(
            f"  {m}" for m in missing
        )


class TestReturnTypeAnnotationGuard:
    """Guard: all public functions must have return type annotations."""

    def test_public_functions_have_return_types(self) -> None:
        import ast

        missing = []
        for f in sorted(Path("src/duo").glob("*.py")):
            tree = ast.parse(f.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not node.name.startswith("_") and node.returns is None:
                        missing.append(f"{f.name}:{node.lineno} {node.name}")
        assert missing == [], (
            "Public functions without return type annotation:\n"
            + "\n".join(f"  {m}" for m in missing)
        )


class TestParameterTypeAnnotationGuard:
    """Guard: all public function parameters must have type annotations."""

    # Click decorators inject params without annotations; inner functions too
    ALLOWLIST = {
        ("transport.py", "decorator"),
        ("transport.py", "wrapper"),
    }

    def test_public_functions_have_param_types(self) -> None:
        import ast

        missing = []
        for f in sorted(Path("src/duo").glob("*.py")):
            tree = ast.parse(f.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name.startswith("_"):
                        continue
                    if (f.name, node.name) in self.ALLOWLIST:
                        continue
                    for arg in node.args.args:
                        if arg.arg == "self" or arg.arg == "cls":
                            continue
                        if arg.annotation is None:
                            missing.append(
                                f"{f.name}:{node.lineno} {node.name}(..{arg.arg}..)"
                            )
        assert missing == [], "Parameters without type annotation:\n" + "\n".join(
            f"  {m}" for m in missing
        )


class TestTestNamingConventionGuard:
    """Guard: all methods in Test* classes must start with test_ or _."""

    def test_test_methods_follow_convention(self) -> None:
        import ast

        bad = []
        for f in sorted(Path("tests").glob("test_*.py")):
            tree = ast.parse(f.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef):
                            if not item.name.startswith(
                                "test_"
                            ) and not item.name.startswith("_"):
                                bad.append(
                                    f"{f.name}:{item.lineno} {node.name}.{item.name}"
                                )
        assert bad == [], "Test methods not following test_ convention:\n" + "\n".join(
            f"  {b}" for b in bad
        )


class TestChangelogFormatGuard:
    """Guard: CHANGELOG.md follows Keep a Changelog format."""

    def test_every_version_has_content(self) -> None:
        """Each version section must have at least one ### subsection."""
        import re

        content = Path("CHANGELOG.md").read_text()
        sections = re.split(r"^## ", content, flags=re.MULTILINE)[1:]
        assert len(sections) >= 2, "CHANGELOG needs at least 2 version sections"
        for section in sections:
            title = section.split("\n")[0].strip()
            assert "###" in section, f"## {title} has no ### subsection"

    def test_valid_subsection_headers(self) -> None:
        """### headers must use standard Keep a Changelog categories."""
        import re

        valid = {
            "Added",
            "Changed",
            "Deprecated",
            "Removed",
            "Fixed",
            "Security",
            "Improved",
        }
        content = Path("CHANGELOG.md").read_text()
        headers = re.findall(r"^### (.+)$", content, re.MULTILINE)
        for h in headers:
            assert h.strip() in valid, f"Invalid CHANGELOG category: ### {h}"


class TestExceptionHandlingGuard:
    """Guard: no bare excepts; broad except Exception must be justified."""

    def test_no_bare_excepts(self) -> None:
        import ast

        bad = []
        for f in sorted(Path("src/duo").glob("*.py")):
            tree = ast.parse(f.read_text())
            bad.extend(
                f"{f.name}:{node.lineno}"
                for node in ast.walk(tree)
                if isinstance(node, ast.ExceptHandler) and node.type is None
            )
        assert bad == [], "Bare except handlers:\n" + "\n".join(f"  {b}" for b in bad)

    def test_broad_exception_handlers_documented(self) -> None:
        """All except Exception must have an inline comment justifying why."""
        import ast

        unjustified = []
        for f in sorted(Path("src/duo").glob("*.py")):
            lines = f.read_text().splitlines()
            tree = ast.parse(f.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) and node.type is not None:
                    if isinstance(node.type, ast.Name) and node.type.id == "Exception":
                        line = (
                            lines[node.lineno - 1] if node.lineno <= len(lines) else ""
                        )
                        if "#" not in line:
                            unjustified.append(f"{f.name}:{node.lineno}")
        assert unjustified == [], (
            "except Exception without justification comment:\n"
            + "\n".join(f"  {u}" for u in unjustified)
        )


class TestDataclassConventionGuard:
    """Guard: dataclass conventions enforced across modules."""

    def test_verifier_results_frozen(self) -> None:
        """Verifier result types (Pass, Correction) must be frozen."""
        from duo.verifier import Correction, Pass

        assert Pass.__dataclass_params__.frozen  # type: ignore[attr-defined]
        assert Correction.__dataclass_params__.frozen  # type: ignore[attr-defined]

    def test_protocol_models_have_slots(self) -> None:
        """All protocol dataclasses must use slots=True for memory efficiency."""
        from duo.protocol import (
            AckResult,
            Heartbeat,
            SecurityPolicy,
            StepResult,
            Subtask,
            Task,
        )

        for cls in [Task, Subtask, SecurityPolicy, Heartbeat, AckResult, StepResult]:
            assert cls.__dataclass_params__.slots, (  # type: ignore[attr-defined]
                f"{cls.__name__} missing slots=True"
            )


class TestLoggerNamingGuard:
    """Guard: all logger instances must use __name__ (not hardcoded strings)."""

    def test_loggers_use_dunder_name(self) -> None:
        bad = []
        for f in sorted(Path("src/duo").glob("*.py")):
            for i, line in enumerate(f.read_text().splitlines(), 1):
                if "getLogger(" in line and "__name__" not in line:
                    bad.append(f"{f.name}:{i} → {line.strip()}")
        assert bad == [], "Loggers not using __name__:\n" + "\n".join(
            f"  {b}" for b in bad
        )


class TestFStringLoggingGuard:
    """Guard: production code must not use f-string logging (use lazy %s)."""

    def test_no_fstring_logging(self) -> None:
        import re

        bad = []
        pattern = re.compile(r'logger\.\w+\(f["\']')
        for f in sorted(Path("src/duo").glob("*.py")):
            for i, line in enumerate(f.read_text().splitlines(), 1):
                if pattern.search(line):
                    bad.append(f"{f.name}:{i} → {line.strip()}")
        assert bad == [], "f-string logging (use lazy %s instead):\n" + "\n".join(
            f"  {b}" for b in bad
        )
