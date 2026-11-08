FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_CACHE_DIR=/tmp/uv-cache
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY . .
ENV PATH="/app/.venv/bin:$PATH"
RUN useradd -m -u 10001 agent && mkdir -p /app/.local && chown -R agent:agent /app
USER agent
CMD ["support-agent", "api", "--host", "0.0.0.0"]
