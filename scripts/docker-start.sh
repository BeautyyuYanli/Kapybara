#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="k-container"
IMAGE_NAME="k-image:latest"

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <workspace_path>" >&2
  echo "example: $0 \"${PWD}\"" >&2
  exit 1
fi

HOST_WORKSPACE="$1"
if [[ ! -d "${HOST_WORKSPACE}" ]]; then
  echo "error: workspace path does not exist or is not a directory: ${HOST_WORKSPACE}" >&2
  exit 1
fi

WORKSPACE_MOUNT="${HOST_WORKSPACE}:/home/k"

# Ensure the fixed-name container can be recreated by this script.
if docker ps -a --format '{{.Names}}' | grep -Fxq "${CONTAINER_NAME}"; then
  docker rm -f "${CONTAINER_NAME}" >/dev/null
fi

docker run -d \
  --name "${CONTAINER_NAME}" \
  -e "PUID=$(id -u)" \
  -e "PGID=$(id -g)" \
  -v "${WORKSPACE_MOUNT}" \
  "${IMAGE_NAME}"

echo "started container ${CONTAINER_NAME}"
