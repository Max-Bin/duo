# Design: `duo think` — Pre-Start Thinking Sessions

> **Status:** Draft v2 — addressing review feedback.

## Problem

`duo start` immediately spawns a Copilot pane, creates a worktree, and
sends the first prompt.  This burns a Premium Request (PR) even if the
idea is half-formed.  There's no structured way to *refine* a task
before committing resources.

This isn't just a human problem — it's primarily a **CEO agent**
problem.  The typical Duo workflow is: user talks to Claude Code (the
CEO), CEO orchestrates executors via `duo start`.  The CEO needs a way
to do structured brainstorming *before* spawning Copilot sessions, so
it can produce well-scoped, actionable plans that minimize wasted PRs.

**Goal:** Give the CEO agent (and optionally humans) a structured
thinking channel that refines ideas into actionable plans, at zero
Copilot cost, then hands off to `duo start`.

## Command Interface

```
duo think <name>              # start / resume interactive REPL (secondary mode)
duo think <name> --ask "..."  # one-shot question — CEO primary path
duo think <name> --finalize   # generate plan.md → ready for duo start
duo think list                # show all open thinking sessions
duo think <name> --delete     # remove a thinking session
```

### `duo think <name> --ask "..."` (Primary Path — CEO)

Appends a single user turn, gets one Claude response, prints it, and
exits.  This is the **primary interface** — designed for programmatic
use by the CEO agent:

```bash
# CEO brainstorms via sequential --ask calls
duo think rate-limiter --ask "I want to add rate limiting to the API. What approaches exist?"
duo think rate-limiter --ask "Let's go with token bucket. What edge cases should we handle?"
duo think rate-limiter --ask "Good. What about distributed environments with Redis?"
duo think rate-limiter --finalize

# Hand off the refined plan
duo start rate-limiter --from-thinking
```

Each `--ask` call is atomic: append user turn → get Claude response →
append assistant turn → exit.  No interactive terminal needed.  The CEO
chains these calls in its own orchestration loop.

### `duo think <name>` (Secondary Mode — Human REPL)

Opens an interactive REPL for humans who want to brainstorm directly:

```
[duo:think:my-app] What's on your mind?
> I want a CLI tool that…
[duo:think:my-app] (Claude responds with streaming output)
> Actually, let's also handle…
[duo:think:my-app] (Claude responds)
> /done
Finalize now? [Y/n] y
Plan written to ~/.duo/thinking/my-app/plan.md
```

- First invocation: creates the session.
- Subsequent invocations: resumes (full conversation history shown).
- Exit: `/done`, `/quit`, or Ctrl-D.
- On `/done`: prompt "Finalize now? [Y/n]" — if yes, immediately
  generate `plan.md` (saves the extra `--finalize` step).

### `duo think <name> --finalize`

Reads the full conversation from `session.jsonl` and asks Claude to
distill it into a structured `plan.md` (see Plan File Format below).

Output: `~/.duo/thinking/{name}/plan.md`

After writing, prints:

```
Plan written to ~/.duo/thinking/rate-limiter/plan.md
Review it, then run:
  duo start rate-limiter --from-thinking
```

### `duo think list`

Lists all thinking sessions with status (active / finalized / stale):

```
NAME            TURNS  STATUS      LAST ACTIVITY
rate-limiter       6   finalized   2 hours ago
refactor           3   active      5 minutes ago
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
{"role": "user", "content": "...", "ts": "<ISO8601-UTC>"}
{"role": "assistant", "content": "...", "ts": "<ISO8601-UTC>"}
```

**meta.json:**
```json
{
  "name": "rate-limiter",
  "created_at": "<ISO8601-UTC>",
  "status": "active",
  "model": "<from-config>",
  "turn_count": 6
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

### Decision: Option (c) — Local Claude API Call

`duo think` calls the Claude API directly from the CLI process.
No tmux, no Copilot, no pane.

**How it works:**
1. `--ask` (or REPL input) provides a user message.
2. `duo think` sends the full conversation history + new message to the
   Claude API (via `anthropic` Python SDK).
3. Response is streamed to stdout and appended to `session.jsonl`.
4. For `--ask`: exit.  For REPL: repeat until `/done`.

**Alternatives considered:**

| Approach | Pros | Cons |
|----------|------|------|
| **(a)** New tmux pane with Claude Code | Familiar UX, reuse transport | Burns a PR just to think; overkill |
| **(b)** Write prompts to file, existing session reads | No API key needed | Requires a running Copilot; still burns PR |
| **(c)** Direct API call ✅ | Zero PR cost; fast; simple | Needs API key; no tool use |

**(c)** wins because:
- Zero Copilot interaction = zero PR burned.
- The thinking phase doesn't need tool use or file editing — it's pure
  conversation.
- The `anthropic` SDK is lightweight.
- If the user doesn't have an API key, fall back to offline mode.

### API Key Configuration

```bash
duo config set anthropic_api_key sk-ant-...
# or
export ANTHROPIC_API_KEY=sk-ant-...
```

If no key is configured, `duo think` operates in **offline mode**:
- `--ask` appends the user turn to `session.jsonl` but prints a
  warning instead of a response: "No API key configured — user turn
  recorded but no response generated."
- `--finalize` still works (reads whatever is in the session).
- REPL mode prints: "No API key configured. Running in offline mode —
  responses will not be generated. Use /done to exit."

## Model Selection

- **Default:** `claude-haiku-4-5-latest` — fast and cheap, ideal for
  the rapid back-and-forth of a thinking session.
- **Config override:** `duo config set thinking_model <model-id>` —
  persists across invocations.
- **Per-invocation override:** `duo think my-app --model opus` —
  convenience aliases: `haiku`, `sonnet`, `opus` expand to their
  latest IDs.

The thinking phase is high-frequency, low-stakes conversation.
Defaulting to the cheapest capable model keeps costs proportional to
the value (zero PR, minimal API cost).

## Thinking Agent System Prompt

The system prompt sent with every API call:

```
You are a software-engineering thinking partner. Your job is to help
the user refine a software task idea from vague to actionable.

