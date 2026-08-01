You are generating a migration patch proposal for exactly one Migration Bundle.

Requirements:

- Generate one migration bundle only.
- Modify only files listed in `allowed_files`.
- Preserve repository behavior.
- Use only the supplied migration and replacement rules.
- Treat an operation named `pass_handler_function_value` literally: pass the
  referenced function as a value and do not append `()` or invoke it. When a
  following operation wraps that value, place the bare function identifier
  inside the wrapper call.
- Before adding an import, compare its default package identifier with every
  existing import identifier in that file. If it conflicts and the matching
  rule supplies `target_import_alias`, import the target package with that
  alias and use the alias for every new target symbol reference. Preserve the
  existing project import and its identifier. Never import two packages under
  the same identifier.
- Never invent Hertz APIs.
- Never modify files outside the bundle.
- Generate the smallest correct change.
- Return each file change as a raw unified diff in its own `patches` entry.
- Every patch string must start with `diff --git a/<file> b/<file>` (with no
  prose before it), followed by matching `--- a/<file>`, `+++ b/<file>`, and
  at least one `@@` hunk.
- A valid patch string begins like this:
  `diff --git a/path/file.go b/path/file.go\n--- a/path/file.go\n+++ b/path/file.go\n@@ ... @@`
- Every `@@ -oldStart,oldCount +newStart,newCount @@` count must exactly match
  the context, removed, and added lines that follow it.
- Build every hunk from the exact current content in `relevant_source_files`.
  Unchanged context lines must be copied verbatim, including their order,
  indentation, blank lines, and import ordering.
- Do not silently reorder imports or rewrite any other unchanged text inside a
  hunk. If text must move or change, represent it explicitly with `-` and `+`
  lines. Never place an invented or reordered line behind the context prefix.
- Hunk line numbers and old-side lines must describe the supplied current file,
  not an assumed version from an earlier migration phase.
- Every patch entry must modify exactly the file named by that entry.
- Never wrap patches in Markdown code fences.
- Do not add, delete, or rename files.
- Never apply patches or claim that patches were applied.
- If migration cannot be completed safely, return `blocked` with a clear reason.
- Copy the bundle ID exactly.
- `patch_generated` requires at least one patch and matching `modified_files`.
- `no_change_needed` requires empty `patches` and `modified_files`.

The provider enforces the BundlePatchProposal JSON schema. Do not repeat it.

Migration Bundle context:

$context_json
