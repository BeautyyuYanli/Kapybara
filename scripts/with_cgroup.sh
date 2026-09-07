#!/bin/sh
# Run a development command in its own user-delegated cgroup.
set -eu

if [ "$#" -eq 0 ]; then
    printf 'Usage: %s COMMAND [ARG ...]\n' "$0" >&2
    exit 2
fi

exec systemd-run --user --scope --quiet --collect --property=Delegate=yes \
    /bin/sh -c '
        set -eu
        scope_cgroup=$(cut -d: -f3 /proc/self/cgroup)
        export KAPY_CGROUP_ROOT="/sys/fs/cgroup$scope_cgroup"
        test -w "$KAPY_CGROUP_ROOT/cgroup.procs"
        test -w "$KAPY_CGROUP_ROOT/cgroup.kill"
        exec "$@"
    ' kapy-delegated "$@"
