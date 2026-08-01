# Code Migration Agent

Stage 9 adds LLM-powered execution of one ready repository migration bundle on
top of the deterministic inspection and bundle-planning services. Proposals are
validated but never applied; source repositories remain read-only.

## Requirements

- Python 3.13.14
- Go

## Python CLI

```shell
python -m agent --help
```

Inspect a Go repository and print its deterministic Gin-to-Hertz migration
inventory:

```shell
python -m agent inspect --repo <repository-path>
```

Write the complete structured plan outside the inspected repository:

```shell
python -m agent inspect --repo <repository-path> --output migration-plan.json
```

Use `--analyzer-timeout <seconds>` to override the 30-second analyzer timeout.

Run the same deterministic inspection through the Stage 7 LangGraph workflow:

```shell
python -m agent workflow inspect \
  --repo <repository-path> \
  --output workflow-result.json
```

Use `--rules`, `--analyzer-timeout`, and `--checkpoint-memory` to override the
rule registry, timeout, and optional test/debug in-memory checkpointing.

Generate a structured proposal for exactly one routing task:

```shell
python -m agent patch generate \
  --repo <repository-path> \
  --plan <migration-plan.json> \
  --phase routing \
  --output <patch-proposal.json>
```

Configure the AiHubMix OpenAI-compatible Gemini endpoint with
`AIHUBMIX_API_KEY`. `PATCH_MODEL_PROVIDER` defaults to `aihubmix`,
`PATCH_MODEL_NAME` defaults to `gemini-3.6-flash-free`, and
`AIHUBMIX_BASE_URL` defaults to `https://aihubmix.com/v1`. `--model` can
override the model name. This command does not apply the returned patch or run
formatters, builds, tests, or repair loops.

Copy `.env.example` to `.env`, then put the real key only in `.env`:

```dotenv
AIHUBMIX_API_KEY=your-real-key
```

The CLI loads the project-root `.env` without overriding environment variables
already set by the calling process. `.env` is ignored by Git; `.env.example`
contains no secret and can be committed.

Build the deterministic repository-wide bundle plan, then generate a proposal
for exactly one ready bundle:

```shell
python -m agent bundle plan \
  --repo <repository-path> \
  --output <repository-migration-plan.json>

python -m agent bundle execute \
  --repo <repository-path> \
  --bundle <bundle-id> \
  --output <bundle-proposal.json>
```

Bundle execution defaults to the direct Gemini provider with
`GEMINI_API_KEY` and model `gemini-2.5-flash`; `--model` overrides the model.
It validates raw unified diffs but never applies them or runs repository tools.

Safely dry-run or apply one generated bundle proposal to a Git repository:

```shell
python -m agent bundle apply \
  --repo <repository-path> \
  --bundle <bundle.json> \
  --proposal <bundle-proposal.json> \
  --dry-run

python -m agent bundle apply \
  --repo <repository-path> \
  --bundle <bundle.json> \
  --proposal <bundle-proposal.json> \
  --output <apply-result.json>
```

The repository must be clean by default. `--allow-dirty` preserves unrelated
changes but still rejects a proposal whose target files are dirty. Application
uses `git apply --check`, snapshots every affected file, applies one bundle
atomically, verifies the resulting file set and diff, and rolls back affected
files on any post-mutation failure. It does not call a model, format, compile,
test, or repair the repository.

Capture a non-mutating build/test/vet baseline before applying a bundle:

```shell
python -m agent validate baseline \
  --repo <repository-path> \
  --output <validation-baseline.json>
```

After one bundle has been applied, format its modified Go files and validate
affected packages plus the full repository:

```shell
python -m agent bundle validate \
  --repo <repository-path> \
  --bundle <bundle.json> \
  --proposal <bundle-proposal.json> \
  --apply-result <apply-result.json> \
  --baseline <validation-baseline.json> \
  --level full \
  --output <validation-result.json>
```

Validation levels are `format`, `affected`, `repository`, and `full`. Stage 11
uses only predefined gofmt/goimports/go commands, parses structured diagnostics,
and classifies failures against the optional baseline. It reports whether repair
is required but never calls a model, generates a repair, applies another patch,
or rolls back the bundle.

Orchestrate every bundle in dependency order and run dependency reconciliation
plus final repository validation once all source bundles are applied:

```shell
python -m agent migrate repository \
  --repo <clean-git-repository> \
  --plan <repository-migration-plan.json> \
  --analyzer-timeout 120 \
  --output-dir <migration-run-directory>
```

If `--output-dir` is omitted, run artifacts are written beneath
`output/migration/migration-run-<generated-id>`.

Add `--dry-run` to validate the clean repository and complete bundle ordering
without calling a model or changing files. During a real run, each proposal and
apply result is stored separately. A bundle-stage failure restores all earlier
bundle files; final build/test/vet failures remain visible for review and never
trigger automatic repair.

After installing the project, the CLI is also available as:

```shell
code-migration-agent --help
```

## Go analyzer CLI

```shell
cd go-analyzer
go run ./cmd/analyzer <repository-path>
```

The analyzer loads every package below the repository path and emits JSON with
module, package, source file, import, structured package-loading error, and
semantically resolved Gin usage information.
