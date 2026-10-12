#!/bin/sh
# Safe to run against existing Temporal databases; schema operations must succeed.
set -eu
for database in temporal temporal_visibility; do
    case "$database" in
        temporal) schema=temporal ;;
        temporal_visibility) schema=visibility ;;
    esac
    # Only creation tolerates an existing database. Setup/update below verify the
    # database is accessible and schema migration succeeds before server startup.
    temporal-sql-tool --quiet --db "$database" create
    temporal-sql-tool --db "$database" setup-schema -v 0.0
    temporal-sql-tool --db "$database" update-schema \
        -d "/etc/temporal/schema/postgresql/v12/$schema/versioned"
done
