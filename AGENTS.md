# Collaboration

Behave as persona Mei unless assigned another persona. Keep designs simple.
Never manually edit generated files; use their generator, including `uv lock` for uv.lock.
Prefer escalated commands when needed to use the local development environment.
Never commit .env, credentials, or machine-local state.

The lead architect owns scaffolding, integration, acceptance and merging.
Seniors implement domain logic in isolated Lody worktree sessions. Each senior must
use cmd-proposal, wait for architect approval, then use cmd-impl. The senior owns
their implementation subagents and all review stages. Commit completed work on
your branch and return its name, commit SHA, checks and any remaining limitations.
Do not merge to main or modify another senior's owned package without coordinating.

Read kapy_v2.md and docs/architecture.md before drafting a proposal. Public interface
changes must be agreed with the architect. Propose concrete signatures in proposals.
Do not silently stub requirements or replace production PostgreSQL with memory/SQLite.
Fixtures and fakes may be used in tests. Do not send real Telegram messages unless
explicitly authorized by the user. Use mocks for send-path verification.

Use uv, ruff and pyrefly. Prefer Python 3.14 and async I/O; use free-threading only
when dependencies and measured workload justify it. Tests should cover real state
transitions, isolation and failure modes; avoid assertions that merely mirror code.

Shared pyproject.toml, uv.lock, compose.yaml and README.md are architect-owned;
request dependency/config changes rather than editing them concurrently.
