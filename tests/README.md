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
