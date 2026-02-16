---
name: web-search
description: Web search via Jina Search Foundation (compact JSON output).
---

## Upstream dependency
- Upstream: Jina AI Search Foundation
- Official docs: https://docs.jina.ai/

# web-search

Env: `JINA_AI_KEY` (required).

`--out` is required and must point to a unique file path.
The script refuses to overwrite an existing path so concurrent runs do not race.

Output is compact JSON and includes only:
- `title`
- `url`
- `description`
- optional `snippet` (truncated preview)
- optional `full_text_path` (path to an untruncated text file for that result)

The JSON output also includes:
- `output_path` (the JSON file path)
- `full_text_dir` (directory containing per-result full text files)
- optional `upstream` metadata copied from Jina (`code`, `status`, `name`, `message`, `readableMessage`)

## Output JSON schema (brief)
```json
{
  "query": "string",
  "output_path": "string (absolute path)",
  "full_text_dir": "string (absolute path)",
  "upstream": {
    "code": "number, optional",
    "status": "number, optional",
    "name": "string, optional",
    "message": "string, optional",
    "readableMessage": "string, optional"
  },
  "results": [
    {
      "title": "string",
      "url": "string",
      "description": "string",
      "snippet": "string, optional, truncated",
      "full_text_path": "string, optional, absolute path"
    }
  ],
  "error": "string, optional",
  "raw_preview": "string, optional, truncated"
}
```

- `error` and `raw_preview` are only present when upstream returns non-JSON.
- `full_text_path` is present only when full text can be extracted for a result.
- `upstream` mirrors stable top-level fields from Jina's documented search response.

```bash
# Search
~/skills/core/web-search/search "your query" --out /tmp/jina_search_01.json
```

The output file content is exactly the same as stdout. Always read the output file when stdout may be truncated by tooling.
