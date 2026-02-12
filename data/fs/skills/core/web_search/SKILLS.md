---
name: web-search
description: Web search via Jina Search Foundation (JSON output).
---

## Upstream dependency
- Upstream: Jina AI Search Foundation
- Official docs: https://docs.jina.ai/
- Skill created: 2026-02-12

# web-search

Env: `JINA_AI_KEY`.

```bash
q='your query'
out=/tmp/jina_search_${RANDOM}.json
curl -sS --get 'https://s.jina.ai/' --data-urlencode "q=${q}" \
  -H "Authorization: Bearer $JINA_AI_KEY" -H 'Accept: application/json' -o "$out"
sed -n '1,200p' "$out"
```

You may need to wait longer for this command to complete.