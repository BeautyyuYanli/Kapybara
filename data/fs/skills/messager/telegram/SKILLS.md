---
name: telegram
description: Uses curl to call the Telegram Bot API for the handful of methods we actually use.
---

## Upstream dependency
- Upstream: Telegram Bot API
- Official docs: https://core.telegram.org/bots/api
- Skill created: 2026-02-13

# Telegram (Bot API) — minimal

This skill is for sending/editing a few message types via **Telegram Bot API** using `curl`.

Env:
- `TELEGRAM_BOT_TOKEN` (required)

Base URL:

```bash
BASE="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}"
```

## sendMessage

```bash
CHAT_ID=123456789

MSG=$(cat <<'HTML'
<b>Hi I'm here!</b> <i>Welcome</i> to the bot message. <u>Have a great day</u>

<b>Today’s Highlights:</b>
• <b>Bold</b>, <i>italic</i>, <code>code</code>
• <a href="https://core.telegram.org/bots/api">Telegram Bot API</a>

<blockquote>Stay curious, stay kind.</blockquote>
HTML
)

curl -sS -X POST "$BASE/sendMessage" \
  -d chat_id="$CHAT_ID" \
  --data-urlencode text="$MSG" \
  -d parse_mode=HTML \
  -d disable_web_page_preview=true
```

## sendPhoto

```bash
CHAT_ID=123
PHOTO_URL="https://example.com/image.jpg"
CAPTION="Look at this!"

curl -sS -X POST "$BASE/sendPhoto" \
  -d chat_id="$CHAT_ID" \
  -d photo="$PHOTO_URL" \
  --data-urlencode caption="$CAPTION" \
  -d parse_mode=HTML
```

Reply to a message:

```bash
curl -sS -X POST "$BASE/sendMessage" \
  -d chat_id="$CHAT_ID" \
  -d reply_to_message_id=120 \
  --data-urlencode text="Got it"
```

## editMessageText

```bash
CHAT_ID=123
MSG_ID=456
NEW_TEXT="Updated text"

curl -sS -X POST "$BASE/editMessageText" \
  -d chat_id="$CHAT_ID" \
  -d message_id="$MSG_ID" \
  --data-urlencode text="$NEW_TEXT" \
  -d parse_mode=HTML
```

## setMessageReaction

Notes:
- Use `chat_id` + `message_id` to locate the message.
- `reaction` is a JSON array.
- Bots can’t use paid reactions.

```bash
CHAT_ID=567113516
MESSAGE_ID=898

curl -sS -X POST "$BASE/setMessageReaction" \
  -d chat_id="$CHAT_ID" \
  -d message_id="$MESSAGE_ID" \
  --data-urlencode 'reaction=[{"type":"emoji","emoji":"👍"}]'
```

## deleteMessage

```bash
CHAT_ID=123
MSG_ID=456

curl -sS -X POST "$BASE/deleteMessage" \
  -d chat_id="$CHAT_ID" \
  -d message_id="$MSG_ID"
```

## Gotchas

- Prefer `--data-urlencode text=...` so newlines / special chars are encoded correctly.
- For formatting, `parse_mode=HTML` is usually easier than `MarkdownV2` (less escaping).
- In shell scripts, using `cat <<'HTML'` (heredoc) allows direct use of newlines; typing `\n` literally will result in literal backslashes rather than a line break.
