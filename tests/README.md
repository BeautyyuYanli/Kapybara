# Tests

These suites exercise the maintained `src/kapy/` implementation. Earlier tests are
preserved under `prototype/tests/` and are outside the default test target.

Run the full suite inside the runtime container so process, PTY and file tests use
an isolated filesystem and real subprocesses:

```sh
docker compose exec -T runtime python -m pytest -q -p no:cacheprovider tests
```

Database tests own disposable PostgreSQL schemas and Valkey namespaces. The normal
suite uses deterministic SDK models; the live credential test is opt-in. Browser
checks and the isolated frontend API host are documented in `frontend/README.md`.

The shell plugin's real-service test additionally needs the four shellctl Go
binaries (`make -C packages/shellctl build-server`) and `tmux`. Run it inside a
disposable container with those binaries mounted read-only, and set
`KAPY_SHELLCTL_TEST_BIN_DIR` to their absolute container directory. Without this
explicit setting only this extra service integration is skipped; HTTP contract
and failure-path tests still run. With the setting, missing binaries/dependencies
are failures. The fixture owns a private shellctl server, SQLite directory and
tmux socket; PostgreSQL continues to use a disposable schema. It never connects
to a deployed shellctl instance.

```sh
KAPY_SHELLCTL_TEST_BIN_DIR=/work/packages/shellctl/server/bin \
  python -m pytest -q -p no:cacheprovider \
  tests/agent_runner/test_shell_plugin.py tests/agent_runner/test_agent_plugins.py
```
