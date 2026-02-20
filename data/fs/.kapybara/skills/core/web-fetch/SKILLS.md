---
name: web-fetch
description: Fetch one or more URLs via Jina AI Reader.
---

## Upstream dependency
- Upstream: Jina AI Reader
- Official docs: https://jina.ai/

# web-fetch

Env: `JINA_AI_KEY`.

`--out` is required and must point to a unique file path.
The script refuses to overwrite an existing path so concurrent runs do not race.

```bash
# Single URL
~/.kapybara/skills/core/web-fetch/fetch "https://example.com" --out /tmp/web_fetch_example_01.txt

# Multiple URLs
~/.kapybara/skills/core/web-fetch/fetch https://url1.com https://url2.com --out /tmp/web_fetch_batch_01.txt
```

The output file content is exactly the same as stdout. Always read the output file when stdout may be truncated by tooling.
