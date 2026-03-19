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

## Thread routing (`message_thread_id`)

- Telegram API field name is `message_thread_id` (optional; not every message has it).
- If the input message has no `message_thread_id`, do not send this field.
- If the input message is in a thread (`message_thread_id` present), all outgoing messages must stay in that same thread:
  include the same `message_thread_id` on every send call.

## sendMessage

```bash
CHAT_ID=123456789
THREAD_ID=987654321  # use the input message's message_thread_id when replying in-thread

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
  -d message_thread_id="$THREAD_ID" \
  --data-urlencode text="$MSG" \
  -d parse_mode=HTML \
  -d disable_web_page_preview=true | jq
```

## At-mentions (@Mentions)

In Telegram, the **default and mandatory** way to mention users (especially those without public usernames) is to use HTML-formatted User ID links. This ensures the mention works reliably and correctly links to the user's profile. Avoid using the native `@username` format.

**Standard Format (HTML):**
`<a href="tg://user?id=USER_ID">User Name</a>`

Example:
```bash
MSG="Hello <a href=\"tg://user?id=567113516\">Yanli</a>!"
# Then send with parse_mode=HTML
```

## Telegra.ph handoff

For long/structured content, use `skills:messager/telegraph/SKILLS.md`.
**Note**: Telegra.ph requires **HTML**; Markdown is not supported.
That skill creates the Telegra.ph page and prints the URL to stdout.

After you capture the URL, send it back with `sendMessage`.

```bash
URL=$(~/.kapybara/skills/messager/telegraph/create_telegraph "<h3>HTML content here</h3>" \
  --title "Page Title")
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

Reply to a message:

```bash
curl -sS -X POST "$BASE/sendMessage" \
  -d chat_id="$CHAT_ID" \
  -d message_thread_id="$THREAD_ID" \
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
- **Backticks and Code**: In `parse_mode=HTML`, backticks (`` ` ``) are **not** automatically rendered as code. Use `<code>...</code>` for inline code and `<pre>...</pre>` for blocks.
- **Shell Escaping (Crucial)**: In shell scripts, avoid using double quotes `"` for variables containing backticks (e.g., `MSG="...`code`..."`), as the shell will attempt to execute the content inside backticks. Use single quotes `'` or a heredoc with quoted delimiter (`cat <<'HTML'`) to preserve backticks as literal text.
- In shell scripts, using `cat <<'HTML'` (heredoc) allows direct use of newlines. **Do not use literal `\\n` characters inside the heredoc content**, as they will be treated as literal backslashes and the letter 'n' rather than a line break. The heredoc itself preserves actual newlines.
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
