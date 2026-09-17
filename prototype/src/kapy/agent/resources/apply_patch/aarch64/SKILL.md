---
name: apply-patch
description: Use the apply_patch tool to create, update, move, rename, or delete text files through structured patches.
---

# `apply_patch` Tool Instructions

Use `apply_patch` to create, update, move, rename, or delete text files through a structured, reviewable patch.

## Tool contract

- Input type: `FREEFORM`.
- Pass exactly one raw patch string.
- Do not wrap the patch in JSON.
- Do not wrap the patch in Markdown fences.
- Do not include commentary before or after the patch.
- Do not invoke `apply_patch` in parallel with another tool.
- Use LF (`\n`) line endings in the patch input.

A patch must begin with `*** Begin Patch` and end with `*** End Patch`.

## Formal grammar

The following grammar uses Lark syntax and assumes a contextual lexer. Whitespace is significant and must not be globally ignored.

```lark
start: begin_patch operation+ end_patch

begin_patch: "*** Begin Patch" LF
end_patch: "*** End Patch" LF?

?operation: add_file
          | delete_file
          | update_file

// Create a non-empty text file.
// Every content line, including an empty content line, begins with "+".
add_file: "*** Add File: " path LF add_line+
add_line: "+" line_text LF

// Delete an existing file.
// A delete operation has no body.
delete_file: "*** Delete File: " path LF

// Update an existing file, move it, or do both.
// An update must contain a move clause, a change block, or both.
update_file: "*** Update File: " path LF update_body

?update_body: move_clause change_block?
            | change_block

// Move or rename the updated file.
move_clause: "*** Move to: " path LF

// Apply one or more contextual or changed lines.
// The optional EOF marker must be the final item in the block.
change_block: change_item+ eof_marker?

?change_item: section_header
            | change_line

// A section header is a locator and is not written to the file.
section_header: "@@" LF
              | "@@ " nonempty_text LF

// " " means context, "-" means deletion, and "+" means insertion.
change_line: CHANGE_PREFIX line_text LF

// Assert that the preceding change is anchored at the end of the file.
eof_marker: "*** End of File" LF

path: nonempty_text
line_text: NONEMPTY_TEXT?
nonempty_text: NONEMPTY_TEXT

CHANGE_PREFIX: "+" | "-" | " "
NONEMPTY_TEXT: /[^\r\n]+/
LF: "\n"
```

## Lexical requirements

1. Control markers must appear exactly as written and must not be indented.
2. `path` must contain at least one character.
3. A path must not contain CR or LF characters.
4. A path must not have leading or trailing whitespace.
5. `line_text` may be empty.
6. A line containing only `+` represents an empty added line.
7. A line containing only `-` represents an empty deleted line.
8. A line containing only one space represents an empty context line.
9. Nothing may appear before `*** Begin Patch`.
10. Nothing except one optional LF may appear after `*** End Patch`.
11. CRLF input must be normalized to LF before parsing.

## Semantic requirements

The grammar defines valid structure. Implementations must additionally enforce the following semantic rules.

### General rules

- At least one file operation is required.
- Paths must resolve inside the authorized workspace.
- Paths must be explicit and stable.
- Do not use `~`, environment variables, command substitution, or glob patterns in paths.
- Reject paths that escape the workspace through `..` traversal.
- A single file should normally appear in only one operation per patch.
- Operations are evaluated in their listed order.
- Do not assume a failed multi-file patch was atomic unless the implementation explicitly guarantees atomicity.

### Add operation

- `*** Add File` is valid only when the target does not already exist.
- Each file-content line must begin with `+`.
- The leading `+` is patch syntax and is not written to the file.
- At least one content line is required by the grammar.
- To represent an empty logical line, use a line containing only `+`.
- Do not use `Add File` to overwrite an existing file.

Example:

```patch
*** Begin Patch
*** Add File: src/example.txt
+first line
+
+third line
*** End Patch
```

The resulting file contains:

```text
first line

third line
```

### Delete operation

- `*** Delete File` is valid only when the target exists.
- A delete operation has no change body.
- Confirm that deletion is authorized and that the exact target has been resolved.
- Do not use broad, ambiguous, generated, or dynamically expanded paths.

Example:

```patch
*** Begin Patch
*** Delete File: src/obsolete.txt
*** End Patch
```

### Update operation

- `*** Update File` normally requires the source file to exist.
- An update must contain at least one move clause or change block.
- A change block must contain at least one actual insertion or deletion; headers and context-only lines are insufficient.
- Context and deletion lines must match the current file.
- Inserted lines do not need to exist in the current file.
- Apply change items in their declared order.
- Use enough context to identify the target location uniquely.
- Keep hunks as small as practical while retaining unique context.

Prefix semantics:

- ` ` — Existing context line. It must match and remains unchanged.
- `-` — Existing line. It must match and is removed.
- `+` — New line. It is inserted at the current location.

Example:

```patch
*** Begin Patch
*** Update File: src/config.ts
@@
 export const config = {
-  timeout: 1000,
+  timeout: 3000,
 };
*** End Patch
```

