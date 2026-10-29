#!/bin/sh
# Обгортка для `python -m app.cli` у контейнері: завжди від користувача app,
# щоб файли БД (-wal/-shm), створені CLI, не належали root.
set -e
cd /app
if [ "$(id -u)" = "0" ]; then
    exec setpriv --reuid=app --regid=app --init-groups python -m app.cli "$@"
fi
exec python -m app.cli "$@"
