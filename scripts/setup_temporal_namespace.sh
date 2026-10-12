#!/bin/sh
# Initialization runs after the server listens; confirm RPC health before writing.
set -eu
attempt=0
until temporal operator cluster health; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
        exit 1
    fi
    sleep 2
done
if ! temporal operator namespace describe --namespace "$KAPY_TEMPORAL_NAMESPACE"; then
    temporal operator namespace create --namespace "$KAPY_TEMPORAL_NAMESPACE"
fi
