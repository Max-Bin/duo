# Example 01: Hello World

> **Difficulty:** 🟢 Beginner
>
> **Goal:** Create a `hello.py` file that prints "Hello, World!" — the simplest possible Duo task.

## Prerequisites

- A running tmux session (`tmux new -s duo`)
- Duo installed (`bash install.sh`)
- A git repository to work in

## Steps

### 1. Verify your environment

```bash
duo doctor
```

This checks that tmux, git, and Copilot CLI are all available. Fix any issues before proceeding.

### 2. Initialize Duo in your project

```bash
cd your-project
duo init --repo .
```

This creates a `.duo/` directory in your project root to store task state, worktrees, and metadata.

### 3. Start the task

```bash
duo start hello --repo . --desc "Create a hello.py that prints Hello World"
```

Duo will:

1. Create an isolated git worktree for the `hello` task
2. Open a new tmux pane with Copilot CLI
3. Send the description as the initial prompt

### 4. Wait for completion

```bash
duo watch
```

This blocks until the executor finishes or a permission dialog appears. If a dialog appears, approve it:

```bash
duo ceo-approve hello
```

### 5. Check the result

```bash
duo status hello
```

The task should be in `COMPLETED` state.

### 6. Merge the work

```bash
duo merge hello
```

This fast-forward merges the worktree branch back into your current branch.

## What Happened

Behind the scenes, Duo:

1. **Created a worktree** — `git worktree add` in an isolated directory
2. **Started Copilot CLI** — in a dedicated tmux pane
3. **Sent the prompt** — via the JSON file protocol
4. **The executor wrote `hello.py`** — committed to the worktree branch
5. **Merged** — fast-forward merge back to your branch

## Expected Output

After merging, you should have a `hello.py` file:

```python
print("Hello, World!")
```

## Cleanup

The worktree is automatically cleaned up after `duo merge`. To verify:

```bash
duo status hello    # should show COMPLETED
```
