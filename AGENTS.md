# Repository Guidelines

## Project Scope

- This project builds a Gin-to-Hertz migration agent.
- Python handles orchestration.
- Go handles semantic source-code analysis.
- Do not implement future stages unless explicitly requested.

## Go Analysis

- Do not use regular expressions as the primary mechanism for Go semantic analysis.
- Prefer `go/packages`, `go/ast`, and `go/types` for semantic analysis.

## Development Rules

- Every feature must include tests.
- Do not hide compiler errors with `interface{}`, stubs, or commented-out code.
- Keep shell commands restricted to an explicit allowlist.
- Make minimal, scoped changes.
- Run formatters and tests after changes.
- Show a summary and `git diff` after every task.

## Workflow Guidance

- When making changes, commit small, focused diffs and include tests that demonstrate behavior.
- Use Go's type-checker and AST-based tooling rather than regex for code transforms.
- Maintain an explicit allowlist for any shell commands run by CI or scripts.
- After completing a task, provide a brief summary and the git diff for review.

## Contact / Conventions

- Follow existing repository conventions. If unsure, ask before changing architecture or adding new stages.
