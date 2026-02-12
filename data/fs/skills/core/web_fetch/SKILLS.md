---
name: web-fetch
description: Fetch a single URL via Jina AI Reader (r.jina.ai) and return clean text.
---

## Upstream dependency
- Upstream: Jina AI Reader
- Official docs: https://jina.ai/
- Skill created: 2026-02-11

# web-fetch

Env: `JINA_AI_KEY`.

```bash
target='https://example.com/page'
url="https://r.jina.ai/${target}"
out=/tmp/web_fetch_${RANDOM}.txt
curl -sS "$url" -H "Authorization: Bearer $JINA_AI_KEY" -H 'Accept: text/plain' -o "$out"
sed -n '1,120p' "$out"
```

You may need to wait longer for this command to complete.