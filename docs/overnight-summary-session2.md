# Overnight Session 2 Summary — Rounds BS-CB

## Stats
- **Start**: 239 commits, 1765 tests, 100% coverage
- **End**: 249 commits, 1775 tests, 100% coverage
- **Tests added**: 10
- **Rounds completed**: 10 (BS through CB)

## Commits

| Commit | Round | Category | Description |
|--------|-------|----------|-------------|
| `3fbd084` | BS | feat | init creates persistent worktree base dir |
| `ae09987` | BT | feat | Wire auto_allow_all config to start_session |
| `59a0e0e` | BU | security | Shell-quote worktree cd commands |
| `b5f159c` | BV | fix | BLOCKED/ESCALATED tasks don't consume slots or auto-restart |
| `60661c6` | BW | fix | Queued send persists prompt, promotion replays it |
| `26330db` | BX | fix | Resume replays last prompt |
| `53db1bf` | BY | docs | Document resolved findings + update badges |
| `df65670` | BZ | fix | Restart preserves attempt count + replays persisted prompt |
| `bdb9c60` | CA | fix | Bare resume skips QUEUED tasks |
| `118e6b7` | CB | fix | Git errors use DuoUserError with fix suggestion |

## Key Findings Resolved

### Security
- **Shell injection via unquoted worktree path** (CRITICAL): `shlex.quote()` defense-in-depth

### Runtime Correctness
- **BLOCKED tasks auto-restarting** (CRITICAL): Monitor no longer polls BLOCKED tasks
- **ESCALATED/BLOCKED consuming execution slots** (HIGH): Removed from ACTIVE_STATUSES
- **Queued send timing out** (HIGH): Persists prompt file instead of calling transport
- **Resume leaving executor idle** (HIGH): Replays persisted/synthesized prompt
- **Restart losing correction context** (HIGH): Preserves attempt count

### Config/UX
- **auto_allow_all dead config** (HIGH): Now wired to start_session
- **Git errors missing fix suggestions** (MED): Uses DuoUserError framework
- **Resume starting queued tasks** (MED): Excludes QUEUED from bare resume

## Rubber-Duck Audits
- Round BT: `commander.py + cli.py + scheduler.py` UX audit (3 HIGH findings)
- Round BU: Runtime audit followup (6 findings across security/correctness)
- Round CA: CLI edge case audit (1 MED finding)
