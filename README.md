# Code Migration Agent

Stage 4 adds a typed Python adapter for the semantic Go repository analyzer.
LLM, LangGraph, and migration execution are not connected yet.

## Requirements

- Python 3.13.14
- Go

## Python CLI

```shell
python -m agent --help
```

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
