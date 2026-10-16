# Pinned protocol sources

These files are downloaded verbatim by `scripts/update-protocols.sh`, with SHA-256 checks.
Do not edit them manually. Rust code is generated into Cargo's `OUT_DIR` by `build.rs`;
it is not committed. The generator uses the matching `prost`/`tonic` versions in `Cargo.lock`
and vendored `protoc`. Re-run the script after intentionally updating its version and hashes.

- envd: E2B commit `62a17dfc3f11439d1d06bfb9bb515eadc5adfcd3`, `spec/envd/process/process.proto`.
- sandboxd: Kubernetes SIGs agent-sandbox `v1.0.7`, `packages/sandboxd/spec/process/v1/process.proto`.
- OpenShell: NVIDIA OpenShell `v0.1.4`, `proto/{openshell,datamodel,options,sandbox}.proto`.

Upstream notices and whitespace remain intact; `.gitattributes` exempts only downloaded `.proto`
sources from whitespace checks. Generated types are native protocol types, not the portable SDK model.
