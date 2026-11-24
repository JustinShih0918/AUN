FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY aun ./aun
RUN uv sync --frozen --no-dev

ENV PATH=/app/.venv/bin:$PATH \
    AUN_DB=/data/aun.db \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
CMD ["aun", "serve"]
