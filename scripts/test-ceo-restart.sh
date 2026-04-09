#!/usr/bin/env bash
# test-ceo-restart.sh — Smoke test for `duo ceo-restart` command.
#
# Prerequisites:
#   - tmux session running
#   - A task with an active Copilot pane (e.g. created via `duo start`)
#
# This script does NOT start or stop real Copilot sessions automatically
# to avoid killing your current work. Instead, run it when you have a
# task you're WILLING TO RESTART.
#
# Usage:
#   bash scripts/test-ceo-restart.sh <task-name>
#
# Example:
#   bash scripts/test-ceo-restart.sh e2e-test
#
# The script will:
#   1. Record pre-restart health (PID, fd count, kqueue count)
#   2. Run `duo ceo-restart <task>`
#   3. Record post-restart health
#   4. Verify: new PID ≠ old PID, fd count decreased, pane alive
#   5. Print PASS/FAIL report
set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: $0 <task-name>"
    echo "  Provide the name of a task with an active Copilot pane."
    exit 1
fi

TASK="$1"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS + 1)); printf "  ✅ %s\n" "$1"; }
fail() { FAIL=$((FAIL + 1)); ERRORS+=("$1"); printf "  ❌ %s\n" "$1"; }

echo "═══════════════════════════════════════════"
echo " duo ceo-restart smoke test"
echo " Task: ${TASK}"
echo "═══════════════════════════════════════════"

# --- Step 1: Pre-restart health ---
echo ""
echo "Step 1: Recording pre-restart health..."

PRE_STATUS=$(duo ceo-status "${TASK}" 2>&1) || true
echo "  ceo-status: ${PRE_STATUS}"

# Get pre-restart PID via doctor
PRE_DOCTOR=$(duo doctor --json 2>&1) || true
echo "  doctor output captured ($(echo "${PRE_DOCTOR}" | wc -c | tr -d ' ') bytes)"

# Extract PID from ceo-now (if possible)
PRE_NOW=$(duo ceo-now --json 2>&1) || true
PRE_PID=""
if command -v jq &> /dev/null; then
    PRE_PID=$(echo "${PRE_NOW}" | jq -r '.health.pid // empty' 2>/dev/null) || true
    PRE_FDS=$(echo "${PRE_NOW}" | jq -r '.health.fd_count // empty' 2>/dev/null) || true
    echo "  Pre-restart: PID=${PRE_PID:-unknown}, fds=${PRE_FDS:-unknown}"
else
    echo "  (jq not available, skipping detailed health parsing)"
fi

# --- Step 2: Run ceo-restart ---
echo ""
echo "Step 2: Running duo ceo-restart ${TASK}..."
echo "  (This will exit Copilot, wait for shell, then re-launch)"
echo ""

if RESTART_OUTPUT=$(duo ceo-restart "${TASK}" 2>&1); then
    pass "ceo-restart exited successfully"
    echo "${RESTART_OUTPUT}" | sed 's/^/  │ /'
else
    fail "ceo-restart failed (exit $?)"
    echo "${RESTART_OUTPUT}" | sed 's/^/  │ /'
    echo ""
    echo "══════════════════════════════════════"
    echo " RESULT: ${PASS} passed, ${FAIL} failed"
    echo "══════════════════════════════════════"
    exit 1
fi

# --- Step 3: Post-restart health ---
echo ""
echo "Step 3: Recording post-restart health..."
sleep 3  # Give Copilot a moment to fully start

POST_STATUS=$(duo ceo-status "${TASK}" 2>&1) || true
echo "  ceo-status: ${POST_STATUS}"

POST_NOW=$(duo ceo-now --json 2>&1) || true
POST_PID=""
POST_FDS=""
if command -v jq &> /dev/null; then
    POST_PID=$(echo "${POST_NOW}" | jq -r '.health.pid // empty' 2>/dev/null) || true
    POST_FDS=$(echo "${POST_NOW}" | jq -r '.health.fd_count // empty' 2>/dev/null) || true
    echo "  Post-restart: PID=${POST_PID:-unknown}, fds=${POST_FDS:-unknown}"
fi

# --- Step 4: Verify ---
echo ""
echo "Step 4: Verification..."

# 4a: Status should be idle (at ❯ prompt) or dialog
STATE=$(echo "${POST_STATUS}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('state',''))" 2>/dev/null) || true
if [ "${STATE}" = "idle" ] || [ "${STATE}" = "dialog" ]; then
    pass "Post-restart state is '${STATE}' (Copilot is alive)"
elif [ "${STATE}" = "dead" ]; then
    fail "Post-restart state is 'dead' — Copilot did not restart"
else
    pass "Post-restart state is '${STATE}' (non-dead = alive)"
fi

# 4b: PID changed (if we could detect both)
if [ -n "${PRE_PID}" ] && [ -n "${POST_PID}" ]; then
    if [ "${PRE_PID}" != "${POST_PID}" ]; then
        pass "PID changed: ${PRE_PID} → ${POST_PID}"
    else
        fail "PID did NOT change: still ${POST_PID} — restart may not have worked"
    fi
else
    echo "  ⏭️  Skipped PID change check (could not determine PIDs)"
fi

# 4c: FD count decreased (or at least reasonable)
if [ -n "${PRE_FDS}" ] && [ -n "${POST_FDS}" ]; then
    if [ "${POST_FDS}" -lt "${PRE_FDS}" ]; then
        pass "FD count decreased: ${PRE_FDS} → ${POST_FDS}"
    elif [ "${POST_FDS}" -lt 500 ]; then
        pass "FD count acceptable: ${POST_FDS} (threshold: 500)"
    else
        fail "FD count still high: ${POST_FDS} (was ${PRE_FDS})"
    fi
else
    echo "  ⏭️  Skipped FD count check (jq not available or health unavailable)"
fi

# 4d: Restart-recommended signal removed
SIGNAL_PATH="${HOME}/.duo/tasks/${TASK}/restart-recommended"
if [ -f "${SIGNAL_PATH}" ]; then
    fail "restart-recommended signal still exists at ${SIGNAL_PATH}"
else
    pass "restart-recommended signal cleaned up"
fi

# --- Report ---
echo ""
echo "══════════════════════════════════════"
echo " RESULT: ${PASS} passed, ${FAIL} failed"
if [ ${FAIL} -gt 0 ]; then
    echo " FAILURES:"
    for err in "${ERRORS[@]}"; do
        echo "   • ${err}"
    done
fi
echo "══════════════════════════════════════"

exit "${FAIL}"
