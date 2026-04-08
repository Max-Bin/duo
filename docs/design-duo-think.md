# Design: `duo think` — Pre-Start Thinking Sessions

> **Status:** Draft v4 — final pre-implementation review.

## Problem

`duo start` immediately spawns a Copilot pane, creates a worktree, and
sends the first prompt.  This burns a Premium Request (PR) even if the
idea is half-formed.  There's no structured way to *refine* a task
before committing resources.

This is primarily a **CEO agent** problem.  The typical Duo workflow:
user talks to Claude Code (the CEO), CEO orchestrates executors via
`duo start`.  The CEO needs structured brainstorming *before* spawning
Copilot sessions, producing well-scoped plans that minimize wasted PRs.

**Goal:** Give the CEO agent (and optionally humans) a thinking
channel backed by a Claude Code pane — with full tool access (read
files, grep, web search) — at zero Copilot/PR cost, then hand off a
refined plan to `duo start`.

## Architecture Decision

**`duo think` spawns a Claude Code instance in a tmux pane.**

This is the natural choice for Duo's architecture:

- **Consistency:** Duo's core pattern is "tmux pane + agent."
  `duo start` opens Copilot executor + Claude Code commander panes.
  `duo think` opens a Claude Code thinking pane.  Same pattern.
- **Zero extra cost:** Claude Code subscription is already paid.
  Another Claude Code pane uses the same subscription.
- **Full tool access:** The thinking phase benefits from deep research
  — reading repo files, grepping code, searching the web, browsing
  docs.  Only Claude Code has these tools.
- **Zero PR:** No Copilot pane = no Premium Request consumed.
- **Code reuse:** Pane spawn reuses `start_claude_commander()` patterns
  from `commander.py`.

## Command Interface

```
duo think <name>              # ensure pane exists, print info
duo think <name> --ask "..."  # one-shot question — CEO primary path
duo think <name> --finalize   # tell Claude Code to write plan.md
duo think list                # show all thinking sessions
duo think <name> --close      # close pane, keep files
duo think <name> --delete     # close pane + delete files
```

### `duo think <name> --ask "..."` (Primary Path — CEO)

The CEO agent's main interface.  Each call is atomic:

1. If `think-{name}` pane doesn't exist → create it (spawn Claude Code)
2. `content_before = read_pane(label, 200)` (snapshot before sending)
3. Send the message to the pane via `type_text()` + `send_keys("Enter")`
4. `result = wait_for_response_stable(label)` (see algorithm below)
5. If `result == "idle"`: extract response via capture diff, print to
   stdout, append capture to `session.log`, exit 0
6. If `result == "dialog"`: print "Claude Code asked a question in the
   pane — switch to pane 'think-{name}' to answer", exit 1
7. If `result == "timeout"`: print "Thinking pane not responding after
   {timeout}s", exit 1
8. Pane stays open for next `--ask`

```bash
# CEO brainstorms via sequential --ask calls
duo think rate-limiter --ask "I want to add rate limiting. Requirements: per-user, configurable. Approaches?"
# (prints Claude Code's response to stdout)

duo think rate-limiter --ask "Token bucket with sliding window. Edge cases?"
# (prints response)

duo think rate-limiter --ask "Good. Scope down: single-node first, Redis in v2."
# (prints response)

duo think rate-limiter --finalize
# Plan written to ~/.duo/thinking/rate-limiter/plan.md
```

The CEO reads stdout to get each response and decides the next
question in its own orchestration loop.

### `duo think <name>` (No Arguments — Info + Pane Hint)

When called without `--ask`, `--finalize`, `--close`, or `--delete`:

1. Ensure `think-{name}` pane exists (create if needed).
2. Print session info:

```
Thinking session: rate-limiter
  Pane: think-rate-limiter (alive)
  Dir:  ~/.duo/thinking/rate-limiter/
  Plan: not yet finalized

To brainstorm directly, switch to pane 'think-rate-limiter'.
To send a one-shot message: duo think rate-limiter --ask "..."
To finalize: duo think rate-limiter --finalize
```

Humans who want to brainstorm directly should switch to the
`think-{name}` pane in tmux and talk to Claude Code there.  No REPL
middle layer — the pane IS the conversation.

### `duo think <name> --finalize`

1. `result = wait_for_response_stable(label)` — ensure pane is idle.
2. If `result == "dialog"`: abort with "Pane is in a dialog. Switch to
   pane 'think-{name}' to handle it first."
3. If `result == "timeout"`: abort with "Pane unresponsive."
4. Only if `result == "idle"`: send finalize instruction to the pane:

