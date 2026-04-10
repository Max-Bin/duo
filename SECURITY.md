# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly:

1. **Do NOT** open a public issue
2. Email: vborisw@gmail.com
3. Include: description, reproduction steps, impact assessment

We will acknowledge within 48 hours and provide a fix timeline within 7 days.

## Security Model

Duo executes code via tmux-controlled Copilot CLI sessions. Key safeguards:

- **File protocol**: All task communication via JSON files, no network exposure
- **Scope verification**: Changed files validated against `writable_paths` with root-anchored matching
- **Secret detection**: Diffs scanned for 53 credential patterns (API keys, tokens, private keys)
- **Path traversal defense**: Symlink/hardlink detection, Unicode NFC normalization
- **Input validation**: Regex validators use `\Z` (not `$`) to prevent trailing-newline bypass
- **PR budget**: Configurable limit on Premium Request consumption
- **Shell safety**: All subprocess calls use `shell=False` with explicit timeouts
- **Atomic writes**: JSON operations use tmp+rename with symlink refusal
- **Model injection defense**: `DUO_COPILOT_MODEL` env var validated for shell metacharacters
