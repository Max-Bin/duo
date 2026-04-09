# Duo Examples

Step-by-step walkthroughs for common Duo workflows.

| # | Example | Difficulty | Description |
|---|---------|------------|-------------|
| 01 | [Hello World](01-hello-world/) | 🟢 Beginner | The simplest possible task — create a single file |
| 02 | [Bug Fix with Thinking](02-bug-fix/) | 🟡 Intermediate | Use `duo think` to plan before spending Premium Requests |
| 03 | [Multi-Task Parallel](03-multi-task/) | 🔴 Advanced | Run multiple agents simultaneously with cost tracking |

## Prerequisites

All examples assume:

- **Duo is installed** — run `bash install.sh` from the repo root
- **tmux is running** — Duo requires a tmux session (`tmux new -s duo`)
- **A git repository** — each example operates on a git repo
- **Copilot CLI is available** — `duo doctor` will verify this

## How to Use These Examples

1. Read the example README for context and prerequisites
2. Copy-paste the commands into your terminal
3. Each example builds on concepts from previous ones — start with Hello World

## Quick Reference

```bash
duo doctor       # verify your environment before starting
duo --help       # see all available commands
duo cost         # check Premium Request usage at any time
```
