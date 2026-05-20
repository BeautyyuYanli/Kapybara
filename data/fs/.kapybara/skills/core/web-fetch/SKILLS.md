# web-fetch

Env: `EXA_API_KEY`.

Inputs:
- Positional `urls`: one or more URLs.
- `--out-dir` (required): a unique output directory under `/tmp` that does not already exist.

Behavior contract:
- Optimized for fulltext (markdown) without character limits.
- Persists results as `.md` files in a `contents/` subdirectory.
- Each `.md` file starts with a YAML metadata header (title, url, author, publishedDate, id) followed by the markdown content.
- The command prints a JSON manifest to stdout.

Outputs:
- Stdout JSON includes run metadata, success/error counts, and per-URL rows with `markdown_path`.

Reading guidance:
- For large content files, read in chunks with `sed` instead of printing the whole file at once.

```bash
# Multiple URLs
~/.kapybara/skills/core/web-fetch/fetch \
  https://url1.com https://url2.com \
  --out-dir /tmp/web_fetch_20260407_01
```
