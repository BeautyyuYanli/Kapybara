---
name: execute-code
description: Runs scripts reproducibly (esp. uv scripts with inline deps).
---

# execute-code

Ref: https://docs.astral.sh/uv/guides/scripts/

## Inline deps in-file (PEP 723)
Top of `script.py`:
```py
# /// script
# requires-python = ">=3.12"          # optional
# dependencies = ["requests<3", "rich"]
# ///
```

Create/update the block:
```bash
uv init --script script.py --python 3.12
uv add --script script.py 'requests<3' rich
```

## Execute
```bash
uv run script.py
uv run - <<'PY'
print('hello')
PY
uv run --with rich --with 'requests<3' script.py   # per-run deps (no inline block)
```

Project dir (`pyproject.toml` present):
- plain script: `uv run --no-project script.py` (flag before script)
- PEP 723 script: uv ignores project deps (usually no `--no-project` needed)
