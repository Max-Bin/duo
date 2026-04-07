#!/usr/bin/env bash
set -euo pipefail

# ─── Colors & Symbols ────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
RESET='\033[0m'

ok()   { printf "${GREEN}✅ %s${RESET}\n" "$*"; }
warn() { printf "${YELLOW}⚠️  %s${RESET}\n" "$*"; }
err()  { printf "${RED}❌ %s${RESET}\n" "$*"; }
hint() { printf "${DIM}   ↳ %s${RESET}\n" "$*"; }
info() { printf "${BLUE}ℹ️  %s${RESET}\n" "$*"; }
step() { printf "\n${CYAN}${BOLD}▶ %s${RESET}\n" "$*"; }

# ─── Flags ────────────────────────────────────────────────────────────────────
CHECK_ONLY=false
FORCE=false

# --- Help ---
usage() {
    cat <<EOF
Usage: bash install.sh [OPTIONS]

One-click installer for Duo — Agent Orchestration Runtime.

Options:
  -h, --help     Show this help message and exit
  --check        Dry-run: check prerequisites without installing anything
  --force        Force reinstall even if duo is already installed

What it does:
  1. Checks prerequisites (Python 3.12+, git, tmux)
  2. Installs uv (if not found)
  3. Runs uv sync to install dependencies
  4. Installs duo CLI in editable mode
  5. Verifies installation with 'uv run duo version'
  6. Checks for tmux-bridge (smux) and offers install guidance
  7. Prints a summary of all component versions

Environment:
  DUO_COPILOT_MODEL    Override default Copilot model
EOF
    exit 0
}

# Parse arguments
for arg in "$@"; do
    case "$arg" in
        -h|--help)  usage ;;
        --check)    CHECK_ONLY=true ;;
        --force)    FORCE=true ;;
        *)
            err "Unknown option: $arg"
            hint "Run 'bash install.sh --help' to see available options."
            exit 1
            ;;
    esac
done

# ─── Error Handler ────────────────────────────────────────────────────────────
trap 'err "Installation failed at line $LINENO. See output above for details."; hint "Re-run with --check to verify prerequisites, or file an issue at https://github.com/user/duo/issues"' ERR

# ─── Resolve Project Root ─────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

printf "\n${BOLD}🚀 Duo — Agent Orchestration Runtime Installer${RESET}\n"
printf "   ${CYAN}v0.4.0${RESET}\n"
printf "   %s\n" "$SCRIPT_DIR"
if [ "$CHECK_ONLY" = true ]; then
    printf "   ${YELLOW}(--check mode: prerequisites only, no install)${RESET}\n"
fi
if [ "$FORCE" = true ]; then
    printf "   ${YELLOW}(--force mode: reinstall even if already present)${RESET}\n"
fi
printf "\n"

# ─── Version collectors (Bash 3 compatible) ──────────────────────────────────
COMP_NAMES=(Python git tmux uv duo tmux-bridge)
COMP_VERS=("" "" "" "" "" "")
COMP_STATS=("" "" "" "" "" "")

set_comp() {
    local name="$1" ver="$2" status="$3"
    for i in "${!COMP_NAMES[@]}"; do
        if [ "${COMP_NAMES[$i]}" = "$name" ]; then
            COMP_VERS[$i]="$ver"
            COMP_STATS[$i]="$status"
            return
        fi
    done
}

print_summary() {
    printf "\n${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n"
    printf "${BOLD}  Component Versions${RESET}\n"
    printf "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n"
    printf "  ${BOLD}%-14s %-16s %s${RESET}\n" "Component" "Version" "Status"
    printf "  %-14s %-16s %s\n" "─────────────" "────────────────" "──────"
    for i in "${!COMP_NAMES[@]}"; do
        local ver="${COMP_VERS[$i]:-unknown}"
        local stat="${COMP_STATS[$i]:-?}"
        printf "  %-14s %-16s %s\n" "${COMP_NAMES[$i]}" "$ver" "$stat"
    done
    printf "\n"
}

# ─── 1. Check Prerequisites ──────────────────────────────────────────────────
step "Checking prerequisites"

