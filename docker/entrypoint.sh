#!/bin/sh
set -e
if [ "$(id -u)" = "0" ]; then
    mkdir -p "$(dirname "$DATABASE_PATH")"
    chown -R app:app "$(dirname "$DATABASE_PATH")"
    exec setpriv --reuid=app --regid=app --init-groups "$@"
fi
exec "$@"
