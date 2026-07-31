"""Single-task migration proposal execution."""


def execute_migration_task(*args, **kwargs):
    """Load the execution service lazily to avoid dependency cycles."""

    from agent.execution.service import execute_migration_task as execute

    return execute(*args, **kwargs)


__all__ = ["execute_migration_task"]
