# Design: `duo think` — Pre-Start Thinking Sessions

> **Status:** Draft — awaiting review before implementation.

## Problem

`duo start` immediately spawns a Copilot pane, creates a worktree, and
sends the first prompt.  This burns a Premium Request (PR) even if the
user's idea is half-formed.  There's no structured way to *refine* a
task before committing resources.

**Goal:** Let users (or the CEO agent) brainstorm a task's scope,
requirements, and approach *before* any Copilot session exists, then
hand off the refined plan as the initial prompt.

## Command Interface

```
duo think <name>              # start / resume interactive session
duo think <name> --ask "..."  # one-shot question (append + respond)
duo think <name> --finalize   # generate plan → ready for duo start
duo think list                # show all open thinking sessions
duo think <name> --delete     # remove a thinking session
```

### `duo think <name>`

Opens an interactive REPL-style loop in the terminal:

```
[duo:think:my-app] What's on your mind?
> I want a CLI tool that…
[duo:think:my-app] (Claude responds)
> Actually, let's also handle…
[duo:think:my-app] (Claude responds)
> /done
```

- First invocation: creates the session.
- Subsequent invocations: resumes (no separate `--resume` flag — it's
  the default behavior).  The full conversation history is shown before
  the prompt.
- Exit: `/done`, `/quit`, or Ctrl-D.

### `duo think <name> --ask "..."`

Appends a single user turn, gets one Claude response, and exits.
Useful in scripts or when you just want to add one thought.

### `duo think <name> --finalize`

Reads the full conversation and generates a structured prompt suitable
for `duo start`.  The output file is:

```
~/.duo/thinking/{name}/plan.md
```

After writing the plan, prints a suggestion:

```
Plan written to ~/.duo/thinking/my-app/plan.md
Review it, then run:
  duo start my-app --from-thinking
```

### `duo think list`

Lists all thinking sessions with status (active / finalized / stale):

```
NAME         TURNS  STATUS      LAST ACTIVITY
my-app         12   finalized   2 hours ago
refactor        3   active      5 minutes ago
```

### `duo think <name> --delete`

Removes `~/.duo/thinking/{name}/` after confirmation.

## Data Model

### Storage

```
~/.duo/thinking/{name}/
├── session.jsonl    # conversation log (one JSON object per turn)
├── plan.md          # generated plan (after --finalize)
└── meta.json        # session metadata
```

**session.jsonl** — each line:
```json
{"role": "user", "content": "...", "ts": "2025-01-15T10:30:00Z"}
{"role": "assistant", "content": "...", "ts": "2025-01-15T10:30:05Z"}
```

**meta.json:**
```json
{
  "name": "my-app",
  "created_at": "2025-01-15T10:30:00Z",
  "status": "active",
  "model": "claude-sonnet-4-5-20250514",
  "turn_count": 12
}
```

### Relationship to Tasks

Thinking sessions are **completely independent** of the task lifecycle.
No FSM state, no `THINKING` status, no entry in `~/.duo/tasks/`.

Rationale:
- Thinking is *pre-task* — no worktree, no Copilot, no tmux pane.
- Mixing it into the Task dataclass would add complexity for zero
  benefit (no transitions, no monitoring, no scheduling).
- Clean separation: `~/.duo/thinking/` vs `~/.duo/tasks/`.

The only connection point is `--finalize` → `--from-thinking`: a
file path handoff, not a data model link.

## How the Conversation Happens

### Recommended: Option (c) — Local Claude API Call

`duo think` calls the Claude API directly from the CLI process.
No tmux, no Copilot, no pane.

**How it works:**
1. User types a message in the terminal.
2. `duo think` sends the full conversation history + new message to the
   Claude API (via `anthropic` Python SDK).
3. Response is streamed to the terminal and appended to `session.jsonl`.
4. Repeat until `/done`.

**Alternatives considered:**

| Approach | Pros | Cons |
|----------|------|------|
| **(a)** New tmux pane with Claude Code | Familiar UX, reuse transport | Burns a PR just to think; overkill |
| **(b)** Write prompts to file, existing session reads | No API key needed | Requires a running Copilot; still burns PR |
| **(c)** Direct API call ✅ | Zero PR cost; fast; simple | Needs API key; no tool use |

**Recommendation: (c)** because:
- Zero Copilot interaction = zero PR burned.
- The thinking phase doesn't need tool use or file editing — it's pure
  conversation.
- The `anthropic` SDK is lightweight and already available in most
  environments.
- If the user doesn't have an API key, fall back to a simple local-only
  mode (user edits `session.jsonl` manually, `--finalize` still works).

### API Key Configuration

```bash
duo config set anthropic_api_key sk-ant-...
# or
export ANTHROPIC_API_KEY=sk-ant-...
```

