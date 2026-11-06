#!/bin/sh
# Обгортка для `python -m app.cli` у контейнері.
# - `railway ssh` відкриває shell без змінних оточення сервісу (PATH з virtualenv,
#   DATABASE_PATH, BOT_TOKEN, ...), тож беремо їх у працюючого застосунку (PID 1).
# - Завжди від користувача app, щоб файли БД (-wal/-shm), створені CLI, не належали root.
set -e
cd /app
if [ -r /proc/1/environ ]; then
    eval "$(/usr/local/bin/python3 -c '
import shlex
pairs = (item.split("=", 1) for item in open("/proc/1/environ").read().split("\0") if "=" in item)
print("\n".join(f"export {k}={shlex.quote(v)}" for k, v in pairs if k.isidentifier()))
')"
fi
PYTHON=/opt/venv/bin/python
if [ "$(id -u)" = "0" ]; then
    exec setpriv --reuid=app --regid=app --init-groups "$PYTHON" -m app.cli "$@"
fi
exec "$PYTHON" -m app.cli "$@"