```
Please distill our entire conversation into a plan document using the
template at ~/.duo/thinking/{name}/plan-template.md. Write the result
to ~/.duo/thinking/{name}/plan.md. Be concrete and specific — this
plan will be the initial prompt for a code-generating agent.
```

5. Poll for `~/.duo/thinking/{name}/plan.md` to appear and stabilize
   (file exists + size unchanged for 3 seconds, timeout 60s).
6. On success:

```
Plan written to ~/.duo/thinking/rate-limiter/plan.md
Review it, then run:
  duo start rate-limiter --from-thinking
```

7. On timeout: "Claude Code didn't produce plan.md within 60s. Check
   the thinking pane manually, or run --finalize again."

### `duo think list`

Lists all thinking sessions with pane status:

```
NAME            PANE     STATUS      FILES
rate-limiter    alive    finalized   CLAUDE.md plan.md session.log
refactor        dead     active      CLAUDE.md session.log
old-idea        none     active      CLAUDE.md
```

- **alive:** `think-{name}` pane exists and process is Claude Code
- **dead:** pane existed but process exited (shell visible)
- **none:** directory exists but no pane (never started or closed)
- **finalized:** `plan.md` exists in the directory

### `duo think <name> --close`

Terminates the `think-{name}` tmux pane but keeps all files in
`~/.duo/thinking/{name}/`.  The session can be re-opened later (a new
pane will be spawned, Claude Code will see CLAUDE.md).

### `duo think <name> --delete`

Terminates the pane (if alive) AND removes `~/.duo/thinking/{name}/`
after confirmation: `"Delete thinking session '{name}'? This cannot
be undone. [y/N]"`

## `wait_for_response_stable()` Algorithm

This is the key heuristic for determining when Claude Code has
finished responding.  Used by `--ask` and `--finalize`.

```python
def wait_for_response_stable(
    label: str,
    timeout: float = 120.0,
    stable_threshold: float = 2.0,
) -> str:
    """Wait until the thinking pane reaches a stable state.

    Returns:
        "idle"    — at main prompt, output stable for stable_threshold seconds
        "dialog"  — Claude Code is asking a question (ask_user dialog)
        "timeout" — neither idle nor dialog within timeout
    """
    deadline = time.time() + timeout
    last_hash: int | None = None
    stable_since: float | None = None

    while time.time() < deadline:
        content = read_pane(label, 50)

        # Check for active spinner (Claude Code still working)
        has_spinner = any(marker in content for marker in ("\u25c9 ", "\u25ce ", "\u25cb "))

        # Check for dialog (Claude Code asking a question)
        dialog_kind = _detect_dialog_kind(content)
        if dialog_kind != DialogKind.NONE:
            return "dialog"

        # Check for main prompt (idle)
        at_prompt = _is_at_main_prompt(content)

        if at_prompt and not has_spinner:
            current_hash = hash(content)
            if current_hash == last_hash:
                if stable_since is not None and time.time() - stable_since > stable_threshold:
                    return "idle"
            else:
                last_hash = current_hash
                stable_since = time.time()
        else:
            # Reset stability counter if not at prompt
            last_hash = None
            stable_since = None

        time.sleep(0.5)

    return "timeout"
```

**Three return values and their handling:**

| Return | Meaning | `--ask` action | `--finalize` action |
|--------|---------|----------------|---------------------|
| `"idle"` | At prompt, stable 2s | Extract response, print, exit 0 | Send finalize instruction |
| `"dialog"` | Asking user a question | Print hint to switch pane, exit 1 | Abort, print hint |
| `"timeout"` | Neither after 120s | Print timeout error, exit 1 | Abort, print error |

**Why 2 seconds stable:** Claude Code sometimes pauses briefly between
tool calls.  A 2-second stable window at the main prompt reliably
indicates the full response is complete.

## Response Extraction (Capture Diff)

When `--ask` needs to extract Claude Code's response to print to
stdout, it uses the "capture diff" strategy:

