---
name: execute-code
description: Run scripts reproducibly via `uv` PEP 723 scripts. Do not use .py extensions. Always use chmod +x. Prefer Quiet Mode.
---

# execute-code

A framework for running reproducible Python scripts using `uv` and PEP 723 inline dependency metadata.

Ref: https://docs.astral.sh/uv/guides/scripts/

## Core Rules
- **No .py Extensions**: Do not name your scripts with a `.py` suffix. This encourages treating them as standalone executables.
- **Explicit Executability**: Always run `chmod +x <script>` before execution.
- **Shebang Usage**: Always use the `uv` shebang at the top of the file.
- **Quiet Mode**: Prefer using `--quiet` in the shebang to keep logs clean.

## Recommended Format

Use the following template at the start of your script:

```python
#!/usr/bin/env -S uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx",
# ]
# ///
```

### Managing Dependencies
To add or update dependencies in an existing script:
```bash
uv add --script <script_name> 'package-name'
```

## Execution Flow

1. **Create/Edit**: Write the script content.
2. **Make Executable**:
   ```bash
   chmod +x <script_name>
   ```
3. **Run**:
   ```bash
   ./<script_name>
   ```

## Example: One-off Script (Heredoc)

```bash
cat > /tmp/fetch_example <<'PY'
#!/usr/bin/env -S uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx"]
# ///

import httpx
print(httpx.get("https://example.com").status_code)
PY
chmod +x /tmp/fetch_example
/tmp/fetch_example
```

## Python Coding Style

- **Typing**: Use modern Python typing (e.g., `list[int]` instead of `List[int]`).
- **Data Containers**: Use `dataclass(slots=True)` for simple containers or `pydantic.BaseModel` for validation/serialization.
- **Networking**: Prefer `httpx` for HTTP requests.
- **Class Structure**: Define fields using type annotations before methods.