Your conversational style:
- Ask clarifying questions when scope is ambiguous
- Propose 2-3 concrete approaches with trade-offs
- Surface hidden assumptions and risks
- Challenge over-engineering
- Push for minimum viable scope

Do NOT write code. Do NOT propose specific file names or line numbers.
Your output is thinking, not implementation.

When the user says /done or asks to finalize, you will be asked to
produce a structured Plan document. Until then, stay in thinking mode.
```

This prompt is stored as a constant in the source code.  Future
iteration may make it configurable via
`duo config set thinking_system_prompt "..."`.

## Plan File Format

The `--finalize` command instructs Claude to distill the conversation
into this exact markdown structure:

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

---
Generated from thinking session with {turn_count} turns.
```

The finalize prompt to Claude is:

```
Distill our entire conversation into a Plan document using this exact
structure: [structure above]. Be concrete and specific — this plan
will be sent as the initial prompt to a code-generating agent. Do not
include anything we didn't discuss.
```

`duo start --from-thinking` reads this `plan.md` and uses it as the
initial bootstrap prompt, replacing the auto-generated one.

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

### Auto-detection (v2 nice-to-have)

`duo start <name>` could automatically check for
`~/.duo/thinking/{name}/plan.md` and hint.  Deferred to v2.
The existing `duo start` path must remain the default, zero-friction
path with no behavioral change.

## Error Handling

### Network Errors

- Retry up to 3 times with exponential backoff (1s, 2s, 4s).
- On final failure: print the error and exit.
- The user turn is **already** written to `session.jsonl` before the
  API call, so the user's input is never lost.
- The assistant turn is **never** half-written: we buffer the full
  streamed response, then append it atomically.

### Atomic Write Pattern

All writes to `session.jsonl` follow this pattern:
1. Build the complete JSON line in memory (or stream to a buffer).
2. Write to a temp file (`session.jsonl.tmp`) with `fsync`.
3. Append the temp file contents to `session.jsonl`.
4. Delete the temp file.

This ensures a crash at any point never corrupts the session log.

### Rate Limit (HTTP 429)

Print: "Rate limited by the API. Wait a moment and retry:"
```
  duo think {name} --ask "{last message}"
```
The user turn is already in `session.jsonl`, so `--ask` with the same
message will skip the duplicate user turn and just retry the API call.

### Invalid API Key (HTTP 401)

Print: "Invalid or expired API key. Configure it with:"
```
  duo config set anthropic_api_key <your-key>
  # or
  export ANTHROPIC_API_KEY=<your-key>
```

### General Principle

Any error during the API call must:
1. Never corrupt `session.jsonl`.
2. Never lose user input that was already submitted.
3. Print a clear error message with a concrete fix/retry command.

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
- **Crash recovery:** Since each turn is appended atomically (see
  Error Handling), a crash loses at most the in-flight response.
  On resume, if the last entry in `session.jsonl` is a user turn with
  no following assistant turn, `duo think` automatically retries the
  API call.

## CEO Interaction Mode

### Primary Path: `--ask` Sequences

The CEO agent (Claude Code) is the **primary user** of `duo think`.
The typical flow:

```bash
# Step 1: CEO creates a thinking session with structured questions
duo think rate-limiter --ask "I want to add rate limiting to the API gateway. Requirements: per-user limits, configurable thresholds, Redis-backed. What approaches exist?"

# Step 2: CEO reads the response (stdout), decides next question
duo think rate-limiter --ask "Let's go with token bucket with sliding window. What edge cases should we handle for distributed deployments?"

# Step 3: CEO continues refining
duo think rate-limiter --ask "Good analysis. Let's scope down: single-node first, Redis in v2. Finalize the scope."

# Step 4: Generate the plan
duo think rate-limiter --finalize

# Step 5: Launch executor with the refined plan
duo start rate-limiter --from-thinking
```

The CEO controls the conversation loop in its own context window.
Each `--ask` is a synchronous subprocess call — the CEO reads stdout
to get the response and decides the next question.

### No Dialog Mechanism

`duo think` is a local CLI conversation — it doesn't use tmux,
Copilot, or dialogs.  The CEO interacts with it through simple
subprocess calls (`--ask`), not through the dialog/transport layer.

### CEO-native Thinking (Alternative)

The CEO can also think in its own context window and write a plan
file directly.  `duo think` adds value by:
- Providing a **persistent, reviewable** conversation trail
  (`session.jsonl`) that the human can inspect.
- Using a **dedicated system prompt** optimized for task refinement
  (the CEO's own prompt is optimized for orchestration, not design).
- Keeping a **separate API budget** — the thinking conversation uses
  the Anthropic API directly, not the CEO's own context window.

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
| `duo think <name> --ask "..."` (CEO primary) | Tool use during thinking |
| `duo think <name>` interactive REPL (human secondary) | Auto-detect in `duo start` |
| `duo think <name> --finalize` | Web UI for thinking |
| `duo think list` | Sharing sessions |
| `duo think <name> --delete` | Thinking → task auto-link |
| `duo start --from-thinking` | |
| Offline mode (no API key) | |
| `session.jsonl` + `plan.md` output | |
| Atomic writes + retry on error | |
| Haiku default + `--model` override | |
| `/done` → finalize prompt in REPL | |
