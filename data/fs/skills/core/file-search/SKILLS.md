---
name: file-search
description: CLI file/content search cheatsheet.
---

# file-search

## Find files (by name)
```bash
find <root> -type f -name '*.py'
find <root> -type f -iname '*readme*'
find <root> -maxdepth 3 -type f -name '*.md'

fdfind '*.py' <root>    # Debian: fd is packaged as `fd-find` -> `fdfind`
locate <name>           # filename DB (requires an updatedb run)
```

## Search contents
```bash
rg 'pattern' <root>
rg -F 'literal string' <root>
rg -g '*.py' 'pattern' <root>
rg -n -C 2 'pattern' <root>

grep -RIn -- 'pattern' <root>        # portable fallback
```

## Combine (constrain file set, then search)
```bash
find <root> -type f -name '*.log' -print0 | xargs -0 rg 'pattern'
```

## Structural search (AST)
```bash
ast-grep run -l python -p 'print($A)' --globs '*.py' <root>
ast-grep run -l javascript -p 'console.log($A)' --globs '*.js' <root>
```
