# syntax=docker/dockerfile:1.7
FROM python:3.14-slim-trixie

ARG DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        cron \
        curl \
        git \
        openssh-server \
        supervisor \
        ripgrep \
        wget \
        ffmpeg \
        jq \
        fd-find \
        plocate \
        xz-utils \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv ast-grep-cli

WORKDIR /

# Install the local `core/` package into the image.
# `docker/docker-compose.env.yaml` mounts `core/` to `/core` for live dev, so keep
# the install path stable at `/core`.
COPY core/ /core/
RUN uv pip install --system --no-cache -e /core

COPY docker/basic-os/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY docker/basic-os/supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY docker/basic-os/cron.d/tmp-clean /etc/cron.d/tmp-clean
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh \
    && chmod 0644 /etc/cron.d/tmp-clean \
    && mkdir -p /var/run/sshd

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
