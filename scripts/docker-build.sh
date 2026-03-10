#!/usr/bin/env bash
set -euo pipefail

# Resolve repository root from this script location so callers can run it
# from any directory.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_ROOT}"

# Mirror the GHCR workflow's default image coordinate so local builds can be
# consumed by docker compose without overriding IMAGE_NAME.
origin_url="$(git remote get-url origin 2>/dev/null || true)"
owner="$(printf '%s\n' "${origin_url}" | sed -E 's#.*github.com[:/]([^/]+)/.*#\1#' | tr '[:upper:]' '[:lower:]')"
if [[ -z "${owner}" || "${owner}" == "${origin_url}" ]]; then
  owner="beautyyuyanli"
fi

image_name="${IMAGE_NAME:-ghcr.io/${owner}/kapybara:latest}"
docker build -f docker/basic-os/Dockerfile . -t "${image_name}"
