You are generating a migration patch proposal for exactly one routing task.

Requirements:

- Preserve existing behavior.
- Modify only files listed in `allowed_files`.
- Use only the supplied migration rules.
- Never invent Hertz APIs.
- Never modify unrelated files.
- Generate the smallest possible patch.
- Treat every supplied source file as the current repository state. Plan phases
  listed before this task, including phases listed as completed, are not proof
  that their changes were applied. Do not assume that any previous migration
  task changed the repository unless updated source content is explicitly
  supplied in this context.
- Routing migration is not limited to route method names. Evaluate framework
  imports, engine and router-group types, constructors, function return types,
  routing receivers, and middleware registration dependencies.
- For `patch_generated`, return a raw unified diff in the `patch` field.
- Never wrap the diff in Markdown code fences.
- Do not add, delete, or rename files.
- If the supplied context is insufficient, return `blocked` with a clear explanation.
- Copy `migration_task_id` exactly into `task_id`.
- `patch_generated` requires a non-empty patch.
- `no_change_needed` requires a null patch and an empty `files_to_modify`.
- `blocked` requires a clear explanation. If it includes a partial patch, that
  patch must still be a raw unified diff limited to `allowed_files`.
- Do not claim that the patch was applied, formatted, compiled, or tested.

The provider enforces the PatchProposal JSON schema. Do not repeat or explain
that schema.

Migration task context:

$context_json
