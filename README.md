# Code Migration Agent

A file-oriented Gin-to-Hertz migration CLI. It combines semantic Go analysis,
validated YAML rules, one-file LLM proposals, safe content updates, and final
repository validation.

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

Run the complete file-oriented migration workflow:

```shell
python -m agent migrate files \
  --repo <repository-path> \
  --output-dir <artifact-directory> \
  --provider aihubmix \
  --model gemini-3.6-flash-free \
  --analyzer-timeout 120 \
  --model-timeout 300 \
  --apply-timeout 30 \
  --validation-timeout 300
```

Configure the AiHubMix OpenAI-compatible Gemini endpoint with
`AIHUBMIX_API_KEY`. `PATCH_MODEL_PROVIDER` defaults to `aihubmix`,
`PATCH_MODEL_NAME` defaults to `gemini-3.6-flash-free`, and
`AIHUBMIX_BASE_URL` defaults to `https://aihubmix.com/v1`. `--model` can
override the model name.

Copy `.env.example` to `.env`, then put the real key only in `.env`:

```dotenv
AIHUBMIX_API_KEY=your-real-key
```

The CLI loads the project-root `.env` without overriding environment variables
already set by the calling process. `.env` is ignored by Git; `.env.example`
contains no secret and can be committed.

The analyzer identifies Gin usages and the planner attaches relevant migration
rules to one bundle per source file. Each LLM call returns the complete migrated
content for one file. Before writing, the CLI validates the repository-relative
path and original content hash. After every bundle succeeds, it runs `gofmt` on
changed Go files, followed by `go mod tidy`, `go test ./...`, and `go vet ./...`.
Artifacts and captured command results are stored outside the source repository.

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
