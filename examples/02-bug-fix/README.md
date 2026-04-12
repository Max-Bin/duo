# Example 02: Bug Fix with Thinking

> **Difficulty:** 🟡 Intermediate
>
> **Goal:** Use `duo think` to plan a bug fix before spending Premium Requests on execution.

## Scenario

You have a bug report:

> "The login handler returns HTTP 500 on invalid email input instead of a proper 400 validation error."

Instead of jumping straight into a task (which costs Premium Requests), you'll use `duo think` to brainstorm the fix plan first — for free.

## Prerequisites

- A running tmux session (`tmux new -s duo`)
- Duo installed and `duo doctor` passing
- A git repository with the buggy code

## Steps

### Phase 1: Think (Free — No Premium Requests)

#### 1. Start a thinking session

```bash
duo think --repo . --ask "We have a bug: login returns 500 on invalid email. What's the fix?"
```

Duo opens a thinking session where you can brainstorm with the AI without spending Premium Requests. The thinking model will:

- Analyze the likely root cause
- Suggest which files to change
- Outline the fix approach
- Identify edge cases to handle

#### 2. Review the output

Read through the thinking session output carefully. The AI might suggest:

- Adding email validation before the database lookup
- Returning a 400 with a descriptive error message
- Adding test cases for invalid email formats

#### 3. Finalize the plan

```bash
duo think --finalize
```

This saves the thinking session as a reusable plan that can be passed to an executor.

### Phase 2: Execute (Costs Premium Requests)

#### 4. Start the task with thinking context

```bash
duo start fix-login --repo . --desc "Fix login 500 on invalid email" --from-thinking
```

The `--from-thinking` flag carries the thinking session context into the task. The executor already knows:

- What the bug is
- Which files to modify
- The approach to take

This means fewer round-trips and fewer Premium Requests.

#### 5. Auto-handle dialogs

```bash
duo watch fix-login
```

This watches for permission dialogs and lets you handle them as they appear, so the executor can work uninterrupted.

#### 6. Merge the fix

```bash
duo merge fix-login
```

## What Happened

1. **Thinking phase** — You brainstormed the fix plan with the AI for free. No Premium Requests were consumed.
2. **`--from-thinking`** — The plan was carried into the executor as context, so it knew exactly what to do.
3. **`watch`** — Permission dialogs were handled promptly, minimizing idle time.
4. **Result** — The bug was fixed with minimal PR consumption because the executor had a clear plan from the start.

## Why Think First?

| Without `duo think` | With `duo think` |
|---------------------|------------------|
| Executor explores the codebase (costs PRs) | Thinking session explores for free |
| Multiple correction cycles | Clear plan → fewer corrections |
| ~5-8 Premium Requests | ~2-3 Premium Requests |
| Trial and error | Targeted fix |

## Tips

- Use `duo think` for any non-trivial task — the free brainstorming phase pays for itself
- Review the thinking output carefully before finalizing
- The `--from-thinking` flag is optional — you can also manually copy insights into the `--desc`
