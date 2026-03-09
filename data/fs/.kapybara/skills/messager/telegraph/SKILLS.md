---
name: messager/telegraph
description: Publishes HTML content to Telegra.ph and prints the created page URL.
---

## Upstream dependency
- Upstream: Telegra.ph API
- Official docs: https://telegra.ph/api
- Skill created: 2026-03-08

# Telegraph

This skill publishes long/structured HTML content to Telegra.ph.
It only creates the page and prints the resulting URL to stdout so another
messaging skill can deliver the link.

Env:
- `TELEGRAPH_ACCESS_TOKEN` (required)

## createTelegraph (skills:messager/telegraph/SKILLS.md)

```bash
URL=$(~/.kapybara/skills/messager/telegraph/create_telegraph "<h3>HTML content here</h3>" \
  --title "Page Title")
echo "$URL"
```

## Output contract

- Stdout contains only the created Telegra.ph URL.
- Stderr is reserved for errors.
- After capturing the URL, send it with the relevant messaging skill, for
  example `skills:messager/telegram/SKILLS.md`.

## Content notes

- Input can be an HTML fragment; a full document is not required.
- Supported tags are `a`, `aside`, `b`, `blockquote`, `br`, `code`, `em`,
  `figcaption`, `figure`, `h3`, `h4`, `hr`, `i`, `iframe`, `img`, `li`, `ol`,
  `p`, `pre`, `s`, `strong`, `u`, `ul`, and `video`.
- Unsupported HTML tags are downgraded to `<p>`.
- Links preserve `href`; `img` and `video` preserve `src`.
