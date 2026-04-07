.PHONY: install test lint format type-check check coverage clean help

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## Install project in development mode
	uv sync
	uv pip install -e .

test: ## Run tests
	uv run python -m pytest tests/ -q

coverage: ## Run tests with coverage report
	uv run python -m coverage run -m pytest tests/ -q
	uv run python -m coverage report --show-missing --fail-under=95

test-verbose: ## Run tests with verbose output
	uv run python -m pytest tests/ -v

lint: ## Run linter (ruff check)
	uv run ruff check src/duo/ tests/

format: ## Format code (ruff format)
	uv run ruff format src/duo/ tests/

format-check: ## Check formatting without changing files
	uv run ruff format src/duo/ tests/ --check --diff

type-check: ## Run type checker (mypy strict)
	uv run python -m mypy src/duo/ --ignore-missing-imports

check: lint format-check type-check coverage ## Run all checks

clean: ## Remove build artifacts and caches
	rm -rf build/ dist/ *.egg-info .mypy_cache .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
