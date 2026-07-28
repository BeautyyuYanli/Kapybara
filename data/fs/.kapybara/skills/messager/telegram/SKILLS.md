---
name: telegram
description: Sends and manages Telegram Bot API text, documents, photos, stickers, topics, reactions, and deletions. Use for Telegram channel replies and bot messaging operations, including thread-scoped delivery.
---

# Telegram

Use `curl` and the bundled `send_document` helper to communicate through the
Telegram Bot API. Prefer Rich Messages for text because Rich Markdown supports
headings, tables, math, and other structured content.

## Upstream dependency

- Upstream: Telegram Bot API
- Official docs: https://core.telegram.org/bots/api
- Current API: Bot API 10.2; Rich Messages were introduced in Bot API 10.1
- Skill created: 2026-02-13

## Environment

- `TELEGRAM_BOT_TOKEN` (required)

Base URL:

```bash
BASE="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}"
```

## Thread routing (`message_thread_id`)

- Telegram API field name is `message_thread_id` (optional; not every message has it).
- If the input message has no `message_thread_id`, do not send this field.
- If the input message is in a thread (`message_thread_id` present), all outgoing messages must stay in that same thread:
  include the same `message_thread_id` on every send call.

## sendRichMessage

Use `sendRichMessage` for text communication. Its `rich_message` parameter
accepts a JSON-serialized `InputRichMessage` object containing Rich Markdown.

```bash
CHAT_ID=123456789
THREAD_ID=987654321  # use the input message's message_thread_id when replying in-thread

MSG=$(cat <<'HEREDOC'
# Highlights

Hello! This is a **Rich Message**.

| Task | Status |
|:---|:---|
| Update Skill | ==Done== |
| Test Demo | Pending |

$$E = mc^2$$

> "The best way to predict the future is to create it."
HEREDOC
)

RICH_MSG_JSON=$(jq -n --arg md "$MSG" '{markdown: $md}')

curl -sS -X POST "$BASE/sendRichMessage" \
  -d chat_id="$CHAT_ID" \
  -d message_thread_id="$THREAD_ID" \
  --data-urlencode rich_message="$RICH_MSG_JSON" | jq
```

## At-mentions

In Rich Markdown, use the inline link syntax with the `tg://user?id=` protocol for reliable mentions.

Format: `[User Name](tg://user?id=USER_ID)`

Example:
```bash
MSG="Hello [Yanli](tg://user?id=567113516)!"
```

## editMessageText

To edit a message with rich formatting, use the `rich_message` parameter.

```bash
CHAT_ID=123
MSG_ID=456
MSG=$(cat <<'HEREDOC'
# Updated content

New table etc.
HEREDOC
)
RICH_MSG_JSON=$(jq -n --arg md "$MSG" '{markdown: $md}')

curl -sS -X POST "$BASE/editMessageText" \
  -d chat_id="$CHAT_ID" \
  -d message_id="$MSG_ID" \
  --data-urlencode rich_message="$RICH_MSG_JSON" | jq
```

## Long/structured Markdown handoff

For extremely long/structured content (e.g., full reports), write a Markdown file and upload it with `sendDocument`. Telegram can render a direct Markdown file preview.

```bash
CHAT_ID=123
THREAD_ID=987654321
FILE_PATH="/tmp/report.md"
CAPTION="Here's the full report"

~/.kapybara/skills/messager/telegram/send_document \
  "$FILE_PATH" \
  --chat-id "$CHAT_ID" \
  --message-thread-id "$THREAD_ID" \
  --caption "$CAPTION"
```

Rules:

- Use a `.md` suffix.
- Preserve thread routing with `--message-thread-id`.

## createForumTopic

Use this to create a "thread" or "sub-session" in a private chat or forum supergroup.
**Note**: For private chats, the bot must have "Forum Topic Mode" enabled in @BotFather.

```bash
CHAT_ID=123456789
TOPIC_NAME="Work Session"

curl -sS -X POST "$BASE/createForumTopic" \
  -d chat_id="$CHAT_ID" \
  -d name="$TOPIC_NAME" | jq
```

Response includes `message_thread_id`.

## sendSticker

```bash
CHAT_ID=...
THREAD_ID=...
FILE_ID=...
BASE="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}"

curl -sS -X POST "$BASE/sendSticker" \
  -d chat_id="$CHAT_ID" \
  -d message_thread_id="$THREAD_ID" \
  -d sticker="$FILE_ID" | jq
```

## sendDocument

```bash
CHAT_ID=123
THREAD_ID=987654321
FILE_PATH="/path/to/file.txt"
CAPTION="Here is the file"

~/.kapybara/skills/messager/telegram/send_document \
  "$FILE_PATH" \
  --chat-id "$CHAT_ID" \
  --message-thread-id "$THREAD_ID" \
  --caption "$CAPTION"
```

## sendPhoto

```bash
CHAT_ID=123
THREAD_ID=987654321
PHOTO_URL="https://example.com/image.jpg"
CAPTION="Look at this!"

curl -sS -X POST "$BASE/sendPhoto" \
  -d chat_id="$CHAT_ID" \
  -d message_thread_id="$THREAD_ID" \
  -d photo="$PHOTO_URL" \
  --data-urlencode caption="$CAPTION" \
  -d parse_mode=HTML | jq
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
  --data-urlencode 'reaction=[{"type":"emoji","emoji":"👍"}]' | jq
```

## deleteMessage

```bash
CHAT_ID=123
MSG_ID=456

curl -sS -X POST "$BASE/deleteMessage" \
  -d chat_id="$CHAT_ID" \
  -d message_id="$MSG_ID" | jq
```

## Gotchas

- **Rich Markdown**: Prefer `sendRichMessage` for structured text.
- **Backticks and Code**: In Rich Markdown, `` `code` `` works as expected.
- **Shell Escaping**: Use single quotes or heredocs with quoted delimiters (e.g., `cat <<'EOF'`) to preserve special characters like `$` or `` ` `` in your Markdown.
- A quoted heredoc preserves real newlines. Do not use literal `\\n` characters
  inside it; they remain a backslash and the letter `n`.