# Python 3.12+
if command -v python3 &>/dev/null; then
    PY_FULL=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')
    PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
    PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
    if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 12 ]; then
        ok "Python ${PY_FULL} found"
        set_comp Python "$PY_FULL" "✅"
    else
        err "Python 3.12+ required, found ${PY_FULL}"
        hint "Install via: brew install python@3.12  OR  https://www.python.org/downloads/"
        set_comp Python "$PY_FULL" "❌"
        exit 1
    fi
else
    err "Python 3 not found."
    hint "Install via: brew install python@3.12  OR  https://www.python.org/downloads/"
    set_comp Python "(not found)" "❌"
    exit 1
fi

# git
if command -v git &>/dev/null; then
    GIT_VER=$(git --version | awk '{print $3}')
    ok "git ${GIT_VER} found"
    set_comp git "$GIT_VER" "✅"
else
    err "git not found."
    hint "Install via: brew install git  OR  https://git-scm.com/downloads"
    set_comp git "(not found)" "❌"
    exit 1
fi

# tmux
if command -v tmux &>/dev/null; then
    TMUX_VER=$(tmux -V | awk '{print $2}')
    ok "tmux ${TMUX_VER} found"
    set_comp tmux "$TMUX_VER" "✅"
else
    err "tmux not found."
    hint "Install via: brew install tmux  OR  sudo apt install tmux"
    hint "Docs: https://github.com/tmux/tmux/wiki/Installing"
    set_comp tmux "(not found)" "❌"
    exit 1
fi

# ─── Early exit for --check ──────────────────────────────────────────────────
if [ "$CHECK_ONLY" = true ]; then
    step "Checking optional components"

    if command -v uv &>/dev/null; then
        UV_VER=$(uv --version 2>/dev/null | awk '{print $2}')
        ok "uv ${UV_VER} found"
        set_comp uv "$UV_VER" "✅"
    else
        warn "uv not found (will be installed during full install)"
        hint "Manual install: curl -LsSf https://astral.sh/uv/install.sh | sh"
        set_comp uv "(not found)" "⚠️"
    fi

    if uv run duo version &>/dev/null 2>&1; then
        DUO_VER=$(uv run duo version 2>/dev/null)
        ok "duo ${DUO_VER} installed"
        set_comp duo "$DUO_VER" "✅"
    else
        warn "duo CLI not yet installed (will be installed during full install)"
        set_comp duo "(not installed)" "⚠️"
    fi

    BRIDGE_FOUND=false
    if command -v tmux-bridge &>/dev/null; then
        ok "tmux-bridge found in PATH"
        set_comp tmux-bridge "installed" "✅"
        BRIDGE_FOUND=true
    elif [ -x "$HOME/.smux/bin/tmux-bridge" ]; then
        ok "tmux-bridge found at ~/.smux/bin/tmux-bridge"
        set_comp tmux-bridge "installed" "✅"
        BRIDGE_FOUND=true
    fi
    if [ "$BRIDGE_FOUND" = false ]; then
        warn "tmux-bridge (smux) not found"
        hint "Install: git clone https://github.com/user/smux && cd smux && bash install.sh"
        hint "Docs: https://github.com/user/smux"
        set_comp tmux-bridge "(not found)" "⚠️"
    fi

    print_summary
    info "Run 'bash install.sh' (without --check) to install."
    printf "\n"
    exit 0
fi

# ─── 2. Ensure uv is Available ───────────────────────────────────────────────
step "Checking uv package manager"

if command -v uv &>/dev/null; then
    UV_VER=$(uv --version 2>/dev/null | awk '{print $2}')
    ok "uv ${UV_VER} already installed"
    set_comp uv "$UV_VER" "✅"
