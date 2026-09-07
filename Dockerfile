FROM ghcr.io/astral-sh/uv:0.11.13 AS uv
FROM python:3.14-slim
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev
RUN groupadd --gid 10001 kapy \
    && useradd --uid 10001 --gid kapy --create-home --shell /usr/sbin/nologin kapy \
    && install -d -m 0700 -o kapy -g kapy /var/lib/kapy /var/lib/kapy/state /var/lib/kapy/data /run/kapy
USER 10001:10001
CMD ["kapy", "control-server"]
