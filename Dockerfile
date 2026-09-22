FROM ghcr.io/astral-sh/uv:0.11.13 AS uv
FROM node:24-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.14-slim
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages/shellctl/pyproject.toml ./packages/shellctl/pyproject.toml
RUN uv sync --locked --no-install-workspace
COPY packages/shellctl/src ./packages/shellctl/src
COPY packages/shellctl/LICENSE packages/shellctl/README.md ./packages/shellctl/
COPY src ./src
RUN uv sync --locked
COPY tests ./tests
COPY scripts ./scripts
COPY --from=frontend /frontend/dist ./frontend/dist
RUN groupadd --gid 10001 kapy \
    && useradd --uid 10001 --gid kapy --create-home --shell /usr/sbin/nologin kapy \
    && install -d -m 0700 -o kapy -g kapy /var/lib/kapy /var/lib/kapy/state
USER 10001:10001
# Compose selects the independent HTTP or Telegram interface command.
CMD ["sleep", "infinity"]