```python
def extract_response(content_before: str, content_after: str, user_message: str) -> str:
    """Extract the assistant response from pane content delta.

    Args:
        content_before: read_pane() snapshot taken BEFORE sending message.
        content_after:  read_pane() snapshot taken AFTER response is stable.
        user_message:   the message we sent (to filter it out).

    Returns:
        Cleaned assistant response text.
    """
    # 1. Find the delta: lines in content_after not in content_before
    before_lines = content_before.splitlines()
    after_lines = content_after.splitlines()

    # Find where the new content starts (longest common prefix)
    common = 0
    for i, (a, b) in enumerate(zip(before_lines, after_lines)):
        if a == b:
            common = i + 1
        else:
            break

    delta_lines = after_lines[common:]

    # 2. Filter out known noise
    filtered = []
    for line in delta_lines:
        stripped = line.strip()
        # Skip the user message echo
        if stripped == user_message.strip():
            continue
        # Skip empty prompt lines
        if stripped in ("", ">", "\u276f"):
            continue
        # Skip tool call headers (keep results)
        if stripped.startswith(("\u25cf Edit", "\u25cf Read", "\u25cf Bash", "\u25cf Grep")):
            continue
        filtered.append(line)

    # 3. Trim leading/trailing blank lines
    while filtered and not filtered[0].strip():
        filtered.pop(0)
    while filtered and not filtered[-1].strip():
        filtered.pop()

    return "\n".join(filtered)
```

**Known limitations:**
- If the pane scrollback is shorter than the full conversation, older
  context may be lost.  Mitigation: use `read_pane(label, 200)` for
  generous capture.
- Tool call output formatting may vary.  v1 uses simple prefix
  filtering; can be refined based on real-world usage.
- If Claude Code's response is very long, `read_pane(200)` may
  truncate.  v2 could increase the capture window or use tmux's
  `capture-pane -p -S -` for full scrollback.

## Data Model

### Directory Structure

```
~/.duo/thinking/{name}/
\u251c\u2500\u2500 CLAUDE.md           # thinking agent role + instructions
\u251c\u2500\u2500 plan-template.md    # plan format template (written on creation)
\u251c\u2500\u2500 plan.md             # generated plan (after --finalize)
\u2514\u2500\u2500 session.log         # per-ask pane capture snapshots
```

**CLAUDE.md** — Written once on session creation.  Claude Code
auto-discovers this file and follows its instructions.  See
"Thinking CLAUDE.md" section below.

**plan-template.md** — The plan format template, written on session
creation so Claude Code can reference it during finalize.  See
"Plan File Format" section below.

**plan.md** — The finalized plan, produced by Claude Code when
`--finalize` is invoked.  Consumed by `duo start --from-thinking`.

**session.log** — Appended on each `--ask` call (not a background
thread).  Each entry is a timestamped pane capture:

```
--- ask at <ISO8601-UTC> ---
[user] I want to add rate limiting...
[response]
(captured pane delta)
--- ask at <ISO8601-UTC> ---
[user] Token bucket approach...
[response]
(captured pane delta)
```

When humans use the pane directly (not via `--ask`), tmux's own
scrollback buffer serves as the conversation record.

### Relationship to Tasks

Thinking sessions are **completely independent** of the task lifecycle.
No FSM state, no `THINKING` status, no entry in `~/.duo/tasks/`.

- Thinking is *pre-task* — no worktree, no Copilot, no task FSM.
- Clean separation: `~/.duo/thinking/` vs `~/.duo/tasks/`.
- Only connection: `--finalize` \u2192 `plan.md` \u2192 `--from-thinking` (file
  path handoff, not data model link).

### Naming Collisions (Future Concern)

`~/.duo/thinking/` is a flat namespace keyed by `{name}`.  If a user
works on multiple repos and uses the same thinking session name (e.g.,
"refactor"), they collide.

**v1:** Accept this limitation.  Names are user-chosen, collisions
are the user's responsibility.

**v2 (if needed):** Namespace by repo — `~/.duo/thinking/{repo-slug}/{name}/`.
This is a backwards-compatible change (just move directories).

## Thinking CLAUDE.md

Written to `~/.duo/thinking/{name}/CLAUDE.md` on session creation:

```markdown
# Thinking Partner \u2014 {name}

You are a software-engineering thinking partner. Your job is to help
the user refine a software task idea from vague to actionable.

## Your Conversational Style

- Ask clarifying questions when scope is ambiguous
- Propose 2-3 concrete approaches with trade-offs
- Surface hidden assumptions and risks
- Challenge over-engineering
- Push for minimum viable scope

## Rules

- Do NOT write code into the repository
- Do NOT modify any files outside ~/.duo/thinking/{name}/
- You CAN read files, grep code, and search the web to inform your thinking
- Your output is thinking and analysis, not implementation

## Finalize

When the user says "/done", asks to "finalize", or sends a finalize
instruction, produce a plan document:

1. Read the template at ~/.duo/thinking/{name}/plan-template.md
2. Distill the entire conversation into that format
3. Write the result to ~/.duo/thinking/{name}/plan.md
4. Confirm: "Plan written to ~/.duo/thinking/{name}/plan.md"

Be concrete and specific \u2014 this plan will be sent as the initial
prompt to a Copilot code-generating agent.
```

## Plan File Format

