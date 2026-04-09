#!/usr/bin/env bash
# quickstart-test.sh — Verify a fresh clone → install → basic CLI works.
#
# Usage:
#   bash scripts/quickstart-test.sh          # test from a fresh tmpdir clone
#   bash scripts/quickstart-test.sh --local  # test the current working tree
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
BOLD='\033[1m'
NC='\033[0m'

pass_count=0
fail_count=0

check() {
  local label="$1"; shift
  if eval "$@" >/dev/null 2>&1; then
    echo -e "  ${GREEN}✓${NC} ${label}"
    ((pass_count++)) || true
  else
    echo -e "  ${RED}✗${NC} ${label}"
    ((fail_count++)) || true
  fi
}

echo -e "${BOLD}Duo Quickstart Test${NC}"
echo "────────────────────"

if [[ "${1:-}" == "--local" ]]; then
  echo "Testing local working tree..."
  cd "$(dirname "$0")/.."
else
  TMPDIR=$(mktemp -d)
  trap "rm -rf $TMPDIR" EXIT
  echo "Cloning into $TMPDIR..."
  git clone --depth 1 https://github.com/Max-Bin/duo.git "$TMPDIR/duo" 2>/dev/null
  cd "$TMPDIR/duo"
fi

echo ""
echo "Step 1: Install"
uv sync --quiet 2>/dev/null

echo ""
echo "Step 2: CLI checks"
check "duo --help lists commands"       uv run duo --help
check "duo version runs"               uv run duo version
check "duo list runs (empty)"          uv run duo list
check "duo think list runs (empty)"    uv run duo think list
check "duo config list runs"           uv run duo config list
check "duo stats runs"                 uv run duo stats
check "duo completion bash runs"       uv run duo completion bash

echo ""
echo "Step 3: Help text contains key commands"
HELP=$(uv run duo --help 2>&1)
for cmd in start think ceo-loop doctor monitor; do
  if echo "$HELP" | grep -q "$cmd"; then
    echo -e "  ${GREEN}✓${NC} help mentions '$cmd'"
    ((pass_count++)) || true
  else
    echo -e "  ${RED}✗${NC} help mentions '$cmd'"
    ((fail_count++)) || true
  fi
done

echo ""
echo "Step 4: Tests + coverage"
check "pytest passes"                  uv run python -m pytest tests/ -q --tb=line
check "100% coverage"                  uv run python -m pytest tests/ -q --cov=src/duo --cov-fail-under=100

echo ""
echo "Step 5: Code quality"
check "ruff clean"                     uv run ruff check src/ tests/
check "mypy strict"                    uv run mypy --strict src/duo/

echo ""
echo "────────────────────"
total=$((pass_count + fail_count))
echo -e "${BOLD}${pass_count}/${total} checks passed${NC}"

if [[ $fail_count -gt 0 ]]; then
  echo -e "${RED}QUICKSTART FAILED${NC}"
  exit 1
fi
echo -e "${GREEN}QUICKSTART OK${NC}"
