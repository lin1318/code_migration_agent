# Code Migration Agent

Stage 2 provides a semantic Go repository analyzer. Gin-specific migration
logic is not included yet.

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
module, package, source file, import, and structured package-loading error
information.
