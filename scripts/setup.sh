#!/usr/bin/env bash
set -euo pipefail

# Create one shared virtualenv at repo/.venv using core/pyproject.toml
# constraints, then install both projects into that same environment.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CORE_DIR="$ROOT_DIR/core"
COLLECTIONS_DIR="$ROOT_DIR/collections"
CORE_VENV_LINK="$CORE_DIR/.venv"
SHARED_VENV="$ROOT_DIR/.venv"
SHARED_PYTHON="$SHARED_VENV/bin/python"

if ! command -v pdm >/dev/null 2>&1; then
  echo "Error: pdm is not installed or not in PATH." >&2
  exit 1
fi

if [ -L "$CORE_VENV_LINK" ]; then
  rm "$CORE_VENV_LINK"
fi

# Force in-project virtualenv creation. Without this, some PDM configs create
# envs under a shared cache path and core/.venv doesn't exist for the move.
PDM_VENV_IN_PROJECT=1 pdm venv -p "$CORE_DIR" create --with venv --force

# Move the created environment to repo/.venv so both projects can share it.
if [ -e "$SHARED_VENV" ] || [ -L "$SHARED_VENV" ]; then
  rm -rf "$SHARED_VENV"
fi
mv "$CORE_VENV_LINK" "$SHARED_VENV"
ln -s ../.venv "$CORE_VENV_LINK"

# Bind both projects to the same interpreter and install dependencies.
pdm use -p "$CORE_DIR" -f "$SHARED_PYTHON"
pdm install -p "$CORE_DIR"

pdm use -p "$COLLECTIONS_DIR" -f "$SHARED_PYTHON"
pdm install -p "$COLLECTIONS_DIR"

echo "Setup complete: shared venv at $SHARED_VENV"
