FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY leadbot ./leadbot
RUN uv sync --frozen --no-dev
ENV PORT=8000
# One worker: background jobs live in this process. Scale by raising MAX_CONCURRENT_JOBS first.
CMD ["sh", "-c", "uv run --no-dev uvicorn leadbot.main:app --host 0.0.0.0 --port ${PORT}"]
