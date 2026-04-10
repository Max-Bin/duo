# Plan: `duo go` — The One-Command User Journey

## Context

User feedback: "从我的角度想问题。假设你是个用户，你希望这个产品该怎么用，就最简单的那种。"

Current pain: user must learn 4+ duo CLI commands to start working. But the user's actual desire is **talk to Claude Code naturally** — Claude Code handles everything. The chicken-and-egg problem: Claude Code needs CLAUDE.md to know it's CEO → but CLAUDE.md only exists inside worktrees created by `duo start` → but `duo start` should be run BY Claude Code → but Claude Code doesn't know about duo yet.

## The Design: Two Commands, Then Chat

```bash
tmux new -s work              # 1. Only prerequisite (user already knows tmux)
duo go                        # 2. Everything else
# Left: Claude Code (CEO, "born ready")
# Right: Copilot (idle, 0 PR burned)
# User starts chatting about their idea
```

## What `duo go` Does (Step by Step)

1. **Check tmux** — if `$TMUX` unset → error: "Run inside tmux first: `tmux new -s work`"
2. **Check git** — if not a git repo → `git init` automatically
3. **`duo init` idempotently** — create `.duo/`, `config.json`, `tasks/`, `instructions.md` if missing
4. **Write project-level CLAUDE.md** to repo root (`{repo}/CLAUDE.md`):
   - If no existing CLAUDE.md → create with full CEO template
   - If exists with `<!-- duo-managed -->` marker → overwrite duo section
   - If exists without marker → prepend duo section with delimiters, preserve existing content
5. **Split tmux: right pane = Copilot (idle)**
   - `tmux split-window -h` → new pane
   - `name_pane(pane, "duo-copilot-standby")`
   - `cd {repo}` + `copilot --model {model} [--yolo]`
   - `wait_for_idle(30s)` + `/allow-all`
   - **Zero PR burned** — no bootstrap prompt
6. **Save go-session state** to `~/.duo/go-session.json`
7. **`exec claude` in current pane** — replaces duo process with Claude Code
   - `os.execvp("claude", ["claude", "--dangerously-skip-permissions"])` if bypass_permissions
   - Claude Code reads `CLAUDE.md` → instantly knows: "I'm CEO, right pane has idle Copilot"

## Project-Level CLAUDE.md (CEO Operating Manual)

Written to `{repo}/CLAUDE.md` with `<!-- duo-managed -->` marker. Content covers:

| Section | Purpose |
|---------|---------|
| Identity | "You are the CEO. User talks to you. You run all duo commands." |
| Phase 1: Understand | "Chat with user, clarify requirements, no PR cost" |
| Phase 2: Start Task | `duo start <name> --reuse-pane duo-copilot-standby` |
| Phase 3: Execute | `duo send <name> "instruction"` — burns 1 PR |
| Phase 4: Monitor | `duo watch` + `duo ceo-select/approve` — all FREE |
| PR Budget Rules | Iron rules about when PR is burned |
| Command Reference | Table of all duo commands with PR cost |
| Known Pitfalls | kqueue leak, CAPIError, send_keys hex, etc. |
| Rubber-duck Protocol | Mode A/B/C |
| Project Context | Auto-detected: project type, README excerpt, directory listing |

## `--reuse-pane` for `duo start`

When CEO runs `duo start my-task --reuse-pane duo-copilot-standby`:
- Skip creating new pane (reuse existing standby)
- Rename pane: `duo-copilot-standby` → `my-task`
- `cd` to worktree
- Write task-level CLAUDE.md to worktree (existing `write_commander_claude_md`)
- Defer mode (default): no prompt sent, 0 PR

## Resume Flow

`duo go` is idempotent. If run again:
1. Check `~/.duo/go-session.json` for existing standby pane
2. If alive → reuse (skip pane creation)
3. If dead → create new
4. Always rewrite CLAUDE.md (picks up latest project context)
5. Always exec Claude Code

## Files to Modify

| File | Change |
|------|--------|
| `cli.py` | New `duo go` command; add `--reuse-pane` to `start`; update `init` to call `write_project_claude_md` |
| `commander.py` | New `write_project_claude_md(repo)` + `PROJECT_CEO_TEMPLATE`; modify `start_session()` to accept `reuse_pane` parameter; extract `_launch_standby_copilot()` helper |
| `protocol.py` (or new `go_session.py`) | `save_go_session()`, `load_go_session()`, `clear_go_session()` |
| `CLAUDE.md` (repo root) | Written dynamically by `duo go` with `<!-- duo-managed -->` marker |

## Tests

- `test_go_happy_path` — tmux + git → panes created, Claude Code exec'd
- `test_go_no_tmux` — clear error
- `test_go_auto_git_init` — fresh dir gets git init
- `test_go_resume` — reuses existing pane
- `test_go_claude_md_merge` — preserves existing user CLAUDE.md content
- `test_start_reuse_pane` — `start_session(reuse_pane=...)` renames and cd's
- `test_start_reuse_dead_pane` — error on dead pane
- `test_project_claude_md_content` — CEO template has all required sections

## Verification

1. `cd /tmp/test-project && git init && tmux new -s test`
2. `duo go` → see two panes, Claude Code left, Copilot right
3. Tell Claude Code: "build a hello world Python script"
4. Verify Claude Code runs `duo start`, `duo send`, `duo watch` automatically
5. Copilot produces code, Claude Code reviews
6. `duo merge` to finish
7. Check: CLAUDE.md at repo root, .duo/ config, worktree created
