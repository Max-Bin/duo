#!/usr/bin/env bash
# smoke-test.sh — End-to-end smoke test for duo CLI.
#
# Tests all commands that work WITHOUT tmux/copilot running.
# Designed to catch CLI wiring bugs, import errors, and help text breakage.
#
# Usage: bash scripts/smoke-test.sh
# Exit 0 = all smoke tests passed, non-zero = failure.
set -euo pipefail

PASS=0
FAIL=0
ERRORS=()

# Temp dir for isolated testing
TMPDIR_BASE=$(mktemp -d)
trap 'rm -rf "${TMPDIR_BASE}"' EXIT

pass() { PASS=$((PASS + 1)); printf "  ✅ %s\n" "$1"; }
fail() { FAIL=$((FAIL + 1)); ERRORS+=("$1: $2"); printf "  ❌ %s: %s\n" "$1" "$2"; }

run_test() {
    local name="$1"
    shift
    if output=$("$@" 2>&1); then
        pass "$name"
    else
        fail "$name" "exit $? — ${output:0:120}"
    fi
}

run_test_expect_fail() {
    local name="$1"
    shift
    if output=$("$@" 2>&1); then
        fail "$name" "expected non-zero exit but got 0"
    else
        pass "$name"
    fi
}

echo "🔧 Duo CLI Smoke Tests"
echo "======================"

# === Version & Help ===
echo ""
echo "📋 Version & Help"
run_test "duo --version" duo --version
run_test "duo --help" duo --help
run_test "duo version" duo version

# === Help for every command group ===
echo ""
echo "📋 Command Help (wiring check)"
for cmd in \
    start send stop status merge diff kill \
    think list monitor watch dashboard logs inspect stats \
    batch queue \
    ceo-wait ceo-select ceo-approve ceo-smart ceo-smart-config \
    ceo-dispatch ceo-status ceo-loop ceo-resume \
    ceo-focus ceo-focus-show ceo-focus-clear ceo-now \
    ceo-session-start ceo-session-list ceo-session-replay \
    ceo-session-stats ceo-metrics \
    recover resume retry \
    export audit cost cleanup events \
    init doctor config bench completion; do
    run_test "duo $cmd --help" duo "$cmd" --help
done

# === Config ===
echo ""
echo "⚙️  Config"
DUO_DIR="${TMPDIR_BASE}/duo-home"
export DUO_DIR

run_test "config list" duo config list
run_test "config set" duo config set poll_interval 3
run_test "config get" duo config get poll_interval
run_test "config reset key" duo config reset poll_interval
run_test "config reset all" duo config reset

# === Init ===
echo ""
echo "🏗️  Init"
INIT_REPO="${TMPDIR_BASE}/test-repo"
git init -q "${INIT_REPO}"
run_test "duo init" duo init --repo "${INIT_REPO}"

# Verify init created expected files
if [[ -d "${INIT_REPO}/.duo" ]]; then
    pass "init created .duo directory"
else
    fail "init .duo directory" "not created"
fi

# === Doctor ===
echo ""
echo "🩺 Doctor"
run_test "duo doctor" duo doctor
run_test "duo doctor --json-output" duo doctor --json-output

# === Completion ===
echo ""
echo "🐚 Completion"
run_test "completion bash" duo completion bash
run_test "completion zsh" duo completion zsh
run_test "completion fish" duo completion fish

# === List (no tasks) ===
echo ""
echo "📊 List & Monitoring (offline)"
run_test "duo list" duo list
run_test "duo list --json-output" duo list --json-output

# === CEO Session ===
echo ""
echo "📝 CEO Session"
run_test "ceo-session-list" duo ceo-session-list
run_test "ceo-metrics" duo ceo-metrics
run_test "ceo-smart-config" duo ceo-smart-config
run_test "ceo-focus-show" duo ceo-focus-show

# === Bench ===
echo ""
echo "⏱️  Benchmarks"
run_test "duo bench all" duo bench all

# === Commands that should fail gracefully (no tmux) ===
echo ""
echo "🛡️  Graceful failures (no tmux)"
run_test_expect_fail "ceo-status no-task" duo ceo-status nonexistent-task
run_test_expect_fail "stop no-task" duo stop nonexistent-task
run_test_expect_fail "send no-task" duo send nonexistent-task "hello"
run_test_expect_fail "inspect no-task" duo inspect nonexistent-task

# === Summary ===
echo ""
echo "========================"
printf "Results: %d passed, %d failed\n" "$PASS" "$FAIL"
if [[ ${#ERRORS[@]} -gt 0 ]]; then
    echo ""
    echo "Failures:"
    for err in "${ERRORS[@]}"; do
        echo "  • $err"
    done
    exit 1
fi
echo "✅ All smoke tests passed!"
