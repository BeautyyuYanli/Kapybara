---
name: create-skill
description: Defines how to create a new skill in ~/skills.
---

# create-skill

Ref: https://agentskills.io/specification.md

## What a skill is
A skill is a folder `~/skills/<group>/<skill-name>/` with an entry doc: `SKILL.md` or `SKILLS.md`.

Groups are an organizing convention (e.g. `core/`, `meta/`, `misc/`) and are not part of the skill name.

## Writing rules
- Be **concise**, **fluent**, and **structured**.
- Don’t follow a rigid template; include only what helps reuse.
- Always include YAML frontmatter.
- If the skill depends on an external tool/service, include a short section:
  - Upstream
  - Official docs
  - Current version/API (if relevant)
  - Skill created (date)

Frontmatter (required):
```yaml
---
name: <lowercase-hyphen-name>
description: <one-line, third-person>
---
```

## Minimal scaffold (optional)
```bash
mkdir -p ~/skills/<group>/<skill-name>
cat > ~/skills/<group>/<skill-name>/SKILL.md <<'MD'
---
name: <lowercase-hyphen-name>
description: <one-line, third-person>
---

# <skill-name>

## Usage
MD
```
