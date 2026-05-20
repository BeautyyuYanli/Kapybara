---
name: web-search
description: Batch web search via Exa API. Optimized for highlights (max 4000 chars) with YAML metadata headers and Markdown formatting.
---

# web-search

Env: `EXA_API_KEY` (required).

Inputs:
- Positional `queries`: multiple search queries (batch input).
- `--out-dir` (required): a unique output directory under `/tmp` that does not already exist.

Behavior contract:
- Optimized for highlights (max 4000 chars) to save on costs and keep context relevant.
- Results are persisted as `.md` files containing YAML metadata headers (title, url, score, id, etc.) and highlight content.
- Multiple results in a single file are separated by `---`.
- The command prints a JSON summary of top 5 results per query to stdout.

Outputs:
- Stdout JSON includes run metadata, query counts, and top 5 results per query.
- Per-query Markdown files include highlights and metadata.

## Examples

```bash
# Multiple queries
~/.kapybara/skills/core/web-search/search \
  "rust async runtime comparison" \
  "httpx asyncclient timeout best practices" \
  --out-dir /tmp/web_search_20260407_01
```
