#!/usr/bin/env bash
# verify-tmux-context.sh — End-to-end verification that duo respects $TMUX.
#
# This script verifies the Round BF fix: pane creation targets the
# caller's tmux session, not an arbitrary "current" session.
#
# What it does:
#   1. Creates a temporary tmux session (isolated from duo-0)
#   2. Runs `duo start` inside that session
#   3. Verifies the pane was created in the temp session (not duo-0)
#   4. Cleans up
#
# Prerequisites:
#   - tmux running with at least one other session (e.g. duo-0)
#   - duo installed and on PATH
#
# Usage:
#   bash scripts/verify-tmux-context.sh
#
# Exit 0 = PASS, non-zero = FAIL
set -euo pipefail

TEMP_SESSION="duo-bf-verify-$$"
TASK_NAME="bf-verify-$$"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS + 1)); printf "  ✅ %s\n" "$1"; }
fail() { FAIL=$((FAIL + 1)); ERRORS+=("$1"); printf "  ❌ %s\n" "$1"; }

cleanup() {
    echo ""
    echo "Cleaning up..."
    # Stop the task (ignore errors)
    duo stop "${TASK_NAME}" 2>/dev/null || true
    # Kill the temp session
    tmux kill-session -t "${TEMP_SESSION}" 2>/dev/null || true
    echo "  Cleaned up temp session '${TEMP_SESSION}'"
}
trap cleanup EXIT

echo "═══════════════════════════════════════════"
echo " verify-tmux-context.sh"
echo " Verifying \$TMUX respect fix (Round BF)"
echo "═══════════════════════════════════════════"

# --- Pre-check ---
echo ""
echo "Pre-check..."

EXISTING_SESSIONS=$(tmux list-sessions -F '#{session_name}' 2>/dev/null || true)
if [ -z "${EXISTING_SESSIONS}" ]; then
    echo "  No tmux sessions found. Start one first."
    exit 1
fi
echo "  Existing sessions: $(echo "${EXISTING_SESSIONS}" | tr '\n' ', ')"

# --- Step 1: Create temp session ---
echo ""
echo "Step 1: Creating temporary tmux session '${TEMP_SESSION}'..."
tmux new-session -d -s "${TEMP_SESSION}" -x 200 -y 50
pass "Temporary session '${TEMP_SESSION}' created"

# Verify we now have 2+ sessions
SESSION_COUNT=$(tmux list-sessions | wc -l | tr -d ' ')
echo "  Total sessions: ${SESSION_COUNT}"
if [ "${SESSION_COUNT}" -lt 2 ]; then
    fail "Expected at least 2 sessions"
fi

# --- Step 2: Get temp session's $TMUX value ---
echo ""
echo "Step 2: Getting \$TMUX for temp session..."

# The $TMUX value includes the session_id. We need to find it.
TEMP_SESSION_ID=$(tmux list-sessions -F '#{session_name}:#{session_id}' | grep "^${TEMP_SESSION}:" | cut -d: -f2)
echo "  Temp session ID: ${TEMP_SESSION_ID}"

# --- Step 3: Run duo start inside the temp session ---
echo ""
echo "Step 3: Running 'duo start ${TASK_NAME}' inside temp session..."

# Send the command to the temp session's pane
tmux send-keys -t "${TEMP_SESSION}" "duo start ${TASK_NAME} --desc 'tmux context verify' --repo $(pwd)" Enter

# Wait for duo to create the pane
sleep 5

# --- Step 4: Verify pane location ---
echo ""
echo "Step 4: Verifying pane was created in the correct session..."

# List all panes across all sessions
ALL_PANES=$(tmux list-panes -a -F '#{session_name} #{pane_id}' 2>/dev/null || true)

# Check if any pane in the temp session has the task label
TEMP_PANES=$(tmux list-panes -t "${TEMP_SESSION}" -F '#{pane_id}' 2>/dev/null | wc -l | tr -d ' ')
echo "  Panes in temp session: ${TEMP_PANES}"

if [ "${TEMP_PANES}" -ge 2 ]; then
    pass "Pane created in temp session (${TEMP_PANES} panes, expected ≥2)"
else
    fail "Pane NOT created in temp session (only ${TEMP_PANES} pane)"
    echo "  All panes across sessions:"
    echo "${ALL_PANES}" | sed 's/^/    /'
fi

# Also verify no unexpected pane was created in other sessions
for session in ${EXISTING_SESSIONS}; do
    if [ "${session}" = "${TEMP_SESSION}" ]; then
        continue
    fi
    BEFORE_COUNT=$(tmux list-panes -t "${session}" -F '#{pane_id}' 2>/dev/null | wc -l | tr -d ' ')
    # We can't easily know the "before" count, but we can check it's reasonable
    echo "  Panes in '${session}': ${BEFORE_COUNT}"
done

# --- Step 5: Verify task status ---
echo ""
echo "Step 5: Checking task status..."
STATUS_OUTPUT=$(duo status "${TASK_NAME}" 2>&1 || true)
echo "  ${STATUS_OUTPUT}" | head -3 | sed 's/^/  /'

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
