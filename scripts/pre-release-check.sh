#!/usr/bin/env bash
# pre-release-check.sh — Validate that the repo is ready for a release.
# Usage: bash scripts/pre-release-check.sh
set -euo pipefail

cd "$(dirname "$0")/.."

# ── Colors ───────────────────────────────────────────────────────────
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

PASS=0
FAIL=0

check() {
    local name="$1"
    shift
    if eval "$@" > /dev/null 2>&1; then
        echo -e "  ${GREEN}✓${NC} $name"
        PASS=$((PASS + 1))
    else
        echo -e "  ${RED}✗${NC} $name"
        FAIL=$((FAIL + 1))
    fi
}

echo "Pre-release checks for Duo"
echo "=========================="

# ── Extract version from pyproject.toml ──────────────────────────────
VERSION=$(grep '^version' pyproject.toml | head -1 | sed 's/.*"\(.*\)"/\1/')
echo "Version: $VERSION"
echo ""

# ── Quality gate ─────────────────────────────────────────────────────
check "make check passes" "make check"
check "quickstart passes" "bash scripts/quickstart-test.sh --local"

# ── CHANGELOG ────────────────────────────────────────────────────────
check "CHANGELOG has current version" "grep -q '\[$VERSION\]' CHANGELOG.md"
check "CHANGELOG has content under current version" \
    "sed -n '/^## \[$VERSION\]/,/^## \[/p' CHANGELOG.md | grep -q '^### '"

# ── Git state ────────────────────────────────────────────────────────
check "No uncommitted changes" 'test -z "$(git status --porcelain)"'
check "Version differs from latest git tag" \
    '! git tag -l "v$VERSION" 2>/dev/null | grep -q .'

# ── Build ────────────────────────────────────────────────────────────
check "uv build succeeds" "uv build"

# ── README badges ────────────────────────────────────────────────────
check "README badges show correct version" "grep -q '$VERSION' README.md"

# ── Summary ──────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"

if [ "$FAIL" -gt 0 ]; then
    echo -e "${RED}NOT ready to release${NC}"
else
    echo -e "${GREEN}Ready to release: v$VERSION${NC}"
fi

# Cleanup build artifacts produced by the check
rm -rf dist/

exit "$FAIL"