else
    info "uv not found — installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Source the env so uv is available in this session
    if [ -f "$HOME/.local/bin/env" ]; then
        # shellcheck disable=SC1091
        . "$HOME/.local/bin/env"
    elif [ -f "$HOME/.cargo/env" ]; then
        # shellcheck disable=SC1091
        . "$HOME/.cargo/env"
    fi
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

    if command -v uv &>/dev/null; then
        UV_VER=$(uv --version 2>/dev/null | awk '{print $2}')
        ok "uv ${UV_VER} installed"
        set_comp uv "$UV_VER" "✅"
    else
        err "Failed to install uv."
        hint "Install manually: curl -LsSf https://astral.sh/uv/install.sh | sh"
        hint "Docs: https://docs.astral.sh/uv/getting-started/installation/"
        set_comp uv "(failed)" "❌"
        exit 1
    fi
fi

# ─── 3. Idempotency — skip if already installed ─────────────────────────────
ALREADY_INSTALLED=false
if uv run duo version &>/dev/null 2>&1; then
    EXISTING_VER=$(uv run duo version 2>/dev/null)
    ALREADY_INSTALLED=true
fi

if [ "$ALREADY_INSTALLED" = true ] && [ "$FORCE" = false ]; then
    step "Duo ${EXISTING_VER} is already installed — skipping reinstall"
    info "Use --force to reinstall."
    set_comp duo "$EXISTING_VER" "✅"
else
    if [ "$ALREADY_INSTALLED" = true ] && [ "$FORCE" = true ]; then
        info "Duo ${EXISTING_VER} found — reinstalling (--force)"
    fi

    # ─── 4. Install Dependencies ──────────────────────────────────────────────
    step "Installing Python dependencies"

    uv sync
    ok "Dependencies synced"

    # ─── 5. Install duo CLI (editable) ────────────────────────────────────────
    step "Installing duo CLI"

    uv pip install -e . 2>/dev/null || uv pip install -e .
    ok "duo CLI installed"

    # ─── 6. Verify installation ───────────────────────────────────────────────
    step "Verifying installation"

    if DUO_VER=$(uv run duo version 2>/dev/null); then
        ok "duo ${DUO_VER} verified — 'uv run duo version' works"
        set_comp duo "$DUO_VER" "✅"
    else
        err "'uv run duo version' failed after install."
        hint "Try: source .venv/bin/activate && duo version"
        hint "Or:  uv run duo --help"
        hint "File an issue: https://github.com/user/duo/issues"
        set_comp duo "(verify failed)" "❌"
        exit 1
    fi
fi

# ─── 7. Check tmux-bridge (smux) ─────────────────────────────────────────────
step "Checking tmux-bridge (smux)"

BRIDGE_FOUND=false
if command -v tmux-bridge &>/dev/null; then
    ok "tmux-bridge found in PATH"
    set_comp tmux-bridge "installed" "✅"
    BRIDGE_FOUND=true
elif [ -x "$HOME/.smux/bin/tmux-bridge" ]; then
    ok "tmux-bridge found at ~/.smux/bin/tmux-bridge"
    set_comp tmux-bridge "installed" "✅"
    BRIDGE_FOUND=true
fi

if [ "$BRIDGE_FOUND" = false ]; then
    warn "tmux-bridge (smux) not found."
    warn "Duo uses smux for terminal interaction with Copilot CLI / Claude Code."
    printf "\n"
    info "To install smux, run:"
    printf "   ${BOLD}git clone https://github.com/user/smux && cd smux && bash install.sh${RESET}\n"
    printf "\n"
    info "Or see: https://github.com/user/smux"
    set_comp tmux-bridge "(not found)" "⚠️"
fi

# ─── 8. Summary ──────────────────────────────────────────────────────────────
print_summary

printf "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n"
printf "${GREEN}${BOLD}  🎉 Duo installation complete!${RESET}\n"
printf "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n\n"

printf "${BOLD}Next steps:${RESET}\n"
printf "  ${CYAN}1.${RESET} Run ${BOLD}uv run duo --help${RESET} to see available commands\n"
printf "  ${CYAN}2.${RESET} Start a session with ${BOLD}uv run duo start${RESET}\n"
if [ "$BRIDGE_FOUND" = false ]; then
    printf "  ${CYAN}3.${RESET} Install ${BOLD}smux${RESET} for tmux-bridge support (see above)\n"
fi
printf "\n"
