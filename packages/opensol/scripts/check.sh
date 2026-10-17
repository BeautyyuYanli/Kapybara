#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
export OPENSOL_TEST_IMAGE=${OPENSOL_TEST_IMAGE:-opensol-tests:latest}
export CARGO_TARGET_DIR=${CARGO_TARGET_DIR:-/target}

cargo fmt --all --check
cargo check --locked --workspace --all-features
cargo check --locked -p opensol-adapters --no-default-features
for feature in local ssh docker pueue tmux envd e2b sandboxd agent-sandbox openshell; do
    cargo check --locked -p opensol-adapters --no-default-features --features "$feature"
done
cargo test --locked -p opensol-core -p opensol-adapters --all-features
cargo clippy --locked --workspace --all-features --all-targets -- -D warnings
RUSTDOCFLAGS=-Dwarnings cargo doc --locked --workspace --all-features --no-deps
maturin build --locked --manifest-path bindings/python/Cargo.toml --out /tmp/opensol-wheels
python -m pip install --force-reinstall /tmp/opensol-wheels/*.whl
mkdir -p /tmp/opensol-protocols
python -m grpc_tools.protoc -I crates/adapters/protocols/envd \
    --python_out=/tmp/opensol-protocols crates/adapters/protocols/envd/process.proto
PYTHONPATH="/tmp/opensol-protocols${PYTHONPATH:+:$PYTHONPATH}" \
    python -m unittest discover -s bindings/python/tests -v
