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
