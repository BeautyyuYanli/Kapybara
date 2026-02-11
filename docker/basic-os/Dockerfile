# syntax=docker/dockerfile:1.7
FROM python:3.14-slim-bookworm

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
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /

COPY docker/basic-os/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY docker/basic-os/supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY docker/basic-os/cron.d/tmp-clean /etc/cron.d/tmp-clean
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh \
    && chmod 0644 /etc/cron.d/tmp-clean \
    && mkdir -p /var/run/sshd

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
