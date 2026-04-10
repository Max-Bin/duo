# Contributing to Duo

Welcome! Duo is an open-source AI agent orchestration runtime, and we'd love your help making it better. Whether you're fixing a bug, adding a feature, improving docs, or just filing an issue — every contribution matters. This guide will get you started.

## Development Setup

```bash
git clone https://github.com/Max-Bin/duo.git
cd duo
uv sync
uv pip install -e .
make check  # runs lint + format-check + type-check + coverage
```

**Prerequisites:** Python 3.12+, [uv](https://docs.astral.sh/uv/) package manager, tmux.

## Code Style

| Tool | Purpose | Command |
|------|---------|---------|
| **ruff format** | Auto-formatter | `ruff format src/duo/ tests/` |
| **ruff check** | Linter (rules: F, E, W, I, UP, B, RET, SIM, PIE, PERF) | `ruff check src/duo/ tests/` |
| **mypy --strict** | Type checker on all source | `mypy src/duo/ --ignore-missing-imports` |
| **pytest + coverage** | 100% coverage enforced | `make coverage` |

Key conventions:

- Python 3.12+ with full type annotations on all public functions.
- Use `from __future__ import annotations` in every module.
- Data models use `@dataclass`, never raw dicts.
- All file writes use atomic tmp+rename via `write_json`.
- **Never use `shell=True`** in subprocess calls — always pass argument lists.
- **100% test coverage is enforced** — every new line of code must have a corresponding test. The CI will reject any PR that drops below 100%.

## Commit Convention

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```
type: description
```

| Type | When to use |
|------|-------------|
| `feat:` | New feature or command |
| `fix:` | Bug fix |
| `refactor:` | Code restructuring with no behavior change |
| `test:` | Adding or updating tests |
| `docs:` | Documentation only |
| `release:` | Version bumps and release prep |

Examples from the repo:

```
feat: duo cost command + multi-project isolation tests
fix: atomic writes everywhere (fsync + tmp+rename) for crash safety
refactor: extract inspect helpers + 10 edge case tests
test: 23 edge case tests for cost, errors, atomic writes, ANSI strip
docs: update badges, CHANGELOG, and getting-started for v0.7.0
release: v1.0.0 — complete PyPI metadata
```

## Pull Request Process

1. **Fork** the repository and create a feature branch.
2. **Implement** your changes with tests.
3. **Run `make check`** — this must pass before opening a PR:
   - `ruff check` (linter)
   - `ruff format --check` (formatter)
   - `mypy --strict` (type checker)
   - `pytest` with 100% coverage
4. **Open a PR** with a clear title using the commit convention above.
5. **Keep PRs focused** — one feature or fix per PR. Smaller PRs get reviewed faster.

All PRs must pass the full CI pipeline (lint, type-check, 100% coverage) before merge.

## Testing Guidelines

- Use `tmp_path` fixture to isolate file operations.
- Use `monkeypatch` to override `TASKS_DIR` — never touch `~/.duo` in tests.
- Mock `subprocess.run` for git/tmux-bridge calls.
- CLI tests use `click.testing.CliRunner`.
- Run the full suite: `python -m pytest tests/ -q`

## Using Duo to Develop Duo

Duo is built with Duo. You can use `duo start` to create tasks that improve Duo itself — this is a core use case and a great way to dogfood the tool. For example:

```bash
duo start "add shell completion for zsh"
duo assign
duo watch
```

See [docs/ceo-workflow.md](docs/ceo-workflow.md) for the full automated workflow.

## Reporting Issues

Please use our issue templates:

- 🐛 [Bug Report](.github/ISSUE_TEMPLATE/bug_report.md) — for unexpected behavior or errors
- 💡 [Feature Request](.github/ISSUE_TEMPLATE/feature_request.md) — for ideas and enhancements

Include reproduction steps, expected vs. actual behavior, and your OS / Python version.

## Further Reading

- [Getting Started](docs/getting-started.md)
- [Architecture](docs/architecture.md)
- [CEO Workflow](docs/ceo-workflow.md)
