---
name: create-skill
description: Defines how to create a new skill in ~/.kapybara/skills.
---

# create-skill

Ref: https://agentskills.io/specification.md

## What a skill is
A skill is a folder `~/.kapybara/skills/<group>/<skill-name>/` with an entry doc: `SKILL.md` or `SKILLS.md`.

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
- If you add a **sidecar script** (an executable helper in the skill folder, e.g. `tool`), prefer a **PEP 723** inline-deps script and make it directly runnable via an `uv` shebang.

Frontmatter (required):
```yaml
---
name: <lowercase-hyphen-name>
description: <one-line, third-person>
---
```

## Minimal scaffold (optional)
```bash
mkdir -p ~/.kapybara/skills/<group>/<skill-name>
cat > ~/.kapybara/skills/<group>/<skill-name>/SKILL.md <<'MD'
---
name: <lowercase-hyphen-name>
description: <one-line, third-person>
---

# <skill-name>

## Usage
MD
```

## Sidecar script (optional)
If the skill needs an executable helper script, put it next to the doc (e.g. `~/.kapybara/skills/<group>/<skill-name>/tool`) and make it runnable directly.

Remember to `chmod +x tool`. And use it like this in the skill doc:
```bash
./tool
```

For the recommended `uv` shebang + PEP 723 inline-dependencies pattern (and copy/paste examples), see: `skills:meta/execute-code/SKILLS.md`.
