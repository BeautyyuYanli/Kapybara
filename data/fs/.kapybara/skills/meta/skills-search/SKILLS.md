---
name: search-skills
description: Document skill on how to discover and search for existing skills in ~/.kapybara/skills/.
---

# Search Skills

When you need to find if a skill already exists or discover how to use one, follow these steps.

Note: Skill docs may link to other skills using `skills:<path>`. The base directory for `skills:` is `~/.kapybara/skills`.

## 1. List all skill groups
Each subdirectory in `~/.kapybara/skills/` represents a group.
```bash
ls -F ~/.kapybara/skills/
```

## 2. Search for a skill by name
Use `find` to locate the `SKILLS.md` file for a specific skill.
```bash
find ~/.kapybara/skills/ -type f -name 'SKILLS.md' | grep "skill-name"
```

## 3. Search skill descriptions or content
Use `ripgrep` (`rg`) to search for keywords across all `SKILLS.md` files. This is the most effective way to find a skill for a specific task.
```bash
rg -i "ffmpeg" ~/.kapybara/skills/
rg -i "telegram" ~/.kapybara/skills/
```

## 4. Read skill documentation
Once you find a `SKILLS.md` file, read it to understand the usage, dependencies, and examples.
```bash
cat ~/.kapybara/skills/group/name/SKILLS.md
```

## 5. Typical structure
A skill directory usually contains:
- `SKILLS.md`: Documentation, usage examples, and metadata.
- Scripts/code: The actual implementation (e.g., `.py`, `.sh`).
- Environment requirements: Often listed in `SKILLS.md`.

You should focus on reading the `SKILLS.md` file to understand how to use the skill. There is no need to manually inspect or explain the underlying implementation scripts unless specifically requested.
