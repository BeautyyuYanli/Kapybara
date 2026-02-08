# syntax=docker/dockerfile:1.7
FROM python:3.14-slim-bookworm

ARG DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG SUPERCRONIC_VERSION=v0.2.38

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        gosu \
        tini \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

RUN set -eux; \
    container_arch="$(dpkg --print-architecture)"; \
    case "${container_arch}" in \
      amd64) supercronic_arch='amd64' ;; \
      arm64) supercronic_arch='arm64' ;; \
      *) echo "Unsupported container architecture: ${container_arch}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${supercronic_arch}"; \
    chmod 0755 /usr/local/bin/supercronic

WORKDIR /

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
