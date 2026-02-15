---
name: execute-code
description: Runs scripts reproducibly via `uv` PEP 723 scripts (recommended).
---

# execute-code

Ref: https://docs.astral.sh/uv/guides/scripts/

## Recommended format: `uv` shebang + PEP 723
Example: at the very top of `script`
```py
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"          # optional
# dependencies = ["httpx"]
# ///
```

Create/update the inline dependency block:
```bash
uv add --script script 'httpx'
```

## Run
```bash
chmod +x script
./script
```

## Heredoc example (quick one-off script)
```bash
cat > /tmp/script <<'PY'
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx"]
# ///

import httpx
print(httpx.get("https://example.com").status_code)
PY
chmod +x /tmp/script
/tmp/script
```

## Python Coding Style

- Always use modern typed (async) python, in latest typing style, e.g. `list[int]` instead of `List[int]`. Use `dataclass(slots=True)` for simple data containers and `pydantic.BaseModel` for data containers that require validation or serialization. Use httpx for HTTP requests. Define fields of classes using type annotations before other methods.