If no key is configured, `duo think` operates in **offline mode**:
- User can still write to `session.jsonl` manually (or via `--ask`
  which just appends the user turn without a response).
- `--finalize` still works (it reads whatever is in the session).
- A warning is printed: "No API key configured. Running in offline
  mode — responses will not be generated."

## From Thinking to Start

### `duo start <name> --from-thinking`

When `--from-thinking` is specified:

1. Look for `~/.duo/thinking/{name}/plan.md`.
2. If found: use its content as the initial prompt (replaces the
   auto-generated `build_bootstrap_prompt`).
3. If not found: error with "No finalized plan for '{name}'. Run
   `duo think {name} --finalize` first."

The task name in `duo start` doesn't need to match the thinking session
name, but by convention they should be the same.

### Auto-detection (no flag needed)

Alternative: `duo start <name>` automatically checks
`~/.duo/thinking/{name}/plan.md`.  If it exists, prompt:

```
Found finalized thinking session 'my-app'.
Use it as the initial prompt? [Y/n]
```

**Recommendation:** Use the explicit `--from-thinking` flag.
Auto-detection is a nice-to-have for v2 but adds complexity and
surprise behavior.  The existing `duo start` path (no thinking)
must remain the default, zero-friction path.

## PR Budget Guarantee

- `duo think` **never** spawns a Copilot pane or tmux session.
- `duo think` **never** calls any transport function.
- The thinking directory (`~/.duo/thinking/`) is completely separate
  from `~/.duo/tasks/`.
- Even if the user accidentally runs `duo start` without
  `--from-thinking`, it works exactly as today — no thinking session
  is consumed or corrupted.

### Accidental `duo start` Without Thinking

If a thinking session exists for the same name and the user runs plain
`duo start my-app` (without `--from-thinking`):

- **v1:** Silent — start works as usual, thinking session is ignored.
- **v2 (nice-to-have):** Print a one-line hint: "Tip: found thinking
  session 'my-app'. Use --from-thinking to include the plan."

## Failure / Abandonment

- **Abandon mid-session:** Just stop typing. The session stays in
  `~/.duo/thinking/{name}/` and can be resumed anytime.
- **Delete:** `duo think <name> --delete` removes the directory after
  confirmation (`Are you sure? This cannot be undone.`).
- **Archive:** Not needed for v1. Sessions are tiny (< 100 KB).
  `duo think list` shows all sessions with last-activity timestamps
  so stale ones are visible.
- **Crash recovery:** Since each turn is appended to `session.jsonl`
  immediately after it's generated, a crash loses at most the
  in-flight response (which can be re-requested on resume).

## CEO Interaction Mode

### Does `duo think` produce dialogs?

No. `duo think` is a local CLI conversation — it doesn't use tmux,
Copilot, or dialogs.  The CEO agent wouldn't interact with `duo think`
through the dialog mechanism at all.

### CEO + Thinking Flow

If the CEO agent wants to use thinking:

```bash
# CEO brainstorms via --ask (one-shot, no interactive REPL)
duo think my-feature --ask "I want to add rate limiting. What approaches exist?"
duo think my-feature --ask "Let's go with token bucket. What edge cases?"
duo think my-feature --ask "Good. What about distributed environments?"
duo think my-feature --finalize

# Now start with the refined plan
duo start my-feature --from-thinking
```

Each `--ask` call is atomic: append user turn → get response → exit.
This works perfectly in a CEO script without interactive terminal.

### Alternative: CEO-native thinking (without `duo think`)

The CEO agent (Claude Code) already has its own reasoning ability.
It could simply:
1. Think internally (in its own context window).
2. Write the refined plan to a file.
3. Pass that file as the prompt to `duo start`.

`duo think` is primarily for **human** users who want structured
brainstorming.  The CEO can use it too (via `--ask`), but it's not
the primary audience.

## Compatibility

- `duo start` without `--from-thinking` works exactly as today.
  No behavioral change.
- No new FSM states, no changes to Task dataclass.
- No changes to any existing command.
- The `anthropic` SDK is an optional dependency — `duo think` in
  offline mode works without it.
- No breaking changes to the file protocol.

## Scope Summary

| In Scope (v1) | Out of Scope |
|----------------|-------------|
| `duo think <name>` interactive REPL | Tool use during thinking |
| `duo think <name> --ask "..."` | Multi-model support |
| `duo think <name> --finalize` | Auto-detect in `duo start` |
| `duo think list` | Web UI for thinking |
| `duo think <name> --delete` | Sharing sessions |
| `duo start --from-thinking` | Thinking → task auto-link |
| Offline mode (no API key) | |
| `session.jsonl` + `plan.md` output | |
