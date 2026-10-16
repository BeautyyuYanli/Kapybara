#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
export AGENT_SANDBOX_TEST_IMAGE=${AGENT_SANDBOX_TEST_IMAGE:-agent-sandbox-sdk-tests:latest}
export CARGO_TARGET_DIR=${CARGO_TARGET_DIR:-/target}

cargo fmt --all --check
cargo check --locked --workspace --all-features
cargo check --locked -p agent-sandbox-adapters --no-default-features
for feature in local ssh docker pueue envd e2b sandboxd agent-sandbox openshell; do
    cargo check --locked -p agent-sandbox-adapters --no-default-features --features "$feature"
done
cargo test --locked -p agent-sandbox-core -p agent-sandbox-adapters --all-features
cargo clippy --locked --workspace --all-features --all-targets -- -D warnings
RUSTDOCFLAGS=-Dwarnings cargo doc --locked --workspace --all-features --no-deps
maturin build --locked --manifest-path bindings/python/Cargo.toml --out /tmp/agent-sandbox-wheels
python -m pip install --force-reinstall /tmp/agent-sandbox-wheels/*.whl
mkdir -p /tmp/agent-sandbox-protocols
python -m grpc_tools.protoc -I crates/adapters/protocols/envd \
    --python_out=/tmp/agent-sandbox-protocols crates/adapters/protocols/envd/process.proto
PYTHONPATH="/tmp/agent-sandbox-protocols${PYTHONPATH:+:$PYTHONPATH}" \
    python -m unittest discover -s bindings/python/tests -v