Written to `~/.duo/thinking/{name}/plan-template.md` on session
creation.  Claude Code references this template when finalizing:

```markdown
# Plan: {name}

## Goal

(One sentence describing what we're building and why.)

## Scope

### In scope
- Item 1
- Item 2

### Out of scope
- Item 1
- Item 2

## Approach

(Concrete technical plan in 3-5 paragraphs. Architecture, key design
decisions, data flow, dependencies.)

## Acceptance Criteria

- [ ] Verifiable condition 1
- [ ] Verifiable condition 2
- [ ] Verifiable condition 3

## Risks / Unknowns

- Risk 1
- Risk 2
```

`duo start --from-thinking` reads the resulting `plan.md` and uses
it as the initial bootstrap prompt, replacing the auto-generated one.

## Pane Lifecycle

### Creation (on first `--ask` or bare `duo think <name>`)

1. Create `~/.duo/thinking/{name}/` directory
2. Write `CLAUDE.md` and `plan-template.md` to that directory
3. `tmux split-window -v -P -F "#{pane_id}"` (vertical split)
   - If this fails (no tmux session, no space): fail fast with
     "Failed to create thinking pane. Is tmux running?"
4. `name_pane(pane_id, f"think-{name}")` (set label)
5. `tmux select-layout tiled`
6. `send_shell_command(label, f"cd {thinking_dir}")`
7. `send_shell_command(label, "claude")`  (start Claude Code)
8. `wait_for_idle(label, timeout=30)` (wait for Claude Code to start)

This mirrors `start_claude_commander()` from `commander.py`.  The key
difference: working directory is `~/.duo/thinking/{name}/` instead of
a worktree, and there's no task/FSM association.

### Reattach (pane already exists)

If `think-{name}` pane is already alive, `--ask` and bare
`duo think` simply use it.  No new pane is created.

Detection: `resolve_label(f"think-{name}")` succeeds \u2192 pane exists.

### Pane Death Recovery

If the pane exists but the process is dead (shell visible instead of
Claude Code):

- `--ask`: automatically re-spawn Claude Code in the existing pane
  (`send_shell_command(label, "claude")`).  Claude Code will re-read
  `CLAUDE.md` and resume.
- Bare `duo think`: same auto-recovery.

### Close (`--close`)

Terminates the tmux pane.  Files in `~/.duo/thinking/{name}/` are
preserved.  A future `duo think <name>` will create a new pane.

### Delete (`--delete`)

Terminate pane (if alive) + `shutil.rmtree(thinking_dir)`.

## CEO Interaction Mode

### Primary Path: `--ask` Sequences

The CEO agent (Claude Code) is the **primary user** of `duo think`.
The typical flow:

```bash
# Step 1: CEO starts a thinking session
duo think rate-limiter --ask "I want to add rate limiting. Requirements: per-user, configurable, Redis-backed. Approaches?"

# Step 2: CEO reads stdout response, decides next question
duo think rate-limiter --ask "Token bucket with sliding window. Edge cases for distributed?"

# Step 3: CEO refines scope
duo think rate-limiter --ask "Scope down: single-node first, Redis in v2. Summarize the agreed scope."

# Step 4: Generate the plan
duo think rate-limiter --finalize

# Step 5: Launch executor with the refined plan
duo start rate-limiter --from-thinking
```

Each `--ask` is a synchronous subprocess call.  The CEO reads stdout
to get Claude Code's response and decides the next question.

### Why This Beats CEO-native Thinking

The CEO *could* think in its own context window.  `duo think` adds:
- **Persistent, reviewable trail:** `session.log` + `plan.md` that
  the human can inspect after the fact.
