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
        uuid-runtime \
        build-essential \
	    libnspr4 libnss3 libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 libxcomposite1 libxdamage1 libgtk-3-0 libcups2 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv ast-grep-cli

WORKDIR /

# Install local packages into the image.
# Compose files mount these paths for live dev, so keep install paths stable.
COPY core/ /core/
COPY collections/ /collections/
RUN uv pip install --system --no-cache -e /core -e /collections typer

COPY docker/basic-os/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY docker/basic-os/supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY docker/basic-os/cron.d/tmp-clean /etc/cron.d/tmp-clean
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh \
    && chmod 0644 /etc/cron.d/tmp-clean \
    && mkdir -p /var/run/sshd

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
