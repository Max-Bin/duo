# Example 03: Multi-Task Parallel

> **Difficulty:** 🔴 Advanced
>
> **Goal:** Run multiple Duo tasks simultaneously and manage them as a fleet.

## Scenario

You have 3 independent improvements to make to your project:

1. Add unit tests for the auth module
2. Fix typos in documentation
3. Add structured logging to API handlers

Since these changes don't overlap, you can run them in parallel — each in its own isolated worktree.

## Prerequisites

- A running tmux session (`tmux new -s duo`)
- Duo installed and `duo doctor` passing
- A git repository with the relevant source code
- Enough Premium Request budget for 3 concurrent tasks

## Steps

### 1. Start all 3 tasks

```bash
duo start add-tests --repo . --desc "Add unit tests for auth module"
duo start fix-typos --repo . --desc "Fix all typos in documentation"
duo start add-logging --repo . --desc "Add structured logging to API handlers"
```

Each task gets its own:

- Git worktree (isolated branch)
- Tmux pane (separate executor)
- State directory (independent FSM)

### 2. Monitor all tasks

```bash
duo list
```

This shows all active tasks with their current FSM states. You'll see something like:

```
TASK          STATE        BRANCH               PANE
add-tests     EXECUTING    duo/add-tests        %3
fix-typos     EXECUTING    duo/fix-typos        %4
add-logging   EXECUTING    duo/add-logging      %5
```

For a live updating view:

```bash
duo dashboard
```

### 3. Auto-handle dialogs for all tasks

```bash
duo watch
```

Running `watch` monitors all tasks for permission dialogs. No manual babysitting required.

### 4. Check costs

```bash
duo cost
```

This shows Premium Request consumption per task:

```
TASK          PRs USED    STATUS
add-tests     3           executing
fix-typos     1           completed
add-logging   4           executing
─────────────────────────
TOTAL         8
```

To enforce a budget limit:

```bash
duo cost --budget 20
```

This will warn you if total consumption approaches the limit.

### 5. Merge completed tasks

As tasks complete, merge them one at a time:

```bash
duo merge fix-typos       # usually finishes first (smallest scope)
duo merge add-tests
duo merge add-logging
```

Each merge is a fast-forward merge into your current branch. Since the worktrees are isolated, there should be no conflicts between tasks — as long as they modify different files.

## What Happened

1. **3 isolated worktrees** — Each task got its own branch and working directory
2. **Parallel execution** — All 3 executors ran simultaneously in separate tmux panes
3. **Independent dialog handling** — `watch` monitored all tasks for dialogs
4. **Cost tracking** — `duo cost` showed real-time PR consumption across all tasks
5. **Sequential merges** — Tasks were merged one by one as they completed

## Tips

- **Use unique, descriptive task names** — they become branch names and show up in `duo list`
- **Check `duo cost` regularly** — parallel tasks burn PRs faster
- **Use `duo cost --budget N`** — set a ceiling to avoid surprises
- **Start with independent tasks** — avoid tasks that modify the same files, or you'll hit merge conflicts
- **Merge smallest tasks first** — reduces the chance of conflicts with longer-running tasks
- **Use `duo status <task>`** — check individual task state if something looks stuck
- **Scale gradually** — start with 2 parallel tasks before jumping to 5+
