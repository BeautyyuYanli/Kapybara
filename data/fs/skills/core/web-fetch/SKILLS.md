---
name: web-fetch
description: Fetch one or more URLs via Jina AI Reader.
---

## Upstream dependency
- Upstream: Jina AI Reader
- Official docs: https://jina.ai/
- Skill created: 2026-02-11

# web-fetch

Env: `JINA_AI_KEY`.

```bash
# Single URL
target='https://example.com/page'
./fetch.py "$target" --out-dir /tmp/my-fetch

# Multiple URLs (parallel)
./fetch.py https://url1.com https://url2.com --out-dir /tmp/my-fetches
```

You may need to wait longer for this command to complete.
