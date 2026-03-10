#!/usr/bin/env bash

set -euo pipefail
set -a
[ -f ~/.env ] && . ~/.env
set +a

# Replace these mock ids with real chat ids if you want to restrict this
# example CLI invocation to specific chats.
CHAT_ID_CSV="111111111,222222222,-1003333333333"

exec env K_CONFIG_BASE="${K_CONFIG_BASE:-/home/k/.kapybara}" \
  python -m kapy_collections.starters.telegram \
  --token "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN is required}" \
  --keyword "kapy" \
  --dispatch-recent-per-chat 10 \
  --chat_id "$CHAT_ID_CSV"
  # If you omit `--chat_id`, the starter accepts updates from all chats
  # Uncomment the line below to enable a chat-id watchlist:
