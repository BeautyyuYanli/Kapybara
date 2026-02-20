---
name: messager/telegram
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
  -d disable_web_page_preview=true | jq
```

## sendViaTelegraph

Use this to publish long/structured content to Telegra.ph and send the link to Telegram.

Env:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAPH_ACCESS_TOKEN`

```bash
CHAT_ID=123456789

# This script creates a page AND sends the link to the specified chat automatically.
# You do NOT need to call sendMessage afterwards; the script handles the delivery.
./send_via_telegraph "<h3>HTML content here</h3>" \
  --chat-id "$CHAT_ID" \
  --title "Page Title"
```


## createForumTopic (Sub-sessions)

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

### Send Sticker (via API)
```bash
CHAT_ID=...
FILE_ID=...
BASE="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}"

curl -sS -X POST "$BASE/sendSticker" \
  -d chat_id="$CHAT_ID" \
  -d sticker="$FILE_ID" | jq
```

## sendDocument

```bash
CHAT_ID=123
FILE_PATH="/path/to/file.txt"
CAPTION="Here is the file"

~/.kapybara/skills/messager/telegram/send_document "$FILE_PATH" --chat-id "$CHAT_ID" --caption "$CAPTION"
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
  -d parse_mode=HTML | jq
```

Reply to a message:

```bash
curl -sS -X POST "$BASE/sendMessage" \
  -d chat_id="$CHAT_ID" \
  -d reply_to_message_id=120 \
  --data-urlencode text="Got it" | jq
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

- Prefer `--data-urlencode text=...` so newlines / special chars are encoded correctly.
- For formatting, `parse_mode=HTML` is usually easier than `MarkdownV2` (less escaping).
- In shell scripts, using `cat <<'HTML'` (heredoc) allows direct use of newlines; typing `\n` literally will result in literal backslashes rather than a line break.
- If you want to inspect the API response, parse the JSON and print it as UTF-8 (some formatters default to ASCII-escaped `\uXXXX` output):

```bash
# jq prints UTF-8 by default; avoid `jq -a/--ascii-output`.
curl -sS -X POST "$BASE/getMe" | jq
```


## Important Note on HTML Sanitization
When using `sendMessage` with `parse_mode=HTML`:
- Always HTML-escape the content variables (like filenames or user-provided strings) before including them in the message.
- Unescaped characters like `<` or `>` will cause the Telegram API to reject the message, often resulting in "blank" displays or delivery failures in certain clients if the tag is interpreted incorrectly.
- Example: If you are mentioning a file path, ensure `/path/to/<file>` becomes `/path/to/&lt;file&gt;`.
