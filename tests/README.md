# Tests

These suites exercise the maintained `src/kapy/` implementation. Earlier tests are
preserved under `prototype/tests/` and are outside the default test target.

Run the full suite inside the runtime container so process, PTY and file tests use
an isolated filesystem and real subprocesses:

```sh
docker compose exec -T runtime python -m pytest -q -p no:cacheprovider tests packages/pydantic-ai-layer-protocol/tests
```

Interface lifespan and Temporal runner tests require a running Temporal service
(`docker compose up -d temporal-namespace`). Compose sets
`KAPY_TEMPORAL_ADDRESS=temporal:7233` for runtime (host default:
`localhost:7233`). Runner tests use unique Activity IDs/task queues and a local HTTP
server for the actual SDK model protocols. See
[`runner`](../src/kapy/runner/README.md).

Database tests own disposable PostgreSQL schemas and Valkey namespaces. The normal
suite uses deterministic SDK models and local model protocol servers. Browser
checks and the isolated frontend API host are documented in `frontend/README.md`.

The durable shell-layer real-service tests additionally need the four shellctl Go
binaries (`make -C packages/shellctl build-server`) and `tmux`. Run it inside a
disposable container with those binaries mounted read-only, and set
`KAPY_SHELLCTL_TEST_BIN_DIR` to their absolute container directory. Without this
explicit setting these extra service integrations are skipped; HTTP contract
and failure-path tests still run. With the setting, missing binaries/dependencies
are failures. The fixture owns a private shellctl server, SQLite directory and
tmux socket; PostgreSQL continues to use a disposable schema. It never connects
to a deployed shellctl instance.

```sh
KAPY_SHELLCTL_TEST_BIN_DIR=/work/packages/shellctl/server/bin \
  python -m pytest -q -p no:cacheprovider \
  tests/layers/test_shell_scopes.py \
  tests/runner/test_layers.py
```

`tests/layers` checks Kapy shell behavior and host scope retries.
The independent package's `packages/pydantic-ai-layer-protocol/tests` checks
typed factory assembly, arbitrary writable deps layouts,
JSON snapshots, native for_run isolation, tool flags, hooks, instructions and ordinary run isolation. Its real Pyrefly probes use the project configuration to check
model/hook signatures, including the required optional State on exit.
`runner/test_layers.py` uses deterministic SDK streaming with real
PostgreSQL, standalone Temporal Activities and a private shellctl server. It covers
sequential calls, cross-run cursors, native ModelRetry, failure/cancellation keeping
the last saved node boundary and session-close retry progress. `runner/test_snapshots.py`
checks the three interrupted transitions through real SDK runs and PostgreSQL,
including prompt-free resumption, interruption feedback, pending tool returns,
layer State, model/output retries and tool failure feedback. It also checks that
tool → end retains the preceding snapshot for text, TextOutput and output tools,
and that recovery requests the model with or without instructions. The runner tests also
exercise three model protocols, single-attempt failures, final-save failure and
resource cleanup. Native ownership, tool flags and deferred capability/Toolset
instructions are checked through ordinary Agent runs. No external model API is called.

Shared PostgreSQL/Valkey fixtures live in `tests/conftest.py`. `tests/control`
covers catalog, retained Temporal execution identity, conditional writer ownership,
explicit close takeover and layer lifecycle.
`tests/agent_output` covers transport and history replay, and `tests/database`
checks historical revisions and the ledger-removal/paired-identity migration. History fixtures
write through HistoryRepository in isolated short transactions; no legacy runner is
used to seed current tests.

`tests/layers/test_mcps.py` runs real Agent → native MCPToolset → stdio Gateway →
upstream subprocesses. It checks per-run configuration and credential isolation,
disabled selections, JSON round trips, process cleanup after success, failure and
cancellation, and model-compatible aliases for dotted and long MCP tool names.
`tests/runner/test_mcps.py` uses real PostgreSQL and standalone Temporal Activities
with a deterministic SDK model. It verifies one Gateway for multiple calls, fresh
processes for subsequent runs, saved configuration/empty State, process cleanup
and retained native result retrieval without MCP I/O. These tests need no JWE or
external model credentials. The independent Gateway's HTTP/JWE and inline suites
live in `packages/mcp_gateway/tests` and run separately from the root suite.
