.PHONY: build-server test-server test-client test-integration

# The upstream default build also generates help for the separate Dify backend.
# Select the four standalone shellctl binaries without changing upstream files.
build-server:
	$(MAKE) -C server bin/shellctl bin/shellctl-sanitize-pty bin/shellctl-runner-exit bin/shellctl-runner

test-server:
	go -C server test ./...

test-client:
	uv run --project ../.. --locked pytest -q -c pyproject.toml tests/upstream

test-integration: build-server
	uv run --project ../.. --locked pytest -q -c pyproject.toml tests/test_server.py
