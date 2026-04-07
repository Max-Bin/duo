#!/usr/bin/env bash
set -euo pipefail

# ─── Colors & Symbols ────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

ok()   { printf "${GREEN}✅ %s${RESET}\n" "$*"; }
warn() { printf "${YELLOW}⚠️  %s${RESET}\n" "$*"; }
err()  { printf "${RED}❌ %s${RESET}\n" "$*"; }
info() { printf "${BLUE}ℹ️  %s${RESET}\n" "$*"; }
step() { printf "\n${CYAN}${BOLD}▶ %s${RESET}\n" "$*"; }

# ─── Error Handler ────────────────────────────────────────────────────────────
trap 'err "Installation failed at line $LINENO. See output above for details."' ERR

# ─── Resolve Project Root ─────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

printf "\n${BOLD}🚀 Duo — Agent Orchestration Runtime Installer${RESET}\n"
printf "   ${CYAN}v0.4.0${RESET}\n"
printf "   %s\n\n" "$SCRIPT_DIR"

# ─── 1. Check Prerequisites ──────────────────────────────────────────────────
step "Checking prerequisites"

# Python 3.12+
if command -v python3 &>/dev/null; then
    PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
    PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
    if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 12 ]; then
        ok "Python ${PY_VERSION} found"
    else
        err "Python 3.12+ required, found ${PY_VERSION}"
        exit 1
    fi
else
    err "Python 3 not found. Install Python 3.12+ first."
    exit 1
fi

# git
if command -v git &>/dev/null; then
    ok "git $(git --version | awk '{print $3}') found"
else
    err "git not found. Install git first."
    exit 1
fi

# tmux
if command -v tmux &>/dev/null; then
    ok "tmux $(tmux -V | awk '{print $2}') found"
else
    err "tmux not found. Install tmux first (brew install tmux / apt install tmux)."
    exit 1
fi

# ─── 2. Ensure uv is Available ───────────────────────────────────────────────
step "Checking uv package manager"

if command -v uv &>/dev/null; then
    ok "uv $(uv --version 2>/dev/null | awk '{print $2}') already installed"
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
        ok "uv $(uv --version 2>/dev/null | awk '{print $2}') installed"
    else
        err "Failed to install uv. Install manually: https://docs.astral.sh/uv/"
        exit 1
    fi
fi

# ─── 3. Install Dependencies ─────────────────────────────────────────────────
step "Installing Python dependencies"

uv sync
ok "Dependencies synced"

# ─── 4. Install duo CLI (editable) ───────────────────────────────────────────
step "Installing duo CLI"

uv pip install -e . 2>/dev/null || uv pip install -e .
ok "duo CLI installed"

# Verify the CLI is accessible
if uv run duo --help &>/dev/null; then
    ok "duo CLI verified — 'uv run duo --help' works"
else
    warn "duo installed but 'uv run duo --help' failed — you may need to activate the venv"
fi

# ─── 5. Check tmux-bridge (smux) ─────────────────────────────────────────────
step "Checking tmux-bridge (smux)"

BRIDGE_FOUND=false
if command -v tmux-bridge &>/dev/null; then
    ok "tmux-bridge found in PATH"
    BRIDGE_FOUND=true
elif [ -x "$HOME/.smux/bin/tmux-bridge" ]; then
    ok "tmux-bridge found at ~/.smux/bin/tmux-bridge"
    BRIDGE_FOUND=true
fi

if [ "$BRIDGE_FOUND" = false ]; then
    warn "tmux-bridge (smux) not found."
    warn "Duo uses smux for terminal interaction with Copilot CLI / Claude Code."
    info "Install smux to enable multi-pane orchestration."
    info "See: https://github.com/user/smux"
fi

# ─── 6. Done ─────────────────────────────────────────────────────────────────
printf "\n${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n"
printf "${GREEN}${BOLD}  🎉 Duo installation complete!${RESET}\n"
printf "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n\n"

printf "${BOLD}Next steps:${RESET}\n"
printf "  ${CYAN}1.${RESET} Run ${BOLD}uv run duo --help${RESET} to see available commands\n"
printf "  ${CYAN}2.${RESET} Start a session with ${BOLD}uv run duo start${RESET}\n"
if [ "$BRIDGE_FOUND" = false ]; then
    printf "  ${CYAN}3.${RESET} Install ${BOLD}smux${RESET} for tmux-bridge support\n"
fi
printf "\n"