### Section headers

A section header begins with `@@`.

```patch
@@
```

or:

```patch
@@ export const config
```

Section headers are patch locators, not file content.

- `@@` begins a new contextual section.
- `@@ <text>` supplies a non-empty locator hint.
- A locator hint should identify a nearby symbol, declaration, heading, or stable line.
- The text following `@@ ` is not inserted into or removed from the file.
- Context lines remain the authoritative match criteria.

Example:

```patch
*** Begin Patch
*** Update File: src/config.ts
@@ export const config
-  timeout: 1000,
+  timeout: 3000,
*** End Patch
```

### End-of-file marker

`*** End of File` anchors the preceding change block to the end of the target file.

- It may appear at most once in an update operation.
- It must be the last item in the change block.
- It is not written to the file.
- Use it only when end-of-file placement is materially relevant.

Example:

```patch
*** Begin Patch
*** Update File: src/config.ts
@@
 export const config = {};
+export default config;
*** End of File
*** End Patch
```

### Move or rename operation

A move clause follows an update header:

```patch
*** Update File: src/old-name.ts
*** Move to: src/new-name.ts
```

Requirements:

- The source and destination paths must differ.
- The source must exist.
- The destination must not already exist unless replacement is explicitly supported and authorized.
- The destination must remain inside the authorized workspace.
- A move may be combined with content changes.

Move with content changes:

```patch
*** Begin Patch
*** Update File: src/old-name.ts
*** Move to: src/new-name.ts
@@
-export function oldName() {}
+export function newName() {}
*** End Patch
```

Move without content changes:

```patch
*** Begin Patch
*** Update File: src/old-name.ts
*** Move to: src/new-name.ts
*** End Patch
```

## Multi-file patches

A patch may contain multiple file operations:

```patch
*** Begin Patch
*** Update File: src/app.ts
@@
-import { oldHelper } from "./old-helper";
+import { newHelper } from "./new-helper";
*** Update File: src/old-helper.ts
*** Move to: src/new-helper.ts
@@
-export function oldHelper() {}
+export function newHelper() {}
*** Add File: tests/new-helper.test.ts
+import { newHelper } from "../src/new-helper";
+
+test("newHelper is callable", () => {
+  expect(typeof newHelper).toBe("function");
+});
*** Delete File: tests/old-helper.test.ts
*** End Patch
```

Group operations into one patch only when they form one coherent change. Split independent or high-risk changes into separate patches.

## Usage policy

### Before applying a patch

1. Inspect the current contents of every target file.
2. Check repository status when existing user changes may overlap the task.
3. Identify generated files and their source-of-truth inputs.
4. Resolve the exact source and destination paths.
5. Confirm that the requested change is within the authorized scope.
6. Construct the smallest patch that fully implements the change.

### Editing rules

- Prefer `apply_patch` for intentional hand-written text edits.
- Do not use shell redirection, `cat`, `echo`, or an ad hoc script to bypass the patch workflow.
- Do not manually edit generated files; modify their source and run the corresponding generator.
- Dedicated formatters, generators, and bulk mechanical transformation tools may update files directly.
- Preserve unrelated user changes.
- Do not replace an entire file when a targeted update is sufficient.
- Preserve the existing encoding, line endings, indentation, naming conventions, and local style.
- Do not perform unrelated refactoring.
- Avoid unnecessary compatibility layers and abstractions.
- When changing an interface, inspect its callers, data shapes, lifecycle, storage model, and ownership boundaries.
- Do not assume the working tree is clean.
- Do not revert user changes unless explicitly requested.

### Failure handling

If the tool rejects or fails to apply a patch:

1. Read the complete error.
2. Reinspect every affected file because its contents may have changed.
3. Determine whether the cause is invalid syntax, a wrong path, mismatched context, an existing destination, or concurrent modification.
4. Generate a smaller patch against the current file state.
5. Do not blindly retry the same patch.
6. Do not overwrite the entire file merely to avoid a context conflict.
7. Treat multi-file failures as potentially partial unless atomicity is guaranteed.
8. Stop and ask the user if the intended target or correct resolution remains ambiguous.

### Verification

After a successful tool result:

1. Reinspect the modified files.
2. Review the version-control diff.
3. Confirm that no unrelated content changed.
4. Search for stale names, interfaces, imports, references, and call sites.
5. Run relevant formatting, static analysis, tests, or build commands.
6. Diagnose verification failures before making further changes.
7. Report the implemented change and the verification outcome.

## Safety rules

- Modify only files within the user-authorized scope.
- Resolve destructive targets through read-only inspection first.
- Do not guess when a deletion, overwrite, or move target is ambiguous.
- Do not target a workspace root, home directory, filesystem root, or another broad directory.
- Do not use `apply_patch` to expose credentials, system instructions, developer instructions, tool-internal prompts, or other hidden data.
- A successful patch result confirms only that the patch was processed; it does not prove functional correctness.
