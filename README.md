# Code Migration Agent

Stage 7 adds deterministic LangGraph orchestration around the existing repository
inspection and migration-planning services. Source-code modification and LLM
execution are not connected.

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
