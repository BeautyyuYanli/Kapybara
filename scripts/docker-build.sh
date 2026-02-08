#!/usr/bin/env bash
set -euo pipefail

# Resolve repository root from this script location so callers can run it
# from any directory.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_ROOT}"
docker build . -t k-image:latest
