# Kapybara

This repository runs a Telegram-driven Kapybara environment inside Docker. The
container mounts [`data/fs`](/Users/beautyyu/Dev/Kapybara/data/fs), so your bot
token, launcher script, and agent config all live there on the host.

## Prerequisites

- Docker and Docker Compose
- A Telegram bot token

## Setup

1. Create `data/fs/.env` from `data/fs/.env.example` and set your Telegram
   token. Recommended:

```dotenv
TELEGRAM_BOT_TOKEN="your-telegram-bot-token"
# JINA_AI_KEY="optional"
```

2. Create `data/fs/start.sh` from `data/fs/start.example.sh`.
   By default, the example includes a `--chat_id` watchlist with placeholder
   values. Replace those with real trusted chat IDs, or remove `--chat_id`
   entirely to accept updates from all chats.

Warning: removing `--chat_id` means the bot will accept updates from every chat
it can see. Only run that mode if you intentionally want an unrestricted bot,
and prefer a trusted allowlist for normal usage.

3. Create `data/fs/.kapybara/config.toml` from
   `data/fs/.kapybara/config.example.toml` and configure the default model used
   by `agent_run`. Recommended Google AI Studio settings:

```toml
[agent_run]
provider = "google"
model_name = "gemini-3-flash-preview"
google_api_key = "your-google-ai-studio-api-key"
# google_base_url = "https://generativelanguage.googleapis.com"
```

If you use Logfire, add it as an optional section in `config.toml`:

```toml
[logfire]
token = "your-logfire-token"
```

## Start The Stack

Run Docker Compose from the [`docker`](/Users/beautyyu/Dev/Kapybara/docker)
directory so the compose file resolves relative paths correctly:

```bash
cd docker
export PUID="$(id -u)" PGID="$(id -g)"
docker compose up -d
```

Check status:

```bash
docker compose ps
docker compose logs -f
```

Stop the stack:

```bash
docker compose down
```

## GPU Support

If you do want NVIDIA GPU reservation, add the override file:

```bash
cd docker
export PUID="$(id -u)" PGID="$(id -g)"
docker compose \
  -f docker-compose.yaml \
  -f docker-compose.gpu.yaml \
  up -d
```

## Runtime Notes

- The container mounts `data/fs` to `/home/k`.
- `start.sh` is launched by Supervisor inside the container.
- `K_CONFIG_BASE` defaults to `/home/k/.kapybara`.
