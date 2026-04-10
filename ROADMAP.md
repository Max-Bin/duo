# Duo Roadmap

## Vision

Duo aims to become the standard orchestration layer for AI coding agents. Today, Premium Requests are treated as an infinite budget — developers fire off agents with no visibility into cost or coordination. Duo makes Premium Requests a **managed resource**: planned, tracked, and optimized across tasks and teams.

## v1.x (Current) — Stability & Polish

The v1.x series focuses on hardening the runtime and improving the developer experience.

- ✅ **Edge-case hardening** — robust error handling, crash recovery, atomic writes
- ✅ **Performance benchmarking** — `duo bench` for measuring orchestration overhead
- ✅ **Shell completion** — tab-completion for bash, zsh, and fish
- ✅ **PR usage analytics** — `duo cost` for tracking Premium Request consumption per task
- ✅ **Developer experience** — actionable error messages, `duo doctor` diagnostics
- ✅ **Security hardening** — 53 secret patterns, path traversal defense, input validation
- ✅ **100% test coverage** — 2322+ tests with statement and branch coverage
- ✅ **CEO automation** — `ceo-loop`, `ceo-smart`, `ceo-dispatch` for autonomous dialog handling
- ✅ **Pre-start brainstorming** — `duo think` for problem decomposition before coding

## v2.x (Next) — Multi-Executor Support

The v2.x series expands Duo beyond a single executor model.

- **Claude Code executor** — support Claude Code as an alternative to Copilot CLI
- **Cross-project orchestration** — manage tasks across multiple repositories with a shared task queue
- **Team mode** — multiple commanders sharing state and coordinating work
- **Cloud-hosted executors** — run executors on remote machines without local tmux

## v3.x (Future) — Distributed Execution

The v3.x series envisions Duo as a platform for distributed AI agent orchestration.

- **Remote executor pools** — elastic scaling of executor capacity
- **Agent marketplace** — pluggable verification strategies, scheduling algorithms, and executor types
- **Web dashboard** — browser-based monitoring and control plane
- **API-first design** — RESTful API for programmatic orchestration

## Disclaimer

> This roadmap reflects current thinking and may change based on community feedback, technical discoveries, and evolving AI tooling. Items listed here are **not commitments** — they represent directions we're exploring.

## Links

- [Getting Started](docs/getting-started.md)
- [Architecture](docs/architecture.md)
- [CEO Workflow](docs/ceo-workflow.md)
- [Design: duo think](docs/design-duo-think.md)
- [Changelog](CHANGELOG.md)
