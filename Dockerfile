FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    DATABASE_PATH=/data/study.db \
    PORT=8000

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data && chown app:app /data \
    && install -m 0755 docker/entrypoint.sh /usr/local/bin/entrypoint \
    && install -m 0755 docker/study-cli.sh /usr/local/bin/study-cli

EXPOSE 8000

# Стартуємо від root лише для chown змонтованого volume (/data на Railway належить root),
# далі entrypoint запускає застосунок від користувача app.
ENTRYPOINT ["entrypoint"]
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --timeout-graceful-shutdown 25"]