- **Dedicated system prompt:** `CLAUDE.md` optimized for task
  refinement (CEO's own prompt is for orchestration, not design).
- **Full tool access:** The thinking Claude Code can read repo files,
  grep code, search the web \u2014 deeper research than internal reasoning.
- **Separate context window:** Doesn't consume CEO context budget.

## From Thinking to Start

### `duo start <name> --from-thinking`

When `--from-thinking` is specified:

1. Look for `~/.duo/thinking/{name}/plan.md`.  The `{name}` must match
   the thinking session name exactly (no cross-name lookup).
2. If found: use its content as the initial prompt (replaces the
   auto-generated `build_bootstrap_prompt`).
3. If not found: error with "No finalized plan for '{name}'. Run
   `duo think {name} --finalize` first."

### Auto-detection (v2 nice-to-have)

`duo start <name>` could auto-check for a matching thinking session
and hint.  Deferred to v2 \u2014 existing `duo start` path must remain
the default, zero-friction path.

## PR Budget Guarantee

- `duo think` **never** spawns a Copilot pane.
- `duo think` **never** calls `copilot` CLI or any Copilot-related
  transport function.
- Claude Code panes do **not** consume Premium Requests.
- The thinking directory (`~/.duo/thinking/`) is completely separate
  from `~/.duo/tasks/`.
- Even if the user runs `duo start` without `--from-thinking`, it
  works exactly as today \u2014 thinking session is untouched.

## Error Handling

### Pane Creation Failure

If `tmux split-window` fails (no tmux session, permissions, no space
for another pane):
- Print: "Failed to create thinking pane. Is tmux running?"
- Exit with non-zero status.
- Do NOT leave partial state (if directory was created but pane failed,
  keep the directory \u2014 it can be reused on retry).

### Pane Not Responding

If `wait_for_response_stable()` returns `"timeout"`:
- Print: "Thinking pane not responding after {timeout}s."
- Suggest: "Try `duo think {name} --close` then retry."
- Do not print partial/corrupted output.

### Pane Dead (Claude Code exited)

If `is_process_alive(label)` returns False when `--ask` is called:
- Auto-recovery: `send_shell_command(label, "claude")` to restart.
- `wait_for_idle(label, timeout=30)` to confirm restart.
- If restart fails: print error, suggest `--close` + retry.

### Finalize Timeout

If `plan.md` doesn't appear within 60 seconds after finalize
instruction:
- Print: "Claude Code didn't produce plan.md within 60s."
- Suggest: "Check the thinking pane manually, or run --finalize again."

### Disk Full / Write Failure

If `CLAUDE.md` or `plan-template.md` write fails:
- Print the OSError message.
- Do not create the pane (fail fast).

### Duplicate Session Name

If `~/.duo/thinking/{name}/` already exists and pane is alive:
- `--ask` and bare `duo think`: attach to existing pane (not an error).
- This is the normal "resume" path.

If directory exists but pane is dead:
- Create new pane in the same directory (Claude Code will re-read
  `CLAUDE.md`).

## Failure / Abandonment

- **Abandon mid-session:** Stop using it.  Pane stays alive until
  `--close` or system reboot.  Files persist indefinitely.
- **Close:** `--close` terminates pane, keeps files.  Re-openable.
- **Delete:** `--delete` terminates pane + removes directory.  Irreversible.
- **Crash recovery:** If the process crashes mid-conversation, just
  run `duo think <name>` again.  A new Claude Code instance starts,
  reads `CLAUDE.md`, and is ready for new questions.  Previous context
  is in Claude Code's own memory only (not persisted beyond
  `session.log` snapshots).

## Code Reuse from commander.py

The thinking pane spawn logic closely mirrors `start_claude_commander()`:

| Step | Commander | Thinking |
|------|-----------|----------|
| Create dir | task worktree (already exists) | `~/.duo/thinking/{name}/` |
| Write CLAUDE.md | `write_commander_claude_md(task)` | `write_thinking_claude_md(name)` |
| Split pane | `tmux split-window -v` | `tmux split-window -v` |
| Name pane | `duo-commander-{task.id}` | `think-{name}` |
| Layout | `tmux select-layout tiled` | `tmux select-layout tiled` |
| cd | `cd {task.worktree}` | `cd ~/.duo/thinking/{name}` |
| Start agent | `claude` | `claude` |

Recommendation: extract a shared helper
`_spawn_claude_pane(label, working_dir)` that both
`start_claude_commander()` and the new thinking code call.  CLAUDE.md
is written by the caller before invoking the shared helper.

## Compatibility

- `duo start` without `--from-thinking` works exactly as today.
- No new FSM states, no changes to Task dataclass.
- No changes to any existing command.
- No new Python dependencies.
- No breaking changes to the file protocol.

## Scope Summary

| In Scope (v1) | Out of Scope |
|----------------|-------------|
| `duo think <name> --ask "..."` (CEO primary) | Auto-detect in `duo start` |
| `duo think <name>` info + pane hint | Web UI for thinking |
| `duo think <name> --finalize` (with idle gate) | Sharing sessions |
| `duo think list` | Thinking \u2192 task auto-link |
| `duo think <name> --close` | Multi-model per-pane |
| `duo think <name> --delete` | Repo-scoped namespacing |
| `duo start --from-thinking` | REPL middle layer |
| `CLAUDE.md` + `plan-template.md` scaffolding | |
| `session.log` per-ask capture | |
| `wait_for_response_stable()` algorithm | |
| `extract_response()` capture diff | |
| Pane auto-recovery on death | |
| Shared `_spawn_claude_pane()` refactor | |
