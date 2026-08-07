You are generating a Gin-to-Hertz migration proposal for exactly one Go file.

Only `$editable_file_path` may be edited.

Requirements:

- Modify only `editable_file.path` and handle all owned usages in that file.
- Treat `read_only_context` as read-only symbol metadata. Never modify the
  files named there and never include them in the patch.
- Follow only the supplied migration rules. Never invent Hertz APIs.
- Do not assume a file was migrated unless it appears in `completed_files`.
- Preserve business behavior and generate the smallest complete file change.
- For a handler file, migrate its signature, context operations, binding,
  responses, data structures, and imports together.
- For a middleware file, migrate HandlerFunc signatures, context parameters,
  chain-control operations, and imports together.
- For a routing file, evaluate framework imports, engine/group types,
  constructors, receivers, handler registration, and middleware registration.
- For a bootstrap file, evaluate Gin setup, server construction, router return
  types, Hertz options, and server startup.
- For a shared-context file, migrate its Gin-bound wrapper signatures and
  operations consistently for downstream callers.
- A generated patch must be a raw one-file unified diff beginning with
  `diff --git a/<file> b/<file>`, followed by matching `---`, `+++`, and `@@`
  headers. Never use Markdown fences.
- Build old-side and context lines from the exact `editable_file.content`.
- Do not add, delete, rename, commit, apply, or claim to apply files.
- Use `blocked` with explicit unresolved dependencies when a safe patch cannot
  be generated.
- Use `no_change_needed` only when the current source genuinely requires no
  source edit under all supplied rules.
- Copy the bundle ID and editable file path exactly.

The provider enforces the FilePatchProposal JSON schema. Do not repeat it.

Single-file bundle context:

$context_json
