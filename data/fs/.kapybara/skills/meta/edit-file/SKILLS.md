---
name: edit-file
description: Safely replace an exact block of text in a file (fails on mismatch/ambiguity).
---

## Upstream dependency
- Official docs: N/A (local script)
- Skill created: 2026-02-11

Args: `filename` (req), `old_content` (req), `new_content` (req), `start_line` (opt, 1-based).

Rules:
- If `start_line` set: `old_content` must match exactly there.
- Else: `old_content` must occur exactly once.

Direct use:
```bash
python3 ~/.kapybara/skills/meta/edit-file/edit.py \
  --filename path/to/file.txt \
  --start-line 12 \
  --old-content '...'
  --new-content '...'
```
