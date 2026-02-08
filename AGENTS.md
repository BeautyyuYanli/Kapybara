# API Agent Guide

## Notes for Agent (must-check)

Before changing any source code, you MUST read the surrounding docstrings and comments. These notes contain required context (invariants, edge cases, trade-offs) and are treated as part of the spec.

Look for:

- The module (file) docstring at the top of a source code file
- Docstrings on classes and functions/methods
- Paragraph/block comments for non-obvious logic

### What to write where

- Keep notes scoped: module notes cover module-wide context, class notes cover class-wide context, function/method notes cover behavioural contracts, and paragraph/block comments cover local “why”. Avoid duplicating the same content across scopes unless repetition prevents misuse.
- **Module (file) docstring**: purpose, boundaries, key invariants, and “gotchas” that a new reader must know before editing.
  - Include cross-links to the key collaborators (modules/services) when discovery is otherwise hard.
  - Prefer stable facts (invariants, contracts) over ephemeral “today we…” notes.
- **Class docstring**: responsibility, lifecycle, invariants, and how it should be used (or not used).
  - If the class is intentionally stateful, note what state exists and what methods mutate it.
  - If concurrency/async assumptions matter, state them explicitly.
- **Function/method docstring**: behavioural contract.
  - Document arguments, return shape, side effects (DB writes, external I/O, task dispatch), and raised domain exceptions.
  - Add examples only when they prevent misuse.
- **Paragraph/block comments**: explain *why* (trade-offs, historical constraints, surprising edge cases), not what the code already states.
  - Keep comments adjacent to the logic they justify; delete or rewrite comments that no longer match reality.

### Rules (must follow)

In this section, “notes” means module/class/function docstrings plus any relevant paragraph/block comments.

- **Before working**
  - Read the notes in the area you’ll touch; treat them as part of the spec.
  - If a docstring or comment conflicts with the current code, treat the **code as the single source of truth** and update the docstring or comment to match reality.
  - If important intent/invariants/edge cases are missing, add them in the closest docstring or comment (module for overall scope, function for behaviour).
- **During working**
  - Keep the notes in sync as you discover constraints, make decisions, or change approach.
  - If you move/rename responsibilities across modules/classes, update the affected docstrings and comments so readers can still find the “why” and the invariants.
  - Record non-obvious edge cases, trade-offs, and the test/verification plan in the nearest docstring or comment that will stay correct.
  - Keep the notes **coherent**: integrate new findings into the relevant docstrings and comments; avoid append-only “recent fix” / changelog-style additions.
- **When finishing**
  - Update the notes to reflect what changed, why, and any new edge cases/tests.
  - Remove or rewrite any comments that could be mistaken as current guidance but no longer apply.
  - Keep docstrings and comments concise and accurate; they are meant to prevent repeated rediscovery.

## Engineering Checklist

- **Code clarity**: add proper inline comments and docstrings where appropriate to explain non-obvious logic and public interfaces.
- **Reuse**: when common logic exists, extract it into a helper utility function to avoid duplication.
- **Testing**: add unit tests when you modify source code that should be tested.
- **Before submit**: `cd core/` then run `pdm run fix`, `pdm run typecheck`, and `pdm run test`.
