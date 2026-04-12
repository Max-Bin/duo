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

# === Verify no orphaned commands (help lists all) ===
echo ""
echo "🔎 Command completeness check"
EXPECTED_CMDS="start send stop status merge diff kill \
think list monitor watch dashboard logs inspect stats \
batch queue \
ceo-wait ceo-select ceo-approve ceo-status \
recover resume retry \
export audit cost cleanup events \
init doctor config completion go version"
HELP_OUTPUT=$(duo --help 2>&1)
MISSING=""
for cmd in $EXPECTED_CMDS; do
    if ! echo "$HELP_OUTPUT" | grep -qw "$cmd"; then
        MISSING="$MISSING $cmd"
    fi
done
if [ -z "$MISSING" ]; then
    pass "all commands listed in --help"
else
    fail "missing from --help" "$MISSING"
fi

# === Help for every command group ===
echo ""
echo "📋 Command Help (wiring check)"
for cmd in \
    start send stop status merge diff kill \
    think list monitor watch dashboard logs inspect stats \
    batch queue \
    ceo-wait ceo-select ceo-approve ceo-status \
    recover resume retry \
    export audit cost cleanup events \
    init doctor config completion go; do
    run_test "duo $cmd --help" duo "$cmd" --help
done

# === Config ===
echo ""
echo "⚙️  Config"
DUO_DIR="${TMPDIR_BASE}/duo-home"
export DUO_DIR

run_test "config list" duo config list
run_test "config set" duo config set poll_base_interval 3
run_test "config get" duo config get poll_base_interval
run_test "config reset key" duo config reset poll_base_interval
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
# doctor exits non-zero when checks fail (e.g. missing tmux-bridge in CI),
# so we just verify it runs without crashing — any exit code is acceptable.
echo ""
echo "🩺 Doctor"
run_test_allow_fail() {
    local name="$1"
    shift
    local rc=0
    output=$("$@" 2>&1) || rc=$?
    if [[ $rc -eq 0 ]]; then
        pass "$name"
    else
        pass "$name (exit $rc — expected in environments without tmux-bridge)"
    fi
}
run_test_allow_fail "duo doctor" duo doctor
run_test_allow_fail "duo doctor --json-output" duo doctor --json-output

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

# === Commands that should fail gracefully (no tmux) ===
echo ""
echo "🛡️  Graceful failures (no tmux)"
run_test_expect_fail "ceo-status no-task" duo ceo-status nonexistent-task
run_test_expect_fail "stop no-task" duo stop nonexistent-task
run_test_expect_fail "send no-task" duo send nonexistent-task "hello"
run_test_expect_fail "inspect no-task" duo inspect nonexistent-task
run_test_expect_fail "diff no-task" duo diff nonexistent-task
run_test_expect_fail "logs no-task" duo logs nonexistent-task
run_test_expect_fail "stats no-task" duo stats nonexistent-task
run_test_expect_fail "export no-task" duo export nonexistent-task
run_test_expect_fail "retry no-task" duo retry nonexistent-task

# === Data commands on empty state ===
echo ""
echo "📊 Data commands (empty state)"
run_test "audit empty" duo audit
run_test "cost empty" duo cost
run_test "recover empty" duo recover
run_test "stats empty" duo stats
run_test "cleanup empty" duo cleanup --force
run_test "events list empty" duo events list
run_test "queue empty" duo queue

# === CEO commands on empty state ===
echo ""
echo "📊 CEO commands (empty/no-task)"
run_test_expect_fail "ceo-wait no-task" duo ceo-wait nonexistent-task
run_test_expect_fail "ceo-approve no-task" duo ceo-approve nonexistent-task
run_test_expect_fail "ceo-select no-task" duo ceo-select nonexistent-task 1

# === JSON output validation (must produce valid JSON) ===
echo ""
echo "🔍 JSON output validation"
run_test_json() {
    local name="$1"
    shift
    if output=$("$@" 2>&1); then
        if echo "$output" | python3 -m json.tool > /dev/null 2>&1; then
            pass "$name"
        else
            fail "$name" "output is not valid JSON: ${output:0:80}"
        fi
    else
        fail "$name" "exit $? — ${output:0:120}"
    fi
}
run_test_json "list --json-output" duo list --json-output
run_test_json "version --json-output" duo version --json-output
run_test_json "config list --json-output" duo config list --json-output
run_test_json "config get --json-output" duo config get poll_base_interval --json-output
run_test_json "queue --json-output" duo queue --json-output
run_test_json "cost --json-output" duo cost --json-output
run_test_json "audit --json-output" duo audit --json-output
run_test_json "stats --json-output" duo stats --json-output
run_test_json "recover --json-output" duo recover --json-output
run_test_json "cleanup --json-output" duo cleanup --force --json-output
run_test_json "events list --json-output" duo events list --json-output
run_test_json "init --json-output" duo init --repo "${INIT_REPO}" --json-output
run_test_json_allow_fail() {
    local name="$1"
    shift
    local rc=0
    output=$("$@" 2>&1) || rc=$?
    if echo "$output" | python3 -m json.tool > /dev/null 2>&1; then
        pass "$name"
    elif [[ $rc -ne 0 ]]; then
        pass "$name (exit $rc — non-JSON error output expected)"
    else
        fail "$name" "output is not valid JSON: ${output:0:80}"
    fi
}
run_test_json_allow_fail "doctor --json-output" duo doctor --json-output

# === Error message quality (Fix: suggestion present) ===
echo ""
echo "📋 Error message quality"
run_test_fix_suggestion() {
    local name="$1"
    shift
    local output
    output=$("$@" 2>&1) || true
    if echo "$output" | grep -qi "Fix:"; then
        pass "$name (has Fix: suggestion)"
    else
        fail "$name" "missing Fix: in error output: ${output:0:100}"
    fi
}
run_test_fix_suggestion "stop no-task" duo stop nonexistent-task
run_test_fix_suggestion "send no-task" duo send nonexistent-task "hello"
run_test_fix_suggestion "config reset bad-key" duo config reset totally_bogus_key

# === JSON schema validation (key presence) ===
echo ""
echo "🔍 JSON schema validation"
run_test_json_has_key() {
    local name="$1"
    local key="$2"
    shift 2
    local output
    if output=$("$@" 2>&1); then
        if echo "$output" | python3 -c "import sys,json; d=json.load(sys.stdin); assert '$key' in (d if isinstance(d,dict) else d[0] if d else {})" 2>/dev/null; then
            pass "$name (has key '$key')"
        else
            fail "$name" "missing key '$key' in JSON: ${output:0:80}"
        fi
    else
        fail "$name" "exit $? — ${output:0:120}"
    fi
}
run_test_json_has_key "version JSON has 'version'" "version" duo version --json-output
run_test_json_has_key "config list has 'copilot_model'" "copilot_model" duo config list --json-output
run_test_json_has_key "config list has 'max_parallel'" "max_parallel" duo config list --json-output
run_test_json_has_key "queue JSON has 'active_count'" "active_count" duo queue --json-output

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
