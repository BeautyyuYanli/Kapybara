#!/bin/sh
set -eu
cd "$(dirname "$0")/../crates/adapters"
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT HUP INT TERM

fetch() {
    destination=$1
    checksum=$2
    url=$3
    curl --fail --silent --show-error --location "$url" --output "$temporary/source.proto"
    printf '%s  %s\n' "$checksum" "$temporary/source.proto" | sha256sum --check --status
    mkdir -p "$(dirname "$destination")"
    cp "$temporary/source.proto" "$destination"
}

fetch protocols/envd/process.proto 8edd9358c7dbfcad96796b3f0ed8d14c262b8b14d6bc7d5e84d468941511b8e0 \
    https://raw.githubusercontent.com/e2b-dev/E2B/62a17dfc3f11439d1d06bfb9bb515eadc5adfcd3/spec/envd/process/process.proto
fetch protocols/sandboxd/process.proto d233a27fe9f63945984a52b74d47c6ffdf2f81ebbe61f954413bb933299cbfda \
    https://raw.githubusercontent.com/kubernetes-sigs/agent-sandbox/v1.0.7/packages/sandboxd/spec/process/v1/process.proto
fetch protocols/openshell/openshell.proto eca4c0201ae623590c9deb6b0027a86b5193eb727d0e79c6852b40704d1a4d7d \
    https://raw.githubusercontent.com/NVIDIA/OpenShell/v0.1.4/proto/openshell.proto
fetch protocols/openshell/datamodel.proto e50bd875a760f54b80269c65ee47984cae424fb623b7f59551ba62550c23e70b \
    https://raw.githubusercontent.com/NVIDIA/OpenShell/v0.1.4/proto/datamodel.proto
fetch protocols/openshell/options.proto b0e03562c92777978f6f3e0f46260cdb82f1d88d1c1837eab0e53cdd0f195eb7 \
    https://raw.githubusercontent.com/NVIDIA/OpenShell/v0.1.4/proto/options.proto
fetch protocols/openshell/sandbox.proto d06089ccd15b57519adb3236e7a4c16661c7c52c9f184f2f826246c62e090839 \
    https://raw.githubusercontent.com/NVIDIA/OpenShell/v0.1.4/proto/sandbox.proto
