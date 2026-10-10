# Tests

These suites exercise the maintained `src/kapy/` implementation. Earlier tests are
preserved under `prototype/tests/` and are outside the default test target.

Run the full suite inside the runtime container so process, PTY and file tests use
an isolated filesystem and real subprocesses:

```sh
docker compose exec -T runtime python -m pytest -q -p no:cacheprovider tests
```

Interface lifespan and Temporal runner tests require a running Temporal service
(`docker compose up -d temporal`). Compose sets
`KAPY_TEMPORAL_ADDRESS=temporal:7233` for runtime (host default:
`localhost:7233`). Runner tests use unique Workflow IDs/task queues and a local HTTP
server for the actual SDK model protocols. See
[`runner_duarable`](../src/kapy/runner_duarable/README.md).

Database tests own disposable PostgreSQL schemas and Valkey namespaces. The normal
suite uses deterministic SDK models; the live credential test is opt-in. Browser
checks and the isolated frontend API host are documented in `frontend/README.md`.

The shell plugin and durable shell-layer real-service tests additionally need the four shellctl Go
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
  tests/agent_runner/test_shell_plugin.py tests/agent_runner/test_agent_plugins.py \
  tests/runner_duarable/test_layers.py
```

`tests/layers` checks typed factory assembly, arbitrary writable deps layouts,
JSON snapshots, native for_run isolation, tool flags, hooks, instructions and
Temporal replay. Its real Pyrefly probes use the project configuration to check
model/hook signatures, including the required optional State on exit.
`runner_duarable/test_layers.py` uses deterministic SDK streaming with real
PostgreSQL, Temporal and a private shellctl server. It covers multiple calls,
cross-run cursors, native retries, failure/cancellation preserving the preceding
snapshot, replay, and session-close retry progress. Nested-capability tests also
verify State is visible before hooks, other layers stay intact, and ordinary or
native ToolReturn values retain their public shape across a consumer-specific deps
layout. SDK ownership differential tests compare whole public tool definitions
and calls with the installed SDK; method/field guards require review on upgrades. No external model API is called.